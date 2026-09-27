"""ADR-0027: the sampled disagreement rate its revisit trigger watches.

The ADR commits to revisiting the decision when **the sampled disagreement
rate exceeds 5% for any object type over a full month**. That commitment was
unfalsifiable while nothing computed the number: a revisit trigger nobody can
evaluate is a sentence in a document, not a control.

This module computes it. It does not decide anything -- suspension stays a
human action, deliberately, because a metric that suspended the agent by
itself would be a second automated authority arriving through the back door
of an observability module.

Three things it refuses to do, each of which would make the number worse than
useless:

**It never reports a rate it cannot support.** One disagreement out of two
resolved samples is 50%, and it means nothing. Below
`MINIMUM_RESOLVED_FOR_SIGNAL` the rate is still shown -- hiding it would be
its own dishonesty -- but `breaches_revisit_trigger` stays false and
`sufficient_sample` says why. The floor is 20 because at a 5% threshold that
is the smallest sample in which a single disagreement is *at* the threshold
rather than four times over it.

**It never counts an unresolved sample as agreement.** A sample nobody has
looked at is not evidence the agent was right. `pending` is reported
separately, and a large pending count with a small resolved count is itself
the finding: the sampling floor is producing work nobody is doing, which
means condition (b) of ADR-0027 is not actually being met.

**It never treats "no data" as "passing".** With the feature off -- which is
every environment today -- every rate is `None` and `measured` is false. That
is the honest report, and it is what this module says.

**AR-11 added a second cut and a clock.** The rate by *risk tier* as well as
by object type: only approvals are sampled, so a DISAGREED is a human saying
an approval was wrong, and the tier slice is the sampled false-approval rate
per risk class. And how long the sample waits for a human: the median,
90th-percentile and slowest time from sampling to a verdict, plus the age of
the oldest sample still unread. Condition (b) rests on humans reading the
sample; this is how long they take and how far behind they are.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import ReviewAuditSample

#: ADR-0027's revisit trigger, verbatim: more than 5% disagreement on any one
#: object type over a full month.
REVISIT_TRIGGER_DISAGREEMENT_RATE = 0.05

#: The window the trigger is stated over.
REVISIT_TRIGGER_WINDOW_DAYS = 30

#: Fewer resolved samples than this and the rate is not a signal. See the
#: module docstring for why 20 and not some rounder number.
MINIMUM_RESOLVED_FOR_SIGNAL = 20


@dataclass(frozen=True, slots=True)
class DisagreementRate:
    """One object type's slice of the trigger."""

    object_type: str
    #: Every sample taken in the window, resolved or not.
    sampled: int
    resolved: int
    agreed: int
    disagreed: int
    #: Sampled but not yet judged. Counted separately and never folded into
    #: agreement -- an unexamined sample is an open question, not a pass.
    pending: int
    #: `None` when nothing has been resolved. Zero would claim a measurement.
    disagreement_rate: float | None
    #: Whether `resolved` reaches `MINIMUM_RESOLVED_FOR_SIGNAL`.
    sufficient_sample: bool
    #: The ADR's condition: rate above threshold **and** enough samples to
    #: mean it. Both halves are required, and a caller that wants the raw
    #: rate has it in the field above.
    breaches_revisit_trigger: bool


@dataclass(frozen=True, slots=True)
class RiskTierDisagreementRate:
    """One risk tier's slice (AR-11).

    Only APPROVED decisions are sampled (`reviewer_agent.auto_decide_tier0_tier1`),
    so every DISAGREED here is a human saying an approval should not have
    happened: `disagreement_rate` is the sampled false-approval rate for the
    tier. The revisit trigger is stated per object type, so a tier slice never
    breaches it on its own; it is the cut that shows whether the agent's
    mistakes gather where the blast radius is largest.
    """

    risk_tier: str
    sampled: int
    resolved: int
    agreed: int
    disagreed: int
    pending: int
    disagreement_rate: float | None
    sufficient_sample: bool


@dataclass(frozen=True, slots=True)
class AuditResolutionTime:
    """How long the sample waits for a human, and how far behind humans are (AR-11)."""

    #: Samples taken in the window that a human has since resolved.
    resolved: int
    #: Hours from sampling to verdict over those. `None` when nothing is
    #: resolved: the median of nothing is not zero.
    median_hours: float | None
    p90_hours: float | None
    max_hours: float | None
    #: Every unread sample the organization has, whatever the window -- the
    #: backlog `reviewer_agent_max_unresolved_samples` bounds.
    pending: int
    #: Age of the oldest of them, or `None` when there is none.
    oldest_pending_hours: float | None


@dataclass(frozen=True, slots=True)
class DisagreementReport:
    window_days: int
    computed_at: datetime
    #: False when no sample in the window has been resolved at all -- which is
    #: the state of every environment while the reviewer agent is off. A
    #: report with `measured=False` is not evidence that the agent is
    #: performing well.
    measured: bool
    threshold: float
    minimum_resolved_for_signal: int
    by_object_type: tuple[DisagreementRate, ...]
    by_risk_tier: tuple[RiskTierDisagreementRate, ...]
    resolution: AuditResolutionTime

    @property
    def breaching_object_types(self) -> tuple[str, ...]:
        return tuple(row.object_type for row in self.by_object_type if row.breaches_revisit_trigger)


