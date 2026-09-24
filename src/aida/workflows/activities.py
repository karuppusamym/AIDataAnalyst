import asyncio
import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
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
from aida.capability_states import CapabilityState
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import (
    CHANGE_COLUMNS_ADDED,
    CHANGE_COLUMNS_REMOVED,
    CHANGE_COLUMNS_RETURNED,
    CHANGE_COLUMNS_RETYPED,
    SIGNAL_DEPRECATED,
    SIGNAL_REACTIVATED,
    SIGNAL_STRUCTURE_CHANGED,
    ChangeSignal,
    record_change_signals,
)
from aida.classification_feed import (
    CLASSIFICATION_SOURCE_EXTERNAL,
    CLASSIFICATION_SOURCE_RULE,
    RuleClassificationResult,
    classify_column_name_with_evidence,
)
from aida.config import Settings, get_settings
from aida.connectors.base import (
    Connector,
    ConnectorValueProfilingUnsupported,
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredConstraint,
    DiscoveredIndex,
    DiscoveredPartition,
    DiscoveredSchema,
    DiscoveredTable,
)
from aida.connectors.discovery import (
    FACET_CONSTRAINTS,
    FACET_GRANTS,
    FACET_INDEXES,
    FACET_OBJECT_COMMENTS,
    FACET_PARTITIONS,
    FACET_ROUTINE_BODIES,
    FACET_VIEW_DEFINITIONS,
    FacetReadScope,
    classify_read_failure,
    facet_read_scope,
)
from aida.connectors.registry import connector_registry
from aida.connectors.write_probe import (
    NOT_PROBED,
    WritePrivilegeProbe,
    probe_write_privileges,
)
from aida.db import session_factory
from aida.discovery_receipt import (
    FACET_OBJECT_VISIBILITY,
    FACET_SEQUENCES,
    FACET_TRIGGERS,
    STREAM_COMPLETE,
    STREAM_IN_PROGRESS,
    STREAM_INTERRUPTED,
    DiscoveryReceipt,
)
from aida.discovery_selection import (
    DiscoverySelection,
    apply_selection,
    grant_in_scope,
    routine_in_scope,
    selection_for,
    table_kind,
)
from aida.envelope_models import (
    MetadataObjectDescription,
    MetadataRoutine,
    MetadataRoutineParameter,
    MetadataSequence,
    MetadataSourceGrant,
    MetadataTrigger,
    MetadataViewDefinition,
)
from aida.events import record_audit, record_outbox
from aida.identity_resolution import IdentityMatch, score_table_rename
from aida.ingestion import (
    EnvelopeScope,
    deprecate_missing_envelope_extensions,
    persist_envelope_extensions,
)
from aida.models import (
    AnalysisRun,
    ClassificationEvidence,
    ColumnProfile,
    ColumnValueProfileArtifact,
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataIndex,
    MetadataPartition,
    MetadataSchema,
    MetadataTable,
    RenameCandidate,
    TableProfile,
)
from aida.pagination import InvalidCursor, apply_keyset, decode_cursor, encode_cursor
from aida.profiling_exceptions import GATED_CLASSIFICATIONS, approved_policy_for
from aida.quality_service import evaluate_analysis_run
from aida.secrets import SecretResolver
from aida.security import SecurityContext
from aida.task_tracking import finish_task, heartbeat_task, start_task
from aida.workflows.continuation import clamp_page_size
from atlas.modules.profiling.facets import (
    derived_column_facets,
    persistable_observation_scope,
)

logger = structlog.get_logger(__name__)

# Worker principal used for both audit evidence and rule-classification
# evidence rows written from inside a Temporal activity (no human principal
# is available on this path).
_METADATA_WORKER_PRINCIPAL = "metadata-worker"


#: R11-FP16: a column retyped can change what a query answers; one returned or added cannot.
_SHAPE_CHANGE_RANK: dict[str, int] = {
    CHANGE_COLUMNS_ADDED: 1,
    CHANGE_COLUMNS_RETURNED: 2,
    CHANGE_COLUMNS_RETYPED: 3,
}


@dataclass(slots=True)
class ChangeTracker:
    created: int = 0
    changed: int = 0
    deprecated: int = 0
    # R11-FP15: which tables changed, not just how many objects. A table created in this call
    # is not a change to anything that could depend on it, so its columns signal nothing.
    new_table_ids: set[UUID] = field(default_factory=set)
    #: R11-FP16: each reshaped table's most consequential shape change (`change_signals`).
    structure_changes: dict[UUID, str] = field(default_factory=dict)
    signals: list[ChangeSignal] = field(default_factory=list)

    def observe(self, existing: object | None, old_fingerprint: str | None, new: str) -> None:
        if existing is None:
            self.created += 1
        elif old_fingerprint != new or getattr(existing, "status", "ACTIVE") != "ACTIVE":
            self.changed += 1

    def reshape(self, table_id: UUID, change_class: str) -> None:
        current = self.structure_changes.get(table_id)
        if current is None or _SHAPE_CHANGE_RANK[change_class] > _SHAPE_CHANGE_RANK[current]:
            self.structure_changes[table_id] = change_class


@dataclass(slots=True)
class SnapshotScope:
    """Object identities observed across one or many chunks of an authoritative snapshot."""

    catalog_ids: set[UUID] = field(default_factory=set)
    schema_ids: set[UUID] = field(default_factory=set)
    table_ids: set[UUID] = field(default_factory=set)
    column_ids: set[UUID] = field(default_factory=set)
    constraint_ids: set[UUID] = field(default_factory=set)
    # Tables actually created (not merely reactivated/updated) across every chunk of this
    # snapshot -- the CT-4 rename-detection "just created" side. Not part of object_counts()
    # since it is a detection input, not an inventory total.
    created_table_ids: set[UUID] = field(default_factory=set)
    index_ids: set[UUID] = field(default_factory=set)
    partition_ids: set[UUID] = field(default_factory=set)

    def object_counts(self) -> dict[str, int]:
        return {
            "catalogs": len(self.catalog_ids),
            "schemas": len(self.schema_ids),
            "tables": len(self.table_ids),
            "columns": len(self.column_ids),
            "constraints": len(self.constraint_ids),
            "indexes": len(self.index_ids),
            "partitions": len(self.partition_ids),
        }


