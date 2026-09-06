"""NT-1 / F12: the REVIEW_REQUESTED relay, and its watermark.

This was the one event kind NT-1 shipped without a real path to a channel,
because review creation has 27 call sites and no funnel to hook. The relay
sweeps a watermark column instead. What the tests below hold is that the sweep
is safe to run on a schedule against a live estate:

* off by default, and while off it stamps **nothing**, so turning the feature
  on delivers the recent backlog rather than discovering a silent gap;
* idempotent -- a second pass over the same rows queues nothing;
* bounded in both directions: at most a batch per pass, and nothing older than
  the age window, so first-enable does not flood a channel with history;
* it notifies about pending reviews only, never about ones already decided;
* the payload is value-free.

And the F12 property that was missing (`Docs/review-2026-09-05/REVIEW.md`):
**the watermark is stamped by intent creation, never by a delivery.** The relay
now creates a durable `DeliveryIntent` in the same transaction as the stamp,
and a separate worker pass talks to the destination. A destination that is
down when the sweep runs therefore cannot cause a review nobody is ever told
about -- which is exactly what happened when `notify_safely` swallowed the
transport error and the sweep stamped anyway.

Deliveries here go to a real loopback HTTP server, not a patched client.
"""

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.delivery_intents import KIND_NOTIFICATION, run_delivery_worker_pass
from aida.governance_notifications import STATUS_FAILED, STATUS_QUEUED, STATUS_SENT
from aida.governance_review_relay import relay_review_requested
from aida.models import DeliveryIntent, GovernanceReview, NotificationEventRecord, Organization
from tests.support.stub_servers import WebhookStub

pytestmark = pytest.mark.asyncio

SENTINEL = "ACME-CUSTOMER-4471"


@pytest.fixture
def channel() -> Iterator[WebhookStub]:
    """A real Slack-shaped webhook endpoint on loopback."""
    with WebhookStub() as stub:
        yield stub


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


@pytest.fixture
def settings_for(channel: WebhookStub) -> Any:
    def _build(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "environment": "test",
            "governance_notifications_enabled": True,
            "slack_webhook_url": channel.url,
            "teams_webhook_url": None,
            "portal_base_url": "https://atlas.example",
            "delivery_worker_enabled": True,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)  # type: ignore[arg-type]

    return _build


async def _deliver(session: AsyncSession, settings: Settings) -> Any:
    """Run the worker the fleet scheduler runs, against this test's session."""
    await session.commit()
    return await run_delivery_worker_pass(settings, session=session, owner="relay-test")


async def _seed_org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _seed_review(
    session: AsyncSession,
    org: Organization,
    *,
    status: str = "PENDING",
    age: timedelta = timedelta(minutes=5),
    object_type: str = "GLOSSARY_TERM",
    requested_by: str = "steward-1",
) -> GovernanceReview:
    review = GovernanceReview(
        organization_id=org.id,
        object_type=object_type,
        object_id=str(uuid4()),
        requested_action="PUBLISH",
        status=status,
        requested_by=requested_by,
        created_at=datetime.now(UTC) - age,
    )
    session.add(review)
    await session.flush()
    return review


async def _intents(session: AsyncSession) -> list[DeliveryIntent]:
    rows = await session.scalars(
        select(DeliveryIntent).where(DeliveryIntent.kind == KIND_NOTIFICATION)
    )
    return list(rows.all())


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


