import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from aida.analysis_tasks import (
    TASK_TYPE_DISCOVER_DATASOURCE,
    TASK_TYPE_FINALIZE_PROFILE_TASKS,
    TASK_TYPE_MAX_ATTEMPTS,
    TASK_TYPE_PLAN_PROFILE_TASKS,
    TASK_TYPE_PROFILE_DATASOURCE,
    TASK_TYPE_PROFILE_TABLE,
)
from aida.classification_feed import (
    CLASSIFICATION_SOURCE_EXTERNAL,
    CLASSIFICATION_SOURCE_RULE,
    RuleClassificationResult,
    classify_column_name_with_evidence,
)
from aida.config import get_settings
from aida.connectors.base import (
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredConstraint,
    DiscoveredSchema,
    DiscoveredTable,
)
from aida.connectors.registry import connector_registry
from aida.db import session_factory
from aida.events import record_audit, record_outbox
from aida.ingestion import persist_envelope_extensions
from aida.key_inference import ColumnStat, infer_key_candidates, key_fingerprint
from aida.models import (
    AnalysisRun,
    ClassificationEvidence,
    ColumnProfile,
    DataSource,
    KeyInferenceCandidate,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    TableProfile,
)
from aida.quality_service import evaluate_analysis_run
from aida.secrets import SecretResolver
from aida.security import SecurityContext
from aida.task_tracking import finish_task, heartbeat_task, start_task

logger = structlog.get_logger(__name__)

# Worker principal used for both audit evidence and rule-classification
# evidence rows written from inside a Temporal activity (no human principal
# is available on this path).
_METADATA_WORKER_PRINCIPAL = "metadata-worker"

# Bounds the per-table key-candidate search invoked from finalize_profile_tasks
# (module 05 sec 7 -- every DAG stage is bounded, key inference included).
MAX_KEY_CANDIDATES_PER_TABLE = 25


@dataclass(slots=True)
class ChangeTracker:
    created: int = 0
    changed: int = 0
    deprecated: int = 0

    def observe(self, existing: object | None, old_fingerprint: str | None, new: str) -> None:
        if existing is None:
            self.created += 1
        elif old_fingerprint != new or getattr(existing, "status", "ACTIVE") != "ACTIVE":
            self.changed += 1


@dataclass(slots=True)
class SnapshotScope:
    """Object identities observed across one or many chunks of an authoritative snapshot."""

    catalog_ids: set[UUID] = field(default_factory=set)
    schema_ids: set[UUID] = field(default_factory=set)
    table_ids: set[UUID] = field(default_factory=set)
    column_ids: set[UUID] = field(default_factory=set)
    constraint_ids: set[UUID] = field(default_factory=set)

    def object_counts(self) -> dict[str, int]:
        return {
            "catalogs": len(self.catalog_ids),
            "schemas": len(self.schema_ids),
            "tables": len(self.table_ids),
            "columns": len(self.column_ids),
            "constraints": len(self.constraint_ids),
        }


def missing_snapshot_scope(existing: SnapshotScope, observed: SnapshotScope) -> SnapshotScope:
    """Return only inventory identities absent from an authoritative full snapshot."""
    return SnapshotScope(
        catalog_ids=existing.catalog_ids - observed.catalog_ids,
        schema_ids=existing.schema_ids - observed.schema_ids,
        table_ids=existing.table_ids - observed.table_ids,
        column_ids=existing.column_ids - observed.column_ids,
        constraint_ids=existing.constraint_ids - observed.constraint_ids,
    )


