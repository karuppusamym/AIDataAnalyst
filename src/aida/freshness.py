"""Freshness watermark contracts (DQ-2).

Deterministic freshness evaluation based on watermark columns.
CRITICAL invariant (ADR-0016): scan age is NEVER presented as freshness.
Freshness is only activated for explicitly configured tables; unconfigured
tables return NOT_CONFIGURED. Configuration requires maker-checker approval.

Two halves, in this order:

* the pure evaluator (`evaluate_freshness`) -- one config, one observed
  watermark, one verdict, no I/O; and
* R11-B8's persistence-backed sweep (`evaluate_freshness_for_datasource`),
  which evaluates a datasource's approved contracts and opens, updates or
  resolves ``DataQualityIncident`` rows.

The sweep deliberately files into the *same* incident sink the built-in
deterministic controls (`quality_service.evaluate_analysis_run`) and the
custom rule packs (`custom_quality_rules.evaluate_rule_pack`) already use,
keyed by its own ``anomaly_type`` (``FRESHNESS_VIOLATION``). DQ-3's runtime
coupling (`quality_coupling.fetch_open_incidents`) filters on
``datasource_id``/``table_id``/``status`` and never on ``anomaly_type``, so a
freshness incident demotes retrieval ranking, gates governed tools and warns
on agent answers through wiring that already exists -- nothing downstream
changes. A parallel "freshness alert" table would have bought none of that.

KNOWN GAP, stated rather than hidden: nothing in the platform writes
``FreshnessObservation`` rows. Reading ``max(<watermark column>)`` means
executing against the live source, which is a connector capability that does
not exist (no connector in `aida/connectors/` mentions a watermark, and the
stored profile snapshots are value-free by ADR-0014, so no max timestamp can
be derived from them either). The sweep therefore judges approved contracts
against whatever observations exist; an ACTIVE contract the platform has
never seen a watermark for evaluates STALE and says exactly that in the
incident summary, which is the honest reading of "this contract is approved
and nothing is proving it".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.events import record_audit, record_outbox
from aida.models import (
    DataQualityIncident,
    FreshnessObservation,
    FreshnessWatermarkConfig,
)
from aida.security import SecurityContext
from aida.timeutil import as_utc

logger = structlog.get_logger(__name__)

#: Worst-to-best, for collapsing a datasource's per-table states into the one
#: value `quality_api.quality_summary` reports. Same ordering, and the same
#: reason for it, as `policy_resource_attributes._FRESHNESS_STATE_ORDER`.
_FRESHNESS_STATE_ORDER = {
    "STALE": 3,
    "AWAITING_APPROVAL": 2,
    "NOT_CONFIGURED": 1,
    "FRESH": 0,
}

#: The sweep's own control name in the shared incident sink. One control per
#: table, so one incident per table -- see `_incident_fingerprint`.
FRESHNESS_ANOMALY_TYPE = "FRESHNESS_VIOLATION"

#: Who the scheduler is when it opens or resolves one of these.
FRESHNESS_SCHEDULER_PRINCIPAL = "freshness-scheduler"

#: Ceiling on contracts one sweep reads when no caller supplies one. A large
#: estate degrades to "evaluated the first N contracts", never an unbounded
#: scan -- the same rule `classification_propagation_max_edges` states.
DEFAULT_FRESHNESS_SWEEP_LIMIT = 500


@dataclass(frozen=True, slots=True)
class WatermarkConfig:
    """Configuration for a table's freshness watermark."""

    table_id: str
    watermark_column: str
    classification: str  # e.g. "CONFIDENTIAL", "INTERNAL", "PUBLIC"
    threshold_minutes: int
    retention_days: int
    approved_by: str | None = None
    approved_at: datetime | None = None
    status: str = "PENDING_APPROVAL"  # PENDING_APPROVAL, ACTIVE, DISABLED


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    """Deterministic freshness evaluation output."""

    status: str  # FRESH, STALE, NOT_CONFIGURED, AWAITING_APPROVAL
    last_watermark: datetime | None
    age_minutes: float | None
    threshold_minutes: int | None
    evidence: dict[str, Any] = field(default_factory=dict)


