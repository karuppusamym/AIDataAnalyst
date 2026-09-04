"""AG-10 / ADR-0027: the agent contract, task ledger, inbox and reviewer agent.

One router because these four surfaces are one product idea -- an agent
workforce a human supervises -- and splitting them would put the contract in
one file, the thing it governs in another, and the screen that shows both in
a third.

Everything here is a read or a governed write. No endpoint executes an agent;
the orchestrator does that, and it enforces the contract this router edits
(`aida.agent_contracts`).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contracts import (
    AgentContractDefinition,
    AgentContractValidationError,
    CapabilityEnvelope,
    load_agent_asset_version,
    load_agent_contract,
    parse_capability_envelope,
    validate_contract_definition,
)
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    AGENT_SAMPLING_RATE_FLOOR,
    AgentContract,
    AgentRun,
    AgentTask,
    AiAsset,
    AiAssetVersion,
    GovernanceReview,
    Organization,
    ReviewAuditSample,
)
from aida.review_risk_tiers import risk_tier_for
from aida.reviewer_agent import (
    ReviewerAgentUnavailable,
    auto_decide_tier0_tier1,
    organization_suspended,
    pre_review_pending,
    resolve_audit_sample,
    set_suspended,
)
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["agent-workforce"])

#: Who may author or change an agent's contract. Deliberately narrow: a
#: contract is the agent's authority, so editing one is a T3-shaped action
#: even though the contract itself is not routed through review.
CONTRACT_AUTHORS = ("PlatformAdmin", "AgentDeveloper", "ModelRiskManager")
CONTRACT_READERS = (*CONTRACT_AUTHORS, "Reviewer", "Auditor", "DataSteward", "Operations")
INBOX_READERS = (
    "PlatformAdmin",
    "AgentDeveloper",
    "ModelRiskManager",
    "Reviewer",
    "MetadataReviewer",
    "Auditor",
    "DataSteward",
    "Operations",
    "Analyst",
)
REVIEWER_AGENT_OPERATORS = ("PlatformAdmin", "Reviewer", "MetadataReviewer")

#: Personas that see the whole organization's pending queue. Everyone else
#: sees only what they proposed -- an analyst's inbox is their own work, not
#: a window onto the governance backlog.
_QUEUE_WIDE_PERSONAS = frozenset({"STEWARD", "REVIEWER", "OPERATOR", "AUDITOR"})


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CapabilityEnvelopeModel(ApiModel):
    tool_slugs: list[str] = Field(default_factory=list)
    context_product_ids: list[str] = Field(default_factory=list)
    write_lanes: list[str] = Field(default_factory=list)


class AgentContractWrite(ApiModel):
    agent_principal_id: str = Field(min_length=1, max_length=255)
    capability_envelope: CapabilityEnvelopeModel = Field(
        default_factory=CapabilityEnvelopeModel
    )
    autonomy_tier: Literal["T0", "T1", "T2", "T3"] = "T0"
    supervisor_persona: Literal[
        "ANALYST", "CONSUMER", "STEWARD", "REVIEWER", "OPERATOR", "AUDITOR"
    ]
    kill_scope: Literal["AGENT", "TIER", "ALL"] = "AGENT"
    sampling_rate: float = Field(default=AGENT_SAMPLING_RATE_FLOOR, ge=0.0, le=1.0)
    daily_token_cap: int | None = None
    per_run_token_cap: int | None = None
    wall_clock_seconds_cap: int | None = None
    eval_gate_threshold: float | None = None


class AgentContractRead(ApiModel):
    id: UUID
    organization_id: UUID
    ai_asset_version_id: UUID
    agent_principal_id: str
    capability_envelope: dict[str, Any]
    autonomy_tier: str
    supervisor_persona: str
    kill_scope: str
    kill_engaged: bool
    sampling_rate: float
    daily_token_cap: int | None
    per_run_token_cap: int | None
    wall_clock_seconds_cap: int | None
    eval_gate_threshold: float | None
    created_by: str
    created_at: datetime
    updated_at: datetime


class KillSwitchRequest(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)


class AgentTaskRead(ApiModel):
    task_id: UUID
    agent_principal_id: str
    ai_asset_version_id: UUID | None
    agent_run_id: UUID | None
    intent: str
    status: str
    sampled_for_audit: bool
    audit_outcome: str | None
    started_at: datetime
    finished_at: datetime | None


class InboxBudget(ApiModel):
    daily_token_cap: int | None
    daily_tokens_used: int | None


class InboxAgent(ApiModel):
    ai_asset_id: UUID
    version_id: UUID | None
    name: str
    risk_tier: str | None
    autonomy_tier: str
    runs_recent: int
    success_rate: float | None
    budget: InboxBudget
    kill_scope: str
    kill_engaged: bool
    supervisor_persona: str | None


class InboxPendingItem(ApiModel):
    review_id: UUID
    object_type: str
    object_id: str | None
    title: str
    proposed_by: str
    proposed_by_kind: Literal["HUMAN", "AGENT"]
    risk_tier: str
    confidence: float | None
    blast_radius: int | None
    negative_knowledge_hits: int
    recommendation: Literal["APPROVE", "REJECT", "NONE"]
    created_at: datetime


class InboxAutoApplied(ApiModel):
    task_id: UUID
    agent_name: str
    action: str
    object_type: str
    object_id: str | None
    applied_at: datetime
    sampled_for_audit: bool
    audit_outcome: str | None


class InboxRecentTask(ApiModel):
    task_id: UUID
    agent_name: str
    intent: str
    status: str
    started_at: datetime
    finished_at: datetime | None


class InboxSummary(ApiModel):
    pending_decisions: int
    auto_applied_since: int
    sampled_for_audit: int
    agents_active: int
    kill_switch_engaged: bool


class AgentInboxRead(ApiModel):
    organization_id: UUID
    persona: str
    generated_at: datetime
    summary: InboxSummary
    agents: list[InboxAgent]
    pending: list[InboxPendingItem]
    auto_applied: list[InboxAutoApplied]
    recent_tasks: list[InboxRecentTask]


class ReviewerAgentRunResult(ApiModel):
    pre_reviewed: int = 0
    decided: int = 0
    approved: int = 0
    rejected: int = 0
    sampled_for_audit: int = 0


class ReviewAuditSampleRead(ApiModel):
    sample_id: UUID
    governance_review_id: UUID
    agent_principal_id: str
    object_type: str
    risk_tier: str
    decision: str
    sampled_at: datetime
    human_outcome: str
    human_principal_id: str | None
    human_rationale: str | None
    resolved_at: datetime | None


class ResolveSampleRequest(ApiModel):
    human_outcome: Literal["AGREED", "DISAGREED"]
    rationale: str = Field(min_length=1, max_length=4000)


class ReviewerAgentStateRead(ApiModel):
    organization_id: UUID
    enabled: bool
    suspended: bool
    max_tier: str
    sampling_rate: float
    agent_principal_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contract_read(contract: AgentContract) -> AgentContractRead:
    return AgentContractRead(
        id=contract.id,
        organization_id=contract.organization_id,
        ai_asset_version_id=contract.ai_asset_version_id,
        agent_principal_id=contract.agent_principal_id,
        capability_envelope=dict(contract.capability_envelope or {}),
        autonomy_tier=contract.autonomy_tier,
        supervisor_persona=contract.supervisor_persona,
        kill_scope=contract.kill_scope,
        kill_engaged=contract.kill_engaged,
        sampling_rate=contract.sampling_rate,
        daily_token_cap=contract.daily_token_cap,
        per_run_token_cap=contract.per_run_token_cap,
        wall_clock_seconds_cap=contract.wall_clock_seconds_cap,
        eval_gate_threshold=contract.eval_gate_threshold,
        created_by=contract.created_by,
        created_at=contract.created_at,
        updated_at=contract.updated_at,
    )


def _definition_from(body: AgentContractWrite) -> AgentContractDefinition:
    try:
        envelope = parse_capability_envelope(body.capability_envelope.model_dump())
    except AgentContractValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    return AgentContractDefinition(
        agent_principal_id=body.agent_principal_id,
        capability_envelope=CapabilityEnvelope(
            tool_slugs=envelope.tool_slugs,
            context_product_ids=envelope.context_product_ids,
            write_lanes=envelope.write_lanes,
        ),
        autonomy_tier=body.autonomy_tier,
        supervisor_persona=body.supervisor_persona,
        kill_scope=body.kill_scope,
        sampling_rate=body.sampling_rate,
        daily_token_cap=body.daily_token_cap,
        per_run_token_cap=body.per_run_token_cap,
        wall_clock_seconds_cap=body.wall_clock_seconds_cap,
        eval_gate_threshold=body.eval_gate_threshold,
    )


async def _require_agent_version(
    session: AsyncSession, organization_id: UUID, ai_asset_version_id: UUID
) -> tuple[AiAsset, AiAssetVersion]:
    found = await load_agent_asset_version(
        session, organization_id=organization_id, ai_asset_version_id=ai_asset_version_id
    )
    if found is None:
        raise HTTPException(
            status_code=404, detail="AGENT-kind AI asset version not found in this organization"
        )
    return found


# ---------------------------------------------------------------------------
# Contract CRUD
# ---------------------------------------------------------------------------


@router.get(
    "/organizations/{organization_id}/agents/{ai_asset_version_id}/contract",
    response_model=AgentContractRead,
)
async def get_agent_contract(
    organization_id: UUID,
    ai_asset_version_id: UUID,
    context: SecurityContext = Depends(require_roles(*CONTRACT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> AgentContractRead:
    enforce_organization(context, organization_id)
    await _require_agent_version(session, organization_id, ai_asset_version_id)
    contract = await load_agent_contract(
        session, organization_id=organization_id, ai_asset_version_id=ai_asset_version_id
    )
    if contract is None:
        raise HTTPException(status_code=404, detail="this agent version has no contract")
    return _contract_read(contract)


@router.put(
    "/organizations/{organization_id}/agents/{ai_asset_version_id}/contract",
    response_model=AgentContractRead,
)
async def put_agent_contract(
    organization_id: UUID,
    ai_asset_version_id: UUID,
    body: AgentContractWrite,
    context: SecurityContext = Depends(require_roles(*CONTRACT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> AgentContractRead:
    """Create or replace one agent version's contract.

    Idempotent by (organization, version): a second PUT edits the same row
    rather than creating a rival authority for the same agent.
    """
    enforce_organization(context, organization_id)
    asset, version = await _require_agent_version(session, organization_id, ai_asset_version_id)
    definition = _definition_from(body)
    try:
        validate_contract_definition(
            definition,
            actor_principal_id=context.principal_id,
            human_principal_ids=frozenset(
                p for p in (version.owner_principal, asset.created_by) if p
            ),
        )
    except AgentContractValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc

    contract = await load_agent_contract(
        session, organization_id=organization_id, ai_asset_version_id=ai_asset_version_id
    )
    created = contract is None
    if contract is None:
        contract = AgentContract(
            organization_id=organization_id,
            ai_asset_version_id=ai_asset_version_id,
            created_by=context.principal_id,
            kill_engaged=False,
        )
        session.add(contract)
    contract.agent_principal_id = definition.agent_principal_id.strip()
    contract.capability_envelope = definition.capability_envelope.as_json()
    contract.autonomy_tier = definition.autonomy_tier
    contract.supervisor_persona = definition.supervisor_persona
    contract.kill_scope = definition.kill_scope
    contract.sampling_rate = definition.sampling_rate
    contract.daily_token_cap = definition.daily_token_cap
    contract.per_run_token_cap = definition.per_run_token_cap
    contract.wall_clock_seconds_cap = definition.wall_clock_seconds_cap
    contract.eval_gate_threshold = definition.eval_gate_threshold

    audit_context = replace(context, organization_id=organization_id)
    record_audit(
        session,
        audit_context,
        action="agent_contract.create" if created else "agent_contract.update",
        resource_type="agent_contract",
        resource_id=str(ai_asset_version_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "agent_principal_id": contract.agent_principal_id,
            "autonomy_tier": contract.autonomy_tier,
            "kill_scope": contract.kill_scope,
            "sampling_rate": contract.sampling_rate,
            "tool_slug_count": len(definition.capability_envelope.tool_slugs),
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="agent_contract",
        aggregate_id=str(ai_asset_version_id),
        event_type="agent.contract_published.v1",
        payload={
            "ai_asset_version_id": str(ai_asset_version_id),
            "agent_principal_id": contract.agent_principal_id,
            "autonomy_tier": contract.autonomy_tier,
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="agent contract conflict") from exc
    return _contract_read(contract)


async def _set_kill(
    session: AsyncSession,
    organization_id: UUID,
    ai_asset_version_id: UUID,
    context: SecurityContext,
    *,
    engaged: bool,
    reason: str,
) -> AgentContractRead:
    enforce_organization(context, organization_id)
    contract = await load_agent_contract(
        session, organization_id=organization_id, ai_asset_version_id=ai_asset_version_id
    )
    if contract is None:
        raise HTTPException(status_code=404, detail="this agent version has no contract")
    contract.kill_engaged = engaged
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="agent_contract.kill" if engaged else "agent_contract.release",
        resource_type="agent_contract",
        resource_id=str(ai_asset_version_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "kill_engaged": engaged,
            "kill_scope": contract.kill_scope,
            "reason": reason,
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="agent_contract",
        aggregate_id=str(ai_asset_version_id),
        event_type=(
            "agent.kill_switch_engaged.v1" if engaged else "agent.kill_switch_released.v1"
        ),
        payload={
            "ai_asset_version_id": str(ai_asset_version_id),
            "kill_scope": contract.kill_scope,
            "agent_principal_id": contract.agent_principal_id,
        },
    )
    await session.commit()
    return _contract_read(contract)


@router.post(
    "/organizations/{organization_id}/agents/{ai_asset_version_id}/contract/kill",
    response_model=AgentContractRead,
)
async def engage_agent_kill_switch(
    organization_id: UUID,
    ai_asset_version_id: UUID,
    body: KillSwitchRequest,
    context: SecurityContext = Depends(require_roles(*CONTRACT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> AgentContractRead:
    """Stop this agent now. Takes effect on its very next run: the
    orchestrator queries the switch live rather than caching it."""
    # INV-5: deny a foreign tenant before reading the body. Ordering the
    # tenancy check after request processing is the bug
    # `test_inv5_tenant_isolation` exists to catch.
    enforce_organization(context, organization_id)
    return await _set_kill(
        session, organization_id, ai_asset_version_id, context, engaged=True, reason=body.reason
    )


@router.post(
    "/organizations/{organization_id}/agents/{ai_asset_version_id}/contract/release",
    response_model=AgentContractRead,
)
async def release_agent_kill_switch(
    organization_id: UUID,
    ai_asset_version_id: UUID,
    body: KillSwitchRequest,
    context: SecurityContext = Depends(require_roles(*CONTRACT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> AgentContractRead:
    enforce_organization(context, organization_id)  # INV-5: before the body
    return await _set_kill(
        session, organization_id, ai_asset_version_id, context, engaged=False, reason=body.reason
    )


# ---------------------------------------------------------------------------
# Task ledger
# ---------------------------------------------------------------------------


@router.get("/organizations/{organization_id}/agent-tasks", response_model=Page)
async def list_agent_tasks(
    organization_id: UUID,
    task_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CONTRACT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    stmt = select(AgentTask).where(AgentTask.organization_id == organization_id)
    count_stmt = (
        select(func.count()).select_from(AgentTask).where(
            AgentTask.organization_id == organization_id
        )
    )
    if task_status:
        stmt = stmt.where(AgentTask.status == task_status)
        count_stmt = count_stmt.where(AgentTask.status == task_status)
    total = int(await session.scalar(count_stmt) or 0)
    rows = (
        await session.scalars(
            stmt.order_by(AgentTask.started_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[
            AgentTaskRead(
                task_id=row.id,
                agent_principal_id=row.agent_principal_id,
                ai_asset_version_id=row.ai_asset_version_id,
                agent_run_id=row.agent_run_id,
                intent=row.intent,
                status=row.status,
                sampled_for_audit=row.sampled_for_audit,
                audit_outcome=row.audit_outcome,
                started_at=row.started_at,
                finished_at=row.finished_at,
            ).model_dump(mode="json")
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Agent inbox
# ---------------------------------------------------------------------------


@router.get("/organizations/{organization_id}/agent-inbox", response_model=AgentInboxRead)
async def get_agent_inbox(
    organization_id: UUID,
    persona: str = Query(default="STEWARD"),
    limit: int = Query(default=50, ge=1, le=200),
    since_hours: int = Query(default=168, ge=1, le=8760),
    context: SecurityContext = Depends(require_roles(*INBOX_READERS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AgentInboxRead:
    """One screen's worth of "what did my agents do, and what needs me".

    Composed in a fixed number of queries regardless of how many agents,
    proposals or tasks come back -- the counts and the four lists are each
    one statement, and the agent name lookup is a single batched read keyed
    by version id. No per-row query anywhere.
    """
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    now = datetime.now(UTC)
    since = now - timedelta(hours=since_hours)
    persona_key = persona.upper()

    contracts = (
        await session.scalars(
            select(AgentContract).where(AgentContract.organization_id == organization_id)
        )
    ).all()
    agent_principals = {c.agent_principal_id for c in contracts}
    version_ids = [c.ai_asset_version_id for c in contracts]

    versions_by_id: dict[UUID, AiAssetVersion] = {}
    assets_by_version: dict[UUID, AiAsset] = {}
    if version_ids:
        rows = (
            await session.execute(
                select(AiAssetVersion, AiAsset)
                .join(AiAsset, AiAsset.id == AiAssetVersion.asset_id)
                .where(AiAssetVersion.id.in_(version_ids))
            )
        ).all()
        for version, asset in rows:
            versions_by_id[version.id] = version
            assets_by_version[version.id] = asset

    run_counts: dict[UUID, tuple[int, int]] = {}
    if version_ids:
        for version_id, total, completed in (
            await session.execute(
                select(
                    AgentRun.ai_asset_version_id,
                    func.count(),
                    func.sum(
                        func.case((AgentRun.status == "COMPLETED", 1), else_=0)
                    ),
                )
                .where(
                    AgentRun.organization_id == organization_id,
                    AgentRun.ai_asset_version_id.in_(version_ids),
                    AgentRun.created_at >= since,
                )
                .group_by(AgentRun.ai_asset_version_id)
            )
        ).all():
            if version_id is not None:
                run_counts[version_id] = (int(total or 0), int(completed or 0))

    agents: list[InboxAgent] = []
    for contract in contracts:
        version = versions_by_id.get(contract.ai_asset_version_id)
        asset = assets_by_version.get(contract.ai_asset_version_id)
        total, completed = run_counts.get(contract.ai_asset_version_id, (0, 0))
        agents.append(
            InboxAgent(
                ai_asset_id=asset.id if asset else contract.ai_asset_version_id,
                version_id=contract.ai_asset_version_id,
                name=(version.name if version else None) or "unnamed agent",
                risk_tier=version.risk_tier if version else None,
                autonomy_tier=contract.autonomy_tier,
                runs_recent=total,
                success_rate=(completed / total) if total else None,
                budget=InboxBudget(
                    daily_token_cap=contract.daily_token_cap,
                    # Token accounting per agent is not recorded yet; the
                    # cap is real and enforced by the model gateway's own
                    # budget contract, the *consumption* number is not
                    # attributable per agent until AgentRun carries it.
                    daily_tokens_used=None,
                ),
                kill_scope=contract.kill_scope,
                kill_engaged=contract.kill_engaged,
                supervisor_persona=contract.supervisor_persona,
            )
        )

    pending_stmt = select(GovernanceReview).where(
        GovernanceReview.organization_id == organization_id,
        GovernanceReview.status == "PENDING",
    )
    if persona_key not in _QUEUE_WIDE_PERSONAS:
        pending_stmt = pending_stmt.where(
            GovernanceReview.requested_by == context.principal_id
        )
    pending_rows = (
        await session.scalars(
            pending_stmt.order_by(GovernanceReview.created_at.desc()).limit(limit)
        )
    ).all()

    pending: list[InboxPendingItem] = []
    for review in pending_rows:
        evidence = review.pre_review_evidence or {}
        tier = review.risk_tier or risk_tier_for(review.object_type)
        pending.append(
            InboxPendingItem(
                review_id=review.id,
                object_type=review.object_type,
                object_id=review.object_id,
                title=f"{review.requested_action} {review.object_type}",
                proposed_by=review.requested_by,
                proposed_by_kind=(
                    "AGENT" if review.requested_by in agent_principals else "HUMAN"
                ),
                risk_tier=tier,
                confidence=review.pre_review_confidence,
                blast_radius=evidence.get("blast_radius"),
                negative_knowledge_hits=int(evidence.get("negative_knowledge_hits") or 0),
                recommendation=review.pre_review_recommendation or "NONE",
                created_at=review.created_at,
            )
        )
    # Highest blast radius first, then highest confidence: the two questions
    # a reviewer asks in that order.
    pending.sort(
        key=lambda item: (item.blast_radius or 0, item.confidence or 0.0), reverse=True
    )

    def _agent_name(contract: AgentContract) -> str:
        version = versions_by_id.get(contract.ai_asset_version_id)
        return (version.name if version else None) or contract.agent_principal_id

    name_by_principal = {c.agent_principal_id: _agent_name(c) for c in contracts}

    applied_rows = (
        await session.scalars(
            select(AgentTask)
            .where(
                AgentTask.organization_id == organization_id,
                AgentTask.status.in_(["APPLIED", "SAMPLED"]),
                AgentTask.started_at >= since,
            )
            .order_by(AgentTask.started_at.desc())
            .limit(limit)
        )
    ).all()
    auto_applied = [
        InboxAutoApplied(
            task_id=row.id,
            agent_name=name_by_principal.get(row.agent_principal_id, row.agent_principal_id),
            action=row.intent,
            object_type=row.proposal_ref_type or "AGENT_RUN",
            object_id=str(row.proposal_ref_id) if row.proposal_ref_id else None,
            applied_at=row.finished_at or row.started_at,
            sampled_for_audit=row.sampled_for_audit,
            audit_outcome=row.audit_outcome,
        )
        for row in applied_rows
    ]

    recent_rows = (
        await session.scalars(
            select(AgentTask)
            .where(AgentTask.organization_id == organization_id)
            .order_by(AgentTask.started_at.desc())
            .limit(limit)
        )
    ).all()
    recent_tasks = [
        InboxRecentTask(
            task_id=row.id,
            agent_name=name_by_principal.get(row.agent_principal_id, row.agent_principal_id),
            intent=row.intent,
            status=row.status,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )
        for row in recent_rows
    ]

    pending_total = int(
        await session.scalar(
            select(func.count())
            .select_from(GovernanceReview)
            .where(
                GovernanceReview.organization_id == organization_id,
                GovernanceReview.status == "PENDING",
            )
        )
        or 0
    )
    sampled_open = int(
        await session.scalar(
            select(func.count())
            .select_from(ReviewAuditSample)
            .where(
                ReviewAuditSample.organization_id == organization_id,
                ReviewAuditSample.human_outcome == "PENDING",
            )
        )
        or 0
    )

    return AgentInboxRead(
        organization_id=organization_id,
        persona=persona_key,
        generated_at=now,
        summary=InboxSummary(
            pending_decisions=pending_total,
            auto_applied_since=len(auto_applied),
            sampled_for_audit=sampled_open,
            agents_active=sum(1 for c in contracts if not c.kill_engaged),
            kill_switch_engaged=any(c.kill_engaged for c in contracts)
            or settings.reviewer_agent_suspended,
        ),
        agents=agents,
        pending=pending,
        auto_applied=auto_applied,
        recent_tasks=recent_tasks,
    )


# ---------------------------------------------------------------------------
# Reviewer agent (ADR-0027)
# ---------------------------------------------------------------------------


@router.get(
    "/organizations/{organization_id}/reviewer-agent", response_model=ReviewerAgentStateRead
)
async def get_reviewer_agent_state(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*CONTRACT_READERS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ReviewerAgentStateRead:
    enforce_organization(context, organization_id)
    return ReviewerAgentStateRead(
        organization_id=organization_id,
        enabled=settings.reviewer_agent_enabled,
        suspended=settings.reviewer_agent_suspended
        or await organization_suspended(session, organization_id),
        max_tier=settings.reviewer_agent_max_tier,
        sampling_rate=settings.reviewer_agent_sampling_rate,
        agent_principal_id=settings.reviewer_agent_principal_id,
    )


@router.post(
    "/organizations/{organization_id}/reviewer-agent/pre-review",
    response_model=ReviewerAgentRunResult,
)
async def run_pre_review(
    organization_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    context: SecurityContext = Depends(require_roles(*REVIEWER_AGENT_OPERATORS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ReviewerAgentRunResult:
    """Attach tier, evidence and a recommendation to pending items.

    Deliberately available even when the agent is disabled: pre-review
    decides nothing, and a queue annotated with blast radius and prior
    rejections is worth having whether or not anything auto-decides.
    """
    enforce_organization(context, organization_id)
    outcomes = await pre_review_pending(
        session, organization_id, settings=settings, limit=limit
    )
    await session.commit()
    return ReviewerAgentRunResult(pre_reviewed=len(outcomes))


@router.post(
    "/organizations/{organization_id}/reviewer-agent/run", response_model=ReviewerAgentRunResult
)
async def run_reviewer_agent(
    organization_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    context: SecurityContext = Depends(require_roles(*REVIEWER_AGENT_OPERATORS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ReviewerAgentRunResult:
    enforce_organization(context, organization_id)
    try:
        decisions = await auto_decide_tier0_tier1(
            session, organization_id, settings=settings, limit=limit
        )
    except ReviewerAgentUnavailable as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=exc.reason_code) from exc
    await session.commit()
    return ReviewerAgentRunResult(
        decided=len(decisions),
        approved=sum(1 for d in decisions if d.decision == "APPROVED"),
        rejected=sum(1 for d in decisions if d.decision == "REJECTED"),
        sampled_for_audit=sum(1 for d in decisions if d.sampled_for_audit),
    )


@router.post(
    "/organizations/{organization_id}/reviewer-agent/suspend",
    response_model=ReviewerAgentStateRead,
)
async def suspend_reviewer_agent(
    organization_id: UUID,
    body: KillSwitchRequest,
    context: SecurityContext = Depends(require_roles(*REVIEWER_AGENT_OPERATORS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ReviewerAgentStateRead:
    """ADR-0027 condition (c). One action, effective immediately."""
    enforce_organization(context, organization_id)  # INV-5: before the body
    await set_suspended(
        session, organization_id, suspended=True, context=context, reason=body.reason
    )
    await session.commit()
    return ReviewerAgentStateRead(
        organization_id=organization_id,
        enabled=settings.reviewer_agent_enabled,
        suspended=True,
        max_tier=settings.reviewer_agent_max_tier,
        sampling_rate=settings.reviewer_agent_sampling_rate,
        agent_principal_id=settings.reviewer_agent_principal_id,
    )


@router.post(
    "/organizations/{organization_id}/reviewer-agent/resume",
    response_model=ReviewerAgentStateRead,
)
async def resume_reviewer_agent(
    organization_id: UUID,
    body: KillSwitchRequest,
    context: SecurityContext = Depends(require_roles(*REVIEWER_AGENT_OPERATORS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ReviewerAgentStateRead:
    enforce_organization(context, organization_id)
    await set_suspended(
        session, organization_id, suspended=False, context=context, reason=body.reason
    )
    await session.commit()
    return ReviewerAgentStateRead(
        organization_id=organization_id,
        enabled=settings.reviewer_agent_enabled,
        suspended=settings.reviewer_agent_suspended,
        max_tier=settings.reviewer_agent_max_tier,
        sampling_rate=settings.reviewer_agent_sampling_rate,
        agent_principal_id=settings.reviewer_agent_principal_id,
    )


@router.get("/organizations/{organization_id}/reviewer-agent/samples", response_model=Page)
async def list_audit_samples(
    organization_id: UUID,
    outcome: str = Query(default="PENDING"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CONTRACT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    base = select(ReviewAuditSample).where(
        ReviewAuditSample.organization_id == organization_id
    )
    counter = (
        select(func.count())
        .select_from(ReviewAuditSample)
        .where(ReviewAuditSample.organization_id == organization_id)
    )
    if outcome != "ALL":
        base = base.where(ReviewAuditSample.human_outcome == outcome)
        counter = counter.where(ReviewAuditSample.human_outcome == outcome)
    total = int(await session.scalar(counter) or 0)
    rows = (
        await session.scalars(
            base.order_by(ReviewAuditSample.sampled_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[
            ReviewAuditSampleRead(
                sample_id=row.id,
                governance_review_id=row.governance_review_id,
                agent_principal_id=row.agent_principal_id,
                object_type=row.object_type,
                risk_tier=row.risk_tier,
                decision=row.decision,
                sampled_at=row.sampled_at,
                human_outcome=row.human_outcome,
                human_principal_id=row.human_principal_id,
                human_rationale=row.human_rationale,
                resolved_at=row.resolved_at,
            ).model_dump(mode="json")
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/organizations/{organization_id}/reviewer-agent/samples/{sample_id}/resolve",
    response_model=ReviewAuditSampleRead,
    status_code=status.HTTP_200_OK,
)
async def resolve_sample(
    organization_id: UUID,
    sample_id: UUID,
    body: ResolveSampleRequest,
    context: SecurityContext = Depends(require_roles(*REVIEWER_AGENT_OPERATORS)),
    session: AsyncSession = Depends(get_session),
) -> ReviewAuditSampleRead:
    """A human's verdict on one sampled agent decision.

    Human-only by construction: the roles here are human reviewer roles, and
    a sample resolved by the agent that made the decision would defeat the
    entire point of sampling.
    """
    enforce_organization(context, organization_id)
    sample = await session.get(ReviewAuditSample, sample_id)
    if sample is None or sample.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="audit sample not found")
    if sample.agent_principal_id == context.principal_id:
        raise HTTPException(
            status_code=409, detail="an agent cannot resolve its own sampled decision"
        )
    try:
        await resolve_audit_sample(
            session,
            sample,
            human_outcome=body.human_outcome,
            rationale=body.rationale,
            context=context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return ReviewAuditSampleRead(
        sample_id=sample.id,
        governance_review_id=sample.governance_review_id,
        agent_principal_id=sample.agent_principal_id,
        object_type=sample.object_type,
        risk_tier=sample.risk_tier,
        decision=sample.decision,
        sampled_at=sample.sampled_at,
        human_outcome=sample.human_outcome,
        human_principal_id=sample.human_principal_id,
        human_rationale=sample.human_rationale,
        resolved_at=sample.resolved_at,
    )
