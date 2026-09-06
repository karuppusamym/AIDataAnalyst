"""F04: SIEM delivery against real destinations, or an honest refusal.

`Docs/review-2026-09-05/REVIEW.md` F04: both configured transport branches
formatted a message, wrote a local structlog line and returned ``True``.
Nothing was sent. The acceptance criteria it sets are the sections below --
destination receipt demonstrated, timeout and retry paths, disabled and
not-configured and delivered distinguishable, and configured detail
suppression enforced.

Every destination here is a real loopback server (`tests/support/stub_servers`),
never a patch of our own transport: the defect being fixed was a function that
claimed success without contacting anything, so a test that stubs the
contacting proves nothing.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.delivery_intents import (
    KIND_SIEM,
    STATE_DEAD_LETTER,
    STATE_DELIVERED,
    STATE_DISCARDED,
    STATE_RETRYING,
    DeliveryOutcome,
    DeliveryPassResult,
    TransportUnavailable,
    run_delivery_worker_pass,
)
from aida.models import AuditEvent, DeliveryAttempt, DeliveryIntent
from aida.siem_delivery import (
    build_siem_transport,
    format_syslog_message,
    frame_octet_counted,
    siem_config_from_settings,
    syslog_priority,
)
from aida.siem_routing import SecurityEvent, SiemConfig, route_to_siem, siem_payload
from tests.support.stub_servers import (
    SyslogTcpStub,
    SyslogUdpStub,
    WebhookStub,
    unused_tcp_port,
)

_audit_event_ids = itertools.count(10_000)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "delivery_worker_enabled": True,
        "delivery_backoff_base_seconds": 1.0,
        "delivery_backoff_max_seconds": 8.0,
        "siem_enabled": True,
        "siem_delivery_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _event(**overrides: object) -> SecurityEvent:
    values: dict[str, object] = {
        "event_type": "AUTH_FAILURE",
        "severity": "HIGH",
        "source": "10.0.0.1",
        "principal_id": "user-1",
        "correlation_id": f"corr-{uuid4().hex[:8]}",
        "details": {"reason": "missing bearer token", "attempts": 3},
        "timestamp": datetime(2024, 6, 15, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return SecurityEvent(**values)  # type: ignore[arg-type]


async def _queue(session: AsyncSession, settings: Settings, **event_overrides: object) -> None:
    outcome = route_to_siem(session, _event(**event_overrides), siem_config_from_settings(settings))
    assert outcome is DeliveryOutcome.QUEUED
    await session.commit()


async def _intent(session: AsyncSession) -> DeliveryIntent:
    row = await session.scalar(select(DeliveryIntent).where(DeliveryIntent.kind == KIND_SIEM))
    assert row is not None
    await session.refresh(row)
    return row


async def _attempts(session: AsyncSession) -> list[DeliveryAttempt]:
    rows = await session.scalars(select(DeliveryAttempt).order_by(DeliveryAttempt.attempt_number))
    return list(rows.all())


async def _drain(
    session: AsyncSession, settings: Settings, *, now: datetime | None = None
) -> DeliveryPassResult:
    return await run_delivery_worker_pass(
        settings, now=now or datetime.now(UTC), session=session, owner="test-worker"
    )


# ---------------------------------------------------------------------------
# Destination receipt: the criterion the old implementation could not meet
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_webhook_collector_actually_receives_the_event(session: AsyncSession) -> None:
    with WebhookStub() as stub:
        settings = _settings(siem_transport="webhook", siem_endpoint=stub.url)
        await _queue(session, settings)

        result = await _drain(session, settings)

        assert result.delivered == 1
        assert len(stub.received) == 1
        body = stub.received[0]
        assert body["event_type"] == "AUTH_FAILURE"
        assert body["principal_id"] == "user-1"

    intent = await _intent(session)
    assert intent.state == STATE_DELIVERED
    assert intent.delivered_at is not None
    assert intent.attempted_at is not None
    assert intent.last_outcome == str(DeliveryOutcome.DELIVERED)


@pytest.mark.asyncio
async def test_a_syslog_udp_collector_actually_receives_an_rfc5424_message(
    session: AsyncSession,
) -> None:
    with SyslogUdpStub() as stub:
        settings = _settings(
            siem_transport="syslog", siem_endpoint=f"syslog+udp://127.0.0.1:{stub.port}"
        )
        await _queue(session, settings)

        await _drain(session, settings)
        datagrams = stub.wait_for(1)

    assert len(datagrams) == 1
    wire = datagrams[0].decode("utf-8")
    # <PRI>VERSION SP TIMESTAMP SP HOSTNAME SP APP-NAME ...
    assert wire.startswith(f"<{syslog_priority('HIGH')}>1 ")
    assert " atlas " in wire
    assert "﻿CEF:0|Atlas|DataIntelligence|1.0|100|" in wire


@pytest.mark.asyncio
async def test_a_syslog_tcp_collector_receives_an_octet_counted_frame(
    session: AsyncSession,
) -> None:
    with SyslogTcpStub() as stub:
        settings = _settings(
            siem_transport="syslog", siem_endpoint=f"syslog+tcp://127.0.0.1:{stub.port}"
        )
        await _queue(session, settings)

        await _drain(session, settings)
        streams = stub.wait_for(1)

    assert len(streams) == 1
    frame = streams[0]
    length_prefix, _, message = frame.partition(b" ")
    # RFC 6587 §3.4.1: the prefix counts octets of the message that follows.
    assert int(length_prefix) == len(message)
    assert message.startswith(b"<")
    assert b"CEF:0|Atlas" in message


def test_octet_counting_counts_bytes_not_characters() -> None:
    """The classic RFC 6587 bug: a non-ASCII field makes `len(str)` under-count
    and desynchronises every subsequent message on the connection."""
    payload = siem_payload(_event(principal_id="ünicode-user"), SiemConfig())
    message = format_syslog_message(payload, hostname="host", procid="1")
    frame = frame_octet_counted(message)
    prefix, _, body = frame.partition(b" ")
    assert int(prefix) == len(body)
    assert int(prefix) > len(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# Disabled / not configured / delivered are three different answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_queues_nothing_and_says_disabled(session: AsyncSession) -> None:
    settings = _settings(siem_enabled=False, siem_endpoint="https://soc.example/x")
    outcome = route_to_siem(session, _event(), siem_config_from_settings(settings))
    assert outcome is DeliveryOutcome.DISABLED
    await session.flush()
    assert (await session.scalars(select(DeliveryIntent))).all() == []


@pytest.mark.asyncio
async def test_not_configured_queues_nothing_and_says_not_configured(
    session: AsyncSession,
) -> None:
    settings = _settings(siem_endpoint="internal://security-log-pipeline")
    outcome = route_to_siem(session, _event(), siem_config_from_settings(settings))
    assert outcome is DeliveryOutcome.NOT_CONFIGURED
    await session.flush()
    assert (await session.scalars(select(DeliveryIntent))).all() == []


def test_an_unconfigured_transport_refuses_rather_than_succeeding() -> None:
    """The `NullTransport` default. `send` raises; there is no third result
    that logs and returns something success-shaped."""
    transport = build_siem_transport(SiemConfig(enabled=True, endpoint=""))
    assert transport.available is False
    with pytest.raises(TransportUnavailable):
        transport.send({"event_type": "AUTH_FAILURE"})


@pytest.mark.asyncio
async def test_an_endpoint_that_stops_being_configured_is_discarded_not_delivered(
    session: AsyncSession,
) -> None:
    """A queued intent whose destination is removed from settings must never
    read as delivered."""
    with WebhookStub() as stub:
        queued_settings = _settings(siem_transport="webhook", siem_endpoint=stub.url)
        await _queue(session, queued_settings)

    await _drain(session, _settings(siem_endpoint=""))

    intent = await _intent(session)
    assert intent.state == STATE_DISCARDED
    assert intent.delivered_at is None
    assert intent.last_outcome == str(DeliveryOutcome.NOT_CONFIGURED)
    attempts = await _attempts(session)
    assert [a.outcome for a in attempts] == [str(DeliveryOutcome.NOT_CONFIGURED)]


# ---------------------------------------------------------------------------
# include_details, in both transports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_include_details_false_suppresses_details_in_the_webhook_body(
    session: AsyncSession,
) -> None:
    with WebhookStub() as stub:
        settings = _settings(
            siem_transport="webhook", siem_endpoint=stub.url, siem_include_details=False
        )
        await _queue(session, settings)
        await _drain(session, settings)

        assert len(stub.received) == 1
        body = stub.received[0]
    assert "details" not in body
    assert "missing bearer token" not in str(body)


@pytest.mark.asyncio
async def test_include_details_false_suppresses_details_in_the_cef_extension(
    session: AsyncSession,
) -> None:
    """The review names this explicitly: suppression must hold in the CEF
    extension too, not only in one of the two transports."""
    with SyslogUdpStub() as stub:
        settings = _settings(
            siem_transport="syslog",
            siem_endpoint=f"syslog+udp://127.0.0.1:{stub.port}",
            siem_include_details=False,
        )
        await _queue(session, settings)
        await _drain(session, settings)
        datagrams = stub.wait_for(1)

    wire = datagrams[0].decode("utf-8")
    assert "missing bearer token" not in wire
    assert "cs2Label=" not in wire


@pytest.mark.asyncio
async def test_include_details_true_does_carry_them_in_both_transports(
    session: AsyncSession,
) -> None:
    with WebhookStub() as webhook, SyslogUdpStub() as syslog:
        http_settings = _settings(siem_transport="webhook", siem_endpoint=webhook.url)
        await _queue(session, http_settings, correlation_id="corr-details-http")
        await _drain(session, http_settings)

        syslog_settings = _settings(
            siem_transport="syslog", siem_endpoint=f"syslog+udp://127.0.0.1:{syslog.port}"
        )
        await _queue(session, syslog_settings, correlation_id="corr-details-syslog")
        await _drain(session, syslog_settings)
        datagrams = syslog.wait_for(1)

        assert webhook.received[0]["details"]["reason"] == "missing bearer token"
    assert "missing bearer token" in datagrams[-1].decode("utf-8")


@pytest.mark.asyncio
async def test_suppressed_details_are_not_even_persisted(session: AsyncSession) -> None:
    """Minimisation is applied once, at enqueue. A detail that never reaches
    the stored payload cannot be leaked by a transport added later."""
    settings = _settings(
        siem_transport="webhook",
        siem_endpoint="https://soc.example/hook",
        siem_include_details=False,
    )
    await _queue(session, settings)
    intent = await _intent(session)
    assert "details" not in intent.payload


# ---------------------------------------------------------------------------
# Timeout, retry, backoff, recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_connection_is_retryable_and_backs_off(
    session: AsyncSession,
) -> None:
    port = unused_tcp_port()
    settings = _settings(siem_transport="webhook", siem_endpoint=f"http://127.0.0.1:{port}/hook")
    await _queue(session, settings)

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    await _drain(session, settings, now=now)

    intent = await _intent(session)
    assert intent.state == STATE_RETRYING
    assert intent.last_outcome == str(DeliveryOutcome.FAILED_RETRYABLE)
    assert intent.delivered_at is None
    assert intent.attempt_count == 1
    assert intent.next_attempt_at.replace(tzinfo=UTC) == now + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_backoff_actually_holds_the_intent_back(session: AsyncSession) -> None:
    port = unused_tcp_port()
    settings = _settings(siem_transport="webhook", siem_endpoint=f"http://127.0.0.1:{port}/hook")
    await _queue(session, settings)

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    await _drain(session, settings, now=now)
    # Still inside the backoff window: the worker must not claim it again.
    result = await _drain(session, settings, now=now + timedelta(milliseconds=500))
    assert result.claimed == 0


@pytest.mark.asyncio
async def test_a_4xx_is_permanent_and_dead_letters_without_burning_retries(
    session: AsyncSession,
) -> None:
    with WebhookStub(fail_status=400) as stub:
        settings = _settings(siem_transport="webhook", siem_endpoint=stub.url)
        await _queue(session, settings)
        await _drain(session, settings)

    intent = await _intent(session)
    assert intent.state == STATE_DEAD_LETTER
    assert intent.last_outcome == str(DeliveryOutcome.FAILED_PERMANENT)
    assert intent.attempt_count == 1
    attempts = await _attempts(session)
    assert attempts[0].status_code == 400


@pytest.mark.asyncio
async def test_a_429_is_retryable(session: AsyncSession) -> None:
    with WebhookStub(fail_status=429) as stub:
        settings = _settings(siem_transport="webhook", siem_endpoint=stub.url)
        await _queue(session, settings)
        await _drain(session, settings)

    intent = await _intent(session)
    assert intent.state == STATE_RETRYING


@pytest.mark.asyncio
async def test_a_slow_destination_times_out_retryably(session: AsyncSession) -> None:
    with WebhookStub(delay_seconds=1.5) as stub:
        settings = _settings(
            siem_transport="webhook", siem_endpoint=stub.url, siem_delivery_timeout_seconds=0.25
        )
        await _queue(session, settings)
        await _drain(session, settings)

    intent = await _intent(session)
    assert intent.state == STATE_RETRYING
    assert intent.last_error is not None
    assert "timeout" in intent.last_error.lower()


@pytest.mark.asyncio
async def test_the_retry_budget_is_finite_and_ends_in_a_dead_letter(
    session: AsyncSession,
) -> None:
    port = unused_tcp_port()
    settings = _settings(
        siem_transport="webhook",
        siem_endpoint=f"http://127.0.0.1:{port}/hook",
        delivery_max_attempts=3,
    )
    await _queue(session, settings)

    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    for step in range(3):
        now = now + timedelta(minutes=10 * step + 1)
        await _drain(session, settings, now=now)

    intent = await _intent(session)
    assert intent.attempt_count == 3
    assert intent.state == STATE_DEAD_LETTER
    assert intent.last_error is not None
    assert "retry budget" in intent.last_error


@pytest.mark.asyncio
async def test_an_outage_followed_by_recovery_eventually_delivers(
    session: AsyncSession,
) -> None:
    """The F04/F12 acceptance criterion, end to end: a destination that is down
    when the event happens must still receive it once it comes back, and the
    event must not have been reported as delivered in the meantime."""
    with WebhookStub(fail_status=503) as stub:
        settings = _settings(siem_transport="webhook", siem_endpoint=stub.url)
        await _queue(session, settings)

        now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
        await _drain(session, settings, now=now)
        outage_intent = await _intent(session)
        assert outage_intent.state == STATE_RETRYING
        assert outage_intent.delivered_at is None
        assert stub.received == [] or len(stub.received) == 1  # the 503'd POST

        # The collector comes back.
        stub.fail_status = None
        stub.received.clear()
        await _drain(session, settings, now=now + timedelta(minutes=5))

        assert len(stub.received) == 1

    intent = await _intent(session)
    assert intent.state == STATE_DELIVERED
    assert intent.attempt_count == 2


# ---------------------------------------------------------------------------
# Durability and deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attempts_survive_a_restart(session: AsyncSession) -> None:
    """The engine is `StaticPool` over one in-memory database, so a *new
    session* is the closest honest analogue of a restarted process: nothing
    in-memory carries over, only what was committed."""
    port = unused_tcp_port()
    settings = _settings(siem_transport="webhook", siem_endpoint=f"http://127.0.0.1:{port}/hook")
    await _queue(session, settings)
    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    await _drain(session, settings, now=now)

    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    async with maker() as reborn:
        intent = await reborn.scalar(select(DeliveryIntent))
        assert intent is not None
        assert intent.attempt_count == 1
        assert intent.state == STATE_RETRYING
        attempts = (await reborn.scalars(select(DeliveryAttempt))).all()
        assert len(attempts) == 1
        assert attempts[0].outcome == str(DeliveryOutcome.FAILED_RETRYABLE)

        # And a fresh worker picks the same intent back up, past the backoff.
        result = await run_delivery_worker_pass(
            settings, now=now + timedelta(minutes=5), session=reborn, owner="worker-after-restart"
        )
        assert result.claimed == 1


@pytest.mark.asyncio
async def test_a_duplicate_intent_is_not_delivered_twice(session: AsyncSession) -> None:
    """Two intents with the same identity -- the same event replayed by a
    retried request -- must produce one message, not two."""
    with WebhookStub() as stub:
        settings = _settings(siem_transport="webhook", siem_endpoint=stub.url)
        config = siem_config_from_settings(settings)
        duplicate = _event(correlation_id="corr-identical")

        # Two separate transactions, so the same-session guard is not what is
        # being tested here: this is the worker's durable check.
        route_to_siem(session, duplicate, config)
        await session.commit()
        route_to_siem(session, duplicate, config)
        await session.commit()

        intents = (await session.scalars(select(DeliveryIntent))).all()
        assert len(intents) == 2, "both intents are recorded; only one is delivered"

        await _drain(session, settings)

        assert len(stub.received) == 1

    states = sorted(row.state for row in (await session.scalars(select(DeliveryIntent))).all())
    assert states == ["DELIVERED", "DUPLICATE"]


@pytest.mark.asyncio
async def test_the_same_transaction_does_not_stage_two_identical_intents(
    session: AsyncSession,
) -> None:
    settings = _settings(siem_transport="webhook", siem_endpoint="https://soc.example/hook")
    config = siem_config_from_settings(settings)
    duplicate = _event(correlation_id="corr-same-txn")
    route_to_siem(session, duplicate, config)
    route_to_siem(session, duplicate, config)
    await session.commit()

    assert len((await session.scalars(select(DeliveryIntent))).all()) == 1


@pytest.mark.asyncio
async def test_the_worker_is_off_by_default_and_sends_nothing(session: AsyncSession) -> None:
    with WebhookStub() as stub:
        settings = _settings(siem_transport="webhook", siem_endpoint=stub.url)
        await _queue(session, settings)

        off = _settings(
            siem_transport="webhook", siem_endpoint=stub.url, delivery_worker_enabled=False
        )
        result = await _drain(session, off)

        assert result.claimed == 0
        assert stub.received == []

    # And nothing was lost: the intent is still there for whenever it is on.
    intent = await _intent(session)
    assert intent.state == "PENDING"


def test_the_worker_ships_off() -> None:
    assert Settings(_env_file=None, environment="test").delivery_worker_enabled is False


@pytest.mark.asyncio
async def test_the_credential_in_a_destination_url_is_never_persisted(
    session: AsyncSession,
) -> None:
    """A webhook path is a bearer credential. The intent stores a label, and
    the live endpoint is re-read from settings at delivery time."""
    settings = _settings(
        siem_transport="webhook", siem_endpoint="https://soc.example/hooks/SECRET-TOKEN-123"
    )
    await _queue(session, settings)
    intent = await _intent(session)
    assert "SECRET-TOKEN-123" not in intent.destination
    assert intent.destination.startswith("https://soc.example/#")