def fingerprint(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_column_name(name: str) -> str:
    """Deterministic name-pattern classification (module 05 sec 9).

    Delegates to ``aida.classification_feed`` so the rule set is defined in
    exactly one place; this wrapper keeps the plain ``str`` return value the
    rest of discovery already expects.
    """
    return classify_column_name_with_evidence(name).classification


async def _record_rule_classification_evidence(
    session: AsyncSession,
    *,
    column: MetadataColumn,
    result: RuleClassificationResult,
) -> None:
    """Append the evidence row for a rule-based classification decision.

    Superseding any prior evidence row (``is_current`` flips to False) mirrors
    ``aida.classification_feed.ingest_classification_feed`` exactly, so the
    ledger is append-only and one query always finds "the current reason"
    regardless of whether it was a rule or an authoritative feed override.
    """
    await session.execute(
        update(ClassificationEvidence)
        .where(
            ClassificationEvidence.column_id == column.id,
            ClassificationEvidence.is_current.is_(True),
        )
        .values(is_current=False)
    )
    session.add(
        ClassificationEvidence(
            organization_id=column.organization_id,
            column_id=column.id,
            classification=result.classification,
            source_type=CLASSIFICATION_SOURCE_RULE,
            rule_id=result.rule_id,
            confidence=None,
            matched_signal={
                "value_scope": "METADATA_ONLY",
                "actual_values_inspected": False,
                **result.matched_signal,
            },
            is_current=True,
            created_by=_METADATA_WORKER_PRINCIPAL,
        )
    )


async def _get_or_create_catalog(
    session: AsyncSession,
    datasource: DataSource,
    discovered: DiscoveredCatalog,
    tracker: ChangeTracker,
) -> MetadataCatalog:
    catalog = await session.scalar(
        select(MetadataCatalog).where(
            MetadataCatalog.datasource_id == datasource.id,
            MetadataCatalog.name == discovered.name,
        )
    )
    catalog_fingerprint = fingerprint(
        {"name": discovered.name, "attributes": discovered.attributes}
    )
    tracker.observe(catalog, catalog.fingerprint if catalog else None, catalog_fingerprint)
    if catalog is None:
        catalog = MetadataCatalog(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            name=discovered.name,
            fingerprint=catalog_fingerprint,
        )
        session.add(catalog)
        await session.flush()
    else:
        catalog.status = "ACTIVE"
        catalog.deprecated_at = None
        catalog.fingerprint = catalog_fingerprint
    return catalog


async def _get_or_create_schema(
    session: AsyncSession,
    datasource: DataSource,
    catalog: MetadataCatalog,
    discovered: DiscoveredSchema,
    tracker: ChangeTracker,
) -> MetadataSchema:
    schema = await session.scalar(
        select(MetadataSchema).where(
            MetadataSchema.catalog_id == catalog.id,
            MetadataSchema.name == discovered.name,
        )
    )
    schema_fingerprint = fingerprint({"name": discovered.name, "attributes": discovered.attributes})
    tracker.observe(schema, schema.fingerprint if schema else None, schema_fingerprint)
    if schema is None:
        schema = MetadataSchema(
            organization_id=datasource.organization_id,
            catalog_id=catalog.id,
            name=discovered.name,
            fingerprint=schema_fingerprint,
        )
        session.add(schema)
        await session.flush()
    else:
        schema.status = "ACTIVE"
        schema.deprecated_at = None
        schema.fingerprint = schema_fingerprint
    return schema


async def _get_or_create_table(
    session: AsyncSession,
    datasource: DataSource,
    schema: MetadataSchema,
    discovered: DiscoveredTable,
    tracker: ChangeTracker,
) -> MetadataTable:
    table = await session.scalar(
        select(MetadataTable).where(
            MetadataTable.schema_id == schema.id,
            MetadataTable.name == discovered.name,
        )
    )
    table_fingerprint = fingerprint(asdict(discovered))
    tracker.observe(table, table.fingerprint if table else None, table_fingerprint)
    if table is None:
        table = MetadataTable(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=discovered.name,
            object_type=discovered.object_type,
            source_description=discovered.source_description,
            fingerprint=table_fingerprint,
        )
        session.add(table)
        await session.flush()
    else:
        table.status = "ACTIVE"
        table.deprecated_at = None
        table.object_type = discovered.object_type
        table.source_description = discovered.source_description
        table.fingerprint = table_fingerprint
    return table


async def _get_or_create_column(
    session: AsyncSession,
    datasource: DataSource,
    table: MetadataTable,
    discovered: DiscoveredColumn,
    tracker: ChangeTracker,
) -> MetadataColumn:
    column = await session.scalar(
        select(MetadataColumn).where(
            MetadataColumn.table_id == table.id,
            MetadataColumn.name == discovered.name,
        )
    )
    column_fingerprint = fingerprint(asdict(discovered))
    tracker.observe(column, column.fingerprint if column else None, column_fingerprint)
    rule_result = classify_column_name_with_evidence(discovered.name)
    if column is None:
        column = MetadataColumn(
            organization_id=datasource.organization_id,
            table_id=table.id,
            name=discovered.name,
            ordinal_position=discovered.ordinal_position,
            physical_type=discovered.physical_type,
            nullable=discovered.nullable,
            default_expression=discovered.default_expression,
            classification=rule_result.classification,
            classification_source=CLASSIFICATION_SOURCE_RULE,
            fingerprint=column_fingerprint,
        )
        session.add(column)
        await _record_rule_classification_evidence(session, column=column, result=rule_result)
    else:
        column.status = "ACTIVE"
        column.deprecated_at = None
        column.ordinal_position = discovered.ordinal_position
        column.physical_type = discovered.physical_type
        column.nullable = discovered.nullable
        column.default_expression = discovered.default_expression
        column.fingerprint = column_fingerprint
        # An authoritative external classification (see aida.classification_feed)
        # must never be silently overwritten by rediscovery's rule inference
        # (module 05 sec 9 exit condition) -- only ever refine an UNCLASSIFIED
        # column that a feed has not already spoken for.
        if (
            column.classification == "UNCLASSIFIED"
            and column.classification_source != CLASSIFICATION_SOURCE_EXTERNAL
        ):
            column.classification = rule_result.classification
            await _record_rule_classification_evidence(session, column=column, result=rule_result)
    return column


async def _get_or_create_constraint(
    session: AsyncSession,
    datasource: DataSource,
    table: MetadataTable,
    referenced_table: MetadataTable | None,
    discovered: DiscoveredConstraint,
    tracker: ChangeTracker,
) -> MetadataConstraint:
    constraint = await session.scalar(
        select(MetadataConstraint).where(
            MetadataConstraint.table_id == table.id,
            MetadataConstraint.name == discovered.name,
        )
    )
    constraint_fingerprint = fingerprint(asdict(discovered))
    tracker.observe(
        constraint,
        constraint.fingerprint if constraint else None,
        constraint_fingerprint,
    )
    if constraint is None:
        constraint = MetadataConstraint(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=table.id,
            name=discovered.name,
            constraint_type=discovered.constraint_type,
            columns=list(discovered.columns),
            referenced_table_id=referenced_table.id if referenced_table else None,
            referenced_columns=list(discovered.referenced_columns),
            fingerprint=constraint_fingerprint,
        )
        session.add(constraint)
    else:
        constraint.status = "ACTIVE"
        constraint.deprecated_at = None
        constraint.constraint_type = discovered.constraint_type
        constraint.columns = list(discovered.columns)
        constraint.referenced_table_id = referenced_table.id if referenced_table else None
        constraint.referenced_columns = list(discovered.referenced_columns)
        constraint.fingerprint = constraint_fingerprint
    return constraint


async def _deprecate_missing(
    session: AsyncSession,
    datasource: DataSource,
    *,
    seen_catalog_ids: set[UUID],
    seen_schema_ids: set[UUID],
    seen_table_ids: set[UUID],
    seen_column_ids: set[UUID],
    seen_constraint_ids: set[UUID],
) -> int:
    now = datetime.now(UTC)
    catalog_ids = set(
        await session.scalars(
            select(MetadataCatalog.id).where(MetadataCatalog.datasource_id == datasource.id)
        )
    )
    table_ids = set(
        await session.scalars(
            select(MetadataTable.id).where(MetadataTable.datasource_id == datasource.id)
        )
    )
    existing = SnapshotScope(
        catalog_ids=catalog_ids,
        schema_ids=set(
            await session.scalars(
                select(MetadataSchema.id).where(MetadataSchema.catalog_id.in_(catalog_ids))
            )
        ),
        table_ids=table_ids,
        column_ids=set(
            await session.scalars(
                select(MetadataColumn.id).where(MetadataColumn.table_id.in_(table_ids))
            )
        ),
        constraint_ids=set(
            await session.scalars(
                select(MetadataConstraint.id).where(
                    MetadataConstraint.datasource_id == datasource.id
                )
            )
        ),
    )
    missing = missing_snapshot_scope(
        existing,
        SnapshotScope(
            catalog_ids=seen_catalog_ids,
            schema_ids=seen_schema_ids,
            table_ids=seen_table_ids,
            column_ids=seen_column_ids,
            constraint_ids=seen_constraint_ids,
        ),
    )
    statements = [
        update(model)
        .where(model.id.in_(object_ids), model.status == "ACTIVE")
        .values(status="DEPRECATED", deprecated_at=now, updated_at=now)
        for model, object_ids in (
            (MetadataCatalog, missing.catalog_ids),
            (MetadataSchema, missing.schema_ids),
            (MetadataTable, missing.table_ids),
            (MetadataColumn, missing.column_ids),
            (MetadataConstraint, missing.constraint_ids),
        )
        if object_ids
    ]
    deprecated = 0
    for statement in statements:
        result = cast(CursorResult[Any], await session.execute(statement))
        deprecated += result.rowcount
    return deprecated


async def deprecate_missing_snapshot(
    session: AsyncSession,
    datasource: DataSource,
    scope: SnapshotScope,
) -> int:
    return await _deprecate_missing(
        session,
        datasource,
        seen_catalog_ids=scope.catalog_ids,
        seen_schema_ids=scope.schema_ids,
        seen_table_ids=scope.table_ids,
        seen_column_ids=scope.column_ids,
        seen_constraint_ids=scope.constraint_ids,
    )


async def persist_discovery_snapshot(
    session: AsyncSession,
    run: AnalysisRun,
    datasource: DataSource,
    catalogs: tuple[DiscoveredCatalog, ...],
    *,
    deprecate_missing: bool = True,
    connector_capabilities: dict[str, Any] | None = None,
    scope: SnapshotScope | None = None,
) -> dict[str, int]:
    counts = {"catalogs": 0, "schemas": 0, "tables": 0, "columns": 0, "constraints": 0}
    tracker = ChangeTracker()
    table_map: dict[tuple[str, str, str], MetadataTable] = {}
    snapshot_scope = scope or SnapshotScope()
    for discovered_catalog in catalogs:
        catalog = await _get_or_create_catalog(session, datasource, discovered_catalog, tracker)
        snapshot_scope.catalog_ids.add(catalog.id)
        counts["catalogs"] += 1
        for discovered_schema in discovered_catalog.schemas:
            schema = await _get_or_create_schema(
                session, datasource, catalog, discovered_schema, tracker
            )
            snapshot_scope.schema_ids.add(schema.id)
            counts["schemas"] += 1
            for discovered_table in discovered_schema.tables:
                table = await _get_or_create_table(
                    session, datasource, schema, discovered_table, tracker
                )
                snapshot_scope.table_ids.add(table.id)
                table_key = (
                    discovered_catalog.name,
                    discovered_schema.name,
                    discovered_table.name,
                )
                table_map[table_key] = table
                counts["tables"] += 1
                persisted_columns: list[MetadataColumn] = []
                for discovered_column in discovered_table.columns:
                    column = await _get_or_create_column(
                        session, datasource, table, discovered_column, tracker
                    )
                    persisted_columns.append(column)
                    counts["columns"] += 1
                    if counts["columns"] % 100 == 0 and activity.in_activity():
                        activity.heartbeat(counts)
                # Column defaults are assigned during flush; collect identities only
                # after the table batch is persisted so FULL reconciliation is exact.
                await session.flush()
                snapshot_scope.column_ids.update(column.id for column in persisted_columns)

    for discovered_catalog in catalogs:
        for discovered_schema in discovered_catalog.schemas:
            for discovered_table in discovered_schema.tables:
                table = table_map[
                    (discovered_catalog.name, discovered_schema.name, discovered_table.name)
                ]
                for discovered_constraint in discovered_table.constraints:
                    referenced_table = None
                    if (
                        discovered_constraint.referenced_schema
                        and discovered_constraint.referenced_table
                    ):
                        referenced_table = table_map.get(
                            (
                                discovered_catalog.name,
                                discovered_constraint.referenced_schema,
                                discovered_constraint.referenced_table,
                            )
                        )
                        if referenced_table is None:
                            referenced_table = await session.scalar(
                                select(MetadataTable)
                                .join(
                                    MetadataSchema,
                                    MetadataSchema.id == MetadataTable.schema_id,
                                )
                                .join(
                                    MetadataCatalog,
                                    MetadataCatalog.id == MetadataSchema.catalog_id,
                                )
                                .where(
                                    MetadataTable.datasource_id == datasource.id,
                                    MetadataCatalog.name == discovered_catalog.name,
                                    MetadataSchema.name == discovered_constraint.referenced_schema,
                                    MetadataTable.name == discovered_constraint.referenced_table,
                                )
                            )
                    constraint = await _get_or_create_constraint(
                        session,
                        datasource,
                        table,
                        referenced_table,
                        discovered_constraint,
                        tracker,
                    )
                    await session.flush()
                    snapshot_scope.constraint_ids.add(constraint.id)
                    counts["constraints"] += 1

    if deprecate_missing:
        tracker.deprecated = await deprecate_missing_snapshot(session, datasource, snapshot_scope)

    run.discovered_catalogs = counts["catalogs"]
    run.discovered_schemas = counts["schemas"]
    run.discovered_tables = counts["tables"]
    run.discovered_columns = counts["columns"]
    run.discovered_constraints = counts["constraints"]
    run.created_objects = tracker.created
    run.changed_objects = tracker.changed
    run.deprecated_objects = tracker.deprecated
    run.status = "PROFILING"
    datasource.status = "ACTIVE"
    if connector_capabilities is None:
        connector_capabilities = dict(
            connector_registry.definition(datasource.connector_type).capabilities
        )
    datasource.capabilities = connector_capabilities
    return {
        **counts,
        "created_objects": tracker.created,
        "changed_objects": tracker.changed,
        "deprecated_objects": tracker.deprecated,
    }


async def _mark_run_cancelled(run_uuid: UUID) -> None:
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_uuid)
        if run is None:
            return
        run.status = "CANCELLED"
        run.error_class = None
        run.error_message = None
        worker_context = SecurityContext(
            principal_id="metadata-worker",
            principal_type="WORKER",
            organization_id=run.organization_id,
            roles=frozenset({"MetadataWorker"}),
        )
        record_audit(
            session,
            worker_context,
            action="metadata.analysis.cancelled",
            resource_type="analysis_run",
            resource_id=str(run.id),
            outcome="SUCCESS",
            correlation_id=str(run.id),
        )
        record_outbox(
            session,
            organization_id=run.organization_id,
            aggregate_type="analysis_run",
            aggregate_id=str(run.id),
            event_type="metadata.analysis.cancelled.v1",
            payload={"run_id": str(run.id), "datasource_id": str(run.datasource_id)},
        )
        await session.commit()


