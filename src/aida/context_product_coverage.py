"""R11-FP12: what a context product's routines and views stand on, resolved for its readers.

A product scoped by `table_ids` alone can say "these tables", but not "this procedure builds that
one", nor "this view's definition was withheld from us". The two resolvers here feed both doors a
version is read through -- compilation (`context_compiler_api._load_source`) and MCP's resource
read -- so the two cannot describe the same version differently:

* `load_routine_references` resolves the routines a version names in `routine_ids`: identity,
  status, whether its body would be released on request, the state of its lineage, and the
  tables it reads and writes;
* `load_view_coverage` derives, from the version's own `table_ids`, the views and materialized
  views among them and the same facts about their definitions.

**Nothing a product does not cover leaks through.** Read and write table ids are cut to the
product's own `table_ids`, and nothing is counted: a routine that also reads a table outside the
product looks exactly like one that does not. **Value-free.** Flags, lineage state, ids already in
scope, and a digest of the *stored* value-free text -- never a body, a definition, or a digest of
the literal-bearing original (`definition_fingerprint`/`body_fingerprint` are that, and stay
internal). Callers run these only after the product's own read decision, and they use only
`session.execute(...).all()`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context_compiler import ResolvedRoutineReference, ResolvedViewCoverage
from aida.discovery_selection import table_kind
from aida.envelope_models import AVAILABLE, MetadataRoutine, MetadataViewDefinition
from aida.ingest_screening import is_eligible_for_model_context
from aida.models import MetadataCatalog, MetadataSchema, MetadataTable, ViewLineageEdge
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES

#: Some ACTIVE edge was parsed from the definition (UNPARSED markers do not count).
LINEAGE_ACTIVE: Final = "ACTIVE"
#: Only edges nobody has decided yet: they steer nothing, and are reported so.
LINEAGE_PROPOSED: Final = "PROPOSED"
LINEAGE_NONE: Final = "NONE"
_UNPARSED: Final = "UNPARSED"


def _uuids(values: Sequence[Any]) -> list[UUID]:
    parsed: list[UUID] = []
    for value in values:
        try:
            parsed.append(UUID(str(value)))
        except ValueError:
            continue
    return parsed


def _releasable(availability: str, redaction_status: str, screening_status: str) -> bool:
    """The gate MCP `get_transformation_detail` applies before releasing code text."""
    return (
        availability == AVAILABLE
        and redaction_status in VALUE_FREE_REDACTION_STATUSES
        and is_eligible_for_model_context(screening_status)
    )


def _digest(stored_text: str | None, redaction_status: str) -> str | None:
    if stored_text is None or redaction_status not in VALUE_FREE_REDACTION_STATUSES:
        return None
    return hashlib.sha256(stored_text.encode("utf-8")).hexdigest()


def _lineage(object_id: UUID, active: set[UUID], proposed: set[UUID]) -> str:
    if object_id in active:
        return LINEAGE_ACTIVE
    if object_id in proposed:
        return LINEAGE_PROPOSED
    return LINEAGE_NONE


async def load_routine_references(
    session: AsyncSession,
    organization_id: UUID,
    routine_ids: Sequence[Any],
    scope_table_ids: Sequence[Any],
) -> list[ResolvedRoutineReference]:
    """The routines named, in this organization, whatever their status -- the caller compares
    the count with what it asked for and refuses a version that names one no longer there."""
    ids = _uuids(routine_ids)
    if not ids:
        return []
    rows = (
        await session.execute(
            select(MetadataRoutine, MetadataSchema.name, MetadataCatalog.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataRoutine.id.in_(ids),
                MetadataRoutine.organization_id == organization_id,
            )
        )
    ).all()
    if not rows:
        return []
    scope = {str(value) for value in scope_table_ids}
    edge_rows = (
        await session.execute(
            select(
                DeepProcedureLineageEdge.routine_id,
                DeepProcedureLineageEdge.source_table_id,
                DeepProcedureLineageEdge.target_table_id,
                DeepProcedureLineageEdge.is_write,
                DeepProcedureLineageEdge.is_intermediate,
                DeepProcedureLineageEdge.transformation_type,
                DeepProcedureLineageEdge.review_status,
            ).where(
                DeepProcedureLineageEdge.routine_id.in_([routine.id for routine, _, _ in rows]),
                DeepProcedureLineageEdge.organization_id == organization_id,
                DeepProcedureLineageEdge.review_status.in_((LINEAGE_ACTIVE, LINEAGE_PROPOSED)),
            )
        )
    ).all()
    active: set[UUID] = set()
    proposed: set[UUID] = set()
    unparsed: set[UUID] = set()
    reads: dict[UUID, set[str]] = {}
    writes: dict[UUID, set[str]] = {}
    for (
        routine_id,
        source_table_id,
        target_table_id,
        is_write,
        is_intermediate,
        transformation_type,
        review_status,
    ) in edge_rows:
        if review_status == LINEAGE_PROPOSED:
            proposed.add(routine_id)
            continue
        if transformation_type == _UNPARSED:
            unparsed.add(routine_id)
            continue
        active.add(routine_id)
        if is_intermediate:
            continue
        if source_table_id is not None and str(source_table_id) in scope:
            reads.setdefault(routine_id, set()).add(str(source_table_id))
        if is_write and target_table_id is not None and str(target_table_id) in scope:
            writes.setdefault(routine_id, set()).add(str(target_table_id))
    return [
        ResolvedRoutineReference(
            routine_id=str(routine.id),
            qualified_name=f"{catalog_name}.{schema_name}.{routine.name}",
            routine_type=routine.routine_type,
            signature=routine.signature,
            status=routine.status,
            definition_available=_releasable(
                routine.availability, routine.redaction_status, routine.screening_status
            ),
            lineage=_lineage(routine.id, active, proposed),
            fully_parsed=routine.id in active and routine.id not in unparsed,
            reads_table_ids=tuple(sorted(reads.get(routine.id, set()))),
            writes_table_ids=tuple(sorted(writes.get(routine.id, set()))),
            definition_digest=_digest(routine.body_sql_redacted, routine.redaction_status),
        )
        for routine, schema_name, catalog_name in rows
    ]


async def load_view_coverage(
    session: AsyncSession,
    organization_id: UUID,
    table_ids: Sequence[Any],
) -> list[ResolvedViewCoverage]:
    """The views and materialized views among a version's own tables; plain tables yield
    nothing, so a product holding none compiles as it always did."""
    ids = _uuids(table_ids)
    if not ids:
        return []
    rows = (
        await session.execute(
            select(
                MetadataTable.id,
                MetadataTable.object_type,
                MetadataTable.status,
                MetadataViewDefinition,
            )
            .outerjoin(MetadataViewDefinition, MetadataViewDefinition.table_id == MetadataTable.id)
            .where(
                MetadataTable.id.in_(ids),
                MetadataTable.organization_id == organization_id,
            )
        )
    ).all()
    views = [
        (table_id, table_kind(object_type), status, definition)
        for table_id, object_type, status, definition in rows
        if table_kind(object_type) != "TABLE"
    ]
    if not views:
        return []
    edge_rows = (
        await session.execute(
            select(ViewLineageEdge.target_table_id, ViewLineageEdge.review_status).where(
                ViewLineageEdge.target_table_id.in_([table_id for table_id, _, _, _ in views]),
                ViewLineageEdge.organization_id == organization_id,
                ViewLineageEdge.review_status.in_((LINEAGE_ACTIVE, LINEAGE_PROPOSED)),
            )
        )
    ).all()
    active = {target for target, review in edge_rows if review == LINEAGE_ACTIVE}
    proposed = {target for target, review in edge_rows if review == LINEAGE_PROPOSED}
    return [
        ResolvedViewCoverage(
            table_id=str(table_id),
            object_type=kind,
            status=status,
            definition_available=(
                definition is not None
                and definition.status == "ACTIVE"
                and _releasable(
                    definition.availability,
                    definition.redaction_status,
                    definition.screening_status,
                )
            ),
            truncated=bool(definition is not None and definition.truncated),
            lineage=_lineage(table_id, active, proposed),
            definition_digest=(
                _digest(definition.definition_sql_redacted, definition.redaction_status)
                if definition is not None
                else None
            ),
        )
        for table_id, kind, status, definition in views
    ]
