"""NT-1: relay REVIEW_REQUESTED without inventing a funnel that does not exist.

Six of NT-1's seven event kinds have one place they happen -- the review
decision core, the agent kill switch, quality incident routing, the
certification expiry sweep -- so a call at that point covers every case.
`REVIEW_REQUESTED` does not. A `GovernanceReview` is constructed at 27 sites
across 17 modules (glossary publication, semantic model versions, tool
approvals, marketplace listings, mined query proposals, classification
propagation, ...), each inside its own domain transaction, and no two of them
share an entry point.

The tempting fix is to add one: a `request_governance_review` helper and 27
edits. That is a large refactor of the platform's most safety-critical write
path, undertaken so that a Slack message can be sent, and it would put a
network call inside 27 governance transactions.

This module takes the other road. It sweeps for reviews that have not yet been
considered for a notification, notifies, and stamps a watermark. Three
properties follow from that shape, and they are why it is not merely the
cheaper option:

* **It cannot affect the decision.** The sweep runs in its own session, after
  the governance transaction has already committed. There is no path by which
  a slow or failing webhook delays or rolls back an approval request.
* **Nothing is lost to a downed channel.** The watermark is stamped in the
  same transaction as the delivery attempt, so a crashed sweep simply retries
  the same rows next pass.
* **It covers sites that do not exist yet.** A 28th `GovernanceReview(...)`
  written next month is relayed with no change here and no change there.

What it costs, stated plainly: notification is not immediate. It lags by one
scheduler iteration. For an approval queue a human works through this is the
right trade; for anything time-critical it would not be.

**Off by default.** With `governance_notifications_enabled` false the sweep
returns immediately and stamps nothing, so enabling the feature later delivers
the recent backlog rather than a silent gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import session_factory
from aida.governance_notifications import notify_safely
from aida.models import GovernanceReview
from aida.review_risk_tiers import risk_tier_for

#: The status a review must be in to be worth telling anyone about. A review
#: created and decided between two sweeps is never announced, which is
#: correct: "please approve this" about something already approved is noise.
_NOTIFIABLE_STATUS = "PENDING"


@dataclass(frozen=True, slots=True)
class RelayOutcome:
    """What one sweep did, returned for logging and for the tests."""

    notified: tuple[UUID, ...]
    #: Considered but deliberately not sent -- too old to be news. Stamped all
    #: the same, so they are not re-examined forever.
    skipped_stale: tuple[UUID, ...]

    @property
    def examined(self) -> int:
        return len(self.notified) + len(self.skipped_stale)


def _payload(review: GovernanceReview) -> dict[str, object]:
    """The message body's fields.

    Value-free by construction (INV-6): an object type, an id, who asked, and
    the risk tier. `render_message` is an allowlist as well, so this is the
    second of two barriers rather than the only one -- but a caller assembling
    the payload is the natural place for a value to slip in, so it is worth
    being explicit here too. Notably absent: `decision_reason`, which is
    free text a human wrote and could name anything.
    """
    return {
        "object_type": review.object_type,
        "object_id": str(review.id),
        "risk_tier": review.risk_tier or risk_tier_for(review.object_type, {}),
        "principal_id": review.requested_by,
        "occurred_at": review.created_at.isoformat() if review.created_at else "",
    }


async def relay_review_requested(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> RelayOutcome:
    """Notify for every pending review not yet considered, and stamp them.

    Does not commit -- the caller owns the transaction, matching every other
    sweep in this codebase.
    """
    settings = settings or get_settings()
    if not settings.governance_notifications_enabled:
        # Stamp nothing. An organization that turns notifications on tomorrow
        # should receive today's pending approvals, not discover that the
        # sweep quietly consumed them while the feature was off.
        return RelayOutcome(notified=(), skipped_stale=())

    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=settings.governance_review_notify_max_age_hours)

    reviews = (
        await session.scalars(
            select(GovernanceReview)
            .where(
                GovernanceReview.status == _NOTIFIABLE_STATUS,
                GovernanceReview.review_requested_notified_at.is_(None),
            )
            .order_by(GovernanceReview.created_at)
            .limit(settings.governance_review_notify_batch_size)
        )
    ).all()

    notified: list[UUID] = []
    skipped: list[UUID] = []
    for review in reviews:
        created_at = review.created_at
        if created_at is not None and created_at.tzinfo is None:
            # SQLite hands back naive datetimes for a timezone-aware column;
            # comparing one to an aware `cutoff` raises rather than returning
            # a wrong answer, so normalise before the comparison.
            created_at = created_at.replace(tzinfo=UTC)
        if created_at is not None and created_at < cutoff:
            skipped.append(review.id)
        else:
            await notify_safely(
                session,
                review.organization_id,
                "REVIEW_REQUESTED",
                _payload(review),
                settings=settings,
            )
            notified.append(review.id)
        # Stamped either way, and in the same transaction as the delivery
        # attempt: a sweep that dies mid-batch retries exactly the rows it did
        # not reach.
        review.review_requested_notified_at = now

    return RelayOutcome(notified=tuple(notified), skipped_stale=tuple(skipped))


async def run_review_notification_pass(
    settings: Settings, *, now: datetime | None = None
) -> RelayOutcome:
    """Scheduler entry point. Opens its own session, same shape as
    `run_certification_expiry_warning_pass`."""
    if not settings.governance_notifications_enabled:
        return RelayOutcome(notified=(), skipped_stale=())
    async with session_factory() as session, session.begin():
        return await relay_review_requested(session, settings=settings, now=now)