@activity.defn(name="discover_datasource")
async def discover_datasource(run_id: str) -> dict[str, Any]:
    run_uuid = UUID(run_id)
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_uuid)
        if run is None:
            raise ValueError(f"analysis run not found: {run_id}")
        datasource = await session.get(DataSource, run.datasource_id)
        if datasource is None:
            raise ValueError(f"datasource not found: {run.datasource_id}")
        await start_task(
            analysis_run_id=run.id,
            organization_id=run.organization_id,
            task_type=TASK_TYPE_DISCOVER_DATASOURCE,
            table_id=None,
            max_attempts=TASK_TYPE_MAX_ATTEMPTS[TASK_TYPE_DISCOVER_DATASOURCE],
        )
        if datasource.status == "DISABLED":
            run.status = "CANCELLED"
            await session.commit()
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_DISCOVER_DATASOURCE,
                table_id=None,
                outcome="CANCELLED",
            )
            raise ApplicationError(
                "datasource is disabled", type="DataSourceDisabledError", non_retryable=True
            )
        run.status = "RUNNING"
        await session.commit()

    activity.heartbeat({"stage": "connecting"})
    await heartbeat_task(
        analysis_run_id=run_uuid,
        task_type=TASK_TYPE_DISCOVER_DATASOURCE,
        table_id=None,
        detail={"stage": "connecting"},
    )
    try:
        dsn = SecretResolver().resolve(datasource.credential_reference)
        connector = connector_registry.create(datasource.connector_type, dsn)
        await connector.test_connection()
        activity.heartbeat({"stage": "discovering"})
        await heartbeat_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_DISCOVER_DATASOURCE,
            table_id=None,
            detail={"stage": "discovering"},
        )
        catalogs = await connector.discover()
        if activity.is_cancelled():
            raise asyncio.CancelledError

        async with session_factory() as session:
            run = await session.get(AnalysisRun, run_uuid)
            datasource = await session.get(DataSource, run.datasource_id) if run else None
            if run is None or datasource is None:
                raise ValueError("analysis run or datasource disappeared during discovery")
            counts = await persist_discovery_snapshot(session, run, datasource, catalogs)
            # Envelope 1.1 (gap/02 N1). The pull path collects views, routines,
            # comments and grants in `connector.discover()`; without this call it
            # would drop them at persistence while both push paths kept them. No
            # version gate is needed: a pull snapshot comes from a connector whose
            # capability flags already say which axes it collected, so a connector
            # that collects an axis is authoritative for it.
            counts |= await persist_envelope_extensions(
                session,
                datasource,
                catalogs,
                deprecate_missing=(run.mode == "FULL"),
            )
            worker_context = SecurityContext(
                principal_id="metadata-worker",
                principal_type="WORKER",
                organization_id=run.organization_id,
                roles=frozenset({"MetadataWorker"}),
            )
            record_audit(
                session,
                worker_context,
                action="metadata.discovery.complete",
                resource_type="analysis_run",
                resource_id=str(run.id),
                outcome="SUCCESS",
                correlation_id=str(run.id),
                details=counts,
            )
            record_outbox(
                session,
                organization_id=run.organization_id,
                aggregate_type="analysis_run",
                aggregate_id=str(run.id),
                event_type="metadata.discovery.snapshot.v1",
                payload={"run_id": str(run.id), "datasource_id": str(datasource.id), **counts},
            )
            await session.commit()
        logger.info("datasource_discovery_completed", run_id=run_id, **counts)
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_DISCOVER_DATASOURCE,
            table_id=None,
            outcome="SUCCESS",
        )
        return {"run_id": run_id, "status": "COMPLETED", **counts}
    except asyncio.CancelledError:
        await _mark_run_cancelled(run_uuid)
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_DISCOVER_DATASOURCE,
            table_id=None,
            outcome="CANCELLED",
        )
        raise
    except Exception as exc:
        logger.exception(
            "datasource_discovery_failed", run_id=run_id, error_type=type(exc).__name__
        )
        async with session_factory() as session:
            run = await session.get(AnalysisRun, run_uuid)
            if run is not None:
                run.status = "FAILED"
                run.error_class = type(exc).__name__
                run.error_message = str(exc)[:4000]
                await session.commit()
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_DISCOVER_DATASOURCE,
            table_id=None,
            outcome="ERROR",
            error_class=type(exc).__name__,
            error_message=str(exc)[:4000],
        )
        raise


