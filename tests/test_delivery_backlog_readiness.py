"""R11-B10: is a stalled outbound delivery queue visible without reading logs?

Two properties, proven separately because they fail separately.

**Delivery.** The path a governance event actually takes is
`notify_governance_event` -> a `DeliveryIntent` staged in the business
transaction -> `run_delivery_worker_pass` (what the fleet scheduler calls)
-> `WebhookTransport` -> a socket. Every test here that claims delivery drives
that whole path and asserts against bytes that arrived at a real HTTP server on
loopback (`tests/support/stub_servers.py`'s `WebhookStub`). None of them calls a
transport directly: a test that did would re-prove the thing that was never
broken and skip the routing and drain that were.

**Visibility.** `aida.readiness.probe_delivery_backlog` is what an operator
reads. The assertions below are deliberately written against its reported
detail string rather than against the database, because the detail string is
the artefact a human or an alert rule actually consumes -- asserting on a query
this module also wrote would not show that the number reaches anybody.

The property the brief singles out -- *age matters more than depth* -- has its
own test: a queue of one whose oldest row is a day old must read as a day old,
even though a count alone would call it nearly empty.

What is NOT proven here: that a real Slack, Teams or SOC collector accepts
these bytes. That is live-infrastructure-only and is written up as a runnable
procedure in `Docs/40-engineering/12-notification-delivery-runbook.md`.
"""

from __future__ import annotations

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
from aida.delivery_intents import (
    KIND_NOTIFICATION,
    KIND_SIEM,
    STATE_DEAD_LETTER,
    STATE_RETRYING,
    dedup_key_for,
    enqueue_intent,
    run_delivery_worker_pass,
)
from aida.governance_notifications import STATUS_QUEUED, notify_governance_event
from aida.models import DeliveryIntent, Organization
from aida.readiness import (
    DELIVERY_BACKLOG,
    DOWN,
    UP,
    evaluate_readiness,
    probe_delivery_backlog,
    reset_last_success,
)
from tests.support.stub_servers import WebhookStub

pytestmark = pytest.mark.asyncio


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "governance_notifications_enabled": True,
        "portal_base_url": "https://atlas.example",
        "delivery_worker_enabled": True,
        "teams_webhook_url": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def channel() -> Iterator[WebhookStub]:
    """A real Slack-shaped webhook endpoint on loopback."""
    with WebhookStub() as stub:
        yield stub


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[Any]:
    """One in-memory database, reachable both as a session and as a factory.

    `StaticPool` keeps every session on the same connection, so the probe --
    which opens its own session through the factory, exactly as it does in
    production -- reads what the business path committed.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


def _signals(detail: str | None) -> dict[str, str]:
    """Parse the probe's `key=value;key=value` detail into a dict."""
    assert detail is not None, "the probe reported no detail at all"
    return dict(part.split("=", 1) for part in detail.split(";"))


async def _backlog(
    factory: Any, settings: Settings, *, now: datetime | None = None
) -> dict[str, str]:
    result = await probe_delivery_backlog(
        settings, timeout_seconds=5.0, now=now, session_factory=factory
    )
    assert result.state == UP, f"the probe could not measure: {result.detail}"
    assert result.required is False, "a delivery backlog must never take the API out of rotation"
    return _signals(result.detail)


# ---------------------------------------------------------------------------
# The whole path: queued, visible, delivered, drained
# ---------------------------------------------------------------------------


async def test_a_queued_notification_is_visible_as_backlog_before_anything_is_sent(
    sessions: Any, channel: WebhookStub
) -> None:
    """The business transaction opens no socket, so the only evidence that a
    message is owed is the backlog reading. It has to be there.
    """
    settings = _settings(slack_webhook_url=channel.url)
    async with sessions() as session:
        org = await _seed_org(session)
        outcomes = await notify_governance_event(
            session,
            org.id,
            "REVIEW_REQUESTED",
            {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4()), "risk_tier": "T1"},
            settings=settings,
        )
        await session.commit()

    assert [o.status for o in outcomes if o.channel == "SLACK"] == [STATUS_QUEUED]
    assert channel.received == [], "nothing may be sent from the business transaction"

    signals = await _backlog(sessions, settings)
    assert signals["queued"] == "1"
    assert signals["queued_notification"] == "1"
    assert signals["failed"] == "0"
    assert signals["worker"] == "enabled"
    assert "oldest_age_seconds" in signals


