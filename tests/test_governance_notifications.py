"""NT-1 / F12: governance notifications to Slack and Teams.

The properties that matter are the safety ones, because this is the one part
of the platform that sends data *out*:

* off by default, and a skipped dispatch is persisted with its reason so an
  operator can tell "not configured" from "queued" from "delivered";
* value-free -- a message carries object type, id, principal and a link, and
  never a source value, a description's text, or SQL;
* it can never fail, delay or roll back the governance transaction that
  triggered it -- and now cannot even reach a socket from one.

`Docs/review-2026-09-05/REVIEW.md` F12 changed the shape of this module: a
hook point stages a `DeliveryIntent` and the fleet scheduler's worker delivers
it. So these tests queue, then run the worker against a real loopback HTTP
server, rather than patching an HTTP client. The commit-lifecycle half of F12
has its own section at the end.
"""

from collections.abc import AsyncIterator, Iterator
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
from aida.governance_notifications import (
    EVENT_KINDS,
    STATUS_QUEUED,
    STATUS_SENT,
    STATUS_SKIPPED_DISABLED,
    STATUS_SKIPPED_NO_URL,
    deep_link,
    notify_governance_event,
    notify_safely,
    render_message,
    sync_notification_ledger,
)
from aida.models import AuditEvent, DeliveryIntent, NotificationEventRecord, Organization
from tests.support.stub_servers import WebhookStub

SENTINEL = "ACME-CUSTOMER-4471"

#: A URL nothing listens on. Enough to make a channel "configured" for the
#: tests that only care about queueing, never about receipt.
UNREACHABLE = "https://hooks.example/slack"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "governance_notifications_enabled": True,
        "slack_webhook_url": UNREACHABLE,
        "teams_webhook_url": None,
        "portal_base_url": "https://atlas.example",
        "delivery_worker_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


@pytest.fixture
def channel() -> Iterator[WebhookStub]:
    """A real Slack-shaped webhook endpoint on loopback."""
    with WebhookStub() as stub:
        yield stub


async def _deliver(session: AsyncSession, settings: Settings) -> Any:
    await session.commit()
    return await run_delivery_worker_pass(settings, session=session, owner="notification-test")


async def _intents(session: AsyncSession) -> list[DeliveryIntent]:
    rows = await session.scalars(
        select(DeliveryIntent).where(DeliveryIntent.kind == KIND_NOTIFICATION)
    )
    return list(rows.all())


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


# ---------------------------------------------------------------------------
# Off by default, and skips are visible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_sends_nothing_and_records_why(session: AsyncSession) -> None:
    org = await _seed_org(session)

    outcomes = await notify_governance_event(
        session,
        org.id,
        "REVIEW_REQUESTED",
        {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4())},
        settings=_settings(governance_notifications_enabled=False),
    )
    await session.flush()

    assert await _intents(session) == []
    assert {o.status for o in outcomes} == {STATUS_SKIPPED_DISABLED}
    rows = (await session.scalars(select(NotificationEventRecord))).all()
    assert {row.status for row in rows} == {STATUS_SKIPPED_DISABLED}


@pytest.mark.asyncio
async def test_enabled_without_a_url_records_skipped_no_url(session: AsyncSession) -> None:
    org = await _seed_org(session)

    outcomes = await notify_governance_event(
        session,
        org.id,
        "REVIEW_REQUESTED",
        {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4())},
        settings=_settings(slack_webhook_url=None, teams_webhook_url=None),
    )
    await session.flush()

    assert await _intents(session) == []
    assert {o.status for o in outcomes} == {STATUS_SKIPPED_NO_URL}


@pytest.mark.asyncio
async def test_the_settings_default_is_off() -> None:
    assert Settings(_env_file=None, environment="test").governance_notifications_enabled is False


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_configured_channel_receives_the_message(
    session: AsyncSession, channel: WebhookStub
) -> None:
    """Destination receipt, against a real HTTP server: the hook point queues,
    the worker delivers, and the bytes arrive."""
    org = await _seed_org(session)
    object_id = str(uuid4())
    settings = _settings(slack_webhook_url=channel.url)

    outcomes = await notify_governance_event(
        session,
        org.id,
        "REVIEW_REQUESTED",
        {"object_type": "GLOSSARY_TERM", "object_id": object_id, "risk_tier": "T1"},
        settings=settings,
    )
    await session.flush()

    assert [o.status for o in outcomes if o.channel == "SLACK"] == [STATUS_QUEUED]
    assert channel.received == [], "nothing is sent from the business transaction"

    await _deliver(session, settings)

    assert len(channel.received) == 1
    body = channel.received[0]
    assert "Approval requested" in body["text"]
    assert object_id in body["text"]
    assert "https://atlas.example/#/governance" in body["text"]


