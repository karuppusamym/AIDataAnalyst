"""The delivery state machine itself: claiming, backoff, dedup, and refusal.

`tests/test_siem_delivery.py` proves the transports reach real destinations.
This module tests the machinery underneath them with a transport double that
is *not* a mock of our own function -- it implements the `DeliveryTransport`
protocol and returns receipts or raises `TransportError`, which is exactly what
a real transport does at that boundary. Anything that depends on bytes actually
crossing a socket is tested against a real server, not here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
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
from aida.delivery_intents import (
    KIND_NOTIFICATION,
    STATE_DELIVERED,
    STATE_DELIVERING,
    STATE_DUPLICATE,
    STATE_PENDING,
    STATE_RETRYING,
    DeliveryOutcome,
    NullTransport,
    TransportError,
    TransportReceipt,
    backoff_seconds,
    claim_due_intents,
    dedup_key_for,
    destination_label,
    enqueue_intent,
    notification_transport_for,
    run_delivery_worker_pass,
)
from aida.models import DeliveryAttempt, DeliveryIntent, Organization

pytestmark = pytest.mark.asyncio


class RecordingTransport:
    """A `DeliveryTransport` that answers however the test tells it to.

    Implements the protocol rather than patching a function: the boundary being
    faked is the destination, not our own code path.
    """

    name = "recording"
    available = True
    destination = "recording://test"

    def __init__(self, *, error: TransportError | None = None) -> None:
        self.error = error
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: Mapping[str, Any]) -> TransportReceipt:
        if self.error is not None:
            raise self.error
        self.sent.append(dict(payload))
        return TransportReceipt(
            destination=self.destination, transport=self.name, detail="accepted"
        )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "test",
        "delivery_worker_enabled": True,
        "delivery_backoff_base_seconds": 2.0,
        "delivery_backoff_max_seconds": 60.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Deterministic clock.
#
# `_queue` used to enqueue at wall-clock time while every claim test asked for
# intents due at a hard-coded `2026-09-06 12:00Z`. That made the whole file a
# time bomb: `claim_due_intents` selects on `next_attempt_at <= now`, so the
# tests passed only while real time was *before* noon UTC on that date and
# failed for every run after it -- which is exactly what happened. A test whose
# result depends on the hour it runs is not a gate.
#
# Both instants are fixed here, and `_queue` defaults to the earlier one, so
# "queued before the claim window" is a property of the fixture rather than of
# the clock.
# ---------------------------------------------------------------------------

QUEUED_AT = datetime(2026, 9, 6, 11, 0, tzinfo=UTC)
CLAIM_AT = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


async def _seed_org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _queue(
    session: AsyncSession,
    org: Organization,
    *,
    dedup: str = "k1",
    now: datetime | None = None,
) -> DeliveryIntent:
    intent = enqueue_intent(
        session,
        organization_id=org.id,
        kind=KIND_NOTIFICATION,
        channel="SLACK",
        destination="https://hooks.example/#abc",
        dedup_key=dedup,
        payload={"text": "hello"},
        now=now or QUEUED_AT,
    )
    assert intent is not None
    await session.commit()
    return intent


async def _run(
    session: AsyncSession,
    settings: Settings,
    transport: Any,
    *,
    now: datetime | None = None,
    owner: str = "worker-a",
) -> Any:
    # Same reason as `_queue`: defaulting to wall-clock made a pass ordered
    # against a fixed `CLAIM_AT` in one test and against the real hour in
    # another, so a retry scheduled from `datetime.now()` was never due at a
    # later fixed instant.
    return await run_delivery_worker_pass(
        settings,
        now=now or CLAIM_AT,
        session=session,
        owner=owner,
        resolvers={KIND_NOTIFICATION: lambda _intent, _settings: transport},
    )


# ---------------------------------------------------------------------------
# Enqueue is I/O-free and belongs to the caller's transaction
# ---------------------------------------------------------------------------


async def test_enqueue_starts_pending_with_nothing_attempted(session: AsyncSession) -> None:
    org = await _seed_org(session)
    intent = await _queue(session, org)
    assert intent.state == STATE_PENDING
    assert intent.attempt_count == 0
    assert intent.attempted_at is None
    assert intent.delivered_at is None
    assert intent.requested_at is not None


async def test_the_three_timestamps_are_three_different_facts(session: AsyncSession) -> None:
    """F12: "one timestamp cannot mean all three"."""
    org = await _seed_org(session)
    requested = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    await _queue(session, org, now=requested)
    attempted = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)

    await _run(session, _settings(), RecordingTransport(), now=attempted)

    intent = await session.scalar(select(DeliveryIntent))
    assert intent is not None
    assert intent.requested_at.replace(tzinfo=UTC) == requested
    assert intent.attempted_at is not None
    assert intent.attempted_at.replace(tzinfo=UTC) == attempted
    assert intent.delivered_at is not None
    assert intent.delivered_at.replace(tzinfo=UTC) == attempted


async def test_a_rolled_back_transaction_leaves_no_obligation(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await session.commit()
    enqueue_intent(
        session,
        organization_id=org.id,
        kind=KIND_NOTIFICATION,
        channel="SLACK",
        destination="d",
        dedup_key="rolled-back",
        payload={},
        now=QUEUED_AT,
    )
    await session.rollback()
    assert (await session.scalars(select(DeliveryIntent))).all() == []


async def test_the_same_session_will_not_stage_the_same_intent_twice(
    session: AsyncSession,
) -> None:
    org = await _seed_org(session)
    first = enqueue_intent(
        session,
        organization_id=org.id,
        kind=KIND_NOTIFICATION,
        channel="SLACK",
        destination="d",
        dedup_key="same",
        payload={},
        now=QUEUED_AT,
    )
    second = enqueue_intent(
        session,
        organization_id=org.id,
        kind=KIND_NOTIFICATION,
        channel="SLACK",
        destination="d",
        dedup_key="same",
        payload={},
        now=QUEUED_AT,
    )
    assert first is not None
    assert second is None


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


async def test_a_claim_is_committed_before_anything_is_sent(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await _queue(session, org)
    now = CLAIM_AT

    claimed = await claim_due_intents(
        session,
        kinds=(KIND_NOTIFICATION,),
        owner="worker-a",
        now=now,
        batch_size=10,
        claim_seconds=300,
    )

    assert len(claimed) == 1
    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    async with maker() as other:
        row = await other.scalar(select(DeliveryIntent))
        assert row is not None
        assert row.state == STATE_DELIVERING
        assert row.claimed_by == "worker-a"


async def test_two_workers_get_disjoint_batches(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await _queue(session, org, dedup="a")
    await _queue(session, org, dedup="b")
    now = CLAIM_AT

    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    async with maker() as one, maker() as two:
        first = await claim_due_intents(
            one, kinds=(KIND_NOTIFICATION,), owner="w1", now=now, batch_size=10, claim_seconds=300
        )
        second = await claim_due_intents(
            two, kinds=(KIND_NOTIFICATION,), owner="w2", now=now, batch_size=10, claim_seconds=300
        )

    assert len(first) == 2
    assert second == [], "the compare-and-set update, not a prior select, arbitrates"


async def test_an_expired_claim_is_reclaimable(session: AsyncSession) -> None:
    """How a worker killed mid-attempt releases its work. Expiry, not a
    heartbeat, because a claim must survive the connection that took it."""
    org = await _seed_org(session)
    await _queue(session, org)
    now = CLAIM_AT
    await claim_due_intents(
        session, kinds=(KIND_NOTIFICATION,), owner="dead", now=now, batch_size=10, claim_seconds=60
    )

    reclaimed = await claim_due_intents(
        session,
        kinds=(KIND_NOTIFICATION,),
        owner="alive",
        now=now + timedelta(minutes=5),
        batch_size=10,
        claim_seconds=60,
    )
    assert len(reclaimed) == 1
    assert reclaimed[0].claimed_by == "alive"


# ---------------------------------------------------------------------------
# Outcomes and backoff
# ---------------------------------------------------------------------------


async def test_backoff_is_exponential_and_clamped() -> None:
    assert backoff_seconds(0, base=2.0, cap=60.0) == 0.0
    assert backoff_seconds(1, base=2.0, cap=60.0) == 2.0
    assert backoff_seconds(2, base=2.0, cap=60.0) == 4.0
    assert backoff_seconds(3, base=2.0, cap=60.0) == 8.0
    assert backoff_seconds(20, base=2.0, cap=60.0) == 60.0


async def test_a_retryable_failure_records_an_attempt_and_reschedules(
    session: AsyncSession,
) -> None:
    org = await _seed_org(session)
    await _queue(session, org)
    now = CLAIM_AT
    transport = RecordingTransport(error=TransportError("collector is down", retryable=True))

    result = await _run(session, _settings(), transport, now=now)

    assert result.retrying == 1
    intent = await session.scalar(select(DeliveryIntent))
    assert intent is not None
    assert intent.state == STATE_RETRYING
    assert intent.next_attempt_at.replace(tzinfo=UTC) == now + timedelta(seconds=2)
    attempts = (await session.scalars(select(DeliveryAttempt))).all()
    assert [a.outcome for a in attempts] == [str(DeliveryOutcome.FAILED_RETRYABLE)]
    assert attempts[0].detail == "collector is down"


async def test_a_permanent_failure_does_not_reschedule(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await _queue(session, org)
    transport = RecordingTransport(
        error=TransportError("HTTP 403", retryable=False, status_code=403)
    )

    result = await _run(session, _settings(), transport)

    assert result.dead_lettered == 1
    intent = await session.scalar(select(DeliveryIntent))
    assert intent is not None
    assert intent.state == "DEAD_LETTER"
    assert intent.delivered_at is None


async def test_an_unconfigured_transport_is_discarded_never_delivered(
    session: AsyncSession,
) -> None:
    org = await _seed_org(session)
    await _queue(session, org)

    result = await _run(session, _settings(), NullTransport("nothing is configured"))

    assert result.discarded == 1
    assert result.delivered == 0
    intent = await session.scalar(select(DeliveryIntent))
    assert intent is not None
    assert intent.state == "DISCARDED"
    assert intent.last_outcome == str(DeliveryOutcome.NOT_CONFIGURED)
    assert intent.delivered_at is None


async def test_delivery_sets_delivered_and_clears_the_error(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await _queue(session, org)
    now = CLAIM_AT
    await _run(session, _settings(), RecordingTransport(error=TransportError("x", retryable=True)))

    transport = RecordingTransport()
    await _run(session, _settings(), transport, now=now + timedelta(hours=1))

    intent = await session.scalar(select(DeliveryIntent))
    assert intent is not None
    assert intent.state == STATE_DELIVERED
    assert intent.last_error is None
    assert intent.attempt_count == 2
    assert transport.sent == [{"text": "hello"}]


# ---------------------------------------------------------------------------
# Deduplication at delivery
# ---------------------------------------------------------------------------


async def test_an_equivalent_intent_is_suppressed_rather_than_sent_twice(
    session: AsyncSession,
) -> None:
    org = await _seed_org(session)
    await _queue(session, org, dedup="same-message", now=datetime(2026, 9, 5, 9, 0, tzinfo=UTC))
    await _queue(session, org, dedup="same-message", now=datetime(2026, 9, 5, 9, 1, tzinfo=UTC))

    transport = RecordingTransport()
    result = await _run(session, _settings(), transport)

    assert len(transport.sent) == 1
    assert result.delivered == 1
    assert result.duplicates == 1
    states = sorted(row.state for row in (await session.scalars(select(DeliveryIntent))).all())
    assert states == [STATE_DELIVERED, STATE_DUPLICATE]


async def test_a_different_channel_is_not_a_duplicate(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await _queue(session, org, dedup="shared")
    enqueue_intent(
        session,
        organization_id=org.id,
        kind=KIND_NOTIFICATION,
        channel="TEAMS",
        destination="https://hooks.example/#teams",
        dedup_key="shared",
        payload={"text": "hello"},
        now=QUEUED_AT,
    )
    await session.commit()

    transport = RecordingTransport()
    result = await _run(session, _settings(), transport)

    assert result.delivered == 2
    assert result.duplicates == 0


# ---------------------------------------------------------------------------
# Off by default, and helpers
# ---------------------------------------------------------------------------


async def test_the_worker_does_nothing_when_disabled(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await _queue(session, org)
    transport = RecordingTransport()

    result = await _run(session, _settings(delivery_worker_enabled=False), transport)

    assert result.claimed == 0
    assert transport.sent == []
    intent = await session.scalar(select(DeliveryIntent))
    assert intent is not None
    assert intent.state == STATE_PENDING, "queued, not lost"


async def test_a_destination_label_never_carries_the_credential() -> None:
    label = destination_label("https://hooks.slack.com/services/T000/B000/XXXXSECRETXXXX")
    assert "XXXXSECRETXXXX" not in label
    assert label.startswith("https://hooks.slack.com/#")


async def test_two_different_messages_get_different_dedup_keys() -> None:
    assert dedup_key_for("A", 1) != dedup_key_for("A", 2)
    assert dedup_key_for("A", 1) == dedup_key_for("A", 1)


async def test_the_notification_resolver_refuses_an_unconfigured_channel() -> None:
    intent = DeliveryIntent(kind=KIND_NOTIFICATION, channel="TEAMS")
    transport = notification_transport_for(
        intent, _settings(governance_notifications_enabled=True, teams_webhook_url=None)
    )
    assert transport.available is False


async def test_the_notification_resolver_refuses_while_notifications_are_off() -> None:
    intent = DeliveryIntent(kind=KIND_NOTIFICATION, channel="SLACK")
    transport = notification_transport_for(
        intent,
        _settings(
            governance_notifications_enabled=False, slack_webhook_url="https://hooks.example/x"
        ),
    )
    assert transport.available is False