async def test_the_backlog_clears_only_once_the_destination_has_received_the_bytes(
    sessions: Any, channel: WebhookStub
) -> None:
    """The joint claim of the row: a configured worker delivers, and the
    backlog an operator reads reflects that it did.

    Driven from `notify_governance_event` through `run_delivery_worker_pass`
    -- the same two entry points production uses -- and asserted against the
    bytes the loopback server actually received.
    """
    settings = _settings(slack_webhook_url=channel.url)
    object_id = str(uuid4())
    async with sessions() as session:
        org = await _seed_org(session)
        await notify_governance_event(
            session,
            org.id,
            "REVIEW_REQUESTED",
            {"object_type": "GLOSSARY_TERM", "object_id": object_id, "risk_tier": "T1"},
            settings=settings,
        )
        await session.commit()

    assert (await _backlog(sessions, settings))["queued"] == "1"

    async with sessions() as session:
        result = await run_delivery_worker_pass(settings, session=session, owner="backlog-test")

    assert result.delivered == 1, f"the worker did not deliver: {result}"
    assert len(channel.received) == 1, "no bytes reached the destination"
    assert object_id in channel.received[0]["text"]
    assert "https://atlas.example/#/governance" in channel.received[0]["text"]

    drained = await _backlog(sessions, settings)
    assert drained["queued"] == "0"
    assert drained["failed"] == "0"
    assert "oldest_age_seconds" not in drained, "an empty queue has no oldest row"


# ---------------------------------------------------------------------------
# The failure modes the row asks to be visible
# ---------------------------------------------------------------------------


async def test_a_refusing_destination_reads_as_backlog_not_as_silence(
    sessions: Any, channel: WebhookStub
) -> None:
    """F12's shape, seen from the operator's side: a destination that answers
    503 leaves the message owed, and the backlog must say so. Before this probe
    the queue looked identical to an empty one from outside the logs.
    """
    channel.fail_status = 503
    settings = _settings(slack_webhook_url=channel.url)
    async with sessions() as session:
        org = await _seed_org(session)
        await notify_governance_event(
            session,
            org.id,
            "REVIEW_DECIDED",
            {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4())},
            settings=settings,
        )
        await session.commit()

    async with sessions() as session:
        result = await run_delivery_worker_pass(settings, session=session, owner="backlog-test")
    assert result.delivered == 0
    assert result.retrying == 1

    async with sessions() as session:
        states = list(await session.scalars(select(DeliveryIntent.state)))
    assert states == [STATE_RETRYING]

    signals = await _backlog(sessions, settings)
    assert signals["queued"] == "1", "a retrying message is still owed to a destination"
    assert signals["failed"] == "0", "retrying is not yet failed"
    assert "oldest_age_seconds" in signals


async def test_an_exhausted_retry_budget_is_reported_as_failed_rather_than_queued(
    sessions: Any, channel: WebhookStub
) -> None:
    """A dead-lettered message will never be retried, so counting it as backlog
    would tell an operator to wait for a drain that is never coming. It is
    reported separately, as the thing that needs a human.
    """
    channel.fail_status = 503
    settings = _settings(slack_webhook_url=channel.url, delivery_max_attempts=1)
    async with sessions() as session:
        org = await _seed_org(session)
        await notify_governance_event(
            session,
            org.id,
            "REVIEW_DECIDED",
            {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4())},
            settings=settings,
        )
        await session.commit()

    async with sessions() as session:
        await run_delivery_worker_pass(settings, session=session, owner="backlog-test")
        states = list(await session.scalars(select(DeliveryIntent.state)))
    assert states == [STATE_DEAD_LETTER]

    signals = await _backlog(sessions, settings)
    assert signals["failed"] == "1"
    assert signals["failed_notification"] == "1", "a dead letter names its own kind"
    assert signals["queued"] == "0"


async def test_backlog_age_is_measured_from_the_request_not_from_the_last_attempt(
    sessions: Any,
) -> None:
    """The property the row cares about most: a queue of three stuck for a day
    is worse than a queue of nine hundred draining normally.

    A count alone cannot express that. The age is anchored to `requested_at` --
    the timestamp that commits with the governance decision -- so retrying a
    day-old message every five minutes does not make it look fresh.
    """
    settings = _settings(slack_webhook_url="https://hooks.example/slack")
    now = datetime.now(UTC)
    day_old = now - timedelta(days=1)

    async with sessions() as session:
        org = await _seed_org(session)
        enqueue_intent(
            session,
            organization_id=org.id,
            kind=KIND_NOTIFICATION,
            channel="SLACK",
            destination="https://hooks.example/#stub",
            dedup_key=dedup_key_for("stale", uuid4()),
            payload={"text": "owed since yesterday"},
            now=day_old,
        )
        # A recent attempt against the same row: `attempted_at` moves, the age
        # this probe reports must not.
        intent = await session.scalar(select(DeliveryIntent))
        assert intent is not None
        intent.attempted_at = now
        intent.state = STATE_RETRYING
        intent.attempt_count = 4
        await session.commit()

    signals = await _backlog(sessions, settings, now=now)
    assert signals["queued"] == "1"
    age = float(signals["oldest_age_seconds"])
    assert age >= 86_000, f"a day-old backlog reported as {age}s old"


