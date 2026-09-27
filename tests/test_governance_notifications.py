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

import json
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
        # The shipped Teams body. Asserted by shape here and in detail in the
        # Teams section below; a MessageCard arriving instead would mean the
        # default had silently reverted to the retired connector format.
        assert teams.received[0]["type"] == "message"


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


def test_the_legacy_message_card_still_has_no_actions() -> None:
    """A notification must never be an action surface -- in either format."""
    body = render_message(
        _settings(teams_card_format="MESSAGE_CARD"),
        "KILL_SWITCH_ENGAGED",
        {"object_type": "AGENT"},
        channel="TEAMS",
    )
    assert body["@type"] == "MessageCard"
    assert "potentialAction" not in body


# ---------------------------------------------------------------------------
# Teams: the format that works on a current tenant
#
# Microsoft disabled Office 365 connectors inside Teams between 2026-05-18 and
# 2026-05-22 (the retirement notice, last updated 2026-04-14). The legacy
# `MessageCard` body is a connector payload, so on a current tenant it has no
# live mechanism to arrive through; the supported one is a Workflows (Power
# Automate) webhook taking an Adaptive Card. These tests fix the default at
# the format that works, keep the legacy one reachable, and keep both of them
# free of anything a person could act on.
#
# What they cannot show is that a real Teams tenant accepts these bytes -- the
# destination here is a `ThreadingHTTPServer` on 127.0.0.1. See
# `Docs/40-engineering/12-notification-delivery-runbook.md` §3.2 for the
# procedure that closes that, which needs a tenant.
# ---------------------------------------------------------------------------