@activity.defn(name="profile_datasource")
async def profile_datasource(run_id: str) -> dict[str, Any]:
    """Build retry-safe, bounded, value-free profiles after discovery."""
    run_uuid = UUID(run_id)
    settings = get_settings()
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_uuid)
        if run is None:
            raise ValueError(f"analysis run not found: {run_id}")
        datasource = await session.get(DataSource, run.datasource_id)
        if datasource is None:
            raise ValueError(f"datasource not found: {run.datasource_id}")
        await start_task(
            analysis_run_id=run.id,
            organization_id=run.organization_id,
            task_type=TASK_TYPE_PROFILE_DATASOURCE,
            table_id=None,
            max_attempts=TASK_TYPE_MAX_ATTEMPTS[TASK_TYPE_PROFILE_DATASOURCE],
        )
        if datasource.status == "DISABLED":
            run.status = "CANCELLED"
            await session.commit()
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_PROFILE_DATASOURCE,
                table_id=None,
                outcome="CANCELLED",
            )
            raise ApplicationError(
                "datasource is disabled", type="DataSourceDisabledError", non_retryable=True
            )
        table_rows = (
            await session.execute(
                select(MetadataTable, MetadataSchema)
                .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
                .where(
                    MetadataTable.datasource_id == datasource.id,
                    MetadataTable.status == "ACTIVE",
                    MetadataTable.object_type == "BASE_TABLE",
                )
                .order_by(MetadataSchema.name, MetadataTable.name)
                .limit(settings.profile_max_tables_per_run)
            )
        ).all()

    connector = connector_registry.create(
        datasource.connector_type,
        SecretResolver().resolve(datasource.credential_reference),
    )
    profiled_tables = 0
    profiled_columns = 0
    try:
        for table, schema in table_rows:
            if activity.is_cancelled():
                raise asyncio.CancelledError
            heartbeat_detail = {
                "stage": "profiling",
                "schema": schema.name,
                "table": table.name,
                "profiled_tables": profiled_tables,
            }
            activity.heartbeat(heartbeat_detail)
            await heartbeat_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_PROFILE_DATASOURCE,
                table_id=None,
                detail=heartbeat_detail,
            )
            async with session_factory() as session:
                existing = await session.scalar(
                    select(TableProfile).where(
                        TableProfile.analysis_run_id == run_uuid,
                        TableProfile.table_id == table.id,
                    )
                )
                if existing is not None:
                    existing_column_count = await session.scalar(
                        select(func.count())
                        .select_from(ColumnProfile)
                        .where(ColumnProfile.table_profile_id == existing.id)
                    )
                    profiled_tables += 1
                    profiled_columns += existing_column_count or 0
                    continue
                columns = (
                    await session.scalars(
                        select(MetadataColumn)
                        .where(
                            MetadataColumn.table_id == table.id,
                            MetadataColumn.status == "ACTIVE",
                        )
                        .order_by(MetadataColumn.ordinal_position)
                    )
                ).all()

            snapshot = await connector.profile_table(
                schema.name,
                table.name,
                tuple(column.name for column in columns),
                sample_rows=settings.profile_sample_rows,
                column_batch_size=settings.profile_column_batch_size,
                timeout_seconds=settings.query_timeout_seconds,
            )
            columns_by_name = {column.name: column for column in columns}
            async with session_factory() as session:
                profile = TableProfile(
                    organization_id=datasource.organization_id,
                    analysis_run_id=run_uuid,
                    datasource_id=datasource.id,
                    table_id=table.id,
                    schema_fingerprint=table.fingerprint,
                    row_count_estimate=snapshot.row_count_estimate,
                    sampled_row_count=snapshot.sampled_row_count,
                )
                session.add(profile)
                await session.flush()
                for column_snapshot in snapshot.columns:
                    column = columns_by_name[column_snapshot.name]
                    session.add(
                        ColumnProfile(
                            organization_id=datasource.organization_id,
                            table_profile_id=profile.id,
                            column_id=column.id,
                            null_count=column_snapshot.null_count,
                            non_null_count=column_snapshot.non_null_count,
                            approximate_distinct_count=(column_snapshot.approximate_distinct_count),
                            min_length=column_snapshot.min_length,
                            max_length=column_snapshot.max_length,
                        )
                    )
                await session.commit()
            profiled_tables += 1
            profiled_columns += len(snapshot.columns)

        async with session_factory() as session:
            run = await session.get(AnalysisRun, run_uuid)
            if run is None:
                raise ValueError(f"analysis run not found: {run_id}")
            run.profiled_tables = profiled_tables
            run.profiled_columns = profiled_columns
            run.status = "COMPLETED"
            worker_context = SecurityContext(
                principal_id="metadata-worker",
                principal_type="WORKER",
                organization_id=run.organization_id,
                roles=frozenset({"MetadataWorker"}),
            )
            details = {
                "profiled_tables": profiled_tables,
                "profiled_columns": profiled_columns,
                "sample_rows_per_table": settings.profile_sample_rows,
                "profile_version": "safe-v1",
            }
            record_audit(
                session,
                worker_context,
                action="metadata.profiling.complete",
                resource_type="analysis_run",
                resource_id=str(run.id),
                outcome="SUCCESS",
                correlation_id=str(run.id),
                details=details,
            )
            record_outbox(
                session,
                organization_id=run.organization_id,
                aggregate_type="analysis_run",
                aggregate_id=str(run.id),
                event_type="metadata.analysis.completed.v1",
                payload={
                    "run_id": str(run.id),
                    "datasource_id": str(run.datasource_id),
                    **details,
                },
            )
            await session.commit()
        logger.info("datasource_profiling_completed", run_id=run_id, **details)
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_PROFILE_DATASOURCE,
            table_id=None,
            outcome="SUCCESS",
        )
        return {"run_id": run_id, "status": "COMPLETED", **details}
    except asyncio.CancelledError:
        await _mark_run_cancelled(run_uuid)
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_PROFILE_DATASOURCE,
            table_id=None,
            outcome="CANCELLED",
        )
        raise
    except Exception as exc:
        logger.exception(
            "datasource_profiling_failed", run_id=run_id, error_type=type(exc).__name__
        )
        async with session_factory() as session:
            run = await session.get(AnalysisRun, run_uuid)
            if run is not None:
                run.status = "FAILED"
                run.error_class = type(exc).__name__
                run.error_message = str(exc)[:4000]
                await session.commit()
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_PROFILE_DATASOURCE,
            table_id=None,
            outcome="ERROR",
            error_class=type(exc).__name__,
            error_message=str(exc)[:4000],
        )
        raise


