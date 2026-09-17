"""R11-FP05: which objects are behind one footprint gap's count, for the person who closes it.

`aida.footprint_gaps` says how many gaps of each kind a source has and the one route that closes
each. That is the right shape for a fleet view and the wrong shape for doing the work: a steward
told "7 definitions withheld" still has to go and find which seven, and a source administrator
asking for read access has to name the objects. This is that list, per source and per kind,
bounded and read under the same authorization as the count it expands.

**What it adds over the count, and what it does not.** Identity only: an object id, the qualified
name the catalog already shows, and, where the record carries one, the stable code that says why
(`UNAVAILABLE`, a signal type, an unparsed reason). Never a definition, a body, or a value --
those have their own gated routes. `SOURCE_OBJECTS_INVISIBLE` is the honest empty case: those
objects are not in the catalog at all, so there is nothing here to name, and the route says so
rather than returning an empty list that reads like "none". `SOURCE_READS_REFUSED` is the same
case for a sharper reason -- a refused read returned no rows, so the count is of facets and
there is no object behind it at all (R11-FP02).

**Bounded.** At most `MAX_OBJECTS` per request, oldest-first by name so the list is stable
between reads, with `truncated` saying when a source has more than a person can act on at once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.change_signal_models import MetadataChangeSignal
from aida.change_signal_processing import SOURCE_CHANGE_ANOMALY_TYPE
from aida.envelope_models import AVAILABLE, UNAVAILABLE, MetadataRoutine, MetadataViewDefinition
from aida.footprint_gaps import GAP_DEFINITIONS
from aida.ingest_screening import CLEAN
from aida.models import (
    DataQualityIncident,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    ViewLineageEdge,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_call_descent import CALLEE_BODY_WITHHELD, CALLEE_NOT_CAPTURED
from aida.schemas import ApiModel
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES

#: A person acts on a page of objects, not on an estate. More than this and the answer is a
#: selection change or a grant, not a list.
MAX_OBJECTS: Final = 200

#: The gap whose objects Atlas cannot name, because it never saw them (R11-FP02).
UNNAMEABLE: Final = "SOURCE_OBJECTS_INVISIBLE"
UNNAMEABLE_NOTE: Final = (
    "These objects are not in the catalog: the last completed run counted them in the source's "
    "own catalog and was not allowed to read them, so Atlas has no name, no id and nothing else "
    "to show. Granting the scanning principal read access and rescanning is what names them."
)

#: R11-FP02: the refused-read gap has nothing to list either, and for a sharper reason -- a
#: refused read returns no rows at all, so there is not even a count of objects behind it, only
#: the facets. An empty `objects` list with no note here would read as "none", which is the
#: false-clean reading this gap exists to prevent.
REFUSED_READS: Final = "SOURCE_READS_REFUSED"
REFUSED_READS_NOTE: Final = (
    "There are no objects to list: the source refused these reads, so nothing came back to "
    "name. The count is of facets, and the last completed run's receipt "
    "(`analysis_run.discovery_receipt`) names which ones. What Atlas already holds for a "
    "refused facet is kept as it was and never retired over the refusal, so the gap is "
    "staleness, not loss. Granting the scanning principal the read and rescanning closes it."
)

#: Gap kinds whose count can never be expanded into a list of objects, and the reason each
#: one cannot, so an empty list is never served bare.
_UNLISTABLE_NOTES: Final[dict[str, str]] = {
    UNNAMEABLE: UNNAMEABLE_NOTE,
    REFUSED_READS: REFUSED_READS_NOTE,
}


class FootprintGapObjectRead(ApiModel):
    object_type: str
    object_id: UUID
    qualified_name: str
    #: The stable code the record itself carries, where it has one; never free text from a source.
    detail: str | None = None


class FootprintGapDetailRead(ApiModel):
    datasource_id: UUID
    kind: str
    resolution: str
    owner: str
    explanation: str
    objects: list[FootprintGapObjectRead]
    truncated: bool
    #: Set only where a kind has objects it cannot name; see `UNNAMEABLE_NOTE`.
    note: str | None = None


class UnknownGapKind(ValueError):
    """The kind asked for is not one this platform records."""


def _view_objects(organization_id: UUID, datasource_id: UUID, *condition: Any) -> Select[Any]:
    """View definitions of this source matching `condition`, named by their table."""
    return (
        select(
            MetadataTable.id,
            MetadataCatalog.name,
            MetadataSchema.name,
            MetadataTable.name,
        )
        # The definition is the left side: every selected column names its table, so nothing in
        # the select list says which FROM the joins hang off.
        .select_from(MetadataViewDefinition)
        .join(MetadataTable, MetadataTable.id == MetadataViewDefinition.table_id)
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
        .where(
            MetadataViewDefinition.organization_id == organization_id,
            MetadataViewDefinition.datasource_id == datasource_id,
            MetadataViewDefinition.status == "ACTIVE",
            *condition,
        )
    )


def _routine_objects(organization_id: UUID, datasource_id: UUID, *condition: Any) -> Select[Any]:
    """Routines of this source matching `condition`, named by schema and signature."""
    return (
        select(
            MetadataRoutine.id,
            MetadataCatalog.name,
            MetadataSchema.name,
            MetadataRoutine.name,
        )
        .select_from(MetadataRoutine)
        .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
        .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
        .where(
            MetadataRoutine.organization_id == organization_id,
            MetadataRoutine.datasource_id == datasource_id,
            MetadataRoutine.status == "ACTIVE",
            *condition,
        )
    )


async def _named(
    session: AsyncSession, statement: Select[Any], object_type: str
) -> list[FootprintGapObjectRead]:
    """Identity and name only. These kinds carry no per-object code worth adding: the gap's own
    kind already says the definition was withheld, truncated or quarantined, and the source's
    reason for withholding it is free text it wrote, which belongs behind the gated code route
    rather than in a list a whole operations team reads."""
    rows = (await session.execute(statement.limit(MAX_OBJECTS + 1))).all()
    return [
        FootprintGapObjectRead(
            object_type=object_type,
            object_id=object_id,
            qualified_name=f"{catalog_name}.{schema_name}.{name}",
        )
        for object_id, catalog_name, schema_name, name in rows
    ]


async def _table_names(
    session: AsyncSession, organization_id: UUID, table_ids: list[UUID]
) -> dict[UUID, str]:
    if not table_ids:
        return {}
    rows = (
        await session.execute(
            select(MetadataTable.id, MetadataCatalog.name, MetadataSchema.name, MetadataTable.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataTable.id.in_(table_ids),
                MetadataTable.organization_id == organization_id,
            )
        )
    ).all()
    return {
        table_id: f"{catalog_name}.{schema_name}.{name}"
        for table_id, catalog_name, schema_name, name in rows
    }


async def _routine_names(
    session: AsyncSession, organization_id: UUID, routine_ids: list[UUID]
) -> dict[UUID, str]:
    if not routine_ids:
        return {}
    rows = (
        await session.execute(
            select(
                MetadataRoutine.id,
                MetadataCatalog.name,
                MetadataSchema.name,
                MetadataRoutine.name,
            )
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataRoutine.id.in_(routine_ids),
                MetadataRoutine.organization_id == organization_id,
            )
        )
    ).all()
    return {
        routine_id: f"{catalog_name}.{schema_name}.{name}"
        for routine_id, catalog_name, schema_name, name in rows
    }


async def _code_objects(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID, *, kind: str
) -> list[FootprintGapObjectRead]:
    """The views and routines whose captured code is withheld, truncated or quarantined."""
    by_kind: dict[str, Callable[[Any], tuple[Any, ...]]] = {
        "CODE_WITHHELD": lambda model: (model.availability == UNAVAILABLE,),
        "CODE_TRUNCATED": lambda model: (
            model.availability == AVAILABLE,
            model.truncated.is_(True),
        ),
        "CODE_QUARANTINED": lambda model: (
            model.availability == AVAILABLE,
            model.screening_status != CLEAN,
        ),
    }
    conditions = by_kind[kind]
    views = await _named(
        session,
        _view_objects(organization_id, datasource_id, *conditions(MetadataViewDefinition)),
        "VIEW",
    )
    routines = await _named(
        session,
        _routine_objects(organization_id, datasource_id, *conditions(MetadataRoutine)),
        "ROUTINE",
    )
    return views + routines


async def _awaiting_parse(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID
) -> list[FootprintGapObjectRead]:
    """The lineage agent's backlog: eligible code with no parsed lineage yet."""
    views = await _named(
        session,
        _view_objects(
            organization_id,
            datasource_id,
            MetadataViewDefinition.availability == AVAILABLE,
            MetadataViewDefinition.redaction_status == "PARSED",
            MetadataViewDefinition.screening_status == CLEAN,
            ~exists().where(ViewLineageEdge.target_table_id == MetadataViewDefinition.table_id),
        ),
        "VIEW",
    )
    routines = await _named(
        session,
        _routine_objects(
            organization_id,
            datasource_id,
            MetadataRoutine.availability == AVAILABLE,
            MetadataRoutine.redaction_status.in_(sorted(VALUE_FREE_REDACTION_STATUSES)),
            MetadataRoutine.screening_status == CLEAN,
            MetadataRoutine.routine_type != "PACKAGE",
            ~exists().where(DeepProcedureLineageEdge.routine_id == MetadataRoutine.id),
        ),
        "ROUTINE",
    )
    return views + routines


