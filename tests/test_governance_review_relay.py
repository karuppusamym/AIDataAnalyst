"""NT-1: the REVIEW_REQUESTED relay.

This was the one event kind NT-1 shipped without a real path to a channel,
because review creation has 27 call sites and no funnel to hook. The relay
sweeps a watermark column instead. What the tests below hold is that the sweep
is safe to run on a schedule against a live estate:

* off by default, and while off it stamps **nothing**, so turning the feature
  on delivers the recent backlog rather than discovering a silent gap;
* idempotent -- a second pass over the same rows sends nothing;
* bounded in both directions: at most a batch per pass, and nothing older than
  the age window, so first-enable does not flood a channel with history;
* it notifies about pending reviews only, never about ones already decided;
* the payload is value-free.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
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
from aida.governance_review_relay import relay_review_requested
from aida.models import GovernanceReview, NotificationEventRecord, Organization

pytestmark = pytest.mark.asyncio

SENTINEL = "ACME-CUSTOMER-4471"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
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


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Records what would have gone over the wire."""
    sent: list[tuple[str, dict[str, Any]]] = []

    def _install() -> list[tuple[str, dict[str, Any]]]:
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            sent.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr("aida.governance_notifications.httpx.AsyncClient", factory)
        return sent

    return _install


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


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


async def test_disabled_sends_nothing_and_stamps_nothing(
    session: AsyncSession, wire: Any
) -> None:
    """Stamping while disabled would consume the backlog silently: an
    organization that enables notifications tomorrow would never hear about
    the approvals raised today."""
    sent = wire()
    org = await _seed_org(session)
    review = await _seed_review(session, org)

    outcome = await relay_review_requested(
        session, settings=_settings(governance_notifications_enabled=False)
    )

    assert sent == []
    assert outcome.examined == 0
    assert review.review_requested_notified_at is None


async def test_enabling_later_delivers_the_recent_backlog(
    session: AsyncSession, wire: Any
) -> None:
    sent = wire()
    org = await _seed_org(session)
    await _seed_review(session, org)
    await relay_review_requested(
        session, settings=_settings(governance_notifications_enabled=False)
    )
    assert sent == []

    outcome = await relay_review_requested(session, settings=_settings())

    assert len(outcome.notified) == 1
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# What gets notified
# ---------------------------------------------------------------------------


async def test_a_pending_review_is_notified_once(session: AsyncSession, wire: Any) -> None:
    sent = wire()
    org = await _seed_org(session)
    review = await _seed_review(session, org)

    first = await relay_review_requested(session, settings=_settings())
    second = await relay_review_requested(session, settings=_settings())

    assert first.notified == (review.id,)
    assert second.examined == 0, "a second pass over the same rows sends nothing"
    assert len(sent) == 1
    _url, body = sent[0]
    assert "Approval requested" in body["text"]
    assert str(review.id) in body["text"]


async def test_an_already_decided_review_is_never_announced(
    session: AsyncSession, wire: Any
) -> None:
    """A review raised and approved between two sweeps produces no message.
    "Please approve this" about something already approved is noise, and the
    decision itself has its own event kind."""
    sent = wire()
    org = await _seed_org(session)
    await _seed_review(session, org, status="APPROVED")

    outcome = await relay_review_requested(session, settings=_settings())

    assert outcome.examined == 0
    assert sent == []


async def test_history_is_stamped_but_not_sent(session: AsyncSession, wire: Any) -> None:
    """The day an operator first configures a webhook, a year of pending
    reviews must not all arrive at once -- but they must also stop being
    re-examined on every pass forever."""
    sent = wire()
    org = await _seed_org(session)
    ancient = await _seed_review(session, org, age=timedelta(days=90))
    fresh = await _seed_review(session, org, age=timedelta(minutes=1))

    outcome = await relay_review_requested(session, settings=_settings())

    assert outcome.notified == (fresh.id,)
    assert outcome.skipped_stale == (ancient.id,)
    assert len(sent) == 1
    assert ancient.review_requested_notified_at is not None, "stamped, so never re-examined"


async def test_the_batch_size_bounds_one_pass(session: AsyncSession, wire: Any) -> None:
    sent = wire()
    org = await _seed_org(session)
    for _ in range(5):
        await _seed_review(session, org)

    outcome = await relay_review_requested(
        session, settings=_settings(governance_review_notify_batch_size=2)
    )

    assert outcome.examined == 2
    assert len(sent) == 2


async def test_the_oldest_pending_review_is_notified_first(
    session: AsyncSession, wire: Any
) -> None:
    """Whoever has waited longest is told about first; a sweep that took the
    newest rows would starve the back of the queue indefinitely."""
    wire()
    org = await _seed_org(session)
    older = await _seed_review(session, org, age=timedelta(hours=6))
    await _seed_review(session, org, age=timedelta(minutes=1))

    outcome = await relay_review_requested(
        session, settings=_settings(governance_review_notify_batch_size=1)
    )

    assert outcome.notified == (older.id,)


# ---------------------------------------------------------------------------
# Value freedom and the ledger
# ---------------------------------------------------------------------------


async def test_the_message_carries_no_object_value(session: AsyncSession, wire: Any) -> None:
    """INV-6 at the one place the platform sends data outward. The object type
    and the review's own id identify what needs approving; the *content* being
    approved never leaves."""
    sent = wire()
    org = await _seed_org(session)
    await _seed_review(session, org, object_type="GLOSSARY_TERM", requested_by="steward-1")
    review = await _seed_review(session, org)
    review.decision_reason = f"rejected because it named {SENTINEL}"
    await session.flush()

    await relay_review_requested(session, settings=_settings())

    for _url, body in sent:
        assert SENTINEL not in body["text"]


async def test_each_attempt_lands_in_the_notification_ledger(
    session: AsyncSession, wire: Any
) -> None:
    wire()
    org = await _seed_org(session)
    await _seed_review(session, org)

    await relay_review_requested(session, settings=_settings())
    await session.flush()

    rows = (await session.scalars(select(NotificationEventRecord))).all()
    assert [row.status for row in rows if row.channel == "SLACK"] == ["SENT"]
    assert all(row.incident_id is None for row in rows), "a governance event has no incident"


async def test_a_downed_channel_does_not_lose_the_event_or_raise(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep runs outside the governance transaction, so the worst a
    broken webhook can do is record a FAILED attempt. It must not raise into
    the scheduler and stop every later sweep in the same iteration."""
    org = await _seed_org(session)
    await _seed_review(session, org)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(
        "aida.governance_notifications.httpx.AsyncClient",
        lambda *a, **kw: original(*a, **{**kw, "transport": transport}),
    )

    outcome = await relay_review_requested(session, settings=_settings())
    await session.flush()

    assert len(outcome.notified) == 1
    rows = (await session.scalars(select(NotificationEventRecord))).all()
    assert [row.status for row in rows if row.channel == "SLACK"] == ["FAILED"]