def missing_snapshot_scope(existing: SnapshotScope, observed: SnapshotScope) -> SnapshotScope:
    """Return only inventory identities absent from an authoritative full snapshot."""
    return SnapshotScope(
        catalog_ids=existing.catalog_ids - observed.catalog_ids,
        schema_ids=existing.schema_ids - observed.schema_ids,
        table_ids=existing.table_ids - observed.table_ids,
        column_ids=existing.column_ids - observed.column_ids,
        constraint_ids=existing.constraint_ids - observed.constraint_ids,
        index_ids=existing.index_ids - observed.index_ids,
        partition_ids=existing.partition_ids - observed.partition_ids,
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
    *,
    created_table_ids: set[UUID] | None = None,
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
        tracker.new_table_ids.add(table.id)
        if created_table_ids is not None:
            created_table_ids.add(table.id)
    else:
        if table.status != "ACTIVE":
            tracker.signals.append(ChangeSignal("TABLE", table.id, SIGNAL_REACTIVATED))
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
    # R11-FP15: a column added to, returned to or retyped in an existing table changes the
    # table's shape. A description edit does not, so only type and nullability are compared.
    if table.id not in tracker.new_table_ids:
        if column is None:
            tracker.reshape(table.id, CHANGE_COLUMNS_ADDED)
        elif (
            column.physical_type != discovered.physical_type
            or column.nullable != discovered.nullable
        ):
            tracker.reshape(table.id, CHANGE_COLUMNS_RETYPED)
        elif column.status != "ACTIVE":
            tracker.reshape(table.id, CHANGE_COLUMNS_RETURNED)
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
            source_description=discovered.source_description,
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
        column.source_description = discovered.source_description
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


@dataclass(slots=True)
class DeprecationResult:
    """Total rows tombstoned plus, specifically, which tables -- the CT-4 rename-detection
    "just tombstoned" input."""

    total: int
    deprecated_table_ids: set[UUID] = field(default_factory=set)


async def _get_or_create_index(
    session: AsyncSession,
    datasource: DataSource,
    table: MetadataTable,
    discovered: DiscoveredIndex,
    tracker: ChangeTracker,
) -> MetadataIndex:
    index = await session.scalar(
        select(MetadataIndex).where(
            MetadataIndex.table_id == table.id,
            MetadataIndex.name == discovered.name,
        )
    )
    index_fingerprint = fingerprint(asdict(discovered))
    tracker.observe(index, index.fingerprint if index else None, index_fingerprint)
    if index is None:
        index = MetadataIndex(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=table.id,
            name=discovered.name,
            index_type=discovered.index_type,
            columns=list(discovered.columns),
            is_unique=discovered.is_unique,
            is_primary=discovered.is_primary,
            fingerprint=index_fingerprint,
        )
        session.add(index)
    else:
        index.status = "ACTIVE"
        index.deprecated_at = None
        index.index_type = discovered.index_type
        index.columns = list(discovered.columns)
        index.is_unique = discovered.is_unique
        index.is_primary = discovered.is_primary
        index.fingerprint = index_fingerprint
    return index


async def _get_or_create_partition(
    session: AsyncSession,
    datasource: DataSource,
    table: MetadataTable,
    discovered: DiscoveredPartition,
    tracker: ChangeTracker,
) -> MetadataPartition:
    partition = await session.scalar(
        select(MetadataPartition).where(
            MetadataPartition.table_id == table.id,
            MetadataPartition.name == discovered.name,
        )
    )
    partition_fingerprint = fingerprint(asdict(discovered))
    tracker.observe(partition, partition.fingerprint if partition else None, partition_fingerprint)
    if partition is None:
        partition = MetadataPartition(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=table.id,
            name=discovered.name,
            partition_type=discovered.partition_type,
            ordinal_position=discovered.ordinal_position,
            key_columns=list(discovered.key_columns),
            high_value=discovered.high_value,
            fingerprint=partition_fingerprint,
        )
        session.add(partition)
    else:
        partition.status = "ACTIVE"
        partition.deprecated_at = None
        partition.partition_type = discovered.partition_type
        partition.ordinal_position = discovered.ordinal_position
        partition.key_columns = list(discovered.key_columns)
        partition.high_value = discovered.high_value
        partition.fingerprint = partition_fingerprint
    return partition


async def _deprecate_missing(
    session: AsyncSession,
    datasource: DataSource,
    *,
    seen_catalog_ids: set[UUID],
    seen_schema_ids: set[UUID],
    seen_table_ids: set[UUID],
    seen_column_ids: set[UUID],
    seen_constraint_ids: set[UUID],
    seen_index_ids: set[UUID],
    seen_partition_ids: set[UUID],
    analysis_run_id: UUID | None = None,
) -> DeprecationResult:
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
        index_ids=set(
            await session.scalars(
                select(MetadataIndex.id).where(MetadataIndex.datasource_id == datasource.id)
            )
        ),
        partition_ids=set(
            await session.scalars(
                select(MetadataPartition.id).where(MetadataPartition.datasource_id == datasource.id)
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
            index_ids=seen_index_ids,
            partition_ids=seen_partition_ids,
        ),
    )
    # Captured *before* the UPDATE below flips them: exactly the tables that are about to
    # transition ACTIVE -> DEPRECATED in this call, as opposed to `missing.table_ids`, which
    # also includes tables missing from a prior run that are already DEPRECATED. This is the
    # CT-4 rename-detection "just tombstoned in this run" input (module 04 SS6).
    deprecated_table_ids: set[UUID] = (
        set(
            await session.scalars(
                select(MetadataTable.id).where(
                    MetadataTable.id.in_(missing.table_ids),
                    MetadataTable.status == "ACTIVE",
                )
            )
        )
        if missing.table_ids
        else set()
    )
    # R11-FP15: the same "about to flip" read for columns -- a table that stays but loses a
    # column changed shape. Recorded in this transaction, before the updates below.
    reshaped_table_ids: set[UUID] = (
        set(
            await session.scalars(
                select(MetadataColumn.table_id).where(
                    MetadataColumn.id.in_(missing.column_ids),
                    MetadataColumn.status == "ACTIVE",
                )
            )
        )
        - deprecated_table_ids
        if missing.column_ids
        else set()
    )
    record_change_signals(
        session,
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        analysis_run_id=analysis_run_id,
        signals=[
            *(
                ChangeSignal("TABLE", table_id, SIGNAL_DEPRECATED)
                for table_id in sorted(deprecated_table_ids, key=str)
            ),
            *(
                ChangeSignal("TABLE", table_id, SIGNAL_STRUCTURE_CHANGED, CHANGE_COLUMNS_REMOVED)
                for table_id in sorted(reshaped_table_ids, key=str)
            ),
        ],
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
            (MetadataIndex, missing.index_ids),
            (MetadataPartition, missing.partition_ids),
        )
        if object_ids
    ]
    deprecated = 0
    for statement in statements:
        result = cast(CursorResult[Any], await session.execute(statement))
        deprecated += result.rowcount
    return DeprecationResult(total=deprecated, deprecated_table_ids=deprecated_table_ids)


async def deprecate_missing_snapshot(
    session: AsyncSession,
    datasource: DataSource,
    scope: SnapshotScope,
    *,
    analysis_run_id: UUID | None = None,
) -> DeprecationResult:
    return await _deprecate_missing(
        session,
        datasource,
        seen_catalog_ids=scope.catalog_ids,
        seen_schema_ids=scope.schema_ids,
        seen_table_ids=scope.table_ids,
        seen_column_ids=scope.column_ids,
        seen_constraint_ids=scope.constraint_ids,
        seen_index_ids=scope.index_ids,
        seen_partition_ids=scope.partition_ids,
        analysis_run_id=analysis_run_id,
    )


def union_snapshot_scopes(left: SnapshotScope, right: SnapshotScope) -> SnapshotScope:
    return SnapshotScope(
        catalog_ids=left.catalog_ids | right.catalog_ids,
        schema_ids=left.schema_ids | right.schema_ids,
        table_ids=left.table_ids | right.table_ids,
        column_ids=left.column_ids | right.column_ids,
        constraint_ids=left.constraint_ids | right.constraint_ids,
        created_table_ids=set(left.created_table_ids),
        index_ids=left.index_ids | right.index_ids,
        partition_ids=left.partition_ids | right.partition_ids,
    )


def union_envelope_scopes(left: EnvelopeScope, right: EnvelopeScope) -> EnvelopeScope:
    return EnvelopeScope(
        view_definition_ids=left.view_definition_ids | right.view_definition_ids,
        routine_ids=left.routine_ids | right.routine_ids,
        routine_parameter_ids=left.routine_parameter_ids | right.routine_parameter_ids,
        object_description_ids=left.object_description_ids | right.object_description_ids,
        grant_ids=left.grant_ids | right.grant_ids,
        # R11-FP01: unioned like every other axis. Omitting them here is the quiet way
        # the "counted as seen" protections below would have stopped working -- the
        # retained ids would be computed correctly and then dropped on the way into the
        # reconciliation pass.
        trigger_ids=left.trigger_ids | right.trigger_ids,
        sequence_ids=left.sequence_ids | right.sequence_ids,
    )


_ID_CHUNK = 1000


def _id_chunks(ids: set[UUID]) -> list[list[UUID]]:
    ordered = sorted(ids, key=str)
    return [ordered[start : start + _ID_CHUNK] for start in range(0, len(ordered), _ID_CHUNK)]