async def _unparsed_routines(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID, *, callees_only: bool
) -> list[FootprintGapObjectRead]:
    """Routines carrying UNPARSED markers -- all of them, or only unread calls."""
    filters: list[Any] = [
        DeepProcedureLineageEdge.organization_id == organization_id,
        DeepProcedureLineageEdge.datasource_id == datasource_id,
        DeepProcedureLineageEdge.review_status == "ACTIVE",
        DeepProcedureLineageEdge.transformation_type == "UNPARSED",
    ]
    if callees_only:
        filters.append(
            or_(
                DeepProcedureLineageEdge.unparsed_reason.like(f"%({CALLEE_NOT_CAPTURED})"),
                DeepProcedureLineageEdge.unparsed_reason.like(f"%({CALLEE_BODY_WITHHELD})"),
            )
        )
    rows = (
        await session.execute(
            select(
                DeepProcedureLineageEdge.routine_id,
                DeepProcedureLineageEdge.unparsed_reason,
            )
            .where(*filters)
            .order_by(DeepProcedureLineageEdge.routine_id)
        )
    ).all()
    reasons: dict[UUID, str | None] = {}
    for routine_id, reason in rows:
        reasons.setdefault(routine_id, _reason_code(reason))
    names = await _routine_names(session, organization_id, list(reasons)[: MAX_OBJECTS + 1])
    return [
        FootprintGapObjectRead(
            object_type="ROUTINE",
            object_id=routine_id,
            qualified_name=names[routine_id],
            detail=reasons[routine_id],
        )
        for routine_id in names
    ]