async def test_disabled_sends_nothing_and_stamps_nothing(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    """Stamping while disabled would consume the backlog silently: an
    organization that enables notifications tomorrow would never hear about
    the approvals raised today."""
    org = await _seed_org(session)
    review = await _seed_review(session, org)

    outcome = await relay_review_requested(
        session, settings=settings_for(governance_notifications_enabled=False)
    )

    assert channel.received == []
    assert outcome.examined == 0
    assert review.review_requested_notified_at is None
    assert await _intents(session) == []


async def test_enabling_later_delivers_the_recent_backlog(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    org = await _seed_org(session)
    await _seed_review(session, org)
    await relay_review_requested(
        session, settings=settings_for(governance_notifications_enabled=False)
    )
    assert channel.received == []

    settings = settings_for()
    outcome = await relay_review_requested(session, settings=settings)
    await _deliver(session, settings)

    assert len(outcome.notified) == 1
    assert len(channel.received) == 1


# ---------------------------------------------------------------------------
# What gets notified
# ---------------------------------------------------------------------------


async def test_a_pending_review_is_notified_once(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    org = await _seed_org(session)
    review = await _seed_review(session, org)
    settings = settings_for()

    first = await relay_review_requested(session, settings=settings)
    second = await relay_review_requested(session, settings=settings)
    await _deliver(session, settings)

    assert first.notified == (review.id,)
    assert second.examined == 0, "a second pass over the same rows queues nothing"
    assert len(channel.received) == 1
    body = channel.received[0]
    assert "Approval requested" in body["text"]
    assert str(review.id) in body["text"]


async def test_an_already_decided_review_is_never_announced(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    """A review raised and approved between two sweeps produces no message.
    "Please approve this" about something already approved is noise, and the
    decision itself has its own event kind."""
    org = await _seed_org(session)
    await _seed_review(session, org, status="APPROVED")

    outcome = await relay_review_requested(session, settings=settings_for())

    assert outcome.examined == 0
    assert await _intents(session) == []


async def test_history_is_stamped_but_not_sent(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    """The day an operator first configures a webhook, a year of pending
    reviews must not all arrive at once -- but they must also stop being
    re-examined on every pass forever."""
    org = await _seed_org(session)
    ancient = await _seed_review(session, org, age=timedelta(days=90))
    fresh = await _seed_review(session, org, age=timedelta(minutes=1))
    settings = settings_for()

    outcome = await relay_review_requested(session, settings=settings)
    await _deliver(session, settings)

    assert outcome.notified == (fresh.id,)
    assert outcome.skipped_stale == (ancient.id,)
    assert len(channel.received) == 1
    assert ancient.review_requested_notified_at is not None, "stamped, so never re-examined"


async def test_the_batch_size_bounds_one_pass(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    org = await _seed_org(session)
    for _ in range(5):
        await _seed_review(session, org)
    settings = settings_for(governance_review_notify_batch_size=2)

    outcome = await relay_review_requested(session, settings=settings)
    await _deliver(session, settings)

    assert outcome.examined == 2
    assert len(channel.received) == 2


async def test_the_oldest_pending_review_is_notified_first(
    session: AsyncSession, settings_for: Any
) -> None:
    """Whoever has waited longest is told about first; a sweep that took the
    newest rows would starve the back of the queue indefinitely."""
    org = await _seed_org(session)
    older = await _seed_review(session, org, age=timedelta(hours=6))
    await _seed_review(session, org, age=timedelta(minutes=1))

    outcome = await relay_review_requested(
        session, settings=settings_for(governance_review_notify_batch_size=1)
    )

    assert outcome.notified == (older.id,)


# ---------------------------------------------------------------------------
# Value freedom and the ledger
# ---------------------------------------------------------------------------


async def test_the_message_carries_no_object_value(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    """INV-6 at the one place the platform sends data outward. The object type
    and the review's own id identify what needs approving; the *content* being
    approved never leaves."""
    org = await _seed_org(session)
    await _seed_review(session, org, object_type="GLOSSARY_TERM", requested_by="steward-1")
    review = await _seed_review(session, org)
    review.decision_reason = f"rejected because it named {SENTINEL}"
    await session.flush()
    settings = settings_for()

    await relay_review_requested(session, settings=settings)
    await _deliver(session, settings)

    for body in channel.received:
        assert SENTINEL not in body["text"]
    for intent in await _intents(session):
        assert SENTINEL not in str(intent.payload)


async def test_the_ledger_reads_queued_then_sent(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    """`NotificationEventRecord` is what the portal reads. It says QUEUED while
    the obligation is durable but unattempted, and SENT only once a destination
    answered -- the distinction the previous implementation could not make,
    because it wrote SENT or FAILED from inside the business transaction and
    then never revisited either."""
    org = await _seed_org(session)
    await _seed_review(session, org)
    settings = settings_for()

    await relay_review_requested(session, settings=settings)
    await session.flush()
    rows = (await session.scalars(select(NotificationEventRecord))).all()
    assert [row.status for row in rows if row.channel == "SLACK"] == [STATUS_QUEUED]
    assert all(row.incident_id is None for row in rows), "a governance event has no incident"

    await _deliver(session, settings)
    # The next sweep reconciles the ledger against the finished intent.
    await relay_review_requested(session, settings=settings)
    await session.commit()

    rows = (await session.scalars(select(NotificationEventRecord))).all()
    slack = [row for row in rows if row.channel == "SLACK"]
    assert [row.status for row in slack] == [STATUS_SENT]
    assert slack[0].sent_at is not None


async def test_a_downed_channel_does_not_lose_the_event_and_recovery_delivers(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    """F12's acceptance criterion. The sweep stamps the watermark because it
    created a durable intent -- not because anything was delivered. A webhook
    that is down at that moment therefore loses nothing: the worker retries and
    the message arrives once the channel is back."""
    org = await _seed_org(session)
    review = await _seed_review(session, org)
    channel.fail_status = 503
    settings = settings_for()

    outcome = await relay_review_requested(session, settings=settings)
    await _deliver(session, settings)

    assert len(outcome.notified) == 1
    assert review.review_requested_notified_at is not None
    intents = await _intents(session)
    assert [intent.state for intent in intents] == ["RETRYING"]
    assert intents[0].delivered_at is None

    # The channel comes back. Nothing re-examines the review; the intent is
    # what carries the obligation forward.
    channel.fail_status = None
    channel.received.clear()
    later = datetime.now(UTC) + timedelta(minutes=30)
    await run_delivery_worker_pass(
        settings, now=later, session=session, owner="relay-test"
    )

    assert len(channel.received) == 1
    intents = await _intents(session)
    assert intents[0].state == "DELIVERED"
    assert intents[0].delivered_at is not None


async def test_a_failed_send_cannot_move_the_watermark_on_its_own(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    """The watermark and the intent are written in one transaction. There is no
    ordering in which a review is stamped without a durable obligation behind
    it, which is what made F12's failure permanent."""
    org = await _seed_org(session)
    review = await _seed_review(session, org)
    channel.fail_status = 500
    settings = settings_for()

    await relay_review_requested(session, settings=settings)
    stamped = review.review_requested_notified_at
    assert stamped is not None

    intents = await _intents(session)
    assert len(intents) == 1
    # Stamp time is intent-creation time, not an attempt or a delivery.
    assert intents[0].attempted_at is None
    assert intents[0].delivered_at is None
    assert intents[0].requested_at is not None

    await _deliver(session, settings)
    intents = await _intents(session)
    assert intents[0].attempted_at is not None
    assert intents[0].delivered_at is None


async def test_a_dead_lettered_notification_is_visible_in_the_ledger(
    session: AsyncSession, channel: WebhookStub, settings_for: Any
) -> None:
    org = await _seed_org(session)
    await _seed_review(session, org)
    channel.fail_status = 400  # permanent: the destination rejects the body
    settings = settings_for()

    await relay_review_requested(session, settings=settings)
    await _deliver(session, settings)
    await relay_review_requested(session, settings=settings)
    await session.commit()

    rows = (await session.scalars(select(NotificationEventRecord))).all()
    assert [row.status for row in rows if row.channel == "SLACK"] == [STATUS_FAILED]