async def out_of_scope_existing(
    session: AsyncSession, datasource: DataSource, selection: DiscoverySelection
) -> tuple[SnapshotScope, EnvelopeScope]:
    """R11-FP01: existing objects a discovery selection does not cover.

    A FULL run counts them as seen, because they were never looked for: retiring them would
    turn "narrow the scan" into "delete what earlier scans found". Children follow their
    parent -- the columns of an excluded table, the parameters of an excluded routine.
    """
    snapshot, envelope = SnapshotScope(), EnvelopeScope()
    schema_rows = (
        await session.execute(
            select(MetadataSchema.id, MetadataSchema.name, MetadataSchema.catalog_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(MetadataCatalog.datasource_id == datasource.id)
        )
    ).all()
    schema_names = {schema_id: name for schema_id, name, _ in schema_rows}
    for schema_id, name, catalog_id in schema_rows:
        if not selection.schema_in_scope(name):
            snapshot.schema_ids.add(schema_id)
            snapshot.catalog_ids.add(catalog_id)

    table_rows = await session.execute(
        select(
            MetadataTable.id, MetadataTable.name, MetadataTable.object_type, MetadataTable.schema_id
        ).where(MetadataTable.datasource_id == datasource.id)
    )
    for table_id, name, object_type, schema_id in table_rows.all():
        schema = schema_names.get(schema_id)
        if schema is not None and not selection.object_in_scope(
            schema, name, table_kind(object_type)
        ):
            snapshot.table_ids.add(table_id)
    for chunk in _id_chunks(snapshot.table_ids):
        for target, model in (
            (snapshot.column_ids, MetadataColumn),
            (snapshot.constraint_ids, MetadataConstraint),
            (snapshot.index_ids, MetadataIndex),
            (snapshot.partition_ids, MetadataPartition),
            (envelope.view_definition_ids, MetadataViewDefinition),
        ):
            target.update(await session.scalars(select(model.id).where(model.table_id.in_(chunk))))

    routine_rows = await session.execute(
        select(
            MetadataRoutine.id,
            MetadataRoutine.name,
            MetadataRoutine.routine_type,
            MetadataRoutine.schema_id,
            MetadataRoutine.package_name,
        ).where(MetadataRoutine.datasource_id == datasource.id)
    )
    for routine_id, name, routine_type, schema_id, package_name in routine_rows.all():
        schema = schema_names.get(schema_id)
        # R11-FP03: a package member is out of scope exactly when its package is -- the
        # same rule `apply_selection` scopes it by on the way in, so the two halves agree.
        if schema is not None and not routine_in_scope(
            selection, schema, name, routine_type, package_name or None
        ):
            envelope.routine_ids.add(routine_id)
    for chunk in _id_chunks(envelope.routine_ids):
        envelope.routine_parameter_ids.update(
            await session.scalars(
                select(MetadataRoutineParameter.id).where(
                    MetadataRoutineParameter.routine_id.in_(chunk)
                )
            )
        )
    # R11-FP01: a trigger and a sequence are each scoped by their *own* qualified name
    # and kind, exactly as `discovery_selection.apply_selection` scopes them on the way
    # in -- a trigger by `schema.trigger_name` against kind TRIGGER, never by its firing
    # table's name. Scoping a trigger by its firing table would silently re-admit an
    # object the operator excluded (and, here, would retire one they did not), and the
    # two halves have to agree or an object is dropped from the scan by one rule and
    # tombstoned by the other. `object_in_scope` also covers the kind list, so a
    # selection that simply does not name TRIGGER retires no trigger either.
    trigger_rows = await session.execute(
        select(MetadataTrigger.id, MetadataTrigger.name, MetadataTrigger.schema_id).where(
            MetadataTrigger.organization_id == datasource.organization_id,
            MetadataTrigger.datasource_id == datasource.id,
        )
    )
    for trigger_id, name, schema_id in trigger_rows.all():
        schema = schema_names.get(schema_id)
        if schema is not None and not selection.object_in_scope(schema, name, "TRIGGER"):
            envelope.trigger_ids.add(trigger_id)
    sequence_rows = await session.execute(
        select(MetadataSequence.id, MetadataSequence.name, MetadataSequence.schema_id).where(
            MetadataSequence.organization_id == datasource.organization_id,
            MetadataSequence.datasource_id == datasource.id,
        )
    )
    for sequence_id, name, schema_id in sequence_rows.all():
        schema = schema_names.get(schema_id)
        if schema is not None and not selection.object_in_scope(schema, name, "SEQUENCE"):
            envelope.sequence_ids.add(sequence_id)
    for chunk in _id_chunks(snapshot.schema_ids):
        envelope.object_description_ids.update(
            await session.scalars(
                select(MetadataObjectDescription.id).where(
                    MetadataObjectDescription.schema_id.in_(chunk)
                )
            )
        )
    grant_rows = await session.execute(
        select(
            MetadataSourceGrant.id,
            MetadataSourceGrant.schema_id,
            MetadataSourceGrant.object_type,
            MetadataSourceGrant.object_name,
            MetadataSourceGrant.schema_name,
        ).where(MetadataSourceGrant.datasource_id == datasource.id)
    )
    for grant_id, schema_id, object_type, object_name, schema_name in grant_rows.all():
        schema = schema_name or schema_names.get(schema_id)
        if schema is not None and not grant_in_scope(selection, schema, object_type, object_name):
            envelope.grant_ids.add(grant_id)
    return snapshot, envelope


#: Facet -> the stored axis its read populates, for the reconciliation rule below.
#: `inventory` is absent on purpose: a refused roster fails the run
#: (`connectors.discovery.RETIREMENT_BEARING_FACETS`), because there is no honest way to
#: complete a run that saw no objects.
_FACET_AXES: dict[str, tuple[tuple[str, Any], ...]] = {
    FACET_CONSTRAINTS: (("constraint_ids", MetadataConstraint),),
    FACET_INDEXES: (("index_ids", MetadataIndex),),
    FACET_PARTITIONS: (("partition_ids", MetadataPartition),),
    FACET_VIEW_DEFINITIONS: (("view_definition_ids", MetadataViewDefinition),),
    FACET_ROUTINE_BODIES: (
        ("routine_ids", MetadataRoutine),
        ("routine_parameter_ids", MetadataRoutineParameter),
    ),
    FACET_OBJECT_COMMENTS: (("object_description_ids", MetadataObjectDescription),),
    FACET_GRANTS: (("grant_ids", MetadataSourceGrant),),
    # R11-FP01: the two native-object axes get the same protection as the rest, because
    # they are refusable in exactly the same way -- `pg_trigger` and `sys.triggers` are
    # ordinary relations a login can be denied. The negative control that proved this
    # defect real for grants (`tests/test_facet_refusal.py`) proves it for any axis whose
    # rows a FULL run reconciles, and these two now are such an axis.
    #
    # Registered ahead of the connector that will attribute a read to them: no adapter
    # wraps its trigger query in `read_facet` yet, and `DISCOVERY_FACETS` -- in
    # `connectors.discovery`, another session's this cycle -- has no entry for either
    # name, so `FacetReadScope.record` would reject one today. That makes these two rows
    # unreachable rather than wrong, and they are here so the protection lands with the
    # one-line vocabulary entry instead of trailing a release behind it. Until then a
    # refused trigger read propagates and fails the run, which reconciles nothing.
    FACET_TRIGGERS: (("trigger_ids", MetadataTrigger),),
    FACET_SEQUENCES: (("sequence_ids", MetadataSequence),),
}
_SNAPSHOT_AXES = frozenset({"constraint_ids", "index_ids", "partition_ids"})


async def refused_facet_existing(
    session: AsyncSession, datasource: DataSource, facets: Iterable[str]
) -> tuple[SnapshotScope, EnvelopeScope]:
    """R11-FP02: existing objects of a facet whose read this run was refused.

    The same rule, for the same reason, as `out_of_scope_existing` above: a FULL run
    retires every object it did not see, and an object it was not allowed to *look at* is
    not missing. `discovery_selection`'s hazard note draws that line for a narrowed
    selection -- "narrowing a selection stops maintaining an object; it never retires one"
    -- and a refusal lands on exactly the same side of it. Without this, the first run
    after a grant is dropped would read the source's silence as deletion and tombstone
    every grant, view definition or routine an earlier, better-privileged run captured:
    the refusal would have cost far more than the facet it refused.

    So a refused facet costs exactly its own freshness. Nothing is retired, nothing is
    re-read, and the receipt says PERMISSION_DENIED for that facet so a reader is never
    left to infer the source has none of them.

    INV-5: every read below restates both `organization_id` and `datasource_id`.
    """
    snapshot, envelope = SnapshotScope(), EnvelopeScope()
    for facet in sorted(set(facets)):
        for axis, model in _FACET_AXES.get(facet, ()):
            ids = set(
                await session.scalars(
                    select(model.id).where(
                        model.organization_id == datasource.organization_id,
                        model.datasource_id == datasource.id,
                    )
                )
            )
            target = snapshot if axis in _SNAPSHOT_AXES else envelope
            getattr(target, axis).update(ids)
    return snapshot, envelope


async def detect_rename_candidates(
    session: AsyncSession,
    *,
    run: AnalysisRun,
    datasource: DataSource,
    created_table_ids: set[UUID],
    deprecated_table_ids: set[UUID],
) -> list[RenameCandidate]:
    """CT-4: propose that a table tombstoned in this run is really a table just
    created in this run, renamed.

    Deliberately narrow, per module 04 SS6 and ADR-0017 SS8 ("discovery cannot
    scan everything, all the time"):

    * Same run only -- this is a same-scan tombstone-plus-create pairing, never
      a retroactive sweep across history. `created_table_ids` /
      `deprecated_table_ids` are exactly what THIS call to
      `persist_discovery_snapshot` just created and tombstoned.
    * Same schema only -- `RenameCandidate.schema_id` is a single column, not
      old/new, so a rename that also moves the object to a different schema is
      out of scope for this heuristic (a steward can still relink by hand via
      `aida.identity_merge.merge_table_identity`, called directly).
    * Bounded -- at most `settings.rename_candidate_scan_max_tables` tables on
      each side are considered, chosen deterministically (sorted by id) so a
      retry considers the same subset rather than a random one.

    `aida.identity_resolution.score_table_rename` is the sole arbiter of "strong
    structural match"; this function only bounds candidates, dedupes against
    already-proposed pairs, and persists what the heuristic proposes. Nothing
    here merges identity -- that only happens when a steward approves the
    candidate through the review endpoint, via `aida.identity_merge`.
    """
    if not created_table_ids or not deprecated_table_ids:
        return []
    settings = get_settings()
    scan_cap = settings.rename_candidate_scan_max_tables
    created_ids = sorted(created_table_ids, key=str)[:scan_cap]
    deprecated_ids = sorted(deprecated_table_ids, key=str)[:scan_cap]

    tables = {
        table.id: table
        for table in (
            await session.scalars(
                select(MetadataTable).where(
                    MetadataTable.id.in_(set(created_ids) | set(deprecated_ids))
                )
            )
        ).all()
    }
    deprecated_by_schema: dict[UUID, list[MetadataTable]] = {}
    for table_id in deprecated_ids:
        table = tables.get(table_id)
        if table is not None:
            deprecated_by_schema.setdefault(table.schema_id, []).append(table)
    if not deprecated_by_schema:
        return []

    existing_pairs = {
        (old_id, new_id)
        for old_id, new_id in (
            await session.execute(
                select(RenameCandidate.old_table_id, RenameCandidate.new_table_id)
            )
        ).all()
    }

    all_table_ids = set(tables)
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    if all_table_ids:
        for column in (
            await session.scalars(
                select(MetadataColumn)
                .where(MetadataColumn.table_id.in_(all_table_ids))
                .order_by(MetadataColumn.table_id, MetadataColumn.ordinal_position)
            )
        ).all():
            columns_by_table.setdefault(column.table_id, []).append(column)

    created: list[RenameCandidate] = []
    for new_table_id in created_ids:
        new_table = tables.get(new_table_id)
        if new_table is None:
            continue
        for old_table in deprecated_by_schema.get(new_table.schema_id, []):
            if (old_table.id, new_table.id) in existing_pairs:
                continue
            match: IdentityMatch | None = score_table_rename(
                old_table_name=old_table.name,
                old_columns=columns_by_table.get(old_table.id, []),
                new_table_name=new_table.name,
                new_columns=columns_by_table.get(new_table.id, []),
                min_confidence=settings.rename_candidate_min_confidence,
            )
            if match is None:
                continue
            candidate = RenameCandidate(
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                analysis_run_id=run.id,
                schema_id=new_table.schema_id,
                old_table_id=old_table.id,
                new_table_id=new_table.id,
                detection_rule=match.detection_rule,
                confidence=match.confidence,
                evidence=match.evidence,
                created_by="metadata-worker",
            )
            session.add(candidate)
            created.append(candidate)
            existing_pairs.add((old_table.id, new_table.id))
    if created:
        await session.flush()
    return created


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
    counts = {
        "catalogs": 0,
        "schemas": 0,
        "tables": 0,
        "columns": 0,
        "constraints": 0,
        "indexes": 0,
        "partitions": 0,
    }
    tracker = ChangeTracker()
    table_map: dict[tuple[str, str, str], MetadataTable] = {}
    snapshot_scope = scope or SnapshotScope()
    # ING-4 / P0-01: track table ids that go from absent-in-scope to
    # present-in-scope during THIS call, so the auto-enqueue emitter below
    # only fires once per genuinely-new table even when a chunked caller
    # (`batch_ingestion._process_chunk`, or the loop in `discover_datasource`)
    # threads the same accumulator across many calls.
    _pre_call_created_table_ids: set[UUID] = set(snapshot_scope.created_table_ids)
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
                    session,
                    datasource,
                    schema,
                    discovered_table,
                    tracker,
                    created_table_ids=snapshot_scope.created_table_ids,
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

                # Indexes and partitions have no cross-table references (unlike
                # constraints' foreign keys), so they can be persisted in this same
                # per-table pass rather than needing the second, table_map-resolving
                # pass below.
                for discovered_index in discovered_table.indexes:
                    index = await _get_or_create_index(
                        session, datasource, table, discovered_index, tracker
                    )
                    await session.flush()
                    snapshot_scope.index_ids.add(index.id)
                    counts["indexes"] += 1
                for discovered_partition in discovered_table.partitions:
                    partition = await _get_or_create_partition(
                        session, datasource, table, discovered_partition, tracker
                    )
                    await session.flush()
                    snapshot_scope.partition_ids.add(partition.id)
                    counts["partitions"] += 1

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

    record_change_signals(
        session,
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        analysis_run_id=run.id,
        signals=[
            *tracker.signals,
            *(
                ChangeSignal("TABLE", table_id, SIGNAL_STRUCTURE_CHANGED, change_class)
                for table_id, change_class in sorted(
                    tracker.structure_changes.items(), key=lambda item: str(item[0])
                )
            ),
        ],
    )
    if deprecate_missing:
        deprecation_result = await deprecate_missing_snapshot(
            session, datasource, snapshot_scope, analysis_run_id=run.id
        )
        tracker.deprecated = deprecation_result.total
        # CT-4: same-run tombstone-plus-create pairing. Both sides are only known
        # for certain once deprecation for *this* run has actually happened --
        # `snapshot_scope.created_table_ids` accumulates as tables are persisted
        # above, `deprecation_result.deprecated_table_ids` is exactly what this
        # call just tombstoned.
        await detect_rename_candidates(
            session,
            run=run,
            datasource=datasource,
            created_table_ids=snapshot_scope.created_table_ids,
            deprecated_table_ids=deprecation_result.deprecated_table_ids,
        )

    run.discovered_catalogs = counts["catalogs"]
    run.discovered_schemas = counts["schemas"]
    run.discovered_tables = counts["tables"]
    run.discovered_columns = counts["columns"]
    run.discovered_constraints = counts["constraints"]
    run.discovered_indexes = counts["indexes"]
    run.discovered_partitions = counts["partitions"]
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
    # ING-4 / P0-01: emit `catalog.table.newly_created.v1` for every table
    # this call *actually* created (see `_pre_call_created_table_ids` at the
    # top of this function for why a diff, not the raw accumulator). Gated
    # on the `auto_enqueue_on_ingest` setting so an operator can turn the
    # follow-on drafters off without touching the ingest path itself.
    await _emit_newly_created_table_events(
        session,
        run=run,
        datasource=datasource,
        newly_created_table_ids=snapshot_scope.created_table_ids
        - _pre_call_created_table_ids,
    )
    return {
        **counts,
        "created_objects": tracker.created,
        "changed_objects": tracker.changed,
        "deprecated_objects": tracker.deprecated,
    }


# ING-4 / P0-01: constants and helper for auto-enqueue-on-ingest.
# The event type is the single string documented in
# Docs/30-contracts/04-event-catalog.md (Ingestion section) and consumed by
# `handle_newly_created_table` in `src/aida/newly_created_table_drafter.py`.
NEWLY_CREATED_TABLE_EVENT_TYPE = "catalog.table.newly_created.v1"


async def _emit_newly_created_table_events(
    session: AsyncSession,
    *,
    run: AnalysisRun,
    datasource: DataSource,
    newly_created_table_ids: set[UUID],
) -> None:
    """Emit one `catalog.table.newly_created.v1` outbox event per newly
    created table, so the drafter projector can auto-enqueue an asset
    description (and, once an AnalysisRun completes for the datasource,
    a semantic-inference proposal) without a steward manually POSTing each
    drafter endpoint (P0-01, audit finding).

    No-op when `auto_enqueue_on_ingest` is False, or when no tables were
    newly created in this call. Also writes a batch-level audit row so the
    fact that the follow-on drafters were auto-enqueued is attributable.

    Called from the very end of `persist_discovery_snapshot` on every path
    that persists new tables (chunked pull discovery, single-shot pull
    discovery, push-based batch ingestion).
    """
    settings = get_settings()
    if not settings.auto_enqueue_on_ingest:
        return
    if not newly_created_table_ids:
        return
    worker_context = SecurityContext(
        principal_id=_METADATA_WORKER_PRINCIPAL,
        principal_type="WORKER",
        organization_id=run.organization_id,
        roles=frozenset({"MetadataWorker"}),
    )
    for table_id in sorted(newly_created_table_ids, key=str):
        record_outbox(
            session,
            organization_id=run.organization_id,
            aggregate_type="metadata_table",
            aggregate_id=str(table_id),
            event_type=NEWLY_CREATED_TABLE_EVENT_TYPE,
            payload={
                "organization_id": str(run.organization_id),
                "datasource_id": str(datasource.id),
                "table_id": str(table_id),
                "analysis_run_id": str(run.id),
            },
        )
    record_audit(
        session,
        worker_context,
        action="AUTO_ENQUEUE_DRAFTS_ON_INGEST",
        resource_type="TABLE",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=str(run.id),
        details={
            "datasource_id": str(datasource.id),
            "analysis_run_id": str(run.id),
            "newly_created_table_count": len(newly_created_table_ids),
        },
    )


class SourceAccountCanWrite(RuntimeError):
    """R11-MP22: the source account can write and the policy is REFUSE."""


async def check_source_write_access(
    connector: Connector,
    *,
    datasource: DataSource,
    run: AnalysisRun,
    settings: Settings,
) -> WritePrivilegeProbe:
    """Probe whether the account discovery connects as can write, and act on it.

    Every write the platform could make is refused by the query gateway's parse;
    a read-only account is the second layer, and on every engine but PostgreSQL
    (which also runs reads in a read-only transaction) it is the only one. So an
    account that can write is recorded -- an audit row an operator can find, and
    a warning -- and, where `source_write_access_policy` is REFUSE, discovery
    stops. A probe that itself fails is logged and never stops discovery.
    """
    try:
        probe = await probe_write_privileges(connector)
    except Exception as exc:  # noqa: BLE001 -- a failed probe is not a finding
        logger.warning(
            "source_write_probe_failed",
            datasource_id=str(datasource.id),
            error_type=type(exc).__name__,
        )
        return NOT_PROBED
    if not probe.can_write:
        return probe
    logger.warning(
        "source_account_can_write",
        datasource_id=str(datasource.id),
        connector_type=datasource.connector_type,
        found=probe.detail,
        policy=settings.source_write_access_policy,
    )
    async with session_factory() as session:
        record_audit(
            session,
            SecurityContext(
                principal_id="metadata-worker",
                principal_type="WORKER",
                organization_id=datasource.organization_id,
                roles=frozenset({"MetadataWorker"}),
            ),
            action="datasource.source_account_can_write",
            resource_type="datasource",
            resource_id=str(datasource.id),
            outcome="SUCCESS",
            correlation_id=str(run.id),
            details={
                "connector_type": datasource.connector_type,
                "found": probe.detail,
                "policy": settings.source_write_access_policy,
            },
        )
        await session.commit()
    if settings.source_write_access_policy == "REFUSE":
        raise SourceAccountCanWrite(
            f"the source account can write ({probe.detail}); "
            "source_write_access_policy is REFUSE"
        )
    return probe


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


async def _count_invisible(
    connector: Connector, receipt_outcome: dict[str, tuple[CapabilityState, str]]
) -> dict[str, int] | None:
    """R11-FP02: ask the source how much of itself this login may not see, or give up quietly.

    A connector that cannot ask answers `None`, and so does one whose attempt fails: the run
    goes on reading what it is allowed to, and the receipt says UNKNOWN rather than claiming
    nothing is hidden. The failure is logged, because "we could not ask" is worth knowing.

    Review 2026-09-16 §5: a failure is no longer one undifferentiated `None`. If the source
    *refused* the read, that is recorded as `PERMISSION_DENIED` on the `object_visibility`
    facet -- a different fact from "this adapter has no unfiltered catalog to ask", and one
    a source administrator can act on by granting access. `receipt_outcome` collects it here
    because the receipt is built from this call's own result.

    The refusal is judged by SQLSTATE (`capability_states.is_permission_refusal`), never by
    reading the driver's message: a message can quote a value (INV-6), no two drivers spell
    one the same way, and a guess about the source's intent is worse than an honest
    "we did not get it". Anything else is `UNAVAILABLE`, which is the under-claiming
    direction INV-9 requires.

    The judgement itself is `connectors.discovery.classify_read_failure`, shared with
    `read_facet`, so the visibility question and every facet read are classified by one
    piece of code -- two copies of this rule would be two places for a driver message to
    start leaking into an audit trail.
    """
    try:
        return await connector.count_invisible_objects()
    except Exception as exc:  # noqa: BLE001 -- asking is best-effort; a run must not fail over it
        logger.warning("discovery_invisible_count_failed", exc_info=True)
        receipt_outcome[FACET_OBJECT_VISIBILITY] = classify_read_failure(exc)
        return None


def _drain_facet_reads(
    scope: FacetReadScope, receipt: DiscoveryReceipt, refused: set[str]
) -> None:
    """Move what the connector recorded about its own facet reads onto the receipt.

    Called inside every committed batch, so the receipt a batch writes names the facets
    that batch could not read, and once more when the stream ends. `refused` collects the
    facets the source refused, which the FULL reconciliation needs: their existing objects
    are counted as seen rather than retired (`refused_facet_existing`).

    Nothing here inspects an exception or a message -- the connector already classified
    its own failure through `connectors.discovery.classify_read_failure`, and what arrives
    is a state and a reason code from the two closed vocabularies (INV-6).
    """
    for facet, (state, reason) in scope.drain().items():
        receipt.record_facet_outcome(facet, state=state, reason=reason)
        if state is CapabilityState.PERMISSION_DENIED:
            refused.add(facet)


async def _interrupt_receipt(run_uuid: UUID, receipt: DiscoveryReceipt) -> None:
    """R11-FP02: a cancelled run keeps the batches its receipt already counted, marked as
    not finished -- never left looking like a stream still running."""
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_uuid)
        if run is not None:
            run.discovery_receipt = receipt.as_json(STREAM_INTERRUPTED)
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
        run_mode = run.mode
        await session.commit()

    # R11-FP02: created once the connector has answered, so its capability flags are known;
    # declared here so a failure after that point can still mark it INTERRUPTED.
    receipt: DiscoveryReceipt | None = None
    # R11-FP02: declared out here for the same reason. A refusal that ends the run is
    # recorded by the connector's own read on its way out, and the failure path below --
    # which runs after the scope's block has exited -- writes it onto the INTERRUPTED
    # receipt, so even a run a refusal killed says which read it was refused.
    facet_reads = FacetReadScope()
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
        # R11-MP22: a source account that can write is recorded, or refused.
        await check_source_write_access(
            connector, datasource=datasource, run=run, settings=get_settings()
        )
        activity.heartbeat({"stage": "discovering"})
        await heartbeat_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_DISCOVER_DATASOURCE,
            table_id=None,
            detail={"stage": "discovering"},
        )

        # CN-3/PR-5. `connector.discover_streaming()` replaces a single
        # `connector.discover()` call that, at 100K-table scale, ran ~14
        # sequential unbounded full-source-scan queries into one in-memory
        # tree before this activity persisted anything -- a source that took
        # longer than the 20-minute `start_to_close_timeout`
        # (`workflows/discovery.py`) was retried from scratch with zero rows
        # ever committed, on every attempt. `discover_streaming` yields one
        # bounded `DiscoveredCatalog` batch at a time (a single batch for
        # every connector but Postgres, which is unaffected -- see
        # `Connector.discover_streaming`'s default); each batch is persisted
        # and committed as it arrives, so a mid-run cancellation or a hard
        # activity timeout now leaves whatever batches already landed
        # genuinely committed, instead of losing the entire run.
        #
        # The correctness hazard this loop exists to avoid: `snapshot_scope`
        # and `envelope_scope` accumulate object identities across *every*
        # batch (exactly `workflows.activities.SnapshotScope` /
        # `aida.ingestion.EnvelopeScope`, the same accumulator
        # `batch_ingestion.py`'s chunked push-ingestion path already uses for
        # the identical reason -- INV-11). Every per-batch call below passes
        # `deprecate_missing=False`: a FULL-mode run reconciling "missing" after
        # only the first 500 of 100,000 tables had arrived would tombstone
        # every table outside that first batch. The single deprecate-missing
        # pass runs once, after the stream is fully exhausted, against the
        # complete accumulated scope -- see the `finalize` section below.
        settings = get_settings()
        snapshot_scope = SnapshotScope()
        envelope_scope = EnvelopeScope()
        # R11-FP01: the selection as it stood when the run started; an edit made while
        # the run is in flight applies to the next run, not halfway through this one.
        selection = selection_for(datasource)
        excluded_by_kind: dict[str, int] = {}
        # R11-FP01: where the connector can take it, the schema scope goes into the source's own
        # metadata queries, so an excluded schema is never read. The selection is still applied
        # to every batch below.
        pushed_down = connector.scope_discovery(
            include_schemas=list(selection.include_schemas),
            exclude_schemas=list(selection.exclude_schemas),
            # R11-FP01 remainder: the object scope too, which Oracle, Snowflake, BigQuery
            # and Databricks push into the reads whose rows belong to one object.
            object_kinds=list(selection.object_kinds),
            include_objects=list(selection.include_objects),
            exclude_objects=list(selection.exclude_objects),
        )
        # Review 2026-09-16 §5: a facet read that did not complete is recorded on the
        # receipt rather than failing the run, and a refusal is told apart from a failure.
        visibility_outcome: dict[str, tuple[CapabilityState, str]] = {}
        receipt = DiscoveryReceipt(
            mode=run_mode,
            selection_fingerprint=selection.fingerprint(),
            capabilities=asdict(connector.capabilities),
            selection_pushed_down=pushed_down,
            # R11-FP02: what this run's login may not see, asked once, before the stream. A
            # source that cannot answer leaves it None, which the receipt reports as UNKNOWN
            # rather than as nothing hidden; a failure to ask is the same answer, and never
            # fails a run that can still read what it is allowed to.
            invisible=await _count_invisible(connector, visibility_outcome),
        )
        for facet, (facet_state, facet_reason) in visibility_outcome.items():
            receipt.record_facet_outcome(facet, state=facet_state, reason=facet_reason)
        # R11-FP01: which of the two native-object axes this connector actually reads, and
        # therefore which of them this FULL run may retire from. Derived from the
        # connector's own capability flags rather than from whether the stream happened to
        # contain any, because "read the axis and found none" is the only state that
        # licenses retirement and an empty tuple cannot tell that apart from "never
        # looked" (INV-9). Snowflake reads sequences and has no trigger object at all, so
        # it reconciles one axis and leaves the other alone; Databricks and BigQuery
        # reconcile neither. `ingestion.NATIVE_OBJECT_AXES` has the full argument.
        native_axes_read = frozenset(
            facet
            for facet in (FACET_TRIGGERS, FACET_SEQUENCES)
            if getattr(connector.capabilities, facet, False)
        )
        created_objects_total = 0
        changed_objects_total = 0
        batch_index = 0
        # R11-FP02: the facets this run's login was refused, collected from the connector's
        # own reads (`connectors.discovery.read_facet`) rather than guessed at from a
        # failure. Their existing objects are counted as seen by the reconciliation below,
        # for the reason `refused_facet_existing` gives: a read that was refused is a read
        # that did not happen, and a FULL run may not retire what it never looked at.
        refused_facets: set[str] = set()
        with facet_read_scope(facet_reads):
            async for catalogs in connector.discover_streaming(
                batch_size=settings.discovery_stream_batch_size
            ):
                if activity.is_cancelled():
                    raise asyncio.CancelledError
                batch_index += 1
                outcome = apply_selection(catalogs, selection)
                catalogs = outcome.catalogs
                for kind, count in outcome.excluded.items():
                    excluded_by_kind[kind] = excluded_by_kind.get(kind, 0) + count
                receipt.observe_batch(catalogs, outcome.excluded)
                async with session_factory() as session:
                    run = await session.get(AnalysisRun, run_uuid)
                    datasource = (
                        await session.get(DataSource, run.datasource_id) if run else None
                    )
                    if run is None or datasource is None:
                        raise ValueError(
                            "analysis run or datasource disappeared during discovery"
                        )
                    counts = await persist_discovery_snapshot(
                        session,
                        run,
                        datasource,
                        catalogs,
                        deprecate_missing=False,
                        scope=snapshot_scope,
                    )
                    # Envelope 1.1 (gap/02 N1). The pull path collects views, routines,
                    # comments and grants in `connector.discover_streaming()`; without
                    # this call it would drop them at persistence while both push paths
                    # keep them. No version gate is needed: a pull snapshot comes from a
                    # connector whose capability flags already say which axes it
                    # collected, so a connector that collects an axis is authoritative
                    # for it. `deprecate_missing=False` here for the same INV-11 reason
                    # as the 1.0 pass above -- reconciled once in `finalize`, below.
                    #
                    # R11-FP01: the same call now also writes the triggers and sequences
                    # the connector read. It needs no `native_axes_read` here for the
                    # reason the docstring gives -- that flag governs retirement only,
                    # and nothing retires on this pass.
                    extension_counts = await persist_envelope_extensions(
                        session,
                        datasource,
                        catalogs,
                        scope=envelope_scope,
                        deprecate_missing=False,
                        analysis_run_id=run.id,
                    )
                    created_objects_total += (
                        counts["created_objects"] + extension_counts["created_objects"]
                    )
                    changed_objects_total += (
                        counts["changed_objects"] + extension_counts["changed_objects"]
                    )
                    # R11-FP02: drained inside this batch's own commit, so the facets the
                    # source refused while producing this batch are named by the receipt
                    # this batch writes -- a run killed after batch three still says which
                    # reads it was refused, rather than losing them with the process.
                    _drain_facet_reads(facet_reads, receipt, refused_facets)
                    # Written with the batch it describes, so the receipt never claims a batch
                    # the catalog does not hold.
                    run.discovery_receipt = receipt.as_json(STREAM_IN_PROGRESS)
                    await session.commit()
                batch_progress = {
                    "stage": "discovering",
                    "batch": batch_index,
                    **snapshot_scope.object_counts(),
                }
                activity.heartbeat(batch_progress)
                await heartbeat_task(
                    analysis_run_id=run_uuid,
                    task_type=TASK_TYPE_DISCOVER_DATASOURCE,
                    table_id=None,
                    detail=batch_progress,
                )
            # A connector may refuse a facet on its last batch, or read a
            # run-wide facet (a routine inventory, a schema comment) once before
            # its first: either way the scope is emptied one final time here,
            # before the receipt is written COMPLETE.
            _drain_facet_reads(facet_reads, receipt, refused_facets)

        # finalize: the one and only deprecate-missing pass for this run, now
        # that `snapshot_scope`/`envelope_scope` hold every identity observed
        # across the complete stream -- see `_complete_batch` in
        # `batch_ingestion.py` for the same pattern on the push-ingestion side.
        async with session_factory() as session:
            run = await session.get(AnalysisRun, run_uuid)
            datasource = await session.get(DataSource, run.datasource_id) if run else None
            if run is None or datasource is None:
                raise ValueError("analysis run or datasource disappeared during discovery")
            deprecated_objects_total = 0
            retained_out_of_scope = 0
            if run.mode == "FULL":
                # R11-FP01: an existing object the selection does not cover was never
                # looked for, so it is reconciled as seen -- never retired as missing.
                reconcile_snapshot, reconcile_envelope = snapshot_scope, envelope_scope
                if selection.restricted:
                    kept_snapshot, kept_envelope = await out_of_scope_existing(
                        session, datasource, selection
                    )
                    # R11-FP01: the two native kinds are counted here beside the tables
                    # and routines, because they are objects an operator can exclude and
                    # this number is what tells them a narrowed scan stopped maintaining
                    # rather than deleted. Left out, a scan narrowed to exclude an audit
                    # schema full of triggers would report `retained_out_of_scope: 0`
                    # while retaining dozens.
                    retained_out_of_scope = (
                        len(kept_snapshot.table_ids)
                        + len(kept_envelope.routine_ids)
                        + len(kept_envelope.trigger_ids)
                        + len(kept_envelope.sequence_ids)
                    )
                    reconcile_snapshot = union_snapshot_scopes(snapshot_scope, kept_snapshot)
                    reconcile_envelope = union_envelope_scopes(envelope_scope, kept_envelope)
                if refused_facets:
                    # R11-FP02, and the same rule one line up: an object whose facet the
                    # source refused was not looked at, so it is not missing. Without this
                    # a dropped grant would make the next FULL run tombstone every grant
                    # the last one captured -- the refusal costing the estate rather than
                    # the facet. The receipt already says PERMISSION_DENIED for the facet,
                    # so nothing here is silent.
                    kept_snapshot, kept_envelope = await refused_facet_existing(
                        session, datasource, refused_facets
                    )
                    reconcile_snapshot = union_snapshot_scopes(reconcile_snapshot, kept_snapshot)
                    reconcile_envelope = union_envelope_scopes(reconcile_envelope, kept_envelope)
                deprecation_result = await deprecate_missing_snapshot(
                    session, datasource, reconcile_snapshot, analysis_run_id=run.id
                )
                deprecated_objects_total += deprecation_result.total
                deprecated_objects_total += await deprecate_missing_envelope_extensions(
                    session,
                    datasource,
                    reconcile_envelope,
                    analysis_run_id=run.id,
                    # R11-FP01: only the axes this connector reads may retire from. A
                    # source re-pointed at an adapter that does not collect triggers
                    # would otherwise tombstone every trigger the previous adapter
                    # found, which is the same "silence read as deletion" mistake the
                    # two `*_existing` passes above exist to prevent.
                    native_axes_read=native_axes_read,
                )
                # CT-4: same-run tombstone-plus-create pairing, exactly as the
                # unchunked path used before this change -- `snapshot_scope` here
                # is the same accumulator threaded through every batch above, so
                # `created_table_ids` is every table this run actually created and
                # `deprecated_table_ids` is exactly what the call just above
                # tombstoned.
                await detect_rename_candidates(
                    session,
                    run=run,
                    datasource=datasource,
                    created_table_ids=snapshot_scope.created_table_ids,
                    deprecated_table_ids=deprecation_result.deprecated_table_ids,
                )
                receipt.record_reconciliation(
                    deprecated=deprecated_objects_total,
                    retained_out_of_scope=retained_out_of_scope,
                )
            # R11-FP15: what kinds of change this run recorded, by count.
            change_rows = await session.execute(
                select(MetadataChangeSignal.signal_type, func.count())
                .where(MetadataChangeSignal.analysis_run_id == run.id)
                .group_by(MetadataChangeSignal.signal_type)
            )
            receipt.record_changes(
                {signal_type: int(count) for signal_type, count in change_rows.all()}
            )
            run.discovery_receipt = receipt.as_json(STREAM_COMPLETE)
            object_counts = {**snapshot_scope.object_counts(), **envelope_scope.object_counts()}
            run.discovered_catalogs = object_counts["catalogs"]
            run.discovered_schemas = object_counts["schemas"]
            run.discovered_tables = object_counts["tables"]
            run.discovered_columns = object_counts["columns"]
            run.discovered_constraints = object_counts["constraints"]
            run.discovered_indexes = object_counts["indexes"]
            run.discovered_partitions = object_counts["partitions"]
            run.created_objects = created_objects_total
            run.changed_objects = changed_objects_total
            run.deprecated_objects = deprecated_objects_total
            run.discovery_selection_fingerprint = selection.fingerprint()
            run.excluded_objects = sum(excluded_by_kind.values())
            run.status = "PROFILING"
            datasource.status = "ACTIVE"
            final_counts = {
                **object_counts,
                "created_objects": created_objects_total,
                "changed_objects": changed_objects_total,
                "deprecated_objects": deprecated_objects_total,
            }
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
                details={
                    **final_counts,
                    "selection_fingerprint": run.discovery_selection_fingerprint,
                    "excluded_by_kind": excluded_by_kind,
                    "retained_out_of_scope": retained_out_of_scope,
                },
            )
            record_outbox(
                session,
                organization_id=run.organization_id,
                aggregate_type="analysis_run",
                aggregate_id=str(run.id),
                event_type="metadata.discovery.snapshot.v1",
                payload={
                    "run_id": str(run.id),
                    "datasource_id": str(datasource.id),
                    **final_counts,
                },
            )
            await session.commit()
        logger.info("datasource_discovery_completed", run_id=run_id, **final_counts)
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_DISCOVER_DATASOURCE,
            table_id=None,
            outcome="SUCCESS",
        )
        return {
            "run_id": run_id,
            "status": "COMPLETED",
            **final_counts,
            "excluded_objects": sum(excluded_by_kind.values()),
        }
    except asyncio.CancelledError:
        await _mark_run_cancelled(run_uuid)
        if receipt is not None:
            _drain_facet_reads(facet_reads, receipt, set())
            await _interrupt_receipt(run_uuid, receipt)
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
                if receipt is not None:
                    # R11-FP02: a run a refusal ended says so. `read_facet` records the
                    # outcome before it lets the exception through, so the facet is named
                    # here even though the run could not go on -- a refused *inventory*
                    # read is the case that matters, since completing a run that saw no
                    # objects would retire the estate (`refused_facet_existing`).
                    _drain_facet_reads(facet_reads, receipt, set())
                    run.discovery_receipt = receipt.as_json(STREAM_INTERRUPTED)
                run.error_class = type(exc).__name__
                # INV-6 / ADR-0014: never persist str(exc) here -- it can carry
                # source-connector-returned row data, SQL fragments, or
                # credentials-adjacent text. See query_gateway.py's matching
                # except-Exception block for the pattern this mirrors.
                run.error_message = "datasource discovery failed"
                await session.commit()
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_DISCOVER_DATASOURCE,
            table_id=None,
            outcome="ERROR",
            error_class=type(exc).__name__,
            error_message="datasource discovery failed",
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
                    observation_scope=persistable_observation_scope(snapshot.observation_scope),
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
                            **derived_column_facets(column_snapshot),
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
                # INV-6 / ADR-0014: never persist str(exc) here -- see the
                # matching comment in discover_datasource above.
                run.error_message = "datasource profiling failed"
                await session.commit()
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_PROFILE_DATASOURCE,
            table_id=None,
            outcome="ERROR",
            error_class=type(exc).__name__,
            error_message="datasource profiling failed",
        )
        raise