def _reason_code(reason: str | None) -> str | None:
    """The stable code inside an UNPARSED marker's reason, never the statement text with it.

    A marker reads `SOMETHING (CODE)`; the code is what routes the gap, and the rest can carry
    a fragment of what the parser choked on, which is not ours to publish here.
    """
    if reason is None or not reason.endswith(")") or "(" not in reason:
        return None
    return reason.rsplit("(", 1)[1][:-1] or None


async def _awaiting_review(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID
) -> list[FootprintGapObjectRead]:
    """Proposed lineage nobody has decided: the views and routines it was proposed for."""
    view_rows = list(
        await session.scalars(
            select(ViewLineageEdge.target_table_id)
            .where(
                ViewLineageEdge.organization_id == organization_id,
                ViewLineageEdge.datasource_id == datasource_id,
                ViewLineageEdge.review_status == "PROPOSED",
            )
            .distinct()
            .limit(MAX_OBJECTS + 1)
        )
    )
    routine_rows = list(
        await session.scalars(
            select(DeepProcedureLineageEdge.routine_id)
            .where(
                DeepProcedureLineageEdge.organization_id == organization_id,
                DeepProcedureLineageEdge.datasource_id == datasource_id,
                DeepProcedureLineageEdge.review_status == "PROPOSED",
            )
            .distinct()
            .limit(MAX_OBJECTS + 1)
        )
    )
    tables = await _table_names(session, organization_id, [row for row in view_rows if row])
    routines = await _routine_names(
        session, organization_id, [row for row in routine_rows if row]
    )
    return [
        FootprintGapObjectRead(
            object_type="VIEW", object_id=table_id, qualified_name=name, detail="PROPOSED"
        )
        for table_id, name in tables.items()
    ] + [
        FootprintGapObjectRead(
            object_type="ROUTINE", object_id=routine_id, qualified_name=name, detail="PROPOSED"
        )
        for routine_id, name in routines.items()
    ]