def evaluate_freshness(
    config: WatermarkConfig | None,
    latest_watermark: datetime | None,
    *,
    evaluation_time: datetime | None = None,
) -> FreshnessResult:
    """Evaluate freshness from watermark configuration and latest observed watermark.

    This function ONLY uses the actual data watermark timestamp, never the
    scan/observation time. ADR-0016: scan age is NEVER presented as freshness.
    """
    if config is None:
        return FreshnessResult(
            status="NOT_CONFIGURED",
            last_watermark=None,
            age_minutes=None,
            threshold_minutes=None,
            evidence={"reason": "no freshness configuration for this table"},
        )

    if config.status == "PENDING_APPROVAL":
        return FreshnessResult(
            status="AWAITING_APPROVAL",
            last_watermark=None,
            age_minutes=None,
            threshold_minutes=config.threshold_minutes,
            evidence={
                "reason": "watermark configuration awaiting maker-checker approval",
                "table_id": config.table_id,
            },
        )

    if config.status == "DISABLED":
        return FreshnessResult(
            status="NOT_CONFIGURED",
            last_watermark=None,
            age_minutes=None,
            threshold_minutes=None,
            evidence={"reason": "freshness monitoring is disabled for this table"},
        )

    now = evaluation_time or datetime.now(UTC)

    if latest_watermark is None:
        return FreshnessResult(
            status="STALE",
            last_watermark=None,
            age_minutes=None,
            threshold_minutes=config.threshold_minutes,
            evidence={
                "reason": "no watermark observation recorded",
                "watermark_column": config.watermark_column,
                "table_id": config.table_id,
            },
        )

    # `as_utc`: a watermark read back from SQLite loses its tzinfo (every other
    # backend round-trips it aware), and a naive/aware subtraction raises rather
    # than compares -- the same fix `workspace_service._expired` and
    # `asset_certification.asset_certification_is_active` already needed.
    age_minutes = (as_utc(now) - as_utc(latest_watermark)).total_seconds() / 60.0
    is_fresh = age_minutes <= config.threshold_minutes

    return FreshnessResult(
        status="FRESH" if is_fresh else "STALE",
        last_watermark=latest_watermark,
        age_minutes=round(age_minutes, 2),
        threshold_minutes=config.threshold_minutes,
        evidence={
            "watermark_column": config.watermark_column,
            "table_id": config.table_id,
            "classification": config.classification,
            "evaluation_source": "data_watermark",
        },
    )


def watermark_config_from_row(row: FreshnessWatermarkConfig) -> WatermarkConfig:
    """Adapt a persisted contract into the pure evaluator's input shape.

    The same nine-line transcription was written out at each call site; the
    evaluator's contract is what it is, so the adapter is shared rather than
    re-typed once more here.
    """
    return WatermarkConfig(
        table_id=str(row.table_id),
        watermark_column=row.watermark_column,
        classification=row.classification,
        threshold_minutes=row.threshold_minutes,
        retention_days=row.retention_days,
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        status=row.status,
    )


def worst_freshness_state(statuses: list[str]) -> str:
    """Collapse per-table freshness states to the worst one present.

    An empty estate is NOT_CONFIGURED -- which is what "no table here has a
    freshness contract" means, and emphatically not FRESH. A rolled-up answer
    that looked better than its worst member would be the same class of
    mistake ADR-0016 forbids: a reassuring number nothing measured.
    """
    if not statuses:
        return "NOT_CONFIGURED"
    return max(statuses, key=lambda status: _FRESHNESS_STATE_ORDER.get(status, 0))


@dataclass(frozen=True, slots=True)
class FreshnessStates:
    """One datasource's freshness verdicts, and whether the bound cut them off."""

    results: list[tuple[FreshnessWatermarkConfig, FreshnessResult]]
    truncated: bool

    @property
    def rolled_up_status(self) -> str:
        return worst_freshness_state([result.status for _, result in self.results])


async def _latest_watermarks(
    session: AsyncSession, *, datasource_id: UUID, table_ids: set[UUID]
) -> dict[UUID, datetime]:
    """The most recently *observed* watermark value per table, in one query.

    Ranked with a window function rather than fetched per table: the sweep is
    bounded by table count, so a per-table query would be N round trips for a
    result one statement already carries (the shape
    `custom_quality_rules._latest_profiles` uses).
    """
    if not table_ids:
        return {}
    ranked = (
        select(
            FreshnessObservation.table_id.label("table_id"),
            FreshnessObservation.watermark_value.label("watermark_value"),
            func.row_number()
            .over(
                partition_by=FreshnessObservation.table_id,
                order_by=FreshnessObservation.observed_at.desc(),
            )
            .label("position"),
        )
        .where(
            FreshnessObservation.datasource_id == datasource_id,
            FreshnessObservation.table_id.in_(table_ids),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(ranked.c.table_id, ranked.c.watermark_value).where(ranked.c.position == 1)
        )
    ).all()
    return {table_id: watermark for table_id, watermark in rows}