@activity.defn(name="plan_profile_tasks")
async def plan_profile_tasks(payload: dict[str, Any]) -> dict[str, Any]:
    """PR-5: keyset-paginated plan, one bounded page per call.

    Replaces the old one-shot plan (every table id for the whole run in a
    single activity result, `Docs/20-modules/05-profiling-and-classification.md`
    §13's "fatal at scale" gap) with a page bounded by
    `settings.profile_plan_page_size`, so a 1M-table run's per-call payload
    stays flat regardless of run size -- `DatasourceDiscoveryWorkflow` calls
    this repeatedly, threading `cursor`/`tables_planned_total` from its own
    compact `ProfilingProgress` checkpoint (never a table-id list) rather than
    this activity returning the whole run's plan at once.

    `tables_planned_total` (tables already planned across every earlier page
    of this run, including pages from executions before a `continue_as_new`)
    enforces `settings.profile_max_tables_per_run` as an overall-run cap, the
    same cap the old one-shot `.limit(...)` enforced, just spread across many
    calls instead of one.
    """
    run_id = str(payload["run_id"])
    cursor = payload.get("cursor")
    tables_planned_total = int(payload.get("tables_planned_total", 0))
    run_uuid = UUID(run_id)
    settings = get_settings()
    page_size = clamp_page_size(
        settings.profile_plan_page_size, maximum=settings.profile_plan_page_size
    )
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
        common_response = {
            "run_id": run_id,
            "max_concurrency": datasource.max_concurrency,
            "continue_as_new_after_tables": settings.profile_continue_as_new_after_tables,
        }
        remaining_budget = max(0, settings.profile_max_tables_per_run - tables_planned_total)
        if remaining_budget == 0:
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_PLAN_PROFILE_TASKS,
                table_id=None,
                outcome="SUCCESS",
            )
            return {
                **common_response,
                "table_ids": [],
                "next_cursor": cursor,
                "has_more": False,
            }
        effective_page_size = min(page_size, remaining_budget)
        try:
            statement = select(MetadataTable.id).where(
                # INV-5: the tenant boundary is restated explicitly rather than
                # inherited from the datasource FK, so this query is scoped even
                # if a future caller hands it a datasource from another tenant.
                MetadataTable.organization_id == run.organization_id,
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.status == "ACTIVE",
                MetadataTable.object_type == "BASE_TABLE",
            )
            if cursor is not None:
                try:
                    (last_id_raw,) = decode_cursor(cursor, arity=1)
                except InvalidCursor as exc:
                    raise ApplicationError(
                        "invalid profiling plan cursor",
                        type="InvalidCursorError",
                        non_retryable=True,
                    ) from exc
                order_columns: tuple[Any, ...] = (MetadataTable.id,)
                statement = apply_keyset(statement, order_columns, (UUID(last_id_raw),))
            statement = statement.order_by(MetadataTable.id)
            # Fetch one row past the page so "does more remain" is answered by
            # whether the extra row showed up at all, never by `len(page) ==
            # effective_page_size` -- that comparison misreads an exact-fit final
            # page (exactly `effective_page_size` rows left) as "more remains",
            # which would hand the workflow one pointless extra empty page.
            rows = list(await session.scalars(statement.limit(effective_page_size + 1)))
        except Exception as exc:
            # INV-6 / ADR-0014: never persist str(exc) here -- see the
            # matching comment in discover_datasource above.
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_PLAN_PROFILE_TASKS,
                table_id=None,
                outcome="ERROR",
                error_class=type(exc).__name__,
                error_message="profile task planning failed",
            )
            raise
        has_more = len(rows) > effective_page_size
        page_rows = rows[:effective_page_size]
        next_cursor = encode_cursor(str(page_rows[-1])) if page_rows else cursor
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_PLAN_PROFILE_TASKS,
            table_id=None,
            outcome="SUCCESS",
        )
        return {
            **common_response,
            "table_ids": [str(table_id) for table_id in page_rows],
            "next_cursor": next_cursor,
            "has_more": has_more,
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

        # PR-2: policy-approved value capture -- strictly additive to the
        # value-free snapshot above, and gated behind BOTH an APPROVED,
        # unrevoked `ProfilingExceptionPolicy` for the column's classification
        # AND the connector's `value_range_profiling` capability. ADR-0014's
        # default is "never capture values"; this is the one explicit,
        # per-classification exception path, and it fails closed at either
        # gate rather than simulating support.
        value_snapshots_by_name: dict[str, Any] = {}
        policy_by_classification: dict[str, Any] = {}
        if connector.capabilities.value_range_profiling:
            gated_columns = [
                column for column in columns if column.classification in GATED_CLASSIFICATIONS
            ]
            if gated_columns:
                async with session_factory() as policy_session:
                    for classification in {column.classification for column in gated_columns}:
                        policy = await approved_policy_for(
                            policy_session,
                            organization_id=datasource.organization_id,
                            datasource_id=datasource.id,
                            classification=classification,
                        )
                        if policy is not None:
                            policy_by_classification[classification] = policy
                approved_columns = [
                    column
                    for column in gated_columns
                    if column.classification in policy_by_classification
                ]
                if approved_columns:
                    try:
                        value_snapshots = await connector.profile_column_values(
                            schema.name,
                            table.name,
                            tuple(column.name for column in approved_columns),
                            sample_rows=settings.profile_sample_rows,
                            top_n=settings.profile_value_top_n,
                            timeout_seconds=settings.query_timeout_seconds,
                        )
                        value_snapshots_by_name = {
                            value_snapshot.name: value_snapshot
                            for value_snapshot in value_snapshots
                        }
                    except ConnectorValueProfilingUnsupported:
                        # The capability flag was wrong (or changed mid-flight)
                        # -- fail closed and skip value capture rather than
                        # fail the whole table's value-free profiling task.
                        logger.warning(
                            "value_range_profiling_unsupported",
                            table_id=str(table_uuid),
                            connector_type=datasource.connector_type,
                        )

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
                observation_scope=persistable_observation_scope(snapshot.observation_scope),
            )
            session.add(profile)
            await session.flush()
            captured_at = datetime.now(UTC)
            for column_snapshot in snapshot.columns:
                column = columns_by_name[column_snapshot.name]
                column_profile = ColumnProfile(
                    organization_id=datasource.organization_id,
                    table_profile_id=profile.id,
                    column_id=column.id,
                    null_count=column_snapshot.null_count,
                    non_null_count=column_snapshot.non_null_count,
                    approximate_distinct_count=column_snapshot.approximate_distinct_count,
                    min_length=column_snapshot.min_length,
                    max_length=column_snapshot.max_length,
                    **derived_column_facets(column_snapshot),
                )
                session.add(column_profile)
                value_snapshot = value_snapshots_by_name.get(column_snapshot.name)
                if value_snapshot is not None:
                    policy = policy_by_classification[column.classification]
                    await session.flush()
                    session.add(
                        ColumnValueProfileArtifact(
                            organization_id=datasource.organization_id,
                            datasource_id=datasource.id,
                            table_id=table_uuid,
                            column_id=column.id,
                            column_profile_id=column_profile.id,
                            policy_id=policy.id,
                            classification=column.classification,
                            min_value=value_snapshot.min_value,
                            max_value=value_snapshot.max_value,
                            top_values=[
                                {"value": value, "count": count}
                                for value, count in value_snapshot.top_values
                            ],
                            captured_at=captured_at,
                            expires_at=captured_at + timedelta(days=policy.retention_days),
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
                # INV-6 / ADR-0014: never persist str(exc) here -- this
                # activity runs source-connector queries directly, so raw
                # driver exceptions routinely quote row data. See the
                # matching comment in discover_datasource above.
                failed_run.error_message = "table profiling failed"
                await session.commit()
        await finish_task(
            analysis_run_id=run_uuid,
            task_type=TASK_TYPE_PROFILE_TABLE,
            table_id=table_uuid,
            outcome="ERROR",
            error_class=type(exc).__name__,
            error_message="table profiling failed",
        )
        raise


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
            details = {
                "profiled_tables": profiled_tables,
                "profiled_columns": profiled_columns,
                "profile_version": "safe-v1",
                "execution_model": "TABLE_TASK_DAG_V1",
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
            # INV-6 / ADR-0014: never persist str(exc) here -- see the
            # matching comment in discover_datasource above.
            await finish_task(
                analysis_run_id=run_uuid,
                task_type=TASK_TYPE_FINALIZE_PROFILE_TASKS,
                table_id=None,
                outcome="ERROR",
                error_class=type(exc).__name__,
                error_message="profile finalization failed",
            )
            raise
    await finish_task(
        analysis_run_id=run_uuid,
        task_type=TASK_TYPE_FINALIZE_PROFILE_TASKS,
        table_id=None,
        outcome="SUCCESS",
    )
    return {"run_id": str(run_uuid), "status": "COMPLETED", **details}