@pytest.mark.asyncio
async def test_both_channels_are_delivered_when_both_are_configured(
    session: AsyncSession, channel: WebhookStub
) -> None:
    org = await _seed_org(session)
    with WebhookStub() as teams:
        settings = _settings(slack_webhook_url=channel.url, teams_webhook_url=teams.url)
        await notify_governance_event(
            session,
            org.id,
            "KILL_SWITCH_ENGAGED",
            {"object_type": "AGENT_CONTRACT", "object_id": str(uuid4())},
            settings=settings,
        )
        await session.flush()
        assert {intent.channel for intent in await _intents(session)} == {"SLACK", "TEAMS"}

        await _deliver(session, settings)

        assert len(channel.received) == 1
        assert len(teams.received) == 1
        assert teams.received[0]["@type"] == "MessageCard"


@pytest.mark.asyncio
async def test_a_failing_endpoint_is_retried_rather_than_forgotten(
    session: AsyncSession, channel: WebhookStub
) -> None:
    """F12. A downed Slack must not roll back the governance decision that
    triggered the notification -- and, the part that was missing, must not
    quietly consume it either. The intent stays retryable with a durable
    attempt record behind it."""
    org = await _seed_org(session)
    channel.fail_status = 503
    settings = _settings(slack_webhook_url=channel.url)

    outcomes = await notify_governance_event(
        session,
        org.id,
        "REVIEW_DECIDED",
        {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4())},
        settings=settings,
    )
    await session.flush()
    assert [o.status for o in outcomes if o.channel == "SLACK"] == [STATUS_QUEUED]

    await _deliver(session, settings)

    intents = await _intents(session)
    assert [intent.state for intent in intents] == ["RETRYING"]
    assert intents[0].delivered_at is None
    assert intents[0].attempt_count == 1


@pytest.mark.asyncio
async def test_notify_safely_swallows_everything(session: AsyncSession, monkeypatch) -> None:
    org = await _seed_org(session)

    async def _explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("chat is on fire")

    monkeypatch.setattr(
        "aida.governance_notifications.notify_governance_event", _explode
    )
    # Must not raise.
    await notify_safely(
        session, org.id, "REVIEW_DECIDED", {"object_id": "x"}, settings=_settings()
    )


@pytest.mark.asyncio
async def test_notify_safely_is_a_no_op_when_disabled(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await notify_safely(
        session,
        org.id,
        "REVIEW_DECIDED",
        {"object_id": "x"},
        settings=_settings(governance_notifications_enabled=False),
    )
    await session.flush()
    assert await _intents(session) == []
    assert (await session.scalars(select(NotificationEventRecord))).all() == []


# ---------------------------------------------------------------------------
# Value freedom
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_carries_no_source_value(
    session: AsyncSession, channel: WebhookStub
) -> None:
    """INV-6, at the one place the platform sends data outward. A governance
    notification that leaked a column value into a Slack channel would be the
    most public possible breach of the control plane's core property."""
    org = await _seed_org(session)
    settings = _settings(slack_webhook_url=channel.url)

    await notify_governance_event(
        session,
        org.id,
        "QUALITY_INCIDENT_OPENED",
        {
            "object_type": "TABLE",
            "object_id": str(uuid4()),
            "severity": "HIGH",
            # A caller mistakenly passing a value must not reach the wire.
            "sample_row": SENTINEL,
            "sql": f"SELECT * FROM customers WHERE name = '{SENTINEL}'",  # noqa: S608
        },
        settings=settings,
    )
    await session.flush()
    await _deliver(session, settings)

    body = channel.received[0]
    assert SENTINEL not in body["text"]
    assert "SELECT" not in body["text"]


def test_render_only_emits_known_fields() -> None:
    """The renderer is an allowlist, not a dump of whatever it was handed."""
    body = render_message(
        _settings(),
        "REVIEW_REQUESTED",
        {"object_type": "TABLE", "object_id": "abc", "secret": SENTINEL},
        channel="SLACK",
    )
    assert SENTINEL not in body["text"]


def test_teams_gets_a_message_card_with_no_actions() -> None:
    """A notification must never be an action surface."""
    body = render_message(
        _settings(), "KILL_SWITCH_ENGAGED", {"object_type": "AGENT"}, channel="TEAMS"
    )
    assert body["@type"] == "MessageCard"
    assert "potentialAction" not in body


# ---------------------------------------------------------------------------
# Links, kinds, and the ledger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", EVENT_KINDS)
def test_every_kind_renders_and_links(kind: str) -> None:
    body = render_message(
        _settings(), kind, {"object_type": "TABLE", "object_id": "abc"}, channel="SLACK"
    )
    assert body["text"]
    assert deep_link(_settings(), kind, object_id="abc") is not None


def test_no_portal_base_url_degrades_the_message_rather_than_suppressing_it() -> None:
    assert deep_link(_settings(portal_base_url=None), "REVIEW_REQUESTED", object_id="a") is None
    body = render_message(
        _settings(portal_base_url=None),
        "REVIEW_REQUESTED",
        {"object_type": "TABLE"},
        channel="SLACK",
    )
    assert "Approval requested" in body["text"]


@pytest.mark.asyncio
async def test_an_unknown_kind_is_refused(session: AsyncSession) -> None:
    org = await _seed_org(session)
    outcomes = await notify_governance_event(
        session, org.id, "SOMETHING_INVENTED", {}, settings=_settings()
    )
    assert await _intents(session) == []
    assert outcomes[0].status == "SKIPPED_EVENT_KIND"


@pytest.mark.asyncio
async def test_a_deselected_kind_is_not_sent(session: AsyncSession) -> None:
    """Narrowing the selected kinds is how an organization quiets a noisy
    channel without turning the feature off."""
    org = await _seed_org(session)
    outcomes = await notify_governance_event(
        session,
        org.id,
        "REVIEW_DECIDED",
        {"object_type": "TABLE"},
        settings=_settings(governance_notification_events=["KILL_SWITCH_ENGAGED"]),
    )
    assert await _intents(session) == []
    assert outcomes[0].status == "SKIPPED_EVENT_KIND"


@pytest.mark.asyncio
async def test_every_dispatch_is_audited(session: AsyncSession) -> None:
    org = await _seed_org(session)
    await notify_governance_event(
        session,
        org.id,
        "REVIEW_REQUESTED",
        {"object_type": "TABLE", "object_id": "abc"},
        settings=_settings(),
    )
    await session.flush()
    rows = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "governance.notification.dispatch"
            )
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].details["event_kind"] == "REVIEW_REQUESTED"


