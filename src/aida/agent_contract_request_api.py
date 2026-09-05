"""AG-10 extension: reviewed, eval-gated agent contract requests.

`agent_contract_api.put_agent_contract` (`PUT .../agents/{version}/contract`)
is a direct write: any `CONTRACT_AUTHORS` principal can create or replace an
agent's contract unilaterally, with no second opinion and no check that the
agent has ever actually been evaluated. That path is unchanged here and stays
available for corrections -- this module adds a second, *reviewed* path
alongside it, for the case that actually needs one: bringing a new or
externally-developed agent onto the platform, where "a trusted-enough
principal typed some values into a form" should not by itself be enough to
grant the resulting workload identity production capability.

The flow:

1. `POST .../agent-contract-requests` -- a `CONTRACT_AUTHORS` principal (the
   same submitter set the direct-write path already trusts; this module
   narrows *when the write takes effect*, not *who may propose one* --
   see this module's own module-level note on that scope, stated honestly
   rather than oversold) submits a requested contract definition for one
   `AGENT`-kind `AiAssetVersion`. Validated with the exact same
   `agent_contracts.validate_contract_definition` the direct-write path
   uses, so a request can never propose something the direct write would
   have refused. Opens a `GovernanceReview` (`object_type=
   "AGENT_CONTRACT_REQUEST"`), the same unified maker-checker queue every
   other proposal in this codebase goes through -- no second review
   mechanism.
2. A human reviewer (`PlatformAdmin`/`DataSteward`/`Reviewer`, the same
   generic review-queue role set every other object type in the queue
   already uses -- see `semantic_api.decide_governance_review`) decides it
   through the existing single or bulk decision endpoints. Maker != checker
   is enforced there already, generically, for every object type.
3. On APPROVE, `semantic_api._apply_governance_review_decision`'s new
   `AGENT_CONTRACT_REQUEST` branch requires the AT-8/N17 evaluation gate
   (`aida.agent_eval_gate.compute_agent_eval_gate`) to show PASS for the
   target agent version *at decision time* -- not at submission time, so a
   stale or manufactured pass can never carry a request through -- before
   it writes the `AgentContract`. A non-PASS verdict raises (409), exactly
   like the identical gate already does for `AI_ASSET_VERSION` publication;
   the request stays PENDING and can be re-decided once the gate passes.

Scope, stated honestly: this does not create a new, narrower "external
agent" role. It reuses `CONTRACT_AUTHORS`
(`PlatformAdmin`/`AgentDeveloper`/`ModelRiskManager`) for who may submit --
the same set that could already write a contract directly. What changes is
that submission no longer *is* activation: even a trusted submitter's
request is reviewed by a different principal and blocked on a live
evaluation-gate check before it takes effect. A future row that wants a
genuinely narrower "submit only" role needs its own row against this
platform's role catalog (OIDC-group-derived, per `PersonaNav`'s own
module docstring), which is out of scope here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contract_api import CONTRACT_AUTHORS, CONTRACT_READERS, AgentContractWrite
from aida.agent_contracts import (
    AgentContractDefinition,
    AgentContractValidationError,
    CapabilityEnvelope,
    load_agent_asset_version,
    parse_capability_envelope,
    validate_contract_definition,
)
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import AgentContractRequest, GovernanceReview
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["agent-workforce"])


class AgentContractRequestCreate(AgentContractWrite):
    ai_asset_version_id: UUID


class AgentContractRequestRead(ApiModel):
    id: UUID
    organization_id: UUID
    ai_asset_version_id: UUID
    requested_by: str
    definition: dict[str, Any]
    status: str
    governance_review_id: UUID | None
    eval_gate_verdict: str | None
    activated_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _request_read(request: AgentContractRequest) -> AgentContractRequestRead:
    return AgentContractRequestRead(
        id=request.id,
        organization_id=request.organization_id,
        ai_asset_version_id=request.ai_asset_version_id,
        requested_by=request.requested_by,
        definition=dict(request.definition or {}),
        status=request.status,
        governance_review_id=request.governance_review_id,
        eval_gate_verdict=request.eval_gate_verdict,
        activated_at=request.activated_at,
        created_at=request.created_at,
        updated_at=request.updated_at,
    )


def definition_to_json(definition: AgentContractDefinition) -> dict[str, Any]:
    """The exact fields `agent_contract_request.definition` stores, and the
    exact fields `semantic_api`'s `AGENT_CONTRACT_REQUEST` decision branch
    reads back to reconstruct an `AgentContractDefinition` -- keep these two
    directions in sync by construction rather than by two hand-written
    field lists drifting apart. Used by both this module (write) and
    `semantic_api` (read) -- imported there rather than duplicated.
    """
    return {
        "agent_principal_id": definition.agent_principal_id,
        "capability_envelope": definition.capability_envelope.as_json(),
        "autonomy_tier": definition.autonomy_tier,
        "supervisor_persona": definition.supervisor_persona,
        "kill_scope": definition.kill_scope,
        "sampling_rate": definition.sampling_rate,
        "daily_token_cap": definition.daily_token_cap,
        "per_run_token_cap": definition.per_run_token_cap,
        "wall_clock_seconds_cap": definition.wall_clock_seconds_cap,
        "eval_gate_threshold": definition.eval_gate_threshold,
    }


def definition_from_json(payload: dict[str, Any]) -> AgentContractDefinition:
    """The inverse of `definition_to_json`. Raises `AgentContractValidationError`
    (via `parse_capability_envelope`) on a malformed stored envelope rather
    than trusting a value this module itself only ever writes in the shape
    above -- defensive, not because a caller other than this module writes
    this column today."""
    envelope = parse_capability_envelope(dict(payload.get("capability_envelope") or {}))
    return AgentContractDefinition(
        agent_principal_id=str(payload["agent_principal_id"]),
        capability_envelope=envelope,
        autonomy_tier=str(payload["autonomy_tier"]),
        supervisor_persona=str(payload["supervisor_persona"]),
        kill_scope=str(payload["kill_scope"]),
        sampling_rate=float(payload["sampling_rate"]),
        daily_token_cap=payload.get("daily_token_cap"),
        per_run_token_cap=payload.get("per_run_token_cap"),
        wall_clock_seconds_cap=payload.get("wall_clock_seconds_cap"),
        eval_gate_threshold=payload.get("eval_gate_threshold"),
    )


@router.post(
    "/organizations/{organization_id}/agent-contract-requests",
    response_model=AgentContractRequestRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_agent_contract_request(
    organization_id: UUID,
    body: AgentContractRequestCreate,
    context: SecurityContext = Depends(require_roles(*CONTRACT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> AgentContractRequestRead:
    enforce_organization(context, organization_id)
    found = await load_agent_asset_version(
        session, organization_id=organization_id, ai_asset_version_id=body.ai_asset_version_id
    )
    if found is None:
        raise HTTPException(
            status_code=404, detail="AGENT-kind AI asset version not found in this organization"
        )
    asset, version = found

    try:
        envelope = parse_capability_envelope(body.capability_envelope.model_dump())
    except AgentContractValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.code) from exc
    definition = AgentContractDefinition(
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

    request = AgentContractRequest(
        organization_id=organization_id,
        ai_asset_version_id=body.ai_asset_version_id,
        requested_by=context.principal_id,
        definition=definition_to_json(definition),
        status="PENDING",
    )
    session.add(request)
    await session.flush()

    review = GovernanceReview(
        organization_id=organization_id,
        object_type="AGENT_CONTRACT_REQUEST",
        object_id=str(request.id),
        requested_action="ACTIVATE",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    request.governance_review_id = review.id

    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="agent_contract_request.submit",
        resource_type="agent_contract_request",
        resource_id=str(request.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "ai_asset_version_id": str(body.ai_asset_version_id),
            "review_id": str(review.id),
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="agent_contract_request",
        aggregate_id=str(request.id),
        event_type="agent_contract_request.submitted.v1",
        payload={
            "agent_contract_request_id": str(request.id),
            "ai_asset_version_id": str(body.ai_asset_version_id),
            "review_id": str(review.id),
        },
    )
    await session.commit()
    return _request_read(request)


@router.get(
    "/organizations/{organization_id}/agent-contract-requests",
    response_model=Page,
)
async def list_agent_contract_requests(
    organization_id: UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    ai_asset_version_id: UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CONTRACT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = [AgentContractRequest.organization_id == organization_id]
    if status_filter is not None:
        filters.append(AgentContractRequest.status == status_filter.upper())
    if ai_asset_version_id is not None:
        filters.append(AgentContractRequest.ai_asset_version_id == ai_asset_version_id)
    total = await session.scalar(
        select(func.count()).select_from(AgentContractRequest).where(*filters)
    )
    rows = (
        await session.scalars(
            select(AgentContractRequest)
            .where(*filters)
            .order_by(AgentContractRequest.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[_request_read(row).model_dump(mode="json") for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/agent-contract-requests/{request_id}",
    response_model=AgentContractRequestRead,
)
async def get_agent_contract_request(
    request_id: UUID,
    context: SecurityContext = Depends(require_roles(*CONTRACT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> AgentContractRequestRead:
    request = await session.get(AgentContractRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="agent contract request not found")
    enforce_organization(context, request.organization_id)
    return _request_read(request)