@activity.defn(name="plan_profile_tasks")
async def plan_profile_tasks(run_id: str) -> dict[str, Any]:
    run_uuid = UUID(run_id)
    settings = get_settings()
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_uuid)
        if run is None:
            raise ValueError(f"analysis run not found: {run_id}")
        datasource = await session.get(DataSource, run.datasource_id)
        if datasource is None:
            raise ValueError(f"datasource not found: {run.datasource_id}")
        await start_task(
            analysis_run_id=run.id,
            organization_id=run.organization_id,
            task_type=TASK_TYPE_PLAN_PROFILE_TASKS,
            table_id=None,
            max_attempts=TASK_TYPE_MAX_ATTEMPTS[TASK_TYPE_PLAN_PROFILE_TASKS],
        )
        if datasource.status == "DISABLED":
            run.status = "CANCELLED"
            await session.commit()
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_PLAN_PROFILE_TASKS,
                table_id=None,
                outcome="CANCELLED",
            )
            raise ApplicationError(
                "datasource is disabled", type="DataSourceDisabledError", non_retryable=True
            )
        try:
            table_ids = list(
                await session.scalars(
                    select(MetadataTable.id)
                    .where(
                        # INV-5: the tenant boundary is restated explicitly rather than
                        # inherited from the datasource FK, so this query is scoped even
                        # if a future caller hands it a datasource from another tenant.
                        MetadataTable.organization_id == run.organization_id,
                        MetadataTable.datasource_id == datasource.id,
                        MetadataTable.status == "ACTIVE",
                        MetadataTable.object_type == "BASE_TABLE",
                    )
                    .order_by(MetadataTable.id)
                    .limit(settings.profile_max_tables_per_run)
                )
            )
        except Exception as exc:
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_PLAN_PROFILE_TASKS,
                table_id=None,
                outcome="ERROR",
                error_class=type(exc).__name__,
                error_message=str(exc)[:4000],
            )
            raise
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_PLAN_PROFILE_TASKS,
            table_id=None,
            outcome="SUCCESS",
        )
        return {
            "run_id": run_id,
            "table_ids": [str(table_id) for table_id in table_ids],
            "max_concurrency": datasource.max_concurrency,
        }


