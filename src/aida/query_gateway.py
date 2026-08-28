import hashlib
import hmac
import math
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot import exp, parse_one

from aida.config import Settings
from aida.connectors.base import QueryEstimate
from aida.connectors.registry import connector_registry
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
            sql_hash=hmac.new(
                self.settings.audit_hmac_key.encode("utf-8"),
                sql.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
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
            validation = self.guard.validate(
                sql,
                dialect=datasource.dialect,
                requested_limit=requested_limit,
            )
            execution.normalized_sql = (
                redact_sql_literals(validation.normalized_sql, dialect=datasource.dialect)
                if validation.normalized_sql
                else None
            )
            execution.referenced_tables = list(validation.referenced_tables)
            execution.referenced_columns = list(validation.referenced_columns)
            execution.column_lineage = (
                extract_column_lineage(validation.normalized_sql, dialect=datasource.dialect)
                if validation.normalized_sql
                else []
            )
            if not validation.valid or not validation.normalized_sql:
                raise QueryRejected(", ".join(validation.violations))

            allowed_tables = await self.allowed_tables(session, datasource)
            unauthorized = sorted(
                table
                for table in validation.referenced_tables
                if table.lower() not in allowed_tables
            )
            if unauthorized:
                raise QueryRejected(f"UNKNOWN_OR_UNAUTHORIZED_TABLES: {', '.join(unauthorized)}")

            dsn = SecretResolver(self.settings).resolve(datasource.credential_reference)
            connector = connector_registry.create(datasource.connector_type, dsn)
            if not connector.capabilities.explain:
                raise QueryRejected("QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR")
            estimate = await connector.estimate_read_query(
                validation.normalized_sql,
                timeout_seconds=self.settings.query_timeout_seconds,
            )
            plan_cost, rejection_reason = gate_query_estimate(estimate, self.settings)
            execution.plan_cost = plan_cost
            execution.status = "COSTED"
            if rejection_reason is not None:
                raise QueryRejected(rejection_reason)

            source_result = await connector.execute_read_query(
                validation.normalized_sql,
                timeout_seconds=self.settings.query_timeout_seconds,
            )
            sensitive_names = await self._sensitive_output_names(
                session,
                datasource,
                validation.normalized_sql,
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
                    "tables": list(validation.referenced_tables),
                    "referenced_column_count": len(validation.referenced_columns),
                    "lineage_output_count": len(execution.column_lineage),
                    "row_count": len(rows),
                    "masked_columns": masked_columns,
                    "plan_cost": plan_cost,
                    "estimate_kind": estimate.kind,
                    "estimated_rows": estimate.estimated_rows,
                    "estimated_bytes": estimate.estimated_bytes,
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