async def load_freshness_states(
    session: AsyncSession,
    *,
    datasource_id: UUID,
    now: datetime | None = None,
    limit: int = DEFAULT_FRESHNESS_SWEEP_LIMIT,
) -> FreshnessStates:
    """Evaluate every freshness contract configured on one datasource.

    Contracts of every status are read, not just ACTIVE ones. A config that
    was edited back to PENDING_APPROVAL (the upsert endpoint resets approval
    on every edit) or disabled still has to be *seen*, or an incident opened
    while it was active would stay open with nothing left to close it. The
    pure evaluator already answers AWAITING_APPROVAL / NOT_CONFIGURED for
    those, so the caller gets a verdict for each rather than a silent gap.
    """
    effective_now = now or datetime.now(UTC)
    rows = list(
        (
            await session.scalars(
                select(FreshnessWatermarkConfig)
                .where(FreshnessWatermarkConfig.datasource_id == datasource_id)
                .order_by(FreshnessWatermarkConfig.created_at.asc())
                .limit(limit + 1)
            )
        ).all()
    )
    truncated = len(rows) > limit
    if truncated:
        rows = rows[:limit]
    watermarks = await _latest_watermarks(
        session, datasource_id=datasource_id, table_ids={row.table_id for row in rows}
    )
    results = [
        (
            row,
            evaluate_freshness(
                watermark_config_from_row(row),
                watermarks.get(row.table_id),
                evaluation_time=effective_now,
            ),
        )
        for row in rows
    ]
    return FreshnessStates(results=results, truncated=truncated)


@dataclass(frozen=True, slots=True)
class FreshnessSweep:
    """What one datasource's sweep did, for the audit record and the caller."""

    contracts_evaluated: int
    incidents_opened: int
    incidents_updated: int
    incidents_resolved: int
    truncated: bool

    def as_details(self) -> dict[str, Any]:
        return {
            "contracts_evaluated": self.contracts_evaluated,
            "incidents_opened": self.incidents_opened,
            "incidents_updated": self.incidents_updated,
            "incidents_resolved": self.incidents_resolved,
            "truncated": self.truncated,
        }


def _incident_fingerprint(organization_id: UUID, table_id: UUID) -> str:
    """One fingerprint per (organization, table) freshness control.

    Identical in shape to `quality_service._incident_fingerprint`'s
    ``org:table:anomaly_type`` material -- deliberately, so the sink's UNIQUE
    constraint on ``fingerprint`` makes reopening the same row the only
    possible outcome for a table that goes stale, recovers and goes stale
    again. A table can never accumulate a second freshness incident.
    """
    material = f"{organization_id}:{table_id}:{FRESHNESS_ANOMALY_TYPE}".encode()
    return hashlib.sha256(material).hexdigest()


def _severity(age_minutes: float | None, threshold_minutes: int) -> str:
    """CRITICAL at twice the threshold, matching `data_quality._severity`.

    An approved contract with no observation at all has no age to judge, so it
    is a WARNING: "nothing is proving this" is a weaker claim than "this is
    provably hours late", and overstating it would be the mistake ADR-0016
    guards against in the other direction.
    """
    if age_minutes is None:
        return "WARNING"
    return "CRITICAL" if age_minutes >= threshold_minutes * 2 else "WARNING"


def _violation_summary(
    table_id: UUID, result: FreshnessResult, threshold_minutes: int
) -> str:
    if result.age_minutes is None:
        return (
            "Freshness contract is approved but no watermark has ever been observed "
            f"for table {table_id}."
        )
    return (
        f"Watermark is {round(result.age_minutes)} minutes old, past the "
        f"{threshold_minutes}-minute freshness threshold."
    )