@activity.defn(name="profile_table_task")
async def profile_table_task(payload: dict[str, str]) -> dict[str, int]:
    run_uuid = UUID(payload["run_id"])
    table_uuid = UUID(payload["table_id"])
    settings = get_settings()
    if activity.is_cancelled():
        await _mark_run_cancelled(run_uuid)
        raise asyncio.CancelledError
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_uuid)
        if run is None:
            raise ValueError(f"analysis run not found: {run_uuid}")
        await start_task(
            analysis_run_id=run.id,
            organization_id=run.organization_id,
            task_type=TASK_TYPE_PROFILE_TABLE,
            table_id=table_uuid,
            max_attempts=TASK_TYPE_MAX_ATTEMPTS[TASK_TYPE_PROFILE_TABLE],
        )
        datasource = await session.get(DataSource, run.datasource_id)
        row = (
            await session.execute(
                select(MetadataTable, MetadataSchema)
                .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
                .where(
                    MetadataTable.id == table_uuid,
                    MetadataTable.datasource_id == run.datasource_id,
                    MetadataTable.status == "ACTIVE",
                )
            )
        ).one_or_none()
        if datasource is None or row is None:
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_PROFILE_TABLE,
                table_id=table_uuid,
                outcome="ERROR",
                error_class="ValueError",
                error_message="profile task dependency is unavailable",
            )
            raise ValueError("profile task dependency is unavailable")
        if datasource.status == "DISABLED":
            run.status = "CANCELLED"
            await session.commit()
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_PROFILE_TABLE,
                table_id=table_uuid,
                outcome="CANCELLED",
            )
            raise ApplicationError(
                "datasource is disabled", type="DataSourceDisabledError", non_retryable=True
            )
        table, schema = row
        existing = await session.scalar(
            select(TableProfile).where(
                TableProfile.analysis_run_id == run_uuid,
                TableProfile.table_id == table.id,
            )
        )
        if existing is not None:
            existing_columns = await session.scalar(
                select(func.count())
                .select_from(ColumnProfile)
                .where(ColumnProfile.table_profile_id == existing.id)
            )
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_PROFILE_TABLE,
                table_id=table_uuid,
                outcome="SUCCESS",
            )
            return {"profiled_tables": 1, "profiled_columns": existing_columns or 0}
        columns = (
            await session.scalars(
                select(MetadataColumn)
                .where(
                    MetadataColumn.table_id == table.id,
                    MetadataColumn.status == "ACTIVE",
                )
                .order_by(MetadataColumn.ordinal_position)
            )
        ).all()

    activity.heartbeat({"stage": "profiling", "table_id": str(table_uuid)})
    await heartbeat_task(
        analysis_run_id=run_uuid,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=table_uuid,
        detail={"stage": "profiling"},
    )
    connector = connector_registry.create(
        datasource.connector_type,
        SecretResolver().resolve(datasource.credential_reference),
    )
    try:
        snapshot = await connector.profile_table(
            schema.name,
            table.name,
            tuple(column.name for column in columns),
            sample_rows=settings.profile_sample_rows,
            column_batch_size=settings.profile_column_batch_size,
            timeout_seconds=settings.query_timeout_seconds,
        )
        if activity.is_cancelled():
            await _mark_run_cancelled(run_uuid)
            raise asyncio.CancelledError
        columns_by_name = {column.name: column for column in columns}
        async with session_factory() as session:
            existing = await session.scalar(
                select(TableProfile).where(
                    TableProfile.analysis_run_id == run_uuid,
                    TableProfile.table_id == table_uuid,
                )
            )
            if existing is not None:
                existing_columns = await session.scalar(
                    select(func.count())
                    .select_from(ColumnProfile)
                    .where(ColumnProfile.table_profile_id == existing.id)
                )
                await finish_task(
                    analysis_run_id=run_uuid,
                    task_type=TASK_TYPE_PROFILE_TABLE,
                    table_id=table_uuid,
                    outcome="SUCCESS",
                )
                return {"profiled_tables": 1, "profiled_columns": existing_columns or 0}
            profile = TableProfile(
                organization_id=datasource.organization_id,
                analysis_run_id=run_uuid,
                datasource_id=datasource.id,
                table_id=table_uuid,
                schema_fingerprint=table.fingerprint,
                row_count_estimate=snapshot.row_count_estimate,
                sampled_row_count=snapshot.sampled_row_count,
            )
            session.add(profile)
            await session.flush()
            for column_snapshot in snapshot.columns:
                column = columns_by_name[column_snapshot.name]
                session.add(
                    ColumnProfile(
                        organization_id=datasource.organization_id,
                        table_profile_id=profile.id,
                        column_id=column.id,
                        null_count=column_snapshot.null_count,
                        non_null_count=column_snapshot.non_null_count,
                        approximate_distinct_count=column_snapshot.approximate_distinct_count,
                        min_length=column_snapshot.min_length,
                        max_length=column_snapshot.max_length,
                    )
                )
            await session.commit()
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_PROFILE_TABLE,
            table_id=table_uuid,
            outcome="SUCCESS",
        )
        return {"profiled_tables": 1, "profiled_columns": len(snapshot.columns)}
    except asyncio.CancelledError:
        await _mark_run_cancelled(run_uuid)
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_PROFILE_TABLE,
            table_id=table_uuid,
            outcome="CANCELLED",
        )
        raise
    except Exception as exc:
        async with session_factory() as session:
            failed_run = await session.get(AnalysisRun, run_uuid)
            if failed_run is not None:
                failed_run.status = "FAILED"
                failed_run.error_class = type(exc).__name__
                failed_run.error_message = str(exc)[:4000]
                await session.commit()
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_PROFILE_TABLE,
            table_id=table_uuid,
            outcome="ERROR",
            error_class=type(exc).__name__,
            error_message=str(exc)[:4000],
        )
        raise


