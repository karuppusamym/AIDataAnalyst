"""AR-11: the reviewer agent's oversight is measured, reported and acted on.

The 2026-09-09 review found the enforceable half of AR-11 done -- the agent
stops deciding once its unread audit sample passes a bound -- and everything
measured missing. This is the measured half:

* disagreement by **risk tier** as well as by object type. Only approvals are
  sampled, so a tier's disagreement rate is its sampled false-approval rate;
* **how long the sample waits** for a human -- median, 90th percentile and
  slowest -- and the age of the oldest sample still unread;
* the **backlog** on the state endpoint an operator actually looks at, with
  the bound and whether the agent is stopped by it now;
* the backlog refusal as an **event** -- an audit row and an outbox event that
  survive the run's rollback, and a governance notification -- not only a 409;
* a human **disputing** a sampled decision notifies, because that is where a
  correction starts. The correction procedure is
  `Docs/40-engineering/11-reviewer-agent-oversight-runbook.md`.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.main  # noqa: F401 -- registers every table on Base.metadata
from aida import agent_contract_api
from aida.agent_contract_api import (
    ResolveSampleRequest,
    get_disagreement_rates,
    get_reviewer_agent_state,
    resolve_sample,
    run_reviewer_agent,
)
from aida.config import Settings
from aida.db import Base
from aida.governance_notifications import EVENT_KINDS, deep_link
from aida.models import Organization, ReviewAuditSample
from aida.reviewer_agent import REASON_AUDIT_BACKLOG, REASON_DISABLED
from aida.reviewer_agent_metrics import AuditResolutionTime, disagreement_rates
from aida.security import SecurityContext
from atlas.modules.observability_audit.models import AuditEvent, OutboxEvent
from tests.support.doubles import security_context

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
_BACKLOG_EVENT = "reviewer_agent.audit_backlog_exceeded.v1"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "reviewer_agent_enabled": True,
        "reviewer_agent_principal_id": "agent:reviewer",
        "reviewer_agent_max_tier": "T1",
        "reviewer_agent_max_unresolved_samples": 50,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


def _context(org: Organization, principal_id: str = "steward-a") -> SecurityContext:
    return security_context(
        organization_id=org.id,
        principal_id=principal_id,
        roles=frozenset({"Reviewer", "PlatformAdmin"}),
    )


def _sample(
    org: Organization,
    *,
    tier: str,
    outcome: str,
    sampled_hours_ago: float,
    waited_hours: float | None = None,
    object_type: str = "ASSET_DESCRIPTION_DRAFT",
) -> ReviewAuditSample:
    sampled_at = NOW - timedelta(hours=sampled_hours_ago)
    resolved = outcome != "PENDING"
    return ReviewAuditSample(
        organization_id=org.id,
        governance_review_id=uuid4(),
        agent_principal_id="agent:reviewer",
        object_type=object_type,
        risk_tier=tier,
        decision="APPROVED",
        sampled_at=sampled_at,
        human_outcome=outcome,
        human_principal_id="steward-a" if resolved else None,
        human_rationale="checked against the source" if resolved else None,
        resolved_at=sampled_at + timedelta(hours=waited_hours) if waited_hours else None,
    )


async def _seed_mixed(session: AsyncSession, org: Organization) -> None:
    """Five approvals: at T0 one agreed, one disputed, one unread; at T1 two
    agreed. The four verdicts waited 2, 10, 4 and 6 hours."""
    session.add_all(
        [
            _sample(org, tier="T0", outcome="AGREED", sampled_hours_ago=40, waited_hours=2),
            _sample(org, tier="T0", outcome="DISAGREED", sampled_hours_ago=40, waited_hours=10),
            _sample(org, tier="T0", outcome="PENDING", sampled_hours_ago=30),
            _sample(
                org,
                tier="T1",
                outcome="AGREED",
                sampled_hours_ago=20,
                waited_hours=4,
                object_type="GLOSSARY_LINK_PROPOSAL",
            ),
            _sample(
                org,
                tier="T1",
                outcome="AGREED",
                sampled_hours_ago=20,
                waited_hours=6,
                object_type="GLOSSARY_LINK_PROPOSAL",
            ),
        ]
    )
    await session.flush()


class _Notifications:
    """Stands in for `notify_safely` and keeps what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self, _session: object, _organization_id: object, event_kind: str, payload: dict[str, Any],
        **_kwargs: object,
    ) -> None:
        self.sent.append((event_kind, payload))


# --- measured ---------------------------------------------------------------


async def test_the_disagreement_rate_is_reported_by_risk_tier(session: AsyncSession) -> None:
    org = await _org(session)
    await _seed_mixed(session, org)

    report = await disagreement_rates(session, org.id, now=NOW)

    tiers = {row.risk_tier: row for row in report.by_risk_tier}
    assert (tiers["T0"].resolved, tiers["T0"].disagreed, tiers["T0"].pending) == (2, 1, 1)
    assert tiers["T0"].disagreement_rate == 0.5
    assert tiers["T1"].disagreement_rate == 0.0
    # The same five samples, cut the other way.
    assert sum(row.sampled for row in report.by_object_type) == 5
    assert sum(row.sampled for row in report.by_risk_tier) == 5