async def evaluate_freshness_for_datasource(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    context: SecurityContext,
    now: datetime | None = None,
    max_tables: int = DEFAULT_FRESHNESS_SWEEP_LIMIT,
) -> FreshnessSweep:
    """Evaluate one datasource's freshness contracts into the incident sink.

    The whole lifecycle, in one pass:

    * STALE and no incident yet -> open one (``FRESHNESS_VIOLATION``).
    * STALE and one already open -> update it in place: severity re-derived
      from the current age, evidence replaced, ``last_observed_at`` moved and
      ``occurrence_count`` incremented. No second row.
    * STALE and the last one was RESOLVED -> reopen that same row (counted as
      an open), because the fingerprint is unique per table.
    * FRESH -> **resolve** the open incident. Recovery closes it; it does not
      leave it open for a person to notice. This is the acceptance condition
      the tracker row states, and the one thing a naive "alert on violation"
      pass gets wrong.
    * AWAITING_APPROVAL / NOT_CONFIGURED (a contract edited back to pending,
      or disabled) -> resolve any incident it left behind, with a reason that
      says the contract stopped being active rather than the data recovered.
      Neither state can ever *open* one: an unapproved contract is not a
      control, and never opening on it is what keeps maker-checker meaningful.

    Returns the counts; the caller commits.
    """
    effective_now = now or datetime.now(UTC)
    states = await load_freshness_states(
        session, datasource_id=datasource_id, now=effective_now, limit=max_tables
    )
    opened = updated = resolved = 0

    if states.results:
        existing = (
            await session.scalars(
                select(DataQualityIncident).where(
                    DataQualityIncident.datasource_id == datasource_id,
                    DataQualityIncident.anomaly_type == FRESHNESS_ANOMALY_TYPE,
                    DataQualityIncident.table_id.in_(
                        {row.table_id for row, _ in states.results}
                    ),
                )
            )
        ).all()
    else:
        existing = []
    incident_by_table = {incident.table_id: incident for incident in existing}

    for config_row, result in states.results:
        incident = incident_by_table.get(config_row.table_id)

        if result.status == "STALE":
            severity = _severity(result.age_minutes, config_row.threshold_minutes)
            evidence = {
                **result.evidence,
                "freshness_status": result.status,
                "age_minutes": result.age_minutes,
                "threshold_minutes": config_row.threshold_minutes,
                "last_watermark": result.last_watermark.isoformat()
                if result.last_watermark
                else None,
                "evaluated_at": effective_now.isoformat(),
            }
            summary = _violation_summary(
                config_row.table_id, result, config_row.threshold_minutes
            )
            if incident is None:
                session.add(
                    DataQualityIncident(
                        organization_id=organization_id,
                        datasource_id=datasource_id,
                        table_id=config_row.table_id,
                        policy_id=None,
                        fingerprint=_incident_fingerprint(
                            organization_id, config_row.table_id
                        ),
                        anomaly_type=FRESHNESS_ANOMALY_TYPE,
                        severity=severity,
                        status="OPEN",
                        summary=summary,
                        evidence=evidence,
                        first_observed_at=effective_now,
                        last_observed_at=effective_now,
                    )
                )
                opened += 1
                continue
            reopened = incident.status == "RESOLVED"
            incident.severity = severity
            incident.status = "OPEN"
            incident.summary = summary
            incident.evidence = evidence
            incident.last_observed_at = effective_now
            incident.occurrence_count += 1
            incident.resolved_by = None
            incident.resolved_at = None
            incident.resolution_reason = None
            if reopened:
                opened += 1
            else:
                updated += 1
            continue

        if incident is None or incident.status == "RESOLVED":
            continue

        incident.status = "RESOLVED"
        incident.resolved_by = FRESHNESS_SCHEDULER_PRINCIPAL
        incident.resolved_at = effective_now
        incident.resolution_reason = (
            "Watermark returned within the configured freshness threshold."
            if result.status == "FRESH"
            else "Freshness contract is no longer active for this table."
        )
        resolved += 1

    sweep = FreshnessSweep(
        contracts_evaluated=len(states.results),
        incidents_opened=opened,
        incidents_updated=updated,
        incidents_resolved=resolved,
        truncated=states.truncated,
    )
    await session.flush()
    record_audit(
        session,
        context,
        action="data_quality.freshness.evaluate",
        resource_type="datasource",
        resource_id=str(datasource_id),
        outcome="SUCCESS",
        correlation_id=str(datasource_id),
        details=sweep.as_details(),
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="datasource",
        aggregate_id=str(datasource_id),
        event_type="data_quality.freshness.evaluated.v1",
        payload={"datasource_id": str(datasource_id), **sweep.as_details()},
    )
    return sweep