async def _infer_and_persist_key_candidates(
    session: AsyncSession,
    run: AnalysisRun,
) -> int:
    """The DAG's "infer keys" stage (module 05 sec 6, PR-1) -- run once per
    analysis run, after every table in it has a completed profile.

    Every candidate is derived purely from ``ColumnProfile``/``TableProfile``
    value-free statistics (ADR-0014) via ``aida.key_inference``; nothing here
    reads a source value. Candidates are proposals only -- they land as
    ``PENDING`` and require a maker-checker decision through
    ``POST /v1/key-candidates/{id}/decision`` before anything downstream may
    treat a column (set) as a key. A table_id/key_fingerprint pair already on
    record (pending, approved, or rejected) is never re-proposed, so a human
    decision is never silently reopened by the next run.
    """
    table_profiles = (
        await session.scalars(
            select(TableProfile).where(TableProfile.analysis_run_id == run.id)
        )
    ).all()
    created = 0
    for table_profile in table_profiles:
        column_rows = (
            await session.execute(
                select(ColumnProfile, MetadataColumn)
                .join(MetadataColumn, MetadataColumn.id == ColumnProfile.column_id)
                .where(ColumnProfile.table_profile_id == table_profile.id)
            )
        ).all()
        if not column_rows:
            continue
        stats = [
            ColumnStat(
                column_id=column.id,
                column_name=column.name,
                null_count=column_profile.null_count,
                non_null_count=column_profile.non_null_count,
                approximate_distinct_count=column_profile.approximate_distinct_count,
            )
            for column_profile, column in column_rows
        ]
        columns_by_lower_name = {column.name.lower(): column for _, column in column_rows}
        constraints = (
            await session.scalars(
                select(MetadataConstraint).where(
                    MetadataConstraint.table_id == table_profile.table_id,
                    MetadataConstraint.constraint_type == "PRIMARY_KEY",
                    MetadataConstraint.status == "ACTIVE",
                )
            )
        ).all()
        declared_primary_keys: set[frozenset[UUID]] = set()
        for constraint in constraints:
            declared_columns = frozenset(
                columns_by_lower_name[name.lower()].id
                for name in constraint.columns
                if name.lower() in columns_by_lower_name
            )
            if declared_columns:
                declared_primary_keys.add(declared_columns)
        candidates = infer_key_candidates(
            stats,
            table_profile.row_count_estimate or 0,
            declared_primary_key_column_ids=frozenset(declared_primary_keys),
            max_candidates=MAX_KEY_CANDIDATES_PER_TABLE,
        )
        if not candidates:
            continue
        existing_fingerprints = set(
            await session.scalars(
                select(KeyInferenceCandidate.key_fingerprint).where(
                    KeyInferenceCandidate.table_id == table_profile.table_id
                )
            )
        )
        for candidate in candidates:
            fp = key_fingerprint(candidate.column_ids)
            if fp in existing_fingerprints:
                continue
            session.add(
                KeyInferenceCandidate(
                    organization_id=run.organization_id,
                    datasource_id=run.datasource_id,
                    table_id=table_profile.table_id,
                    table_profile_id=table_profile.id,
                    column_ids=[str(column_id) for column_id in candidate.column_ids],
                    column_names=list(candidate.column_names),
                    column_count=candidate.column_count,
                    key_fingerprint=fp,
                    detection_rule=candidate.detection_rule,
                    confidence=candidate.confidence,
                    estimated_distinctness_ratio=candidate.estimated_distinctness_ratio,
                    evidence=candidate.evidence,
                    created_by=_METADATA_WORKER_PRINCIPAL,
                )
            )
            existing_fingerprints.add(fp)
            created += 1
            # Matches the catalog-documented `key.inferred` row exactly
            # (Docs/30-contracts/04-event-catalog.md) -- payload
            # table_id/columns/confidence, one event per proposed candidate.
            record_outbox(
                session,
                organization_id=run.organization_id,
                aggregate_type="metadata_table",
                aggregate_id=str(table_profile.table_id),
                event_type="key.inferred",
                payload={
                    "table_id": str(table_profile.table_id),
                    "columns": list(candidate.column_names),
                    "confidence": candidate.confidence,
                },
            )
    return created


