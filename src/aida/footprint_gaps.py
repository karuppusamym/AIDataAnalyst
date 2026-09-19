"""R11-FP05/FP17: what Atlas does not know about each source yet, why, and who can close it.

The footprint work records gaps honestly where they occur -- a withheld definition keeps its
reason, an unreadable statement is an UNPARSED marker, an undecided lineage edge stays PROPOSED,
a source change opens a hold, a trigger body nothing can be read from keeps its reason -- but
nothing put them in one place, so a gap was something a person had to go and find. This read
model counts them per datasource and names, for each kind, the one route that closes it:

* `AGENT` -- bounded work an existing task agent does (the lineage agent's backlog);
* `HUMAN_REVIEW` -- a decision a person makes in an existing queue;
* `SOURCE_ACCESS` -- outside Atlas: the source withholds or truncates, or hides an object from
  this principal entirely, so retrying is pointless and never happens; granting read access and
  rescanning closes it;
* `OPERATIONS` -- a pass an operator has not turned on, or that is behind;
* `EXPLAINED` -- terminal: recorded once with its reason, re-examined only when the source changes.

That is the investigation plan (R11-FP05) -- a route per gap through the machinery that exists,
never a new orchestrator and never a retry loop -- and the backlog figures are FP17's queue
metrics. **Authorized dimensions:** a datasource the caller may not read is left out entirely,
not counted, so the totals never disclose that it has gaps. Value-free: counts and codes only.

**Uncertain grain (R11-FP05, 2026-09-18).** A table or view whose row grain the platform cannot
establish is what makes an answer double-count, and it was the one gap FP05 named that nothing
counted. `GRAIN_UNCERTAIN` counts it from evidence already held -- declared keys and unique
indexes, and the composite-key candidates stewards decide -- with no new analysis; see
`grain_established`. It routes to HUMAN_REVIEW because the only machinery that closes it is the
existing composite-key review. An agent that fills that queue from profiles is the obvious next
step and deliberately not built here: an agent may write outside `GovernanceReview` only into a
queue `task_agent` lists as human-only (`_HUMAN_ONLY_QUEUES`), with a pending counter and an
outcome reader of its own, and the composite-key queue is not one -- admitting it changes
ADR-0029's one write path, which is the framework's decision, not the register's.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import AuthorizationDenied, gate
from aida.capability_states import CapabilityState
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signal_processing import SOURCE_CHANGE_ANOMALY_TYPE
from aida.config import Settings
from aida.envelope_models import (
    AVAILABLE,
    UNAVAILABLE,
    MetadataRoutine,
    MetadataTrigger,
    MetadataViewDefinition,
)
from aida.ingest_screening import CLEAN
from aida.models import (
    AnalysisRun,
    CompositeKeyCandidate,
    DataQualityIncident,
    DataSource,
    MetadataConstraint,
    MetadataIndex,
    MetadataTable,
    ViewLineageEdge,
)
from aida.procedure_lineage_models import (
    DeepProcedureLineageEdge,
    TriggerLineageEdge,
    TriggerParseCoverage,
)
from aida.routine_call_descent import CALLEE_BODY_WITHHELD, CALLEE_NOT_CAPTURED
from aida.schemas import ApiModel
from aida.security import SecurityContext
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES

#: kind -> (resolution route, who acts, what the gap means and how it closes)
GAP_DEFINITIONS: Final[dict[str, tuple[str, str, str]]] = {
    "CODE_WITHHELD": (
        "SOURCE_ACCESS",
        "source administrator",
        "The source withholds these view definitions or routine bodies from the scanning "
        "principal. Atlas keeps the reason and does not retry; granting read access and "
        "rescanning closes the gap.",
    ),
    "CODE_TRUNCATED": (
        "SOURCE_ACCESS",
        "source administrator",
        "The source returned only part of these definitions, so lineage read from them may be "
        "incomplete.",
    ),
    "CODE_QUARANTINED": (
        "HUMAN_REVIEW",
        "data steward",
        "Captured code set aside by prompt-risk screening. Nothing reads it for lineage, tools "
        "or model context until a steward clears it.",
    ),
    "LINEAGE_AWAITING_PARSE": (
        "AGENT",
        "agent:lineage (VIEW_LINEAGE, PROCEDURE_LINEAGE, TRIGGER_LINEAGE)",
        "Eligible views, routines and triggers with no parsed lineage yet -- the lineage "
        "agent's backlog. A trigger counts once its body, or the function its action names, "
        "is something the parser can be handed; a trigger with neither is not waiting on an "
        "agent and is counted as withheld code instead.",
    ),
    "LINEAGE_UNPARSED_STATEMENTS": (
        "EXPLAINED",
        "none",
        "Routines and triggers with statements lineage cannot read, such as dynamic SQL, or -- "
        "for a trigger -- a firing-row reference this engine spells in a form the parser cannot "
        "bind to the firing table. Recorded once as UNPARSED and examined again only when the "
        "body changes structurally.",
    ),
    "LINEAGE_UNRESOLVED_CALLEE": (
        "SOURCE_ACCESS",
        "source administrator",
        "Routines whose call to another routine, or read of a table function, could not be read "
        "through because the callee is not captured here or its body is withheld, and triggers "
        "whose action routine could not be reached the same way. Widening the discovery "
        "selection or granting read access, then rescanning, closes it. An ambiguous overload, "
        "a cycle or the descent bound is explained instead, and stays in the count above.",
    ),
    "LINEAGE_AWAITING_REVIEW": (
        "HUMAN_REVIEW",
        "data steward",
        "Proposed lineage edges nobody has decided. They steer no answer until approved.",
    ),
    "SOURCE_CHANGE_HOLDS": (
        "HUMAN_REVIEW",
        "data steward",
        "Tables held because the source changed them. Governed tools over them fail closed until "
        "a steward confirms and resolves the hold.",
    ),
    "SOURCE_OBJECTS_INVISIBLE": (
        "SOURCE_ACCESS",
        "source administrator",
        "Objects the source holds that the scanning principal may not see at all. They are not "
        "in the catalog, so no other gap can count them and no answer can mention them -- the "
        "last completed run asked the source's own catalog how many it kept back. Granting read "
        "access and rescanning closes it; a source that cannot be asked reports none rather "
        "than a false zero.",
    ),
    "SOURCE_READS_REFUSED": (
        "SOURCE_ACCESS",
        "source administrator",
        "Facets the source refused to read for the scanning principal on its last completed "
        "run -- grants, view definitions, routine bodies, constraints, indexes, comments, or "
        "the question of what the login cannot see at all. Counted per facet, not per object: "
        "a refused read returns nothing, so there is no object to count. Nothing about these "
        "facets is current, and nothing Atlas already holds for them is retired on account of "
        "the refusal. Granting the read and rescanning closes it.",
    ),
    "CHANGE_SIGNALS_PENDING": (
        "OPERATIONS",
        "platform operator",
        "Source changes not yet processed into holds and re-examination. They wait on the "
        "change-signal pass (`change_signal_processing_interval_minutes`).",
    ),
    # R11-FP05: the grain gap. See `grain_uncertain` for the derivation.
    "GRAIN_UNCERTAIN": (
        "HUMAN_REVIEW",
        "data steward",
        "Tables and views whose row grain Atlas cannot establish: no declared primary key, "
        "unique constraint or unique index, and no candidate key a steward has approved. "
        "Joining or aggregating over one of these can double-count, and nothing here says "
        "one row is one of anything. A steward closes it by approving a composite-key "
        "candidate discovered from the object's profile, or the source declares a key and a "
        "rescan reads it. Each object's code says which step is next: KEY_UNCONFIRMED or "
        "AMBIGUOUS_KEY (candidates wait for a steward), KEYS_NOT_READ (the last run did not "
        "read constraints, so a key may exist unseen), AGGREGATE_WITHOUT_GROUPING (a view "
        "that aggregates and passes no column through, so its grouping is not among its "
        "outputs), or NO_KEY.",
    ),
}

#: R11-FP05: the constraint types that declare a row's identity -- the same pair
#: `composite_key_api` treats as a declared key when it excludes key columns.
DECLARED_KEY_CONSTRAINT_TYPES: Final = ("PRIMARY_KEY", "UNIQUE")


def grain_established() -> ColumnElement[bool]:
    """A (correlated) catalog table or view whose row grain the platform can state.

    Only evidence the platform already holds, and only the kinds that *state* a
    grain rather than suggest one: a declared primary key or unique constraint, a
    unique or primary index (how a SQL Server indexed view or a PostgreSQL unique
    index declares one), or a composite-key candidate a steward APPROVED through
    the maker-checker queue. A profile's `effectively_unique` or a PENDING
    candidate is evidence *for* a key, not a key -- a sample can be unique by
    accident, which is exactly how double-counting slips in -- so neither
    establishes grain on its own; a pending candidate is what the gap's route
    asks a steward to decide.

    Two unique keys on one table do not make its grain ambiguous: the grain is
    the row, and either key names it. Ambiguity is the undecided case, where
    several candidates wait and none is declared. Every clause restates the
    organization (INV-5). Shared with `footprint_gap_detail` so the count and
    the list it expands cannot drift.
    """
    return or_(
        exists().where(
            MetadataConstraint.organization_id == MetadataTable.organization_id,
            MetadataConstraint.table_id == MetadataTable.id,
            MetadataConstraint.status == "ACTIVE",
            MetadataConstraint.constraint_type.in_(DECLARED_KEY_CONSTRAINT_TYPES),
        ),
        exists().where(
            MetadataIndex.organization_id == MetadataTable.organization_id,
            MetadataIndex.table_id == MetadataTable.id,
            MetadataIndex.status == "ACTIVE",
            or_(MetadataIndex.is_unique.is_(True), MetadataIndex.is_primary.is_(True)),
        ),
        exists().where(
            CompositeKeyCandidate.organization_id == MetadataTable.organization_id,
            CompositeKeyCandidate.table_id == MetadataTable.id,
            CompositeKeyCandidate.status == "APPROVED",
        ),
    )


def grain_uncertain() -> ColumnElement[bool]:
    """A (correlated) ACTIVE catalog table or view whose grain is not established."""
    return and_(MetadataTable.status == "ACTIVE", ~grain_established())


class FootprintGapRead(ApiModel):
    kind: str
    count: int
    resolution: str
    owner: str
    explanation: str


class DatasourceFootprintGapsRead(ApiModel):
    datasource_id: UUID
    datasource_name: str
    gaps: list[FootprintGapRead]
    #: FP17 queue age: how long the oldest unprocessed change signal has waited.
    oldest_pending_signal_minutes: int | None = None


class FootprintGapsRead(ApiModel):
    organization_id: UUID
    generated_at: datetime
    datasources: list[DatasourceFootprintGapsRead]
    #: Summed over the datasources listed above only.
    totals: dict[str, int]


def _merge(*counts: dict[UUID, int]) -> dict[UUID, int]:
    merged: dict[UUID, int] = {}
    for count in counts:
        for datasource_id, value in count.items():
            merged[datasource_id] = merged.get(datasource_id, 0) + value
    return merged


async def _grouped(session: AsyncSession, statement: Select[Any]) -> dict[UUID, int]:
    return {
        datasource_id: int(count)
        for datasource_id, count in (await session.execute(statement)).all()
        if datasource_id is not None
    }


async def readable_datasources(
    session: AsyncSession, context: SecurityContext, settings: Settings, organization_id: UUID
) -> list[tuple[UUID, str]]:
    """The organization's datasources the caller may read metadata of -- one gate call each,
    the rule catalog rows apply. A denied datasource is dropped silently."""
    rows = (
        await session.execute(
            select(DataSource.id, DataSource.name)
            .where(DataSource.organization_id == organization_id)
            .order_by(DataSource.name, DataSource.id)
        )
    ).all()
    readable: list[tuple[UUID, str]] = []
    for datasource_id, name in rows:
        try:
            await gate(
                session,
                context,
                settings=settings,
                action="READ_METADATA",
                resource_type="datasource",
                resource_id=str(datasource_id),
                datasource_id=datasource_id,
            )
        except AuthorizationDenied:
            continue
        readable.append((datasource_id, name))
    return readable


def trigger_awaits_parse() -> ColumnElement[bool]:
    """A (correlated) trigger the lineage agent has something to hand the parser
    for, and has never measured.

    "Never measured" is the coverage record's absence *and* no edge (a trigger
    parsed before `trigger_parse_coverage` existed has only edges). Before the
    coverage record, it was the edge's absence alone -- so a trigger whose body was
    read in full and writes nothing (a PostgreSQL function that only `RETURN NEW`s)
    stayed in the agent's backlog for ever, counted as work nobody had done. Shared
    with `footprint_gap_detail` so the count and the list it expands cannot drift.
    """
    return and_(
        or_(
            and_(
                MetadataTrigger.availability == AVAILABLE,
                MetadataTrigger.redaction_status.in_(sorted(VALUE_FREE_REDACTION_STATUSES)),
                MetadataTrigger.screening_status == CLEAN,
            ),
            and_(
                MetadataTrigger.action_routine.is_not(None),
                MetadataTrigger.action_routine != "",
            ),
        ),
        ~exists().where(
            TriggerLineageEdge.organization_id == MetadataTrigger.organization_id,
            TriggerLineageEdge.trigger_id == MetadataTrigger.id,
        ),
        ~exists().where(
            TriggerParseCoverage.organization_id == MetadataTrigger.organization_id,
            TriggerParseCoverage.trigger_id == MetadataTrigger.id,
        ),
    )


def _code_counts(
    organization_id: UUID, ids: Iterable[UUID], *condition: Any
) -> tuple[Select[Any], Select[Any]]:
    scope = list(ids)
    views = (
        select(MetadataViewDefinition.datasource_id, func.count())
        .where(
            MetadataViewDefinition.organization_id == organization_id,
            MetadataViewDefinition.datasource_id.in_(scope),
            MetadataViewDefinition.status == "ACTIVE",
            *[clause(MetadataViewDefinition) for clause in condition],
        )
        .group_by(MetadataViewDefinition.datasource_id)
    )
    routines = (
        select(MetadataRoutine.datasource_id, func.count())
        .where(
            MetadataRoutine.organization_id == organization_id,
            MetadataRoutine.datasource_id.in_(scope),
            MetadataRoutine.status == "ACTIVE",
            # A package member has no body of its own -- its source is the package's -- which
            # is not the source withholding anything (R11-FP03).
            MetadataRoutine.package_name == "",
            *[clause(MetadataRoutine) for clause in condition],
        )
        .group_by(MetadataRoutine.datasource_id)
    )
    return views, routines


async def footprint_gaps(
    session: AsyncSession,
    *,
    context: SecurityContext,
    settings: Settings,
    organization_id: UUID,
    now: datetime | None = None,
) -> FootprintGapsRead:
    effective_now = now or datetime.now(UTC)
    readable = await readable_datasources(session, context, settings, organization_id)
    ids = [datasource_id for datasource_id, _ in readable]
    counts: dict[str, dict[UUID, int]] = {}
    oldest: dict[UUID, datetime] = {}
    if ids:
        for kind, condition in (
            ("CODE_WITHHELD", lambda model: model.availability == UNAVAILABLE),
            (
                "CODE_TRUNCATED",
                lambda model: (model.availability == AVAILABLE) & model.truncated.is_(True),
            ),
            (
                "CODE_QUARANTINED",
                lambda model: (model.availability == AVAILABLE) & (model.screening_status != CLEAN),
            ),
        ):
            views, routines = _code_counts(organization_id, ids, condition)
            counts[kind] = _merge(
                await _grouped(session, views), await _grouped(session, routines)
            )

        eligible_views = await _grouped(
            session,
            select(MetadataViewDefinition.datasource_id, func.count())
            .where(
                MetadataViewDefinition.organization_id == organization_id,
                MetadataViewDefinition.datasource_id.in_(ids),
                MetadataViewDefinition.status == "ACTIVE",
                MetadataViewDefinition.availability == AVAILABLE,
                MetadataViewDefinition.redaction_status == "PARSED",
                MetadataViewDefinition.screening_status == CLEAN,
                ~exists().where(ViewLineageEdge.target_table_id == MetadataViewDefinition.table_id),
            )
            .group_by(MetadataViewDefinition.datasource_id),
        )
        eligible_routines = await _grouped(
            session,
            select(MetadataRoutine.datasource_id, func.count())
            .where(
                MetadataRoutine.organization_id == organization_id,
                MetadataRoutine.datasource_id.in_(ids),
                MetadataRoutine.status == "ACTIVE",
                MetadataRoutine.availability == AVAILABLE,
                MetadataRoutine.redaction_status.in_(sorted(VALUE_FREE_REDACTION_STATUSES)),
                MetadataRoutine.screening_status == CLEAN,
                MetadataRoutine.routine_type != "PACKAGE",
                ~exists().where(DeepProcedureLineageEdge.routine_id == MetadataRoutine.id),
            )
            .group_by(MetadataRoutine.datasource_id),
        )
        # R11-FP01: a trigger is waiting on the lineage agent when there is
        # something to hand the parser -- its own eligible body, or the function
        # its `action_routine` names, which is where PostgreSQL keeps the code.
        # A trigger with neither is not the agent's to close: the source withheld
        # the body, and `CODE_WITHHELD` is where that belongs.
        eligible_triggers = await _grouped(
            session,
            select(MetadataTrigger.datasource_id, func.count())
            .where(
                MetadataTrigger.organization_id == organization_id,
                MetadataTrigger.datasource_id.in_(ids),
                MetadataTrigger.status == "ACTIVE",
                trigger_awaits_parse(),
            )
            .group_by(MetadataTrigger.datasource_id),
        )
        counts["LINEAGE_AWAITING_PARSE"] = _merge(
            eligible_views, eligible_routines, eligible_triggers
        )
        counts["LINEAGE_UNPARSED_STATEMENTS"] = _merge(
            await _grouped(
                session,
                select(
                    DeepProcedureLineageEdge.datasource_id,
                    func.count(func.distinct(DeepProcedureLineageEdge.routine_id)),
                )
                .where(
                    DeepProcedureLineageEdge.organization_id == organization_id,
                    DeepProcedureLineageEdge.datasource_id.in_(ids),
                    DeepProcedureLineageEdge.review_status == "ACTIVE",
                    DeepProcedureLineageEdge.transformation_type == "UNPARSED",
                )
                .group_by(DeepProcedureLineageEdge.datasource_id),
            ),
            await _grouped(
                session,
                select(
                    TriggerLineageEdge.datasource_id,
                    func.count(func.distinct(TriggerLineageEdge.trigger_id)),
                )
                .where(
                    TriggerLineageEdge.organization_id == organization_id,
                    TriggerLineageEdge.datasource_id.in_(ids),
                    TriggerLineageEdge.review_status == "ACTIVE",
                    TriggerLineageEdge.transformation_type == "UNPARSED",
                )
                .group_by(TriggerLineageEdge.datasource_id),
            ),
        )
        # R11-FP05/FP07: the calls `routine_call_descent` could not read through for a reason
        # someone can act on.
        # R11-FP01: a PostgreSQL trigger whose action routine is not captured here,
        # or whose body that routine withholds, is the same gap with the same
        # route, and `routine_lineage_edges` writes it in the same two words on
        # purpose so this pattern reaches both.
        counts["LINEAGE_UNRESOLVED_CALLEE"] = _merge(
            await _grouped(
                session,
                select(
                    DeepProcedureLineageEdge.datasource_id,
                    func.count(func.distinct(DeepProcedureLineageEdge.routine_id)),
                )
                .where(
                    DeepProcedureLineageEdge.organization_id == organization_id,
                    DeepProcedureLineageEdge.datasource_id.in_(ids),
                    DeepProcedureLineageEdge.review_status == "ACTIVE",
                    DeepProcedureLineageEdge.transformation_type == "UNPARSED",
                    or_(
                        DeepProcedureLineageEdge.unparsed_reason.like(
                            f"%({CALLEE_NOT_CAPTURED})"
                        ),
                        DeepProcedureLineageEdge.unparsed_reason.like(
                            f"%({CALLEE_BODY_WITHHELD})"
                        ),
                    ),
                )
                .group_by(DeepProcedureLineageEdge.datasource_id),
            ),
            await _grouped(
                session,
                select(
                    TriggerLineageEdge.datasource_id,
                    func.count(func.distinct(TriggerLineageEdge.trigger_id)),
                )
                .where(
                    TriggerLineageEdge.organization_id == organization_id,
                    TriggerLineageEdge.datasource_id.in_(ids),
                    TriggerLineageEdge.review_status == "ACTIVE",
                    TriggerLineageEdge.transformation_type == "UNPARSED",
                    or_(
                        TriggerLineageEdge.unparsed_reason.like(f"%({CALLEE_NOT_CAPTURED})"),
                        TriggerLineageEdge.unparsed_reason.like(f"%({CALLEE_BODY_WITHHELD})"),
                    ),
                )
                .group_by(TriggerLineageEdge.datasource_id),
            ),
        )
        counts["LINEAGE_AWAITING_REVIEW"] = _merge(
            await _grouped(
                session,
                select(ViewLineageEdge.datasource_id, func.count())
                .where(
                    ViewLineageEdge.organization_id == organization_id,
                    ViewLineageEdge.datasource_id.in_(ids),
                    ViewLineageEdge.review_status == "PROPOSED",
                )
                .group_by(ViewLineageEdge.datasource_id),
            ),
            await _grouped(
                session,
                select(DeepProcedureLineageEdge.datasource_id, func.count())
                .where(
                    DeepProcedureLineageEdge.organization_id == organization_id,
                    DeepProcedureLineageEdge.datasource_id.in_(ids),
                    DeepProcedureLineageEdge.review_status == "PROPOSED",
                )
                .group_by(DeepProcedureLineageEdge.datasource_id),
            ),
            await _grouped(
                session,
                select(TriggerLineageEdge.datasource_id, func.count())
                .where(
                    TriggerLineageEdge.organization_id == organization_id,
                    TriggerLineageEdge.datasource_id.in_(ids),
                    TriggerLineageEdge.review_status == "PROPOSED",
                )
                .group_by(TriggerLineageEdge.datasource_id),
            ),
        )
        # R11-FP05: tables and views whose grain nothing the platform holds states.
        counts["GRAIN_UNCERTAIN"] = await _grouped(
            session,
            select(MetadataTable.datasource_id, func.count())
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.datasource_id.in_(ids),
                grain_uncertain(),
            )
            .group_by(MetadataTable.datasource_id),
        )
        counts["SOURCE_CHANGE_HOLDS"] = await _grouped(
            session,
            select(DataQualityIncident.datasource_id, func.count())
            .where(
                DataQualityIncident.organization_id == organization_id,
                DataQualityIncident.datasource_id.in_(ids),
                DataQualityIncident.anomaly_type == SOURCE_CHANGE_ANOMALY_TYPE,
                DataQualityIncident.status.in_(("OPEN", "ACKNOWLEDGED")),
            )
            .group_by(DataQualityIncident.datasource_id),
        )
        # R11-FP02: what the last completed run was told it may not see. Read from that run's
        # own receipt rather than recounted, so the figure is the one the run recorded and a
        # source that could not be asked (`invisible: null`) contributes nothing.
        latest_run = (
            select(
                AnalysisRun.datasource_id,
                func.max(AnalysisRun.updated_at).label("finished_at"),
            )
            .where(
                AnalysisRun.organization_id == organization_id,
                AnalysisRun.datasource_id.in_(ids),
                AnalysisRun.status == "COMPLETED",
            )
            .group_by(AnalysisRun.datasource_id)
            .subquery()
        )
        receipt_rows = (
            await session.execute(
                select(AnalysisRun.datasource_id, AnalysisRun.discovery_receipt).join(
                    latest_run,
                    (AnalysisRun.datasource_id == latest_run.c.datasource_id)
                    & (AnalysisRun.updated_at == latest_run.c.finished_at),
                )
            )
        ).all()
        counts["SOURCE_OBJECTS_INVISIBLE"] = {}
        # R11-FP02 / review 2026-09-16 §5: a facet the source *refused* is its own gap, and
        # has to be, because every other reading of a refusal is wrong. The counts above see
        # nothing (a refused read returns no rows, so there is no withheld definition and no
        # truncated body to count), and `invisible` goes null on a refused visibility
        # question -- which the kinds loop below deliberately treats as "could not ask".
        # Without this kind, one missing grant would present as a clean source: the very
        # false-clean reading the receipt exists to prevent, moved one surface along.
        counts["SOURCE_READS_REFUSED"] = {}
        for datasource_id, receipt in receipt_rows:
            if datasource_id is None or not isinstance(receipt, dict):
                continue
            facets = receipt.get("facets")
            if isinstance(facets, dict):
                refused = sum(
                    1
                    for facet in facets.values()
                    if isinstance(facet, dict)
                    and facet.get("state") == CapabilityState.PERMISSION_DENIED.value
                )
                if refused:
                    counts["SOURCE_READS_REFUSED"][datasource_id] = refused
            kinds = receipt.get("kinds")
            if not isinstance(kinds, dict):
                continue
            hidden = sum(
                int(counted.get("invisible") or 0)
                for counted in kinds.values()
                if isinstance(counted, dict)
            )
            if hidden:
                counts["SOURCE_OBJECTS_INVISIBLE"][datasource_id] = hidden
        pending_rows = (
            await session.execute(
                select(
                    MetadataChangeSignal.datasource_id,
                    func.count(),
                    func.min(MetadataChangeSignal.detected_at),
                )
                .where(
                    MetadataChangeSignal.organization_id == organization_id,
                    MetadataChangeSignal.datasource_id.in_(ids),
                    MetadataChangeSignal.status == "PENDING",
                )
                .group_by(MetadataChangeSignal.datasource_id)
            )
        ).all()
        counts["CHANGE_SIGNALS_PENDING"] = {}
        for datasource_id, count, detected_at in pending_rows:
            if datasource_id is None:
                continue
            counts["CHANGE_SIGNALS_PENDING"][datasource_id] = int(count)
            if detected_at is not None:
                oldest[datasource_id] = (
                    detected_at if detected_at.tzinfo else detected_at.replace(tzinfo=UTC)
                )

    datasources: list[DatasourceFootprintGapsRead] = []
    totals: dict[str, int] = {}
    for datasource_id, name in readable:
        gaps: list[FootprintGapRead] = []
        for kind, (resolution, owner, explanation) in GAP_DEFINITIONS.items():
            count = counts.get(kind, {}).get(datasource_id, 0)
            if count <= 0:
                continue
            gaps.append(
                FootprintGapRead(
                    kind=kind,
                    count=count,
                    resolution=resolution,
                    owner=owner,
                    explanation=explanation,
                )
            )
            totals[kind] = totals.get(kind, 0) + count
        waited = oldest.get(datasource_id)
        datasources.append(
            DatasourceFootprintGapsRead(
                datasource_id=datasource_id,
                datasource_name=name,
                gaps=gaps,
                oldest_pending_signal_minutes=(
                    int((effective_now - waited).total_seconds() // 60) if waited else None
                ),
            )
        )
    return FootprintGapsRead(
        organization_id=organization_id,
        generated_at=effective_now,
        datasources=datasources,
        totals=dict(sorted(totals.items())),
    )
