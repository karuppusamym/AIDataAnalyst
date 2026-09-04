import math
from collections.abc import Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot import exp, parse_one

from aida.authorization_gate import AuthorizationDenied, gate
from aida.classification import SENSITIVE_CLASSES
from aida.config import Settings
from aida.connectors.base import QueryEstimate
from aida.connectors.execution_access import open_execution_session
from aida.connectors.sql_execution import SqlExecutor
from aida.events import record_audit, record_outbox
from aida.lob_concurrency import LobConcurrencyDenied, resolve_lob_concurrency_controller
from aida.models import (
    ColumnTokenizationPolicy,
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    QueryExecution,
)
from aida.policy_resource_attributes import (
    resolve_referenced_table_ids,
    resolve_resource_attributes,
)
from aida.secrets import SecretResolver
from aida.security import SecurityContext

# `audit_sql_hash` moved to `aida.signing` (QG-5, KMS-managed HMAC keys); re-exported
# under its historical name (`as audit_sql_hash`, not a plain import) because existing
# tests and `sql_redaction.py`'s docstring reference it as `query_gateway.audit_sql_hash`.
from aida.signing import audit_sql_hash as audit_sql_hash
from aida.signing import resolve_signing_provider
from aida.sql_guard import SqlGuard, SqlValidationResult
from aida.sql_redaction import redact_sql_literals as _redact_sql_literals
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
from aida.tokenization import TokenizationError, resolve_tokenization_provider


