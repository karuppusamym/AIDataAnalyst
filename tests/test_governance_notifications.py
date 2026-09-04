"""NT-1: governance notifications to Slack and Teams.

The properties that matter are the safety ones, because this is the one part
of the platform that sends data *out*:

* off by default, and a skipped attempt is persisted with its reason so an
  operator can tell "not configured" from "delivered";
* value-free -- a message carries object type, id, principal and a link, and
  never a source value, a description's text, or SQL;
* it can never fail the governance transaction that triggered it.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.governance_notifications import (
    EVENT_KINDS,
    STATUS_FAILED,
    STATUS_SENT,
    STATUS_SKIPPED_DISABLED,
    STATUS_SKIPPED_NO_URL,
    deep_link,
    notify_governance_event,
    notify_safely,
    render_message,
)
from aida.models import AuditEvent, NotificationEventRecord, Organization

SENTINEL = "ACME-CUSTOMER-4471"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "governance_notifications_enabled": True,
        "slack_webhook_url": "https://hooks.example/slack",
        "teams_webhook_url": None,
        "portal_base_url": "https://atlas.example",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


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


def _capture(sent: list[tuple[str, dict]], *, fail: bool = False):
    """A transport that records what would have gone over the wire."""

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append((str(request.url), json.loads(request.content)))
        return httpx.Response(500 if fail else 200)

    return httpx.MockTransport(handler)


@pytest.fixture
def wire(monkeypatch):
    sent: list[tuple[str, dict]] = []

    def _install(*, fail: bool = False) -> list[tuple[str, dict]]:
        transport = _capture(sent, fail=fail)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr("aida.governance_notifications.httpx.AsyncClient", factory)
        return sent

    return _install


# ---------------------------------------------------------------------------
# Off by default, and skips are visible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_sends_nothing_and_records_why(
    session: AsyncSession, wire
) -> None:
    sent = wire()
    org = await _seed_org(session)

    outcomes = await notify_governance_event(
        session,
        org.id,
        "REVIEW_REQUESTED",
        {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4())},
        settings=_settings(governance_notifications_enabled=False),
    )
    await session.flush()

    assert sent == []
    assert {o.status for o in outcomes} == {STATUS_SKIPPED_DISABLED}
    rows = (await session.scalars(select(NotificationEventRecord))).all()
    assert {row.status for row in rows} == {STATUS_SKIPPED_DISABLED}


@pytest.mark.asyncio
async def test_enabled_without_a_url_records_skipped_no_url(
    session: AsyncSession, wire
) -> None:
    sent = wire()
    org = await _seed_org(session)

    outcomes = await notify_governance_event(
        session,
        org.id,
        "REVIEW_REQUESTED",
        {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4())},
        settings=_settings(slack_webhook_url=None, teams_webhook_url=None),
    )
    await session.flush()

    assert sent == []
    assert {o.status for o in outcomes} == {STATUS_SKIPPED_NO_URL}


@pytest.mark.asyncio
async def test_the_settings_default_is_off() -> None:
    assert Settings(_env_file=None, environment="test").governance_notifications_enabled is False


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_configured_channel_receives_the_message(
    session: AsyncSession, wire
) -> None:
    sent = wire()
    org = await _seed_org(session)
    object_id = str(uuid4())

    outcomes = await notify_governance_event(
        session,
        org.id,
        "REVIEW_REQUESTED",
        {"object_type": "GLOSSARY_TERM", "object_id": object_id, "risk_tier": "T1"},
        settings=_settings(),
    )
    await session.flush()

    assert [o.status for o in outcomes if o.channel == "SLACK"] == [STATUS_SENT]
    assert len(sent) == 1
    url, body = sent[0]
    assert url == "https://hooks.example/slack"
    assert "Approval requested" in body["text"]
    assert object_id in body["text"]
    assert "https://atlas.example/#/governance" in body["text"]


@pytest.mark.asyncio
async def test_both_channels_are_delivered_when_both_are_configured(
    session: AsyncSession, wire
) -> None:
    sent = wire()
    org = await _seed_org(session)

    await notify_governance_event(
        session,
        org.id,
        "KILL_SWITCH_ENGAGED",
        {"object_type": "AGENT_CONTRACT", "object_id": str(uuid4())},
        settings=_settings(teams_webhook_url="https://hooks.example/teams"),
    )
    await session.flush()

    urls = {url for url, _body in sent}
    assert urls == {"https://hooks.example/slack", "https://hooks.example/teams"}


@pytest.mark.asyncio
async def test_a_failing_endpoint_is_recorded_and_never_raises(
    session: AsyncSession, wire
) -> None:
    """A downed Slack must not roll back the governance decision that
    triggered the notification."""
    sent = wire(fail=True)
    org = await _seed_org(session)

    outcomes = await notify_governance_event(
        session,
        org.id,
        "REVIEW_DECIDED",
        {"object_type": "GLOSSARY_TERM", "object_id": str(uuid4())},
        settings=_settings(),
    )
    await session.flush()

    assert [o.status for o in outcomes if o.channel == "SLACK"] == [STATUS_FAILED]
    assert len(sent) == 2, "a bounded retry, then give up"
    rows = (
        await session.scalars(
            select(NotificationEventRecord).where(
                NotificationEventRecord.status == STATUS_FAILED
            )
        )
    ).all()
    assert len(rows) == 1


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
async def test_notify_safely_is_a_no_op_when_disabled(
    session: AsyncSession, wire
) -> None:
    sent = wire()
    org = await _seed_org(session)
    await notify_safely(
        session,
        org.id,
        "REVIEW_DECIDED",
        {"object_id": "x"},
        settings=_settings(governance_notifications_enabled=False),
    )
    await session.flush()
    assert sent == []
    assert (await session.scalars(select(NotificationEventRecord))).all() == []


# ---------------------------------------------------------------------------
# Value freedom
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_carries_no_source_value(session: AsyncSession, wire) -> None:
    """INV-6, at the one place the platform sends data outward. A governance
    notification that leaked a column value into a Slack channel would be the
    most public possible breach of the control plane's core property."""
    sent = wire()
    org = await _seed_org(session)

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
            "sql": f"SELECT * FROM customers WHERE name = '{SENTINEL}'",
        },
        settings=_settings(),
    )
    await session.flush()

    _url, body = sent[0]
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
async def test_an_unknown_kind_is_refused(session: AsyncSession, wire) -> None:
    sent = wire()
    org = await _seed_org(session)
    outcomes = await notify_governance_event(
        session, org.id, "SOMETHING_INVENTED", {}, settings=_settings()
    )
    assert sent == []
    assert outcomes[0].status == "SKIPPED_EVENT_KIND"


@pytest.mark.asyncio
async def test_a_deselected_kind_is_not_sent(session: AsyncSession, wire) -> None:
    """Narrowing the selected kinds is how an organization quiets a noisy
    channel without turning the feature off."""
    sent = wire()
    org = await _seed_org(session)
    outcomes = await notify_governance_event(
        session,
        org.id,
        "REVIEW_DECIDED",
        {"object_type": "TABLE"},
        settings=_settings(governance_notification_events=["KILL_SWITCH_ENGAGED"]),
    )
    assert sent == []
    assert outcomes[0].status == "SKIPPED_EVENT_KIND"


@pytest.mark.asyncio
async def test_every_dispatch_is_audited(session: AsyncSession, wire) -> None:
    wire()
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