@activity.defn(name="finalize_profile_tasks")
async def finalize_profile_tasks(payload: dict[str, Any]) -> dict[str, Any]:
    run_uuid = UUID(str(payload["run_id"]))
    profiled_tables = int(payload["profiled_tables"])
    profiled_columns = int(payload["profiled_columns"])
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_uuid)
        if run is None:
            raise ValueError(f"analysis run not found: {run_uuid}")
        await start_task(
            analysis_run_id=run.id,
            organization_id=run.organization_id,
            task_type=TASK_TYPE_FINALIZE_PROFILE_TASKS,
            table_id=None,
            max_attempts=TASK_TYPE_MAX_ATTEMPTS[TASK_TYPE_FINALIZE_PROFILE_TASKS],
        )
        try:
            run.profiled_tables = profiled_tables
            run.profiled_columns = profiled_columns
            run.status = "COMPLETED"
            run.error_class = None
            run.error_message = None
            worker_context = SecurityContext(
                principal_id="metadata-worker",
                principal_type="WORKER",
                organization_id=run.organization_id,
                roles=frozenset({"MetadataWorker"}),
            )
            key_candidates_created = await _infer_and_persist_key_candidates(session, run)
            details = {
                "profiled_tables": profiled_tables,
                "profiled_columns": profiled_columns,
                "profile_version": "safe-v1",
                "execution_model": "TABLE_TASK_DAG_V1",
                "key_candidates_created": key_candidates_created,
            }
            quality = await evaluate_analysis_run(
                session,
                analysis_run_id=run.id,
                organization_id=run.organization_id,
                datasource_id=run.datasource_id,
                context=worker_context,
            )
            details["quality"] = quality
            record_audit(
                session,
                worker_context,
                action="metadata.profiling.complete",
                resource_type="analysis_run",
                resource_id=str(run.id),
                outcome="SUCCESS",
                correlation_id=str(run.id),
                details=details,
            )
            record_outbox(
                session,
                organization_id=run.organization_id,
                aggregate_type="analysis_run",
                aggregate_id=str(run.id),
                event_type="metadata.analysis.completed.v1",
                payload={
                    "run_id": str(run.id),
                    "datasource_id": str(run.datasource_id),
                    **details,
                },
            )
            await session.commit()
        except Exception as exc:
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_FINALIZE_PROFILE_TASKS,
                table_id=None,
                outcome="ERROR",
                error_class=type(exc).__name__,
                error_message=str(exc)[:4000],
            )
            raise
    await finish_task(
        analysis_run_id=run_uuid,
        task_type=TASK_TYPE_FINALIZE_PROFILE_TASKS,
        table_id=None,
        outcome="SUCCESS",
    )
    return {"run_id": str(run_uuid), "status": "COMPLETED", **details}