def _walk_card(node: object) -> Iterator[dict[str, Any]]:
    """Every dict anywhere in a card, so an assertion cannot miss a nested one."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_card(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_card(item)


def _adaptive_content(body: dict[str, Any]) -> dict[str, Any]:
    """The card out of the message envelope, asserting the envelope on the way.

    The envelope is the part the Workflows trigger dispatches on: `type:
    message` with one attachment whose `contentType` is the Adaptive Card
    media type. Getting it wrong is the failure mode that looks like success
    on our side, so it is checked wherever a card is read.
    """
    assert body["type"] == "message"
    attachments = body["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    content = attachments[0]["content"]
    assert content["type"] == "AdaptiveCard"
    assert content["$schema"] == "http://adaptivecards.io/schemas/adaptive-card.json"
    assert isinstance(content["version"], str)
    return dict(content)


def test_the_shipped_teams_format_is_the_adaptive_card() -> None:
    """The default has to be the format a current tenant can receive. A
    deployment that upgrades and changes nothing must stop posting connector
    payloads at a mechanism Microsoft switched off in May 2026."""
    assert Settings(_env_file=None, environment="test").teams_card_format == "ADAPTIVE_CARD"


@pytest.mark.asyncio
async def test_teams_receives_an_adaptive_card_from_the_real_entry_point(
    session: AsyncSession,
) -> None:
    """End to end, over a socket: `notify_governance_event` queues, the
    scheduler's worker delivers, and what arrives is the Workflows envelope.

    Driven through the entry point rather than by calling the builder, because
    the builder returning the right dict is not evidence that the right bytes
    leave the process -- that was the substance of F12.
    """
    org = await _seed_org(session)
    object_id = str(uuid4())
    with WebhookStub() as teams:
        settings = _settings(slack_webhook_url=None, teams_webhook_url=teams.url)

        outcomes = await notify_governance_event(
            session,
            org.id,
            "REVIEW_REQUESTED",
            {"object_type": "GLOSSARY_TERM", "object_id": object_id, "risk_tier": "T1"},
            settings=settings,
        )
        await session.flush()
        assert [o.status for o in outcomes if o.channel == "TEAMS"] == [STATUS_QUEUED]
        assert teams.received == [], "nothing is sent from the business transaction"

        await _deliver(session, settings)

        assert len(teams.received) == 1
        content = _adaptive_content(teams.received[0])

        # The information a person needs, and the link -- nothing to act on.
        rendered = json.dumps(content)
        assert "Approval requested" in rendered
        assert object_id in rendered
        assert "T1" in rendered
        assert f"https://atlas.example/#/governance?focus={object_id}" in rendered

        # And the bytes on the wire are the bytes we think they are, not a
        # dict the stub happened to decode leniently.
        assert b'"application/vnd.microsoft.card.adaptive"' in teams.raw[0]
        assert b"MessageCard" not in teams.raw[0]


@pytest.mark.asyncio
async def test_the_legacy_message_card_is_still_selectable_end_to_end(
    session: AsyncSession,
) -> None:
    """Some tenants still have a connector URL alive, or a Workflow built on an
    action that accepts a MessageCard. Deleting the format would break a
    channel that works today, so it stays reachable by configuration."""
    org = await _seed_org(session)
    with WebhookStub() as teams:
        settings = _settings(
            slack_webhook_url=None,
            teams_webhook_url=teams.url,
            teams_card_format="MESSAGE_CARD",
        )
        await notify_governance_event(
            session,
            org.id,
            "KILL_SWITCH_ENGAGED",
            {"object_type": "AGENT_CONTRACT", "object_id": str(uuid4())},
            settings=settings,
        )
        await session.flush()
        await _deliver(session, settings)

        assert len(teams.received) == 1
        body = teams.received[0]
        assert body["@type"] == "MessageCard"
        assert body["@context"] == "https://schema.org/extensions"
        assert "AI kill switch ENGAGED" in body["text"]
        assert "potentialAction" not in body


@pytest.mark.parametrize("kind", EVENT_KINDS)
def test_no_adaptive_card_carries_anything_to_act_on(kind: str) -> None:
    """The constraint the original comment recorded, carried across the format
    change: *a notification here must never be an action*. That is a
    governance property, not a rendering one -- this platform's approvals are
    authorized in the portal, against the portal's own authentication, and a
    card that could approve, publish or grant from a chat client would be a
    second control surface with none of those checks.

    Checked over the whole card tree rather than the top level, so a nested
    `ActionSet` or a `selectAction` on a container could not slip in.
    """
    body = render_message(
        _settings(),
        kind,
        {"object_type": "TABLE", "object_id": "abc", "risk_tier": "T1"},
        channel="TEAMS",
    )
    content = _adaptive_content(body)

    assert "actions" not in content
    for node in _walk_card(content):
        assert "selectAction" not in node
        node_type = node.get("type", "")
        assert not node_type.startswith("Action."), f"{kind} card carries {node_type}"
        assert not node_type.startswith("Input."), f"{kind} card carries {node_type}"
        assert node_type != "ActionSet", f"{kind} card carries an ActionSet"


@pytest.mark.asyncio
async def test_an_adaptive_card_carries_no_source_value(session: AsyncSession) -> None:
    """INV-6 again, against the new format and over the socket. The renderer is
    an allowlist, so a caller mistakenly passing a value must not reach the
    wire in *any* part of the card -- not the headline, not a fact, not the
    link."""
    org = await _seed_org(session)
    with WebhookStub() as teams:
        settings = _settings(slack_webhook_url=None, teams_webhook_url=teams.url)
        await notify_governance_event(
            session,
            org.id,
            "QUALITY_INCIDENT_OPENED",
            {
                "object_type": "TABLE",
                "object_id": str(uuid4()),
                "severity": "HIGH",
                "sample_row": SENTINEL,
                "sql": f"SELECT * FROM customers WHERE name = '{SENTINEL}'",  # noqa: S608
            },
            settings=settings,
        )
        await session.flush()
        await _deliver(session, settings)

        # Asserted as an Adaptive Card, so this cannot pass for the wrong
        # format and quietly stop covering the one that ships.
        _adaptive_content(teams.received[0])
        assert SENTINEL.encode() not in teams.raw[0]
        assert b"SELECT" not in teams.raw[0]
        # Present, so the assertions above are not passing on an empty card.
        assert b"Data quality incident opened" in teams.raw[0]


def test_a_teams_card_without_a_portal_url_still_says_what_happened() -> None:
    """An unset `portal_base_url` degrades the message rather than suppressing
    it, and must not leave a link element with nothing in it."""
    content = _adaptive_content(
        render_message(
            _settings(portal_base_url=None),
            "REVIEW_REQUESTED",
            {"object_type": "TABLE"},
            channel="TEAMS",
        )
    )
    rendered = json.dumps(content)
    assert "Approval requested" in rendered
    assert "Open in Atlas" not in rendered


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