async def test_a_disabled_worker_is_named_so_a_growing_queue_is_not_misread(
    sessions: Any,
) -> None:
    """`delivery_worker_enabled` is False by default. A deployment that has not
    opted in accrues a backlog that will never drain and is working exactly as
    configured, so the reading has to distinguish that from a wedged worker --
    otherwise the alert fires on every default install and gets muted.
    """
    settings = _settings(
        slack_webhook_url="https://hooks.example/slack", delivery_worker_enabled=False
    )
    async with sessions() as session:
        org = await _seed_org(session)
        await notify_governance_event(
            session,
            org.id,
            "KILL_SWITCH_ENGAGED",
            {"object_type": "AGENT_CONTRACT", "object_id": str(uuid4())},
            settings=settings,
        )
        await session.commit()

    signals = await _backlog(sessions, settings)
    assert signals["worker"] == "disabled"
    assert signals["queued"] == "1", "a queue that cannot drain is still a queue"


async def test_the_two_kinds_sharing_the_ledger_are_counted_apart(sessions: Any) -> None:
    """Governance notifications and SIEM security events share one table and
    one worker. "Three things are queued" is not actionable if one of them
    being a security event is invisible.
    """
    settings = _settings(slack_webhook_url="https://hooks.example/slack")
    async with sessions() as session:
        org = await _seed_org(session)
        for kind, channel_name in (
            (KIND_NOTIFICATION, "SLACK"),
            (KIND_SIEM, "WEBHOOK"),
            (KIND_SIEM, "WEBHOOK"),
        ):
            enqueue_intent(
                session,
                organization_id=org.id,
                kind=kind,
                channel=channel_name,
                destination="https://collector.example/#stub",
                dedup_key=dedup_key_for(kind, uuid4()),
                payload={"text": "x"},
            )
        await session.commit()

    result = await probe_delivery_backlog(settings, timeout_seconds=5.0, session_factory=sessions)
    signals = _signals(result.detail)
    assert signals["queued"] == "3"
    assert signals["queued_notification"] == "1"
    assert signals["queued_siem"] == "2"

    # The exact wire shape, pinned because the runbook documents it and an
    # alert rule parses it: `key=value` pairs, `;`-separated, keys sorted.
    assert result.detail is not None
    keys = [part.split("=", 1)[0] for part in result.detail.split(";")]
    assert keys == sorted(keys), f"keys are not in sorted order: {result.detail}"
    assert keys == [
        "failed",
        "oldest_age_seconds",
        "queued",
        "queued_notification",
        "queued_siem",
        "worker",
    ]


# ---------------------------------------------------------------------------
# The probe's own contract
# ---------------------------------------------------------------------------


async def test_a_backlog_never_takes_the_api_out_of_rotation(
    sessions: Any, channel: WebhookStub
) -> None:
    """This module does not own the threshold at which a backlog is an
    incident. A wedged chat webhook is an operational problem, not a reason to
    stop serving queries -- the same call `probe_outbox_backlog` makes.
    """
    channel.fail_status = 503
    settings = _settings(slack_webhook_url=channel.url)
    async with sessions() as session:
        org = await _seed_org(session)
        for _ in range(5):
            await notify_governance_event(
                session,
                org.id,
                "REVIEW_REQUESTED",
                {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4())},
                settings=settings,
            )
        await session.commit()

    reset_last_success()
    report = await evaluate_readiness(
        settings,
        temporal_client=None,
        background_tasks={},
        session_factory=sessions,
    )

    assert report.status == UP, "a delivery backlog must not fail readiness"
    assert report.optional[DELIVERY_BACKLOG] == UP
    assert DELIVERY_BACKLOG not in report.required
    assert _signals(report.signals[f"{DELIVERY_BACKLOG}.detail"])["queued"] == "5"


async def test_the_probe_reports_down_only_when_it_cannot_measure_at_all() -> None:
    """A backlog that cannot be read is not a backlog of zero. Reporting UP
    with no numbers would be the same class of lie F18 removed from `/health/
    ready` in the first place.
    """

    class _FailingSession:
        def __call__(self) -> Any:
            return self

        async def __aenter__(self) -> Any:
            raise RuntimeError("database is gone")

        async def __aexit__(self, *args: object) -> None:
            return None

    result = await probe_delivery_backlog(
        _settings(), timeout_seconds=1.0, session_factory=_FailingSession()
    )
    assert result.state == DOWN
    assert result.required is False
    assert result.detail is not None and "RuntimeError" in result.detail