def _rate(agreed: int, disagreed: int) -> float | None:
    resolved = agreed + disagreed
    if resolved == 0:
        return None
    return disagreed / resolved


def _counts(outcomes: dict[str, int]) -> tuple[int, int, int]:
    """(agreed, disagreed, pending) out of one slice's outcome counts."""
    return outcomes.get("AGREED", 0), outcomes.get("DISAGREED", 0), outcomes.get("PENDING", 0)


def _aware(value: datetime) -> datetime:
    """SQLite hands datetimes back naive; every one stored here is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 2)


def _percentile(ordered: list[float], fraction: float) -> float:
    """Nearest-rank percentile of an ascending, non-empty list."""
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


async def _resolution_time(
    session: AsyncSession, organization_id: UUID, *, since: datetime, moment: datetime
) -> AuditResolutionTime:
    pairs = (
        await session.execute(
            select(ReviewAuditSample.sampled_at, ReviewAuditSample.resolved_at).where(
                ReviewAuditSample.organization_id == organization_id,
                ReviewAuditSample.sampled_at >= since,
                ReviewAuditSample.resolved_at.is_not(None),
            )
        )
    ).all()
    waits = sorted(
        _hours(_aware(resolved_at) - _aware(sampled_at))
        for sampled_at, resolved_at in pairs
        if resolved_at is not None
    )
    pending, oldest = (
        await session.execute(
            select(func.count(), func.min(ReviewAuditSample.sampled_at)).where(
                ReviewAuditSample.organization_id == organization_id,
                ReviewAuditSample.human_outcome == "PENDING",
            )
        )
    ).one()
    return AuditResolutionTime(
        resolved=len(waits),
        median_hours=round(statistics.median(waits), 2) if waits else None,
        p90_hours=_percentile(waits, 0.9) if waits else None,
        max_hours=waits[-1] if waits else None,
        pending=int(pending or 0),
        oldest_pending_hours=_hours(moment - _aware(oldest)) if oldest is not None else None,
    )


async def disagreement_rates(
    session: AsyncSession,
    organization_id: UUID,
    *,
    window_days: int = REVISIT_TRIGGER_WINDOW_DAYS,
    now: datetime | None = None,
) -> DisagreementReport:
    """The revisit trigger's metric, per object type and per risk tier, over
    one window -- with how long the sample took to read.

    Three queries regardless of how many object types, tiers or samples exist.
    """
    moment = now or datetime.now(UTC)
    since = moment - timedelta(days=window_days)

    rows = (
        await session.execute(
            select(
                ReviewAuditSample.object_type,
                ReviewAuditSample.risk_tier,
                ReviewAuditSample.human_outcome,
                func.count(),
            )
            .where(
                ReviewAuditSample.organization_id == organization_id,
                ReviewAuditSample.sampled_at >= since,
            )
            .group_by(
                ReviewAuditSample.object_type,
                ReviewAuditSample.risk_tier,
                ReviewAuditSample.human_outcome,
            )
        )
    ).all()

    by_type: dict[str, dict[str, int]] = {}
    by_tier: dict[str, dict[str, int]] = {}
    for object_type, risk_tier, outcome, count in rows:
        for slices, key in ((by_type, object_type), (by_tier, risk_tier)):
            outcomes = slices.setdefault(key, {})
            outcomes[outcome] = outcomes.get(outcome, 0) + int(count)

    by_object_type: list[DisagreementRate] = []
    for object_type in sorted(by_type):
        agreed, disagreed, pending = _counts(by_type[object_type])
        resolved = agreed + disagreed
        rate = _rate(agreed, disagreed)
        sufficient = resolved >= MINIMUM_RESOLVED_FOR_SIGNAL
        by_object_type.append(
            DisagreementRate(
                object_type=object_type,
                sampled=resolved + pending,
                resolved=resolved,
                agreed=agreed,
                disagreed=disagreed,
                pending=pending,
                disagreement_rate=rate,
                sufficient_sample=sufficient,
                breaches_revisit_trigger=bool(
                    sufficient and rate is not None and rate > REVISIT_TRIGGER_DISAGREEMENT_RATE
                ),
            )
        )

    by_risk_tier: list[RiskTierDisagreementRate] = []
    for risk_tier in sorted(by_tier):
        agreed, disagreed, pending = _counts(by_tier[risk_tier])
        resolved = agreed + disagreed
        by_risk_tier.append(
            RiskTierDisagreementRate(
                risk_tier=risk_tier,
                sampled=resolved + pending,
                resolved=resolved,
                agreed=agreed,
                disagreed=disagreed,
                pending=pending,
                disagreement_rate=_rate(agreed, disagreed),
                sufficient_sample=resolved >= MINIMUM_RESOLVED_FOR_SIGNAL,
            )
        )

    return DisagreementReport(
        window_days=window_days,
        computed_at=moment,
        measured=any(row.resolved > 0 for row in by_object_type),
        threshold=REVISIT_TRIGGER_DISAGREEMENT_RATE,
        minimum_resolved_for_signal=MINIMUM_RESOLVED_FOR_SIGNAL,
        by_object_type=tuple(by_object_type),
        by_risk_tier=tuple(by_risk_tier),
        resolution=await _resolution_time(
            session, organization_id, since=since, moment=moment
        ),
    )
