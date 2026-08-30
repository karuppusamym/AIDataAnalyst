import hashlib
import hmac
import math
from collections.abc import Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot import exp, parse_one

from aida.config import Settings
from aida.connectors.base import QueryEstimate
from aida.connectors.execution_access import open_execution_session
from aida.connectors.sql_execution import SqlExecutor
from aida.events import record_audit, record_outbox
from aida.models import (
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    QueryExecution,
)
from aida.secrets import SecretResolver
from aida.security import SecurityContext
from aida.sql_guard import SqlGuard
from aida.sql_validation import (
    EstimateOutcome,
    SqlFinding,
    SqlValidationReport,
    build_report,
    findings_from_catalog,
    findings_from_columns,
    findings_from_estimate,
    findings_from_guard,
    locally_defined_names,
    resolve_column_references,
    row_limit_finding,
)

SENSITIVE_CLASSES = frozenset({"CONFIDENTIAL", "PII", "PHI", "PCI", "SECRET"})


class QueryRejected(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.execution_id: Any | None = None


def sensitive_projection_names(
    sql: str, *, dialect: str, sensitive_source_names: set[str]
) -> set[str]:
    """Propagate sensitive lineage through aliases and derived select expressions."""
    statement = parse_one(sql, read=dialect)
    output_names: set[str] = set()
    for select_node in statement.find_all(exp.Select):
        for projection in select_node.expressions:
            source_names = {column.name.lower() for column in projection.find_all(exp.Column)}
            if source_names.intersection(sensitive_source_names):
                output_name = projection.alias_or_name
                if output_name:
                    output_names.add(output_name.lower())
    return output_names


def redact_sql_literals(sql: str, *, dialect: str) -> str:
    """Create an evidence-safe SQL representation without user/source literal values."""
    statement = parse_one(sql, read=dialect)
    redacted = statement.transform(
        lambda node: exp.Placeholder(this="redacted") if isinstance(node, exp.Literal) else node
    )
    return redacted.sql(dialect=dialect, pretty=True)


def audit_sql_hash(key: str, sql: str) -> str:
    """HMAC-SHA256 the raw SQL text under the configured audit key.

    Keyed (not a bare hash) so the digest is both tamper-evident and
    unforgeable without the server's ``audit_hmac_key``: an attacker who can
    read a stored execution record still cannot mint a matching hash for
    different SQL, and the digest changes if the recorded SQL is altered
    after the fact.
    """
    return hmac.new(key.encode("utf-8"), sql.encode("utf-8"), hashlib.sha256).hexdigest()


def extract_column_lineage(sql: str, *, dialect: str) -> list[dict[str, Any]]:
    """Extract value-free output-to-source column evidence from SELECT projections."""
    statement = parse_one(sql, read=dialect)
    aliases: dict[str, str] = {}
    for table in statement.find_all(exp.Table):
        canonical = ".".join(part for part in (table.catalog, table.db, table.name) if part).lower()
        aliases[table.alias_or_name.lower()] = canonical
    lineage: list[dict[str, Any]] = []
    for select_node in statement.find_all(exp.Select):
        for projection in select_node.expressions:
            output_name = projection.alias_or_name
            if not output_name:
                continue
            sources = []
            seen: set[tuple[str | None, str]] = set()
            for column in projection.find_all(exp.Column):
                qualifier = column.table.lower() if column.table else None
                source_table = aliases.get(qualifier, qualifier) if qualifier else None
                identity = (source_table, column.name.lower())
                if identity in seen:
                    continue
                seen.add(identity)
                sources.append({"table": source_table, "column": column.name.lower()})
            transformations = sorted(
                {
                    type(node).__name__.upper()
                    for node in projection.walk()
                    if isinstance(node, exp.Func)
                }
            )
            lineage.append(
                {
                    "output_column": output_name.lower(),
                    "lineage_type": (
                        "DIRECT"
                        if isinstance(projection, exp.Column)
                        or (
                            isinstance(projection, exp.Alias)
                            and isinstance(projection.this, exp.Column)
                        )
                        else "DERIVED"
                    ),
                    "source_columns": sources,
                    "transformations": transformations,
                }
            )
    return lineage


def gate_query_estimate(estimate: QueryEstimate, settings: Settings) -> tuple[float, str | None]:
    """Deterministically gate a connector-agnostic query estimate before execution.

    Byte-based dry-run estimates (BigQuery's dry run today; any future connector
    that ships one) are compared against a dedicated byte budget rather than the
    cost ceiling used by cost-plan connectors (PostgreSQL EXPLAIN, SQL Server
    SHOWPLAN_XML, Oracle EXPLAIN PLAN) — on an engine billed by bytes scanned, an
    unbounded query is not a slow query, it is an invoice, and a cost-shaped number
    would not be comparable across connectors. The byte-budget branch is selected
    structurally via ``estimate.estimated_bytes`` rather than by connector name or
    ``estimate.kind``, so no gateway change is needed when the next byte-billed
    connector ships.

    Returns ``(plan_cost, rejection_reason)``; ``rejection_reason`` is ``None`` when
    the estimate is within policy.
    """
    if not math.isfinite(estimate.score):
        raise RuntimeError("source returned an invalid query estimate score")
    if estimate.estimated_bytes is not None:
        plan_cost = float(estimate.estimated_bytes)
        byte_limit = settings.max_query_estimate_bytes
        if plan_cost > byte_limit:
            return plan_cost, f"QUERY_BYTES_EXCEED_POLICY: {plan_cost} > {byte_limit}"
        return plan_cost, None
    plan_cost = float(estimate.score)
    cost_limit = settings.max_query_estimate_cost
    if plan_cost > cost_limit:
        return plan_cost, f"QUERY_COST_EXCEEDS_POLICY: {plan_cost} > {cost_limit}"
    return plan_cost, None


@dataclass(frozen=True, slots=True)
class GatewayResult:
    execution: QueryExecution
    rows: tuple[dict[str, Any], ...]
    masked_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ValidationOutcome:
    """The private result of one validation pass.

    `report` is value-free and safe to return to a caller. `executable_sql` is
    the guard-normalised statement with its literals *intact* -- it is the thing
    that would actually run, so it never leaves the gateway; the report carries
    the redacted form instead. `executor` is the already-opened connector, held
    so that `execute` costs and runs against the same session it validated
    against rather than opening a second one.
    """

    report: SqlValidationReport
    executable_sql: str | None
    executor: SqlExecutor | None


class QueryExecutionGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.guard = SqlGuard(
            default_row_limit=settings.default_query_row_limit,
            hard_row_limit=settings.hard_query_row_limit,
        )

    async def allowed_tables(self, session: AsyncSession, datasource: DataSource) -> set[str]:
        rows = (
            await session.execute(
                select(
                    MetadataCatalog.name,
                    MetadataSchema.name,
                    MetadataTable.name,
                )
                .join(MetadataSchema, MetadataSchema.catalog_id == MetadataCatalog.id)
                .join(MetadataTable, MetadataTable.schema_id == MetadataSchema.id)
                .where(
                    MetadataCatalog.datasource_id == datasource.id,
                    MetadataTable.organization_id == datasource.organization_id,
                    MetadataTable.status == "ACTIVE",
                )
            )
        ).all()
        allowed: set[str] = set()
        unqualified_counts: dict[str, int] = {}
        for catalog_name, schema_name, table_name in rows:
            schema_table = f"{schema_name}.{table_name}".lower()
            catalog_table = f"{catalog_name}.{schema_name}.{table_name}".lower()
            allowed.update((schema_table, catalog_table))
            unqualified_counts[table_name.lower()] = (
                unqualified_counts.get(table_name.lower(), 0) + 1
            )
        allowed.update(name for name, count in unqualified_counts.items() if count == 1)
        return allowed

    async def _catalog_columns(
        self,
        session: AsyncSession,
        datasource: DataSource,
        referenced_tables: Sequence[str],
    ) -> dict[str, frozenset[str]]:
        """Active column names for the referenced tables, keyed the way SQL names them.

        Same catalog binding, tenancy filter and ACTIVE-only rule as
        `allowed_tables`, and keyed with the same qualified/unqualified variants,
        so a name that authorises as a table resolves as a table here too. The
        query is bounded by the statement's own table list rather than loading
        the datasource's whole column catalog.
        """
        leaf_names = {table.rsplit(".", 1)[-1].lower() for table in referenced_tables}
        if not leaf_names:
            return {}
        rows = (
            await session.execute(
                select(
                    MetadataCatalog.name,
                    MetadataSchema.name,
                    MetadataTable.name,
                    MetadataColumn.name,
                )
                .join(MetadataSchema, MetadataSchema.catalog_id == MetadataCatalog.id)
                .join(MetadataTable, MetadataTable.schema_id == MetadataSchema.id)
                .join(MetadataColumn, MetadataColumn.table_id == MetadataTable.id)
                .where(
                    MetadataCatalog.datasource_id == datasource.id,
                    MetadataTable.organization_id == datasource.organization_id,
                    MetadataTable.status == "ACTIVE",
                    MetadataColumn.organization_id == datasource.organization_id,
                    MetadataColumn.status == "ACTIVE",
                    func.lower(MetadataTable.name).in_(leaf_names),
                )
            )
        ).all()
        by_qualified: dict[str, set[str]] = {}
        qualified_by_leaf: dict[str, set[str]] = {}
        for catalog_name, schema_name, table_name, column_name in rows:
            schema_table = f"{schema_name}.{table_name}".lower()
            catalog_table = f"{catalog_name}.{schema_name}.{table_name}".lower()
            for key in (schema_table, catalog_table):
                by_qualified.setdefault(key, set()).add(column_name.lower())
            qualified_by_leaf.setdefault(table_name.lower(), set()).add(catalog_table)
        for leaf, qualified in qualified_by_leaf.items():
            # An unqualified name is only resolvable when it is unambiguous --
            # the same rule `allowed_tables` applies.
            if len(qualified) == 1:
                by_qualified[leaf] = set(by_qualified[next(iter(qualified))])
        return {name: frozenset(values) for name, values in by_qualified.items()}

    async def _run_validation(
        self,
        session: AsyncSession,
        *,
        datasource: DataSource,
        sql: str,
        requested_limit: int | None,
    ) -> _ValidationOutcome:
        """The one deterministic validation pipeline (review item N14).

        Both `validate` and `execute` come through here, which is the whole
        point: a rule that would refuse a statement at execution time is a rule
        an agent can see beforehand, and the two can never disagree because
        there is only one implementation.

        The phases short-circuit exactly as `execute` always has: the source is
        contacted for a dry-run estimate only once the statement has passed the
        guard and every referenced object has resolved and authorised. A bad
        statement never reaches the warehouse.

        INV-2: the only connector call reachable from here is
        `estimate_read_query`. Execution stays in `execute`.
        """
        dialect = datasource.dialect
        guard_result = self.guard.validate(sql, dialect=dialect, requested_limit=requested_limit)
        findings: list[SqlFinding] = findings_from_guard(guard_result)
        limit_finding = row_limit_finding(
            guard_result,
            requested_limit=requested_limit,
            default_row_limit=self.settings.default_query_row_limit,
            hard_row_limit=self.settings.hard_query_row_limit,
        )
        if limit_finding is not None:
            findings.append(limit_finding)

        normalized_sql = guard_result.normalized_sql
        redacted_sql = (
            redact_sql_literals(normalized_sql, dialect=dialect) if normalized_sql else None
        )
        column_lineage = (
            extract_column_lineage(normalized_sql, dialect=dialect) if normalized_sql else []
        )

        estimate_outcome: EstimateOutcome | None = None
        executor: SqlExecutor | None = None

        def blocked() -> bool:
            return any(finding.blocking for finding in findings)

        if normalized_sql is not None and not blocked():
            allowed_tables = await self.allowed_tables(session, datasource)
            findings.extend(
                findings_from_catalog(
                    referenced_tables=guard_result.referenced_tables,
                    allowed_tables=allowed_tables,
                )
            )
            catalog_columns = await self._catalog_columns(
                session, datasource, guard_result.referenced_tables
            )
            findings.extend(
                findings_from_columns(
                    resolve_column_references(normalized_sql, dialect=dialect),
                    catalog_columns=catalog_columns,
                    local_names=locally_defined_names(normalized_sql, dialect=dialect),
                )
            )

        if normalized_sql is not None and not blocked():
            dsn = SecretResolver(self.settings).resolve(datasource.credential_reference)
            executor = open_execution_session(datasource.connector_type, dsn)
            if not executor.capabilities.explain:
                estimate_outcome = EstimateOutcome(supported=False)
            else:
                estimate = await executor.estimate_read_query(
                    normalized_sql,
                    timeout_seconds=self.settings.query_timeout_seconds,
                )
                # Mirrors `gate_query_estimate`'s structural branch selection so
                # the finding names the budget that actually applied.
                byte_shaped = estimate.estimated_bytes is not None
                plan_cost, rejection_reason = gate_query_estimate(estimate, self.settings)
                estimate_outcome = EstimateOutcome(
                    supported=True,
                    plan_cost=plan_cost,
                    limit=(
                        self.settings.max_query_estimate_bytes
                        if byte_shaped
                        else self.settings.max_query_estimate_cost
                    ),
                    over_budget=rejection_reason is not None,
                    byte_shaped=byte_shaped,
                    kind=estimate.kind,
                    estimated_rows=estimate.estimated_rows,
                    estimated_bytes=estimate.estimated_bytes,
                )
            findings.extend(findings_from_estimate(estimate_outcome))

        report = build_report(
            dialect=dialect,
            guard_result=guard_result,
            findings=findings,
            redacted_sql=redacted_sql,
            column_lineage=column_lineage,
            estimate=estimate_outcome,
        )
        return _ValidationOutcome(
            report=report,
            executable_sql=normalized_sql,
            executor=executor,
        )

    async def validate(
        self,
        session: AsyncSession,
        *,
        datasource: DataSource,
        context: SecurityContext,
        correlation_id: str,
        sql: str,
        requested_limit: int | None,
    ) -> SqlValidationReport:
        """Run the full deterministic pipeline and return findings, without executing.

        The audit trail records the attempt under its own action
        (`query.validate.gateway`) rather than writing a `QueryExecution` row: a
        validation is not an execution, and marking one as such would corrupt
        the execution ledger every operational metric is computed from. See
        `Docs/review-2026-08/gap/05-validate-sql-handoff.md` for the schema
        change that would be needed to persist validations as first-class rows,
        which is deliberately not made here.
        """
        outcome = await self._run_validation(
            session,
            datasource=datasource,
            sql=sql,
            requested_limit=requested_limit,
        )
        report = outcome.report
        record_audit(
            session,
            context,
            action="query.validate.gateway",
            resource_type="datasource",
            resource_id=str(datasource.id),
            outcome="SUCCESS" if report.valid else "DENIED",
            correlation_id=correlation_id,
            details={
                "dialect": datasource.dialect,
                "sql_hash": audit_sql_hash(self.settings.audit_hmac_key, sql),
                "referenced_tables": list(report.referenced_tables),
                "referenced_column_count": len(report.referenced_columns),
                "finding_codes": list(report.codes()),
                "applied_row_limit": report.applied_row_limit,
                "plan_cost": report.plan_cost,
                "executed": False,
            },
        )
        await session.commit()
        return report

    async def _sensitive_output_names(
        self,
        session: AsyncSession,
        datasource: DataSource,
        normalized_sql: str,
    ) -> set[str]:
        names = await session.scalars(
            select(MetadataColumn.name)
            .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.status == "ACTIVE",
                MetadataColumn.organization_id == datasource.organization_id,
                MetadataColumn.status == "ACTIVE",
                MetadataColumn.classification.in_(SENSITIVE_CLASSES),
            )
        )
        sensitive_source_names = {name.lower() for name in names}
        return sensitive_source_names.union(
            sensitive_projection_names(
                normalized_sql,
                dialect=datasource.dialect,
                sensitive_source_names=sensitive_source_names,
            )
        )

    async def execute(
        self,
        session: AsyncSession,
        *,
        datasource: DataSource,
        context: SecurityContext,
        correlation_id: str,
        sql: str,
        requested_limit: int | None,
        semantic_version: str | None,
    ) -> GatewayResult:
        execution = QueryExecution(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            principal_id=context.principal_id,
            dialect=datasource.dialect,
            sql_hash=audit_sql_hash(self.settings.audit_hmac_key, sql),
            semantic_version=semantic_version,
        )
        session.add(execution)
        await session.flush()
        record_audit(
            session,
            context,
            action="query.execute.requested",
            resource_type="query_execution",
            resource_id=str(execution.id),
            outcome="SUCCESS",
            correlation_id=correlation_id,
        )
        await session.commit()

        started = perf_counter()
        try:
            # One validation pipeline, two entry points (review item N14): this is
            # the identical call `validate` makes, so a statement an agent was told
            # is valid is a statement this path will accept, and a rule that fires
            # here is a rule the agent could have seen first.
            outcome = await self._run_validation(
                session,
                datasource=datasource,
                sql=sql,
                requested_limit=requested_limit,
            )
            report = outcome.report
            execution.normalized_sql = report.normalized_sql
            execution.referenced_tables = list(report.referenced_tables)
            execution.referenced_columns = list(report.referenced_columns)
            execution.column_lineage = [dict(item) for item in report.column_lineage]
            if report.plan_cost is not None:
                execution.plan_cost = report.plan_cost
                execution.status = "COSTED"
            if not report.valid or outcome.executable_sql is None:
                raise QueryRejected(report.rejection_reason() or "QUERY_REJECTED")
            if outcome.executor is None:  # pragma: no cover - defensive
                raise QueryRejected("QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR")

            estimate_kind = report.estimate_kind
            plan_cost = report.plan_cost
            connector = outcome.executor
            source_result = await connector.execute_read_query(
                outcome.executable_sql,
                timeout_seconds=self.settings.query_timeout_seconds,
            )
            sensitive_names = await self._sensitive_output_names(
                session,
                datasource,
                outcome.executable_sql,
            )
            masked_columns = sorted(
                {key for row in source_result.rows for key in row if key.lower() in sensitive_names}
            )
            rows = tuple(
                {
                    key: "***MASKED***" if key in masked_columns else value
                    for key, value in row.items()
                }
                for row in source_result.rows
            )
            execution.status = "COMPLETED"
            execution.warehouse_query_id = source_result.warehouse_query_id
            execution.row_count = len(rows)
            execution.elapsed_ms = int((perf_counter() - started) * 1000)
            record_audit(
                session,
                context,
                action="query.execute",
                resource_type="query_execution",
                resource_id=str(execution.id),
                outcome="SUCCESS",
                correlation_id=correlation_id,
                details={
                    "tables": list(report.referenced_tables),
                    "referenced_column_count": len(report.referenced_columns),
                    "lineage_output_count": len(execution.column_lineage),
                    "row_count": len(rows),
                    "masked_columns": masked_columns,
                    "plan_cost": plan_cost,
                    "estimate_kind": estimate_kind,
                    "estimated_rows": report.estimated_rows,
                    "estimated_bytes": report.estimated_bytes,
                    "finding_codes": list(report.codes()),
                },
            )
            record_outbox(
                session,
                organization_id=datasource.organization_id,
                aggregate_type="query_execution",
                aggregate_id=str(execution.id),
                event_type="query.execution.completed.v1",
                payload={
                    "execution_id": str(execution.id),
                    "datasource_id": str(datasource.id),
                    "row_count": len(rows),
                },
            )
            await session.commit()
            return GatewayResult(
                execution=execution,
                rows=rows,
                masked_columns=tuple(masked_columns),
            )
        except QueryRejected as exc:
            exc.execution_id = execution.id
            execution.status = "REJECTED"
            execution.error_class = type(exc).__name__
            execution.error_message = str(exc)[:1000]
            execution.elapsed_ms = int((perf_counter() - started) * 1000)
            record_audit(
                session,
                context,
                action="query.execute",
                resource_type="query_execution",
                resource_id=str(execution.id),
                outcome="DENIED",
                correlation_id=correlation_id,
                details={"reason": str(exc)},
            )
            await session.commit()
            raise
        except Exception as exc:
            execution.status = "FAILED"
            execution.error_class = type(exc).__name__
            execution.error_message = "source query execution failed"
            execution.elapsed_ms = int((perf_counter() - started) * 1000)
            record_audit(
                session,
                context,
                action="query.execute",
                resource_type="query_execution",
                resource_id=str(execution.id),
                outcome="FAILURE",
                correlation_id=correlation_id,
                details={"error_class": type(exc).__name__},
            )
            await session.commit()
            raise
