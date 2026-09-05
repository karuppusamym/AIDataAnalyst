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
"""

from __future__ import annotations

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

    @property
    def breaching_object_types(self) -> tuple[str, ...]:
        return tuple(row.object_type for row in self.by_object_type if row.breaches_revisit_trigger)


def _rate(agreed: int, disagreed: int) -> float | None:
    resolved = agreed + disagreed
    if resolved == 0:
        return None
    return disagreed / resolved


async def disagreement_rates(
    session: AsyncSession,
    organization_id: UUID,
    *,
    window_days: int = REVISIT_TRIGGER_WINDOW_DAYS,
    now: datetime | None = None,
) -> DisagreementReport:
    """The revisit trigger's metric, per object type, over one window.

    One grouped query regardless of how many object types or samples exist.
    """
    moment = now or datetime.now(UTC)
    since = moment - timedelta(days=window_days)

    rows = (
        await session.execute(
            select(
                ReviewAuditSample.object_type,
                ReviewAuditSample.human_outcome,
                func.count(),
            )
            .where(
                ReviewAuditSample.organization_id == organization_id,
                ReviewAuditSample.sampled_at >= since,
            )
            .group_by(ReviewAuditSample.object_type, ReviewAuditSample.human_outcome)
        )
    ).all()

    counts: dict[str, dict[str, int]] = {}
    for object_type, outcome, count in rows:
        counts.setdefault(object_type, {})[outcome] = int(count)

    by_object_type: list[DisagreementRate] = []
    for object_type in sorted(counts):
        outcomes = counts[object_type]
        agreed = outcomes.get("AGREED", 0)
        disagreed = outcomes.get("DISAGREED", 0)
        pending = outcomes.get("PENDING", 0)
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

    return DisagreementReport(
        window_days=window_days,
        computed_at=moment,
        measured=any(row.resolved > 0 for row in by_object_type),
        threshold=REVISIT_TRIGGER_DISAGREEMENT_RATE,
        minimum_resolved_for_signal=MINIMUM_RESOLVED_FOR_SIGNAL,
        by_object_type=tuple(by_object_type),
    )