async def _held_tables(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID
) -> list[FootprintGapObjectRead]:
    rows = (
        await session.execute(
            select(DataQualityIncident.table_id, DataQualityIncident.severity)
            .where(
                DataQualityIncident.organization_id == organization_id,
                DataQualityIncident.datasource_id == datasource_id,
                DataQualityIncident.anomaly_type == SOURCE_CHANGE_ANOMALY_TYPE,
                DataQualityIncident.status.in_(("OPEN", "ACKNOWLEDGED")),
            )
            .limit(MAX_OBJECTS + 1)
        )
    ).all()
    severities = {table_id: severity for table_id, severity in rows if table_id is not None}
    names = await _table_names(session, organization_id, list(severities))
    return [
        FootprintGapObjectRead(
            object_type="TABLE",
            object_id=table_id,
            qualified_name=name,
            detail=str(severities[table_id]),
        )
        for table_id, name in names.items()
    ]


async def _pending_signals(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID
) -> list[FootprintGapObjectRead]:
    """Change signals nothing has processed yet, named by the object they are about."""
    rows = (
        await session.execute(
            select(
                MetadataChangeSignal.subject_kind,
                MetadataChangeSignal.subject_id,
                MetadataChangeSignal.signal_type,
            )
            .where(
                MetadataChangeSignal.organization_id == organization_id,
                MetadataChangeSignal.datasource_id == datasource_id,
                MetadataChangeSignal.status == "PENDING",
            )
            .order_by(MetadataChangeSignal.detected_at)
            .limit(MAX_OBJECTS + 1)
        )
    ).all()
    table_ids = [subject_id for kind, subject_id, _ in rows if kind != "ROUTINE" and subject_id]
    routine_ids = [subject_id for kind, subject_id, _ in rows if kind == "ROUTINE" and subject_id]
    tables = await _table_names(session, organization_id, table_ids)
    routines = await _routine_names(session, organization_id, routine_ids)
    objects: list[FootprintGapObjectRead] = []
    for subject_kind, subject_id, signal_type in rows:
        name = (routines if subject_kind == "ROUTINE" else tables).get(subject_id)
        if name is None:
            # The object left the catalog after the signal was recorded; the signal is still
            # pending and still counted, and naming it would mean inventing a name.
            continue
        objects.append(
            FootprintGapObjectRead(
                object_type=str(subject_kind),
                object_id=subject_id,
                qualified_name=name,
                detail=str(signal_type),
            )
        )
    return objects


async def footprint_gap_objects(
    session: AsyncSession, *, organization_id: UUID, datasource_id: UUID, kind: str
) -> FootprintGapDetailRead:
    """The objects behind one source's count of one gap kind, bounded and value-free.

    The caller has already been authorized for this datasource -- `footprint_gap_detail_api`
    runs the same `READ_METADATA` gate the summary applies -- so this reads without re-asking.
    """
    if kind not in GAP_DEFINITIONS:
        raise UnknownGapKind(kind)
    resolution, owner, explanation = GAP_DEFINITIONS[kind]
    note = _UNLISTABLE_NOTES.get(kind)
    objects: list[FootprintGapObjectRead] = []
    if kind in ("CODE_WITHHELD", "CODE_TRUNCATED", "CODE_QUARANTINED"):
        objects = await _code_objects(session, organization_id, datasource_id, kind=kind)
    elif kind == "LINEAGE_AWAITING_PARSE":
        objects = await _awaiting_parse(session, organization_id, datasource_id)
    elif kind == "LINEAGE_UNPARSED_STATEMENTS":
        objects = await _unparsed_routines(
            session, organization_id, datasource_id, callees_only=False
        )
    elif kind == "LINEAGE_UNRESOLVED_CALLEE":
        objects = await _unparsed_routines(
            session, organization_id, datasource_id, callees_only=True
        )
    elif kind == "LINEAGE_AWAITING_REVIEW":
        objects = await _awaiting_review(session, organization_id, datasource_id)
    elif kind == "SOURCE_CHANGE_HOLDS":
        objects = await _held_tables(session, organization_id, datasource_id)
    elif kind == "CHANGE_SIGNALS_PENDING":
        objects = await _pending_signals(session, organization_id, datasource_id)
    truncated = len(objects) > MAX_OBJECTS
    return FootprintGapDetailRead(
        datasource_id=datasource_id,
        kind=kind,
        resolution=resolution,
        owner=owner,
        explanation=explanation,
        objects=sorted(objects, key=lambda item: item.qualified_name)[:MAX_OBJECTS],
        truncated=truncated,
        note=note,
    )
