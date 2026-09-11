"""ADR-0029: scheduled task-agent runs, and the surfaces that count them.

* Off by default: no interval, no run, no session opened.
* A scheduled run is the governed run -- same contract, kill switch and audit
  -- started by the scheduler's worker identity, once per interval, only in an
  organization that registered the agent. A refusal is recorded and the pass
  goes on.
* Task agents write no `AgentRun`. The inbox now counts their runs from the
  audit rows every run leaves, completed and refused, and the roster says where
  their runs are instead of implying there were none.
* Every agent principal setting belongs to a registered task agent or to the
  reviewer agent, so a new agent cannot be left off the scheduler and the
  inbox.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from aida.agent_contract_api import get_agent_inbox
from aida.agent_roster import compose_agent_roster
from aida.models import AgentContract, AuditEvent, Organization
from aida.task_agent_registry import TASK_AGENTS, task_agent_for_principal
from aida.task_agent_schedule import (
    SCHEDULER_PRINCIPAL,
    run_task_agent_schedule_pass,
    task_agent_due,
)
from atlas.platform.config import Settings
from tests.support.task_agents import (
    agent_settings,
    human,
    register_agent,
    seed_estate,
    task_agent_maker,
)

AGENT = "agent:steward"
T0 = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def maker() -> Any:
    async with task_agent_maker() as session_maker:
        yield session_maker


async def _registered_org(
    maker: async_sessionmaker[Any], *, kill_engaged: bool = False
) -> tuple[UUID, AgentContract]:
    async with maker() as session:
        org, _datasource, _schema = await seed_estate(session)
        contract = await register_agent(
            session, org, principal=AGENT, kill_engaged=kill_engaged
        )
        await session.commit()
        return org.id, contract


async def _run_audits(maker: async_sessionmaker[Any]) -> list[AuditEvent]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.action == "steward_agent.run")
                    .order_by(AuditEvent.id)
                )
            ).all()
        )


def test_due_means_never_at_zero_and_once_per_interval() -> None:
    assert task_agent_due(None, T0, 0) is False
    assert task_agent_due(None, T0, 60) is True
    assert task_agent_due(T0, T0 + timedelta(minutes=59), 60) is False
    assert task_agent_due(T0, T0 + timedelta(minutes=60), 60) is True


def test_every_agent_principal_setting_is_a_registered_task_agent_or_the_reviewer() -> None:
    principal_settings = {
        name for name in Settings.model_fields if name.endswith("_agent_principal_id")
    }
    assert principal_settings - {"reviewer_agent_principal_id"} == {
        agent.spec.principal_setting for agent in TASK_AGENTS
    }
    defaults = Settings(_env_file=None, environment="test")  # type: ignore[call-arg]
    assert {agent.spec.interval_minutes(defaults) for agent in TASK_AGENTS} == {0}
    assert task_agent_for_principal(defaults, "agent:reviewer") is None


async def test_nothing_is_scheduled_by_default(maker: async_sessionmaker[Any]) -> None:
    await _registered_org(maker)

    started = await run_task_agent_schedule_pass(
        agent_settings(), now=T0, session_maker=maker, last_run_at={}
    )

    assert started == 0
    assert await _run_audits(maker) == []


async def test_a_registered_agent_runs_once_per_interval_as_the_scheduler(
    maker: async_sessionmaker[Any],
) -> None:
    org_id, _contract = await _registered_org(maker)
    async with maker() as session:
        # A second organization that never registered the agent.
        await seed_estate(session)
        await session.commit()
    settings = agent_settings(steward_agent_interval_minutes=60)
    tracker: dict[Any, datetime] = {}

    first = await run_task_agent_schedule_pass(
        settings, now=T0, session_maker=maker, last_run_at=tracker
    )
    early = await run_task_agent_schedule_pass(
        settings, now=T0 + timedelta(minutes=30), session_maker=maker, last_run_at=tracker
    )
    later = await run_task_agent_schedule_pass(
        settings, now=T0 + timedelta(minutes=61), session_maker=maker, last_run_at=tracker
    )

    assert (first, early, later) == (1, 0, 1)
    audits = await _run_audits(maker)
    assert [(a.organization_id, a.principal_id, a.outcome) for a in audits] == [
        (org_id, SCHEDULER_PRINCIPAL, "SUCCESS"),
        (org_id, SCHEDULER_PRINCIPAL, "SUCCESS"),
    ]


async def test_a_refused_scheduled_run_is_recorded_against_its_version(
    maker: async_sessionmaker[Any],
) -> None:
    killed_org, killed = await _registered_org(maker, kill_engaged=True)
    live_org, _live = await _registered_org(maker)

    started = await run_task_agent_schedule_pass(
        agent_settings(steward_agent_interval_minutes=60),
        now=T0,
        session_maker=maker,
        last_run_at={},
    )

    assert started == 2
    by_org = {a.organization_id: a for a in await _run_audits(maker)}
    assert (by_org[killed_org].outcome, by_org[killed_org].details["reason"]) == (
        "DENIED",
        "agent_kill_switch_engaged",
    )
    assert by_org[killed_org].resource_id == str(killed.ai_asset_version_id)
    assert by_org[live_org].outcome == "SUCCESS"


async def test_the_inbox_counts_task_agent_runs_and_refusals(
    maker: async_sessionmaker[Any],
) -> None:
    org_id, contract = await _registered_org(maker)
    settings = agent_settings(steward_agent_interval_minutes=60)
    tracker: dict[Any, datetime] = {}
    await run_task_agent_schedule_pass(settings, now=T0, session_maker=maker, last_run_at=tracker)
    async with maker() as session:
        await session.execute(
            update(AgentContract).where(AgentContract.id == contract.id).values(kill_engaged=True)
        )
        await session.commit()
    await run_task_agent_schedule_pass(
        settings, now=T0 + timedelta(hours=2), session_maker=maker, last_run_at=tracker
    )

    async with maker() as session:
        org = await session.get(Organization, org_id)
        assert org is not None
        inbox = await get_agent_inbox(
            org_id,
            persona="STEWARD",
            limit=50,
            since_hours=24 * 365 * 5,
            context=human(org),
            session=session,
            settings=settings,
        )

    [agent] = inbox.agents
    assert (agent.runs_recent, agent.success_rate) == (2, 0.5)


async def test_the_roster_says_where_a_task_agents_runs_are(
    maker: async_sessionmaker[Any],
) -> None:
    org_id, _contract = await _registered_org(maker)

    async with maker() as session:
        roster = await compose_agent_roster(
            session, organization_id=org_id, settings=agent_settings()
        )

    [entry] = roster.agents
    assert "steward_agent.run" in entry.method.note
    assert entry.method.sampled_runs == 0