class QueryRejected(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.execution_id: Any | None = None


class AuthorizationRejected(QueryRejected):
    """An authorization refusal, shaped as a rejection so no caller has to change.

    A subclass rather than a separate exception on purpose. Every caller of the
    gateway -- the agent orchestrator, the MCP tool surface, two HTTP handlers --
    already knows how to record a `QueryRejected`, with the execution id, the failure
    reason and the run status handling that goes with it. Introducing a sibling type
    would mean each of those either grows a second branch or silently lets an
    authorization denial escape as a 500, and the second outcome is the one that
    happens to whichever caller is overlooked.

    Handlers that want the correct HTTP status catch this *before* `QueryRejected`
    and answer 403; the ones that do not are still correct, just less specific.
    """

    def __init__(self, reason_code: str, *, workspace_id: UUID | None = None) -> None:
        super().__init__(f"AUTHORIZATION_DENIED:{reason_code}")
        self.reason_code = reason_code
        self.workspace_id = workspace_id


class LobConcurrencyRejected(QueryRejected):
    """QG-3: the requesting LOB was at its concurrency limit past the wait bound.

    Same shape as `AuthorizationRejected` wrapping `AuthorizationDenied`
    (above): a subclass of `QueryRejected` so every existing caller's
    handling -- execution-id bookkeeping, REJECTED status, DENIED audit entry
    -- applies unchanged, while `type(exc).__name__` still distinguishes this
    from an authorization refusal or any other rejection reason.
    """

    def __init__(self, denied: LobConcurrencyDenied) -> None:
        super().__init__(f"LOB_CONCURRENCY_LIMIT_EXCEEDED:{denied.lob_key}")
        self.lob_key = denied.lob_key
        self.limit = denied.limit
        self.waited_seconds = denied.waited_seconds


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
    """Create an evidence-safe SQL representation without user/source literal values.

    Re-exported from `aida.sql_redaction`, where the implementation now lives so the
    ingestion path can use it without importing runtime code (an L1-imports-L3 edge).
    Kept as a name here because existing callers and tests reference it.
    """
    return _redact_sql_literals(sql, dialect=dialect)


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
    # QG-6: output columns whose value was replaced with a reversible token
    # (`aida.tokenization.TokenizationProvider`) rather than the flat
    # "***MASKED***" redaction `masked_columns` still uses. Disjoint from
    # `masked_columns` -- a column configured for tokenization is never also
    # counted as fully redacted.
    tokenized_columns: tuple[str, ...] = ()


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
        # QG-3: resolved through the process-wide cache, not stored as a
        # request-scoped registry -- this gateway is constructed fresh per
        # call site (tool_api.py, mcp_server.py, api.py, agent_orchestrator.py,
        # sql_validation_api.py), so an instance-owned registry would never
        # see more than one execution at a time and would enforce nothing.
        self._lob_concurrency = resolve_lob_concurrency_controller(settings)

    async def _sign_sql(self, sql: str) -> str:
        """Produce the audit HMAC evidence for `sql` (QG-5).

        Resolved fresh per call, the same shape as the `SecretResolver` this
        gateway already builds per call rather than caching -- a signer swapped
        in via config takes effect on the next call, not on the next restart.
        Deliberately un-guarded by a `try/except`: `resolve_signing_provider`
        and `SigningProvider.sign` raise `SigningUnavailable`/`SigningError`
        rather than returning a fallback, and this method lets that propagate
        so a KMS outage rejects the request instead of silently producing an
        unsigned or locally-forged audit hash.
        """
        provider = resolve_signing_provider(self.settings)
        return await provider.sign(sql)

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
        requested_limit: int | None,
        guard_result: SqlValidationResult,
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

        `guard_result` is the caller's own `self.guard.validate(...)` call
        (AU-11), not one computed fresh here: both `validate` and `execute`
        need the parsed statement's `referenced_tables` *before* they gate --
        to resolve the query's real classification/certification/quality/
        freshness attributes onto the gate call (`policy_resource_attributes`)
        -- so the parse happens once, before authorization, and its result is
        threaded through rather than re-parsed after the fact.
        """
        dialect = datasource.dialect
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
        workspace_id: UUID | None = None,
    ) -> SqlValidationReport:
        """Run the full deterministic pipeline and return findings, without executing.

        The audit trail records the attempt under its own action
        (`query.validate.gateway`) rather than writing a `QueryExecution` row: a
        validation is not an execution, and marking one as such would corrupt
        the execution ledger every operational metric is computed from. See
        `Docs/review-2026-08/gap/05-validate-sql-handoff.md` for the schema
        change that would be needed to persist validations as first-class rows,
        which is deliberately not made here.

        Authorized as `READ_METADATA` rather than `READ_DATA`, and that distinction
        is real rather than cosmetic: validation returns findings, table names and a
        cost estimate, never a row. Gating it at the same level as execution would
        make the iterate-against-the-compiler loop unavailable to a `viewer`, who is
        exactly the principal who should be allowed to find out that a statement is
        wrong without being allowed to run it.
        """
        # Parsed once, before authorization (AU-11): the gate needs the statement's
        # real referenced tables to resolve classification/certification/quality/
        # freshness onto the decision, and `_run_validation` reuses this same
        # `guard_result` rather than re-parsing.
        guard_result = self.guard.validate(
            sql, dialect=datasource.dialect, requested_limit=requested_limit
        )
        table_ids = await resolve_referenced_table_ids(
            session, datasource, guard_result.referenced_tables
        )
        # AU-11 fail-closed (2026-09-03): the guard accepted a statement that
        # references at least one table, but leaf-name lookup against ACTIVE
        # MetadataTable returned nothing -- the classification/certification/
        # quality/freshness axes cannot be evaluated for this query. The
        # earlier behaviour was to default every axis to empty/None, which
        # silently bypassed any policy rule keyed on those axes. Deny instead.
        #
        # `guard_result.valid` is part of the condition (2026-09-04) because
        # this control only has meaning for a statement the guard *accepted* --
        # which is what the paragraph above says, and what the original
        # implementation failed to check. A statement the guard already
        # rejected (stacked DDL, a mutation, an unbounded join) is denied on
        # its own merits and can never execute, so raising here instead of
        # returning the findings weakened nothing but replaced an accurate,
        # actionable violation list with a generic authorization error.
        # AU-11/AU-15 (2026-09-03, amended 2026-09-04). The guard accepted a
        # statement that references at least one table, but leaf-name lookup
        # against ACTIVE MetadataTable returned nothing, so the
        # classification/certification/quality/freshness axes cannot be
        # evaluated. `execute` fails closed on this and raises. `validate`
        # does not: it opens no connector and returns no row -- its contract,
        # stated in the docstring above, is to tell the caller what is wrong
        # with a statement, and the pipeline below already refuses every
        # unresolvable reference with `UNKNOWN_OR_UNAUTHORIZED_TABLE`, naming
        # each offending table. Raising here replaced that precise, actionable
        # finding with a generic authorization error, and preempted the
        # catalog allowlist check outright.
        #
        # Leaving the ABAC axes empty is safe *here and only here*: the axes
        # describe catalog objects, every referenced table is absent from the
        # catalog, so there is no classification, certification or quality
        # state to bypass and no metadata about a real asset to disclose. The
        # statement is still refused -- by the finding, on its merits -- and
        # the reason is carried into this call's single audit row below, so
        # the attempt stays attributable (INV-7).
        unresolvable_references = bool(guard_result.referenced_tables) and not table_ids
        resource_attributes = await resolve_resource_attributes(session, datasource, table_ids)
        try:
            await gate(
                session,
                context,
                settings=self.settings,
                action="READ_METADATA",
                resource_type="datasource",
                resource_id=str(datasource.id),
                workspace_id=workspace_id,
                datasource_id=datasource.id,
                classifications=resource_attributes.classifications,
                certification=resource_attributes.certification,
                quality_state=resource_attributes.quality_state,
                freshness_state=resource_attributes.freshness_state,
            )
        except AuthorizationDenied as exc:
            record_audit(
                session,
                context,
                action="query.validate.gateway",
                resource_type="datasource",
                resource_id=str(datasource.id),
                outcome="DENIED",
                correlation_id=correlation_id,
                details={"reason": exc.reason_code, "executed": False},
            )
            await session.commit()
            raise AuthorizationRejected(
                exc.reason_code, workspace_id=exc.workspace_id
            ) from exc
        outcome = await self._run_validation(
            session,
            datasource=datasource,
            requested_limit=requested_limit,
            guard_result=guard_result,
        )
        report = outcome.report
        sql_hash = await self._sign_sql(sql)
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
                "sql_hash": sql_hash,
                "referenced_tables": list(report.referenced_tables),
                "referenced_column_count": len(report.referenced_columns),
                "finding_codes": list(report.codes()),
                "applied_row_limit": report.applied_row_limit,
                "plan_cost": report.plan_cost,
                "executed": False,
                **(
                    {"reason": "unresolvable_table_references"}
                    if unresolvable_references
                    else {}
                ),
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

    async def _tokenized_output_names(
        self,
        session: AsyncSession,
        datasource: DataSource,
        normalized_sql: str,
    ) -> set[str]:
        """Output column names an enabled `ColumnTokenizationPolicy` covers (QG-6).

        Same shape as `_sensitive_output_names` -- name-based lookup, aliases
        and derived expressions expanded through `sensitive_projection_names`
        -- deliberately: a column that stays tokenized under a rename or a
        wrapping expression must not silently fall back to full redaction,
        which the same alias/derived-expression propagation
        `_sensitive_output_names` already relies on also guarantees here. A
        two-column select (rather than the one-column `scalars` call above)
        so `ColumnTokenizationPolicy` participates in the query -- not just a
        classification value on `MetadataColumn` -- and stays queryable by its
        own `enabled` flag independent of classification.
        """
        rows = (
            await session.execute(
                select(ColumnTokenizationPolicy.value_shape, MetadataColumn.name)
                .join(MetadataColumn, MetadataColumn.id == ColumnTokenizationPolicy.column_id)
                .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
                .where(
                    ColumnTokenizationPolicy.organization_id == datasource.organization_id,
                    ColumnTokenizationPolicy.datasource_id == datasource.id,
                    ColumnTokenizationPolicy.enabled.is_(True),
                    MetadataTable.status == "ACTIVE",
                    MetadataColumn.organization_id == datasource.organization_id,
                    MetadataColumn.status == "ACTIVE",
                )
            )
        ).all()
        tokenized_source_names = {name.lower() for _value_shape, name in rows}
        return tokenized_source_names.union(
            sensitive_projection_names(
                normalized_sql,
                dialect=datasource.dialect,
                sensitive_source_names=tokenized_source_names,
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
        workspace_id: UUID | None = None,
    ) -> GatewayResult:
        execution = QueryExecution(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            principal_id=context.principal_id,
            dialect=datasource.dialect,
            sql_hash=await self._sign_sql(sql),
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
            # Authorization runs *after* the execution row and its `requested` audit
            # entry exist, and before anything reaches a connector. Recording first is
            # what makes a refusal attributable (INV-7): a denied attempt leaves a
            # REJECTED row naming who asked and why it was refused, which is the
            # evidence an investigation needs and which gating before the record would
            # throw away. Nothing has left the platform at this point -- the connector
            # is opened inside `_run_validation`, below. Parsing the statement here
            # (AU-11) is safe ahead of that same rule: sqlglot parsing is local and
            # touches no connector, so it can resolve the gate's real
            # classification/certification/quality/freshness attributes before
            # authorization without moving the connector-opening line at all.
            guard_result = self.guard.validate(
                sql, dialect=datasource.dialect, requested_limit=requested_limit
            )
            table_ids = await resolve_referenced_table_ids(
                session, datasource, guard_result.referenced_tables
            )
            # AU-11 fail-closed (2026-09-03): mirror validate() -- an
            # unresolvable table reference on the execute path cannot silently
            # default the ABAC axes to empty/None. Deny with the same reason
            # code so operators see this consistently in the audit trail.
            # `guard_result.valid` mirrors validate()'s 2026-09-04 correction:
            # a statement the guard rejected is refused by `_run_validation`
            # below with its real violation, and never reaches a connector
            # either way, so this control applies only to accepted statements.
            if guard_result.valid and guard_result.referenced_tables and not table_ids:
                raise AuthorizationRejected(
                    "unresolvable_table_references", workspace_id=workspace_id
                )
            resource_attributes = await resolve_resource_attributes(
                session, datasource, table_ids
            )
            try:
                await gate(
                    session,
                    context,
                    settings=self.settings,
                    action="READ_DATA",
                    resource_type="datasource",
                    resource_id=str(datasource.id),
                    workspace_id=workspace_id,
                    datasource_id=datasource.id,
                    classifications=resource_attributes.classifications,
                    certification=resource_attributes.certification,
                    quality_state=resource_attributes.quality_state,
                    freshness_state=resource_attributes.freshness_state,
                )
            except AuthorizationDenied as exc:
                raise AuthorizationRejected(
                    exc.reason_code, workspace_id=exc.workspace_id
                ) from exc
            # One validation pipeline, two entry points (review item N14): this is
            # the identical call `validate` makes, so a statement an agent was told
            # is valid is a statement this path will accept, and a rule that fires
            # here is a rule the agent could have seen first.
            outcome = await self._run_validation(
                session,
                datasource=datasource,
                requested_limit=requested_limit,
                guard_result=guard_result,
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
            # QG-3: fairness under contention. Held only around the real
            # dispatch to the source -- not around validation/estimate above,
            # which is a cheap EXPLAIN-only call, and not around masking/audit
            # below, which touch nothing external -- so the slot's held
            # duration tracks the actual contended resource (the source
            # connection/compute this statement runs against), not
            # bookkeeping this gateway does either side of it. Keyed by the
            # datasource's LOB (see `aida.lob_concurrency`'s module
            # docstring for why that, not the caller, is this platform's
            # real per-LOB dimension for a query execution).
            lob_key = str(datasource.line_of_business_id)
            try:
                async with self._lob_concurrency.slot(lob_key):
                    source_result = await connector.execute_read_query(
                        outcome.executable_sql,
                        timeout_seconds=self.settings.query_timeout_seconds,
                    )
            except LobConcurrencyDenied as exc:
                raise LobConcurrencyRejected(exc) from exc
            sensitive_names = await self._sensitive_output_names(
                session,
                datasource,
                outcome.executable_sql,
            )
            tokenized_names = await self._tokenized_output_names(
                session,
                datasource,
                outcome.executable_sql,
            )
            # QG-6: a column explicitly configured for tokenization is tokenized,
            # not redacted -- `tokenized_names` takes precedence over the
            # conservative `masked_columns` default for the columns it covers.
            # Every other sensitive column keeps today's behaviour unchanged.
            tokenized_columns = sorted(
                {key for row in source_result.rows for key in row if key.lower() in tokenized_names}
            )
            masked_columns = sorted(
                {
                    key
                    for row in source_result.rows
                    for key in row
                    if key.lower() in sensitive_names and key not in tokenized_columns
                }
            )
            tokenize_provider = None
            if tokenized_columns:
                # Resolved fresh per call, the same shape as `_sign_sql`'s
                # `resolve_signing_provider` call -- see `aida.tokenization`'s
                # module docstring. Deliberately un-guarded here too: an
                # unconfigured or unbuildable provider must reject the query,
                # not silently fall back to full redaction for a column a
                # steward explicitly configured to tokenize.
                try:
                    tokenize_provider = resolve_tokenization_provider(self.settings)
                except TokenizationError as exc:
                    raise QueryRejected("TOKENIZATION_PROVIDER_UNAVAILABLE") from exc
            masked_rows: list[dict[str, Any]] = []
            for row in source_result.rows:
                masked_row: dict[str, Any] = {}
                for key, value in row.items():
                    if value is not None and key in tokenized_columns:
                        assert tokenize_provider is not None  # narrows for mypy
                        try:
                            masked_row[key] = await tokenize_provider.tokenize(str(value))
                        except TokenizationError as exc:
                            raise QueryRejected("TOKENIZATION_FAILED") from exc
                    elif key in masked_columns:
                        masked_row[key] = "***MASKED***"
                    else:
                        masked_row[key] = value
                masked_rows.append(masked_row)
            rows = tuple(masked_rows)
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
                    "tokenized_columns": tokenized_columns,
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
                tokenized_columns=tuple(tokenized_columns),
            )
        except QueryRejected as exc:
            # `AuthorizationRejected` is a `QueryRejected`, so a refusal is bookkept
            # here by the same code as a rejected statement: same REJECTED status, same
            # DENIED audit action, same reason field, same execution id handed back to
            # the caller. A refusal taking its own path through the ledger would be a
            # second denial vocabulary for operators to learn, and the first one they
            # forgot to query.
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
