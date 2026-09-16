"""R11-FP05/FP17: what Atlas does not know about each source yet, why, and who can close it.

The footprint work records gaps honestly where they occur -- a withheld definition keeps its
reason, an unreadable statement is an UNPARSED marker, an undecided lineage edge stays PROPOSED,
a source change opens a hold -- but nothing put them in one place, so a gap was something a person
had to go and find. This read model counts them per datasource and names, for each kind, the one
route that closes it:

* `AGENT` -- bounded work an existing task agent does (the lineage agent's backlog);
* `HUMAN_REVIEW` -- a decision a person makes in an existing queue;
* `SOURCE_ACCESS` -- outside Atlas: the source withholds or truncates, so retrying is pointless and
  never happens; granting read access and rescanning closes it;
* `OPERATIONS` -- a pass an operator has not turned on, or that is behind;
* `EXPLAINED` -- terminal: recorded once with its reason, re-examined only when the source changes.

That is the investigation plan (R11-FP05) -- a route per gap through the machinery that exists,
never a new orchestrator and never a retry loop -- and the backlog figures are FP17's queue
metrics. **Authorized dimensions:** a datasource the caller may not read is left out entirely,
not counted, so the totals never disclose that it has gaps. Value-free: counts and codes only.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import AuthorizationDenied, gate
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signal_processing import SOURCE_CHANGE_ANOMALY_TYPE
from aida.config import Settings
from aida.envelope_models import AVAILABLE, UNAVAILABLE, MetadataRoutine, MetadataViewDefinition
from aida.ingest_screening import CLEAN
from aida.models import DataQualityIncident, DataSource, ViewLineageEdge
from aida.procedure_lineage_models import DeepProcedureLineageEdge
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
        "agent:lineage (VIEW_LINEAGE, PROCEDURE_LINEAGE)",
        "Eligible views and routines with no parsed lineage yet -- the lineage agent's backlog.",
    ),
    "LINEAGE_UNPARSED_STATEMENTS": (
        "EXPLAINED",
        "none",
        "Routines with statements lineage cannot read, such as dynamic SQL. Recorded once as "
        "UNPARSED and examined again only when the body changes structurally.",
    ),
    "LINEAGE_UNRESOLVED_CALLEE": (
        "SOURCE_ACCESS",
        "source administrator",
        "Routines whose call to another routine, or read of a table function, could not be read "
        "through because the callee is not captured here or its body is withheld. Widening the "
        "discovery selection or granting read access, then rescanning, closes it. An ambiguous "
        "overload, a cycle or the descent bound is explained instead, and stays in the count "
        "above.",
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
    "CHANGE_SIGNALS_PENDING": (
        "OPERATIONS",
        "platform operator",
        "Source changes not yet processed into holds and re-examination. They wait on the "
        "change-signal pass (`change_signal_processing_interval_minutes`).",
    ),
}


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
        counts["LINEAGE_AWAITING_PARSE"] = _merge(eligible_views, eligible_routines)
        counts["LINEAGE_UNPARSED_STATEMENTS"] = await _grouped(
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
        )
        # R11-FP05/FP07: the calls `routine_call_descent` could not read through for a reason
        # someone can act on.
        counts["LINEAGE_UNRESOLVED_CALLEE"] = await _grouped(
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
                    DeepProcedureLineageEdge.unparsed_reason.like(f"%({CALLEE_NOT_CAPTURED})"),
                    DeepProcedureLineageEdge.unparsed_reason.like(f"%({CALLEE_BODY_WITHHELD})"),
                ),
            )
            .group_by(DeepProcedureLineageEdge.datasource_id),
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
