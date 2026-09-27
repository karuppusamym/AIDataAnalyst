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

from collections import Counter
from collections.abc import Callable
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.capability_states import CapabilityState
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signal_processing import SOURCE_CHANGE_ANOMALY_TYPE
from aida.connectors.discovery import FACET_CONSTRAINTS
from aida.envelope_models import (
    AVAILABLE,
    UNAVAILABLE,
    MetadataRoutine,
    MetadataTrigger,
    MetadataViewDefinition,
)
from aida.footprint_gaps import (
    GAP_DEFINITIONS,
    grain_uncertain,
    trigger_awaits_parse,
    trigger_propagation_gaps,
)
from aida.ingest_screening import CLEAN
from aida.models import (
    AnalysisRun,
    CompositeKeyCandidate,
    DataQualityIncident,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    ViewLineageEdge,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge, TriggerLineageEdge
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


def _trigger_objects(
    organization_id: UUID, datasource_id: UUID, *condition: Any, active_only: bool = True
) -> Select[Any]:
    """Triggers of this source matching `condition`, named by schema, name and the
    table they fire on -- a trigger name is scoped to its table on PostgreSQL, so
    two tables in one schema may each own an `audit_trg`.

    `active_only` is the default a catalog list wants. A propagation gap is not one of those:
    propagation follows a since-dropped trigger's reviewed edges, so its list must name it too.
    """
    return (
        select(
            MetadataTrigger.id,
            MetadataCatalog.name,
            MetadataSchema.name,
            MetadataTrigger.name,
            MetadataTrigger.table_name,
        )
        .select_from(MetadataTrigger)
        .join(MetadataSchema, MetadataSchema.id == MetadataTrigger.schema_id)
        .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
        .where(
            MetadataTrigger.organization_id == organization_id,
            MetadataTrigger.datasource_id == datasource_id,
            *([MetadataTrigger.status == "ACTIVE"] if active_only else []),
            *condition,
        )
    )


async def _named_triggers(
    session: AsyncSession, statement: Select[Any], details: dict[UUID, str | None] | None = None
) -> list[FootprintGapObjectRead]:
    """R11-FP01: the trigger half of a gap. `footprint_gaps` has counted triggers in
    the lineage kinds since trigger lineage landed, and this list did not name them
    -- so a source whose only gap was a trigger showed a count whose list read
    "nothing left to show", the false-clean reading this module exists to prevent."""
    rows = (await session.execute(statement.limit(MAX_OBJECTS + 1))).all()
    return [
        FootprintGapObjectRead(
            object_type="TRIGGER",
            object_id=trigger_id,
            qualified_name=f"{catalog_name}.{schema_name}.{name} on {table_name}",
            detail=(details or {}).get(trigger_id),
        )
        for trigger_id, catalog_name, schema_name, name, table_name in rows
    ]


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
    triggers = await _named_triggers(
        session, _trigger_objects(organization_id, datasource_id, trigger_awaits_parse())
    )
    return views + routines + triggers


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


async def _unparsed_triggers(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID, *, callees_only: bool
) -> list[FootprintGapObjectRead]:
    """Triggers carrying UNPARSED markers -- all of them, or only an action routine
    that could not be reached -- read with exactly the predicate `footprint_gaps`
    counts them by."""
    filters: list[Any] = [
        TriggerLineageEdge.organization_id == organization_id,
        TriggerLineageEdge.datasource_id == datasource_id,
        TriggerLineageEdge.review_status == "ACTIVE",
        TriggerLineageEdge.transformation_type == "UNPARSED",
    ]
    if callees_only:
        filters.append(
            or_(
                TriggerLineageEdge.unparsed_reason.like(f"%({CALLEE_NOT_CAPTURED})"),
                TriggerLineageEdge.unparsed_reason.like(f"%({CALLEE_BODY_WITHHELD})"),
            )
        )
    rows = (
        await session.execute(
            select(TriggerLineageEdge.trigger_id, TriggerLineageEdge.unparsed_reason)
            .where(*filters)
            .order_by(TriggerLineageEdge.trigger_id)
        )
    ).all()
    reasons: dict[UUID, str | None] = {}
    for trigger_id, reason in rows:
        reasons.setdefault(trigger_id, _reason_code(reason))
    if not reasons:
        return []
    return await _named_triggers(
        session,
        _trigger_objects(
            organization_id,
            datasource_id,
            MetadataTrigger.id.in_(list(reasons)[: MAX_OBJECTS + 1]),
        ),
        reasons,
    )


def _reason_code(reason: str | None) -> str | None:
    """The stable code inside an UNPARSED marker's reason, never the statement text with it.

    A marker reads `SOMETHING (CODE)`; the code is what routes the gap, and the rest can carry
    a fragment of what the parser choked on, which is not ours to publish here.
    """
    if reason is None or not reason.endswith(")") or "(" not in reason:
        return None
    return reason.rsplit("(", 1)[1][:-1] or None


async def _trigger_propagation_gap_objects(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID
) -> list[FootprintGapObjectRead]:
    """R11-FP01: the triggers `footprint_gaps` counts as TRIGGER_PROPAGATION_GAPS, each with
    every reason propagation gives for it, as the stable codes and nothing else.

    Read through `trigger_propagation_gaps`, the function the count reads, so the list and the
    count are the same triggers by construction. One entry per trigger -- the screen keys a row by
    object -- with its reasons joined in sorted order (`COLUMN_NOT_IN_CATALOG,TABLE_STAR`).
    """
    reasons = await trigger_propagation_gaps(
        session, organization_id=organization_id, datasource_id=datasource_id
    )
    if not reasons:
        return []
    return await _named_triggers(
        session,
        _trigger_objects(
            organization_id,
            datasource_id,
            MetadataTrigger.id.in_(list(reasons)[: MAX_OBJECTS + 1]),
            active_only=False,
        ),
        {trigger_id: ",".join(found) for trigger_id, found in reasons.items()},
    )


async def _awaiting_review(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID
) -> list[FootprintGapObjectRead]:
    """Proposed lineage nobody has decided: the views, routines and triggers it was
    proposed for."""
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
    trigger_ids = list(
        await session.scalars(
            select(TriggerLineageEdge.trigger_id)
            .where(
                TriggerLineageEdge.organization_id == organization_id,
                TriggerLineageEdge.datasource_id == datasource_id,
                TriggerLineageEdge.review_status == "PROPOSED",
            )
            .distinct()
            .limit(MAX_OBJECTS + 1)
        )
    )
    tables = await _table_names(session, organization_id, [row for row in view_rows if row])
    routines = await _routine_names(
        session, organization_id, [row for row in routine_rows if row]
    )
    triggers = (
        await _named_triggers(
            session,
            _trigger_objects(organization_id, datasource_id, MetadataTrigger.id.in_(trigger_ids)),
            dict.fromkeys(trigger_ids, "PROPOSED"),
        )
        if trigger_ids
        else []
    )
    return triggers + [
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


#: R11-FP05: the per-object codes of `GRAIN_UNCERTAIN`, in the order they are
#: decided -- each names the step that closes that object's gap.
GRAIN_AGGREGATE_WITHOUT_GROUPING: Final = "AGGREGATE_WITHOUT_GROUPING"
GRAIN_AMBIGUOUS_KEY: Final = "AMBIGUOUS_KEY"
GRAIN_KEY_UNCONFIRMED: Final = "KEY_UNCONFIRMED"
GRAIN_KEYS_NOT_READ: Final = "KEYS_NOT_READ"
GRAIN_NO_KEY: Final = "NO_KEY"
#: A constraints read that ended in one of these read no keys, so "no key" is not
#: known -- a refused or unavailable read, or an adapter that does not read them.
_KEYS_UNREAD_STATES: Final = frozenset(
    {
        CapabilityState.PERMISSION_DENIED.value,
        CapabilityState.UNAVAILABLE.value,
        CapabilityState.UNSUPPORTED.value,
    }
)
_VIEW_KINDS: Final = frozenset({"VIEW", "MATERIALIZED_VIEW"})


async def _constraints_unread(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID
) -> bool:
    """Whether the source's last completed run did not read its constraints -- read from
    that run's own receipt, the figure `footprint_gaps` reads refusals from."""
    receipt = (
        await session.execute(
            select(AnalysisRun.discovery_receipt)
            .where(
                AnalysisRun.organization_id == organization_id,
                AnalysisRun.datasource_id == datasource_id,
                AnalysisRun.status == "COMPLETED",
            )
            .order_by(AnalysisRun.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    facets = receipt.get("facets") if isinstance(receipt, dict) else None
    facet = facets.get(FACET_CONSTRAINTS) if isinstance(facets, dict) else None
    return isinstance(facet, dict) and facet.get("state") in _KEYS_UNREAD_STATES


async def _grain_objects(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID
) -> list[FootprintGapObjectRead]:
    """The tables and views `footprint_gaps` counts as GRAIN_UNCERTAIN, each with the code
    for the step that closes it.

    Read with the same predicate the count uses (`grain_uncertain`), and every code comes from
    a record the platform already holds, in this order:

    * AGGREGATE_WITHOUT_GROUPING -- a view whose ACTIVE parsed lineage aggregates and passes no
      column through directly. Its grouping, if any, is not among its outputs, so no column of
      it can name a row; a view that also passes columns through falls to the codes below;
    * AMBIGUOUS_KEY / KEY_UNCONFIRMED -- several, or one, PENDING composite-key candidates: a
      steward's decision is the next step;
    * KEYS_NOT_READ -- no candidate, and the last completed run did not read constraints, so a
      declared key may exist that Atlas has not seen;
    * NO_KEY -- none of the above. Candidate discovery over the object's profile is the next
      step; an object with no profile has nothing to discover from.
    """
    rows = (
        await session.execute(
            select(
                MetadataTable.id,
                MetadataCatalog.name,
                MetadataSchema.name,
                MetadataTable.name,
                MetadataTable.object_type,
            )
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.datasource_id == datasource_id,
                grain_uncertain(),
            )
            .order_by(MetadataCatalog.name, MetadataSchema.name, MetadataTable.name)
            .limit(MAX_OBJECTS + 1)
        )
    ).all()
    table_ids = [row[0] for row in rows]
    if not table_ids:
        return []
    pending = Counter(
        {
            table_id: int(count)
            for table_id, count in (
                await session.execute(
                    select(CompositeKeyCandidate.table_id, func.count())
                    .where(
                        CompositeKeyCandidate.organization_id == organization_id,
                        CompositeKeyCandidate.datasource_id == datasource_id,
                        CompositeKeyCandidate.table_id.in_(table_ids),
                        CompositeKeyCandidate.status == "PENDING",
                    )
                    .group_by(CompositeKeyCandidate.table_id)
                )
            ).all()
        }
    )
    shapes: dict[UUID, set[str]] = {}
    for target_table_id, transformation_type in (
        await session.execute(
            select(ViewLineageEdge.target_table_id, ViewLineageEdge.transformation_type)
            .where(
                ViewLineageEdge.organization_id == organization_id,
                ViewLineageEdge.datasource_id == datasource_id,
                ViewLineageEdge.target_table_id.in_(table_ids),
                ViewLineageEdge.review_status == "ACTIVE",
                ViewLineageEdge.transformation_type.in_(("AGGREGATED", "DIRECT")),
            )
            .distinct()
        )
    ).all():
        if target_table_id is not None:
            shapes.setdefault(target_table_id, set()).add(str(transformation_type))
    keys_unread = await _constraints_unread(session, organization_id, datasource_id)

    objects: list[FootprintGapObjectRead] = []
    for table_id, catalog_name, schema_name, name, object_type in rows:
        is_view = str(object_type).upper() in _VIEW_KINDS
        shape = shapes.get(table_id, set())
        if is_view and shape == {"AGGREGATED"}:
            code = GRAIN_AGGREGATE_WITHOUT_GROUPING
        elif pending[table_id] > 1:
            code = GRAIN_AMBIGUOUS_KEY
        elif pending[table_id] == 1:
            code = GRAIN_KEY_UNCONFIRMED
        elif keys_unread:
            code = GRAIN_KEYS_NOT_READ
        else:
            code = GRAIN_NO_KEY
        objects.append(
            FootprintGapObjectRead(
                object_type="VIEW" if is_view else "TABLE",
                object_id=table_id,
                qualified_name=f"{catalog_name}.{schema_name}.{name}",
                detail=code,
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
    elif kind in ("LINEAGE_UNPARSED_STATEMENTS", "LINEAGE_UNRESOLVED_CALLEE"):
        callees_only = kind == "LINEAGE_UNRESOLVED_CALLEE"
        objects = await _unparsed_routines(
            session, organization_id, datasource_id, callees_only=callees_only
        ) + await _unparsed_triggers(
            session, organization_id, datasource_id, callees_only=callees_only
        )
    elif kind == "LINEAGE_AWAITING_REVIEW":
        objects = await _awaiting_review(session, organization_id, datasource_id)
    elif kind == "TRIGGER_PROPAGATION_GAPS":
        objects = await _trigger_propagation_gap_objects(session, organization_id, datasource_id)
    elif kind == "SOURCE_CHANGE_HOLDS":
        objects = await _held_tables(session, organization_id, datasource_id)
    elif kind == "CHANGE_SIGNALS_PENDING":
        objects = await _pending_signals(session, organization_id, datasource_id)
    elif kind == "GRAIN_UNCERTAIN":
        objects = await _grain_objects(session, organization_id, datasource_id)
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
