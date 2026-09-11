"""ADR-0029: the HTTP shapes every task agent shares.

Each agent's router (`steward_agent_api`, ...) owns its two paths, its roles and
its request model. The state and run responses, and the translation of a
refused run into a rolled-back 409, are the same for every agent and live here,
so one screen component renders any of them and no agent can report its state
differently from another.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import DataSource
from aida.review_risk_tiers import risk_tier_for
from aida.schemas import ApiModel
from aida.security import SecurityContext
from aida.task_agent import (
    ACTION_FAILED,
    ACTION_PROPOSED,
    ACTION_SKIPPED,
    ACTION_WOULD_PROPOSE,
    QUEUE_GOVERNANCE_REVIEW,
    CapabilityWork,
    TaskAgentOutcome,
    TaskAgentRefused,
    TaskAgentRunRequest,
    TaskAgentSpec,
    TaskAgentStatus,
    mode_for,
    record_task_agent_refusal,
    run_task_agent,
)
from atlas.platform.config import Settings

#: Who may read any task agent's state: the agent-contract readers plus the
#: persona roles whose backlogs these agents work.
TASK_AGENT_READERS = (
    "PlatformAdmin",
    "AgentDeveloper",
    "ModelRiskManager",
    "Reviewer",
    "MetadataReviewer",
    "Auditor",
    "Operations",
    "MetadataAdmin",
    "SemanticAdmin",
    "DataAdmin",
    "DataSteward",
)


class TaskAgentCapabilityRead(ApiModel):
    capability: str
    #: What its proposals are decided as.
    object_type: str
    #: Where they are decided: the shared `GOVERNANCE_REVIEW` queue, or a
    #: dedicated human-only queue such as `PARSED_LINEAGE_REVIEW`.
    review_queue: str
    #: The object type's ADR-0027 tier. `None` for a dedicated queue, which the
    #: tier table does not classify because no agent can decide from it.
    risk_tier: str | None
    producer: str


class TaskAgentOutcomeRead(ApiModel):
    object_type: str
    pending: int
    approved: int
    rejected: int
    other: int
    #: Approved over decided; `None` until something has been decided.
    acceptance_rate: float | None


class TaskAgentStateRead(ApiModel):
    agent_key: str
    organization_id: UUID
    agent_principal_id: str
    #: Whether a run would be allowed to start from the contract's point of
    #: view. `refusal_reason` names the refusal when it would not.
    registered: bool
    refusal_reason: str | None
    ai_asset_version_id: UUID | None
    agent_name: str | None
    autonomy_tier: str | None
    #: `OBSERVE` for a T0 contract, `PROPOSE` for T1 and above.
    mode: Literal["OBSERVE", "PROPOSE"] | None
    supervisor_persona: str | None
    kill_engaged: bool | None
    #: Set when a kill switch -- this agent's, its tier's, the organization's or
    #: the model gateway's -- would stop a run now.
    blocking_reason: str | None
    method: str
    #: Always false today: no task agent calls a model.
    uses_model: bool
    capabilities: list[TaskAgentCapabilityRead]
    max_proposals_per_run: int
    #: The review-backlog bound; 0 means unbounded.
    max_pending_proposals: int
    pending_proposals: int
    wall_clock_seconds_cap: int | None
    #: How often the scheduler starts it, in minutes; 0 means only when a
    #: person does.
    interval_minutes: int
    outcomes: list[TaskAgentOutcomeRead]


class TaskAgentRunItemRead(ApiModel):
    capability: str
    subject_id: UUID
    subject_name: str
    action: Literal["PROPOSED", "WOULD_PROPOSE", "SKIPPED", "FAILED"]
    reason: str | None
    object_type: str | None
    object_id: UUID | None
    review_id: UUID | None
    task_id: UUID | None
    confidence: float | None
    rank: int | None
    related_id: UUID | None
    related_name: str | None


class TaskAgentRunRead(ApiModel):
    run_id: str
    agent_key: str
    organization_id: UUID
    agent_principal_id: str
    ai_asset_version_id: UUID
    autonomy_tier: str
    mode: Literal["OBSERVE", "PROPOSE"]
    dry_run: bool
    #: The limit actually applied, after the server-side clamp.
    limit: int
    capabilities: list[str]
    started_at: datetime
    finished_at: datetime
    proposed: int
    would_propose: int
    skipped: int
    failed: int
    skipped_by_reason: dict[str, int]
    #: Set when a budget ended the run early; what it did before is kept.
    stopped_reason: str | None
    items: list[TaskAgentRunItemRead]


def task_agent_state_read(
    organization_id: UUID, state: TaskAgentStatus, spec: TaskAgentSpec, settings: Settings
) -> TaskAgentStateRead:
    authority = state.authority
    contract = authority.contract if authority is not None else None
    return TaskAgentStateRead(
        agent_key=spec.key,
        organization_id=organization_id,
        agent_principal_id=state.agent_principal_id,
        registered=authority is not None,
        refusal_reason=state.refusal_reason,
        ai_asset_version_id=authority.version.id if authority is not None else None,
        agent_name=authority.version.name if authority is not None else None,
        autonomy_tier=contract.autonomy_tier if contract is not None else None,
        mode=mode_for(contract.autonomy_tier) if contract is not None else None,
        supervisor_persona=contract.supervisor_persona if contract is not None else None,
        kill_engaged=contract.kill_engaged if contract is not None else None,
        blocking_reason=state.blocking_reason,
        method=spec.method,
        uses_model=False,
        capabilities=[
            TaskAgentCapabilityRead(
                capability=capability.key,
                object_type=capability.object_type,
                review_queue=capability.queue,
                risk_tier=(
                    risk_tier_for(capability.object_type)
                    if capability.queue == QUEUE_GOVERNANCE_REVIEW
                    else None
                ),
                producer=capability.producer,
            )
            for capability in spec.capabilities
        ],
        max_proposals_per_run=spec.max_proposals_per_run(settings),
        max_pending_proposals=spec.max_pending_proposals(settings),
        pending_proposals=state.pending_proposals,
        wall_clock_seconds_cap=contract.wall_clock_seconds_cap if contract is not None else None,
        interval_minutes=spec.interval_minutes(settings),
        outcomes=[
            TaskAgentOutcomeRead(
                object_type=row.object_type,
                pending=row.pending,
                approved=row.approved,
                rejected=row.rejected,
                other=row.other,
                acceptance_rate=row.acceptance_rate,
            )
            for row in state.outcomes
        ],
    )


def task_agent_run_read(outcome: TaskAgentOutcome) -> TaskAgentRunRead:
    return TaskAgentRunRead(
        run_id=outcome.run_id,
        agent_key=outcome.agent_key,
        organization_id=outcome.organization_id,
        agent_principal_id=outcome.agent_principal_id,
        ai_asset_version_id=outcome.ai_asset_version_id,
        autonomy_tier=outcome.autonomy_tier,
        mode=outcome.mode,
        dry_run=outcome.dry_run,
        limit=outcome.limit,
        capabilities=list(outcome.capabilities),
        started_at=outcome.started_at,
        finished_at=outcome.finished_at or outcome.started_at,
        proposed=outcome.count(ACTION_PROPOSED),
        would_propose=outcome.count(ACTION_WOULD_PROPOSE),
        skipped=outcome.count(ACTION_SKIPPED),
        failed=outcome.count(ACTION_FAILED),
        skipped_by_reason=outcome.skipped_by_reason(),
        stopped_reason=outcome.stopped_reason,
        items=[
            TaskAgentRunItemRead(
                capability=item.capability,
                subject_id=item.subject_id,
                subject_name=item.subject_name,
                action=item.action,
                reason=item.reason,
                object_type=item.object_type,
                object_id=item.object_id,
                review_id=item.review_id,
                task_id=item.task_id,
                confidence=item.confidence,
                rank=item.rank,
                related_id=item.related_id,
                related_name=item.related_name,
            )
            for item in outcome.items
        ],
    )


async def require_datasource_in_organization(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID | None
) -> None:
    """404 for a datasource that is not this organization's (INV-5)."""
    if datasource_id is None:
        return
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None or datasource.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="datasource not found")


async def execute_task_agent_run(
    session: AsyncSession,
    organization_id: UUID,
    *,
    spec: TaskAgentSpec,
    work: Mapping[str, CapabilityWork],
    request: TaskAgentRunRequest,
    settings: Settings,
    context: SecurityContext,
) -> TaskAgentRunRead:
    """Run an agent once and commit it -- or, when it is refused, roll the run
    back whole, record the refusal in a fresh transaction, and answer 409 with
    the stable reason code."""
    try:
        outcome = await run_task_agent(
            session,
            organization_id,
            spec=spec,
            work=work,
            request=request,
            settings=settings,
            triggered_by=context,
        )
    except TaskAgentRefused as exc:
        await session.rollback()
        record_task_agent_refusal(
            session,
            organization_id,
            spec=spec,
            triggered_by=context,
            reason_code=exc.reason_code,
            ai_asset_version_id=exc.ai_asset_version_id,
        )
        await session.commit()
        raise HTTPException(status_code=409, detail=exc.reason_code) from exc
    await session.commit()
    return task_agent_run_read(outcome)
