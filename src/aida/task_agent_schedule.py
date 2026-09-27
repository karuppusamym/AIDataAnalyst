"""ADR-0029: scheduled task-agent runs, off by default.

A person can start any task agent from its console. This adds the other way:
a positive `<key>_agent_interval_minutes` makes the scheduler start that agent
in every organization that has registered it, once per interval. The default
is 0, which means never, so a deployment that sets nothing sees no change.

A scheduled run is the run a person starts, through the same
`task_agent.run_task_agent`. The contract, the kill switch, the tier, the
bounds and the ledger apply unchanged; only `triggered_by` differs -- the
scheduler's worker identity instead of a person's. A refused run is rolled
back and recorded as refused, exactly as the HTTP path does, and one
organization's refusal or failure never stops the pass for the others.

Organizations are those with a contract for the agent's principal on an
APPROVED version: anywhere else a run could only be refused. Due-ness is kept
in process memory per (agent, organization), the trade-off the owner-routing
and rule-pack passes make: a scheduler restart costs at most one early run, and
a run is bounded and safe to repeat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aida.models import AgentContract, AiAssetVersion
from aida.security import SecurityContext
from aida.task_agent import (
    TaskAgentOutcome,
    TaskAgentRefused,
    TaskAgentRunRequest,
    record_task_agent_refusal,
    run_task_agent,
)
from aida.task_agent_registry import TASK_AGENTS, RegisteredTaskAgent
from atlas.platform.config import Settings

logger = structlog.get_logger(__name__)

#: Who a scheduled run is attributed to. The agent still acts as its own
#: principal; this names who started the run, as a person's id does otherwise.
SCHEDULER_PRINCIPAL: Final = "fleet-scheduler"

_last_run_at: dict[tuple[str, UUID], datetime] = {}


def scheduler_context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id=SCHEDULER_PRINCIPAL,
        principal_type="WORKER",
        organization_id=organization_id,
        roles=frozenset({"SchedulerWorker"}),
    )


def task_agent_due(last_run_at: datetime | None, now: datetime, interval_minutes: int) -> bool:
    """Never when the interval is 0 or less; always when it has never run."""
    if interval_minutes <= 0:
        return False
    return last_run_at is None or now - last_run_at >= timedelta(minutes=interval_minutes)


async def registered_organizations(session: AsyncSession, *, principal_id: str) -> list[UUID]:
    rows = await session.scalars(
        select(AgentContract.organization_id)
        .join(AiAssetVersion, AiAssetVersion.id == AgentContract.ai_asset_version_id)
        .where(
            AgentContract.agent_principal_id == principal_id,
            AiAssetVersion.status == "APPROVED",
        )
        .distinct()
        .order_by(AgentContract.organization_id)
    )
    return list(rows.all())


async def run_scheduled_task_agent(
    session: AsyncSession,
    organization_id: UUID,
    agent: RegisteredTaskAgent,
    settings: Settings,
) -> TaskAgentOutcome | None:
    """One scheduled run, committed -- or `None` when it was refused, in which
    case its writes were rolled back and the refusal recorded."""
    context = scheduler_context(organization_id)
    try:
        outcome = await run_task_agent(
            session,
            organization_id,
            spec=agent.spec,
            work=agent.work,
            request=TaskAgentRunRequest(limit=agent.spec.max_proposals_per_run(settings)),
            settings=settings,
            triggered_by=context,
        )
    except TaskAgentRefused as exc:
        await session.rollback()
        record_task_agent_refusal(
            session,
            organization_id,
            spec=agent.spec,
            triggered_by=context,
            reason_code=exc.reason_code,
            ai_asset_version_id=exc.ai_asset_version_id,
        )
        await session.commit()
        return None
    await session.commit()
    return outcome


def _default_session_maker() -> async_sessionmaker[AsyncSession]:
    # Resolved at call time: `aida.db` builds the engine lazily, and a
    # deployment that schedules nothing never needs one here.
    from aida.db import session_factory

    return session_factory


async def run_task_agent_schedule_pass(
    settings: Settings,
    *,
    now: datetime | None = None,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    last_run_at: dict[tuple[str, UUID], datetime] | None = None,
) -> int:
    """Start every scheduled task agent that is due, in every organization that
    registered it. Returns how many runs were started, refused ones included.
    Does nothing, and opens no session, when no agent has an interval."""
    scheduled = [agent for agent in TASK_AGENTS if agent.spec.interval_minutes(settings) > 0]
    if not scheduled:
        return 0
    effective_now = now or datetime.now(UTC)
    tracker = _last_run_at if last_run_at is None else last_run_at
    maker = session_maker or _default_session_maker()
    started = 0
    for agent in scheduled:
        interval = agent.spec.interval_minutes(settings)
        async with maker() as session:
            organizations = await registered_organizations(
                session, principal_id=agent.spec.principal(settings)
            )
        for organization_id in organizations:
            key = (agent.spec.key, organization_id)
            if not task_agent_due(tracker.get(key), effective_now, interval):
                continue
            try:
                async with maker() as session:
                    await run_scheduled_task_agent(session, organization_id, agent, settings)
            except Exception:  # noqa: BLE001 -- one organization must not stop the pass
                logger.exception(
                    "task_agent_scheduled_run_failed",
                    agent=agent.spec.key,
                    organization_id=str(organization_id),
                )
                continue
            tracker[key] = effective_now
            started += 1
    return started
