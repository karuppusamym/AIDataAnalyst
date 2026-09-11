"""ADR-0029: the steward agent's HTTP surface.

Two endpoints: the agent's state -- is it registered here, what may it do, is
anything stopping it, how have its proposals fared -- and a run. Everything the
agent does lives in `aida.steward_agent`; this module translates.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from aida.db import get_session
from aida.models import DataSource
from aida.review_risk_tiers import risk_tier_for
from aida.schemas import ApiModel
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.steward_agent import (
    ACTION_FAILED,
    ACTION_PROPOSED,
    ACTION_SKIPPED,
    ACTION_WOULD_PROPOSE,
    CAPABILITIES,
    CAPABILITY_PRODUCERS,
    METHOD,
    PROPOSAL_OBJECT_TYPES,
    StewardAgentRefused,
    StewardAgentStatus,
    StewardRunOutcome,
    StewardRunRequest,
    mode_for,
    record_steward_refusal,
    run_steward_agent,
    steward_agent_status,
)
from atlas.platform.config import Settings, get_settings

router = APIRouter(prefix="/v1", tags=["agent-workforce"])

#: Who may read the agent's state: the agent-contract readers plus the
#: stewardship roles whose queue it works.
STEWARD_AGENT_READERS = (
    "PlatformAdmin",
    "AgentDeveloper",
    "ModelRiskManager",
    "Reviewer",
    "MetadataReviewer",
    "Auditor",
    "Operations",
    "MetadataAdmin",
    "SemanticAdmin",
    "DataSteward",
)
#: Who may start a run: exactly the roles that may already generate these
#: drafts and link proposals by hand (`asset_description_api.WRITE_ROLES`,
#: `stewardship_api.WRITE_ROLES`). Starting the agent grants no new power -- it
#: is the same drafting, attributed to the agent and bounded by its contract.
STEWARD_AGENT_OPERATORS = ("PlatformAdmin", "MetadataAdmin", "SemanticAdmin", "DataSteward")

Capability = Literal["TABLE_DESCRIPTION", "GLOSSARY_LINK"]


def _every_capability() -> list[Capability]:
    return ["TABLE_DESCRIPTION", "GLOSSARY_LINK"]


class StewardAgentCapabilityRead(ApiModel):
    capability: str
    #: The `GovernanceReview.object_type` its proposals are decided as.
    object_type: str
    #: That object type's ADR-0027 tier -- T0 or T1 for every capability.
    risk_tier: str
    producer: str


class StewardAgentOutcomeRead(ApiModel):
    object_type: str
    pending: int
    approved: int
    rejected: int
    other: int
    #: Approved over decided; `None` until something has been decided.
    acceptance_rate: float | None


class StewardAgentStateRead(ApiModel):
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
    #: Always false: no capability of this agent calls a model.
    uses_model: bool
    capabilities: list[StewardAgentCapabilityRead]
    max_proposals_per_run: int
    #: The review-backlog bound; 0 means unbounded.
    max_pending_proposals: int
    pending_proposals: int
    wall_clock_seconds_cap: int | None
    outcomes: list[StewardAgentOutcomeRead]


class StewardAgentRunRequest(ApiModel):
    capabilities: list[Capability] = Field(
        default_factory=_every_capability, min_length=1, max_length=2
    )
    #: Proposals per capability, clamped server-side to
    #: `steward_agent_max_proposals_per_run`.
    limit: int = Field(default=10, ge=1, le=200)
    datasource_id: UUID | None = None
    #: Report what the agent would propose and open nothing, whatever its tier.
    dry_run: bool = False

    @model_validator(mode="after")
    def _unique_capabilities(self) -> StewardAgentRunRequest:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        return self


class StewardAgentRunItemRead(ApiModel):
    capability: str
    table_id: UUID
    table_name: str
    action: Literal["PROPOSED", "WOULD_PROPOSE", "SKIPPED", "FAILED"]
    reason: str | None
    object_type: str | None
    object_id: UUID | None
    review_id: UUID | None
    task_id: UUID | None
    confidence: float | None
    worklist_rank: int | None
    term_id: UUID | None
    term_name: str | None


class StewardAgentRunRead(ApiModel):
    run_id: str
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
    items: list[StewardAgentRunItemRead]


def _state_read(
    organization_id: UUID, state: StewardAgentStatus, settings: Settings
) -> StewardAgentStateRead:
    authority = state.authority
    contract = authority.contract if authority is not None else None
    return StewardAgentStateRead(
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
        method=METHOD,
        uses_model=False,
        capabilities=[
            StewardAgentCapabilityRead(
                capability=capability,
                object_type=PROPOSAL_OBJECT_TYPES[capability],
                risk_tier=risk_tier_for(PROPOSAL_OBJECT_TYPES[capability]),
                producer=CAPABILITY_PRODUCERS[capability],
            )
            for capability in CAPABILITIES
        ],
        max_proposals_per_run=settings.steward_agent_max_proposals_per_run,
        max_pending_proposals=settings.steward_agent_max_pending_proposals,
        pending_proposals=state.pending_proposals,
        wall_clock_seconds_cap=contract.wall_clock_seconds_cap if contract is not None else None,
        outcomes=[
            StewardAgentOutcomeRead(
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


def _run_read(outcome: StewardRunOutcome) -> StewardAgentRunRead:
    return StewardAgentRunRead(
        run_id=outcome.run_id,
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
            StewardAgentRunItemRead(
                capability=item.capability,
                table_id=item.table_id,
                table_name=item.table_name,
                action=item.action,
                reason=item.reason,
                object_type=item.object_type,
                object_id=item.object_id,
                review_id=item.review_id,
                task_id=item.task_id,
                confidence=item.confidence,
                worklist_rank=item.worklist_rank,
                term_id=item.term_id,
                term_name=item.term_name,
            )
            for item in outcome.items
        ],
    )


@router.get(
    "/organizations/{organization_id}/steward-agent", response_model=StewardAgentStateRead
)
async def get_steward_agent_state(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*STEWARD_AGENT_READERS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StewardAgentStateRead:
    enforce_organization(context, organization_id)
    state = await steward_agent_status(session, organization_id, settings=settings)
    return _state_read(organization_id, state, settings)


@router.post(
    "/organizations/{organization_id}/steward-agent/run",
    response_model=StewardAgentRunRead,
    status_code=status.HTTP_200_OK,
)
async def start_steward_agent_run(
    organization_id: UUID,
    body: StewardAgentRunRequest,
    context: SecurityContext = Depends(require_roles(*STEWARD_AGENT_OPERATORS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> StewardAgentRunRead:
    """Run the agent once, synchronously and bounded.

    409 with a stable reason code when the agent may not act -- no contract, no
    approved version, a kill switch, or authority withdrawn mid-run -- and in
    that case nothing the run produced is kept.
    """
    enforce_organization(context, organization_id)  # INV-5: before the body
    if body.datasource_id is not None:
        datasource = await session.get(DataSource, body.datasource_id)
        if datasource is None or datasource.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="datasource not found")
    try:
        outcome = await run_steward_agent(
            session,
            organization_id,
            request=StewardRunRequest(
                capabilities=tuple(body.capabilities),
                limit=body.limit,
                datasource_id=body.datasource_id,
                dry_run=body.dry_run,
            ),
            settings=settings,
            triggered_by=context,
        )
    except StewardAgentRefused as exc:
        await session.rollback()
        record_steward_refusal(
            session, organization_id, triggered_by=context, reason_code=exc.reason_code
        )
        await session.commit()
        raise HTTPException(status_code=409, detail=exc.reason_code) from exc
    await session.commit()
    return _run_read(outcome)