async def test_the_report_says_how_long_the_sample_waited_for_a_human(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    await _seed_mixed(session, org)

    resolution = (await disagreement_rates(session, org.id, now=NOW)).resolution

    assert resolution == AuditResolutionTime(
        resolved=4,
        median_hours=5.0,
        p90_hours=10.0,
        max_hours=10.0,
        pending=1,
        oldest_pending_hours=30.0,
    )


async def test_an_unmeasured_clock_is_none_not_zero(session: AsyncSession) -> None:
    org = await _org(session)

    resolution = (await disagreement_rates(session, org.id, now=NOW)).resolution

    assert resolution == AuditResolutionTime(
        resolved=0,
        median_hours=None,
        p90_hours=None,
        max_hours=None,
        pending=0,
        oldest_pending_hours=None,
    )


async def test_the_api_report_carries_the_tier_cut_and_the_clock(session: AsyncSession) -> None:
    org = await _org(session)
    await _seed_mixed(session, org)

    report = await get_disagreement_rates(
        org.id, window_days=365, context=_context(org), session=session
    )

    assert [row.risk_tier for row in report.by_risk_tier] == ["T0", "T1"]
    assert report.resolution.resolved == 4
    assert report.resolution.pending == 1


# --- reported ---------------------------------------------------------------


async def test_the_state_endpoint_reports_the_backlog_and_its_bound(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    session.add_all(
        [_sample(org, tier="T0", outcome="PENDING", sampled_hours_ago=h) for h in (1, 2)]
    )
    await session.flush()

    async def state(bound: int) -> tuple[int, int, bool]:
        read = await get_reviewer_agent_state(
            org.id,
            context=_context(org),
            session=session,
            settings=_settings(reviewer_agent_max_unresolved_samples=bound),
        )
        return read.unresolved_samples, read.max_unresolved_samples, read.audit_backlog_exceeded

    assert await state(3) == (2, 3, False)
    assert await state(2) == (2, 2, True)
    # A zero bound disables the check, here as in the agent itself.
    assert await state(0) == (2, 0, False)


# --- acted on ---------------------------------------------------------------


async def test_a_backlog_refusal_is_recorded_and_notified_despite_the_rollback(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await _org(session)
    session.add(_sample(org, tier="T0", outcome="PENDING", sampled_hours_ago=1))
    await session.commit()
    notifications = _Notifications()
    monkeypatch.setattr(agent_contract_api, "notify_safely", notifications)

    with pytest.raises(HTTPException) as refused:
        await run_reviewer_agent(
            org.id,
            limit=10,
            context=_context(org, "ops-a"),
            session=session,
            settings=_settings(reviewer_agent_max_unresolved_samples=1),
        )

    assert (refused.value.status_code, refused.value.detail) == (409, REASON_AUDIT_BACKLOG)
    outbox = (
        await session.scalars(select(OutboxEvent).where(OutboxEvent.event_type == _BACKLOG_EVENT))
    ).all()
    assert [event.payload for event in outbox] == [
        {"unresolved_samples": 1, "max_unresolved_samples": 1}
    ]
    audit = (
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "reviewer_agent.run"))
    ).all()
    assert [(row.outcome, row.principal_id) for row in audit] == [("DENIED", "ops-a")]
    assert [kind for kind, _payload in notifications.sent] == ["REVIEWER_AGENT_AUDIT_BACKLOG"]


async def test_other_refusals_are_not_backlog_events(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await _org(session)
    await session.commit()
    notifications = _Notifications()
    monkeypatch.setattr(agent_contract_api, "notify_safely", notifications)

    with pytest.raises(HTTPException) as refused:
        await run_reviewer_agent(
            org.id,
            limit=10,
            context=_context(org),
            session=session,
            settings=_settings(reviewer_agent_enabled=False),
        )

    assert refused.value.detail == REASON_DISABLED
    assert (
        await session.scalars(select(OutboxEvent).where(OutboxEvent.event_type == _BACKLOG_EVENT))
    ).all() == []
    assert notifications.sent == []


async def test_disputing_a_sampled_decision_notifies_and_agreeing_does_not(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await _org(session)
    disputed = _sample(org, tier="T1", outcome="PENDING", sampled_hours_ago=3)
    agreed = _sample(org, tier="T0", outcome="PENDING", sampled_hours_ago=3)
    session.add_all([disputed, agreed])
    await session.commit()
    notifications = _Notifications()
    monkeypatch.setattr(agent_contract_api, "notify_safely", notifications)

    for sample, outcome in ((agreed, "AGREED"), (disputed, "DISAGREED")):
        await resolve_sample(
            org.id,
            sample.id,
            ResolveSampleRequest(human_outcome=outcome, rationale="checked the object"),
            context=_context(org),
            session=session,
        )

    [(kind, payload)] = notifications.sent
    assert kind == "REVIEWER_AGENT_SAMPLE_DISAGREED"
    # The link a notification carries is the review, where the object is.
    assert payload["object_id"] == str(disputed.governance_review_id)
    assert payload["risk_tier"] == "T1"


def test_both_oversight_events_are_delivered_by_default_and_link_to_the_agent() -> None:
    settings = _settings(portal_base_url="https://portal.example")
    for kind in ("REVIEWER_AGENT_AUDIT_BACKLOG", "REVIEWER_AGENT_SAMPLE_DISAGREED"):
        assert kind in EVENT_KINDS
        assert kind in settings.governance_notification_events
        assert deep_link(settings, kind, object_id=None) == "https://portal.example/#/reviewer-agent"