# ---------------------------------------------------------------------------
# F12's other half: the commit lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_caller_that_already_committed_gets_its_evidence_committed(
    session: AsyncSession, channel: WebhookStub
) -> None:
    """`semantic_api.decide_governance_review` and the kill switch both commit
    and *then* notify. The FastAPI session dependency yields and closes rather
    than committing, so rows staged after that commit used to be discarded --
    a send could happen with its database evidence thrown away. When the caller
    holds no transaction, this module commits its own.
    """
    org = await _seed_org(session)
    await session.commit()  # the caller's business transaction, as at the real site
    assert session.in_transaction() is False
    settings = _settings(slack_webhook_url=channel.url)

    await notify_governance_event(
        session,
        org.id,
        "REVIEW_DECIDED",
        {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4())},
        settings=settings,
    )

    # No flush, no commit from the test: a *different* session must see it.
    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    async with maker() as other:
        intents = await _intents(other)
        assert len(intents) == 1
        ledger = (await other.scalars(select(NotificationEventRecord))).all()
        slack = [row for row in ledger if row.channel == "SLACK"]
        assert [row.status for row in slack] == [STATUS_QUEUED]


@pytest.mark.asyncio
async def test_a_caller_mid_transaction_keeps_its_own_commit(
    session: AsyncSession, channel: WebhookStub
) -> None:
    """`quality_service` and `certification_expiry_warning` call in the middle
    of their own transaction. Committing on their behalf would land their
    business writes early; the intent must ride along with the caller's commit
    instead."""
    org = await _seed_org(session)
    async with session.begin_nested():
        pass
    assert session.in_transaction() is True
    settings = _settings(slack_webhook_url=channel.url)

    await notify_governance_event(
        session,
        org.id,
        "QUALITY_INCIDENT_OPENED",
        {"object_type": "TABLE", "object_id": str(uuid4())},
        settings=settings,
    )

    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    async with maker() as other:
        assert await _intents(other) == [], "not committed until the caller commits"

    await session.commit()
    async with maker() as other:
        assert len(await _intents(other)) == 1


@pytest.mark.asyncio
async def test_the_ledger_converges_to_sent_once_a_destination_answers(
    session: AsyncSession, channel: WebhookStub
) -> None:
    org = await _seed_org(session)
    settings = _settings(slack_webhook_url=channel.url)
    await notify_governance_event(
        session,
        org.id,
        "REVIEW_REQUESTED",
        {"object_type": "TABLE", "object_id": str(uuid4())},
        settings=settings,
    )
    await _deliver(session, settings)

    reconciled = await sync_notification_ledger(session)
    await session.commit()

    assert reconciled == 1
    rows = (await session.scalars(select(NotificationEventRecord))).all()
    slack = [row for row in rows if row.channel == "SLACK"]
    assert [row.status for row in slack] == [STATUS_SENT]
    assert slack[0].sent_at is not None
