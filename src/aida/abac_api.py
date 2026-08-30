"""ABAC policy management and evaluation API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.abac import (
    ABAC_ENGINE_VERSION,
    AbacDecision,
    AbacPolicy,
    evaluate,
    simulate,
)
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.models import AbacDecisionRecord, AbacPolicyRecord
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["abac"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AbacPolicyCreate(ApiModel):
    policy_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,99}$")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=3, max_length=4000)
    effect: Literal["PERMIT", "DENY"]
    subject_conditions: dict[str, Any] = Field(default_factory=dict)
    resource_conditions: dict[str, Any] = Field(default_factory=dict)
    environment_conditions: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=1000)


class AbacPolicyRead(AbacPolicyCreate):
    id: UUID
    organization_id: UUID
    version: int
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class AbacEvaluateRequest(ApiModel):
    subject_attributes: dict[str, Any]
    resource_attributes: dict[str, Any]
    environment_attributes: dict[str, Any] = Field(default_factory=dict)


class AbacEvaluateResponse(ApiModel):
    decision: str
    reasons: list[str]
    contributing_policies: list[str]
    evaluation_time_ms: float
    policy_version: str


class AbacSimulateRequest(ApiModel):
    subject_attributes: dict[str, Any]
    resource_attributes: dict[str, Any]
    environment_attributes: dict[str, Any] = Field(default_factory=dict)
    vary_subject_attributes: list[dict[str, Any]] = Field(
        default_factory=list, max_length=100
    )


class AbacDecisionRead(ApiModel):
    id: UUID
    organization_id: UUID
    principal_id: str
    principal_type: str
    decision: str
    resource_type: str
    resource_id: str | None
    subject_attributes: dict[str, Any]
    resource_attributes: dict[str, Any]
    environment_attributes: dict[str, Any]
    contributing_policy_ids: list[str]
    reasons: list[str]
    evaluation_time_ms: float
    policy_version: str
    correlation_id: str
    evaluated_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_policies(
    session: AsyncSession, organization_id: UUID
) -> list[AbacPolicy]:
    stmt = (
        select(AbacPolicyRecord)
        .where(
            AbacPolicyRecord.organization_id == organization_id,
            AbacPolicyRecord.status == "ACTIVE",
        )
        .order_by(AbacPolicyRecord.priority)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        AbacPolicy(
            id=str(row.id),
            policy_key=row.policy_key,
            version=row.version,
            name=row.name,
            effect=row.effect,
            subject_conditions=row.subject_conditions,
            resource_conditions=row.resource_conditions,
            environment_conditions=row.environment_conditions,
            priority=row.priority,
        )
        for row in rows
    ]


async def _record_decision(
    session: AsyncSession,
    context: SecurityContext,
    result: AbacDecision,
    request: AbacEvaluateRequest,
    correlation_id: str,
) -> None:
    session.add(
        AbacDecisionRecord(
            organization_id=context.organization_id,
            principal_id=context.principal_id,
            principal_type=context.principal_type,
            decision=result.decision,
            resource_type=request.resource_attributes.get("resource_type", "UNKNOWN"),
            resource_id=request.resource_attributes.get("resource_id"),
            subject_attributes=request.subject_attributes,
            resource_attributes=request.resource_attributes,
            environment_attributes=request.environment_attributes,
            contributing_policy_ids=result.contributing_policies,
            reasons=result.reasons,
            evaluation_time_ms=result.evaluation_time_ms,
            policy_version=result.policy_version,
            correlation_id=correlation_id,
        )
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/abac/policies",
    response_model=AbacPolicyRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create an ABAC policy",
)
async def create_policy(
    body: AbacPolicyCreate,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin")
    ),
) -> AbacPolicyRead:
    org_id = context.require_organization()
    correlation_id = get_correlation_id()

    # Determine next version for this policy_key
    stmt = (
        select(func.coalesce(func.max(AbacPolicyRecord.version), 0))
        .where(
            AbacPolicyRecord.organization_id == org_id,
            AbacPolicyRecord.policy_key == body.policy_key,
        )
    )
    max_version = (await session.execute(stmt)).scalar() or 0

    record = AbacPolicyRecord(
        organization_id=org_id,
        policy_key=body.policy_key,
        version=max_version + 1,
        name=body.name,
        description=body.description,
        effect=body.effect,
        subject_conditions=body.subject_conditions,
        resource_conditions=body.resource_conditions,
        environment_conditions=body.environment_conditions,
        priority=body.priority,
        status="ACTIVE",
        created_by=context.principal_id,
    )
    session.add(record)

    record_audit(
        session,
        context,
        action="ABAC_POLICY_CREATED",
        resource_type="ABAC_POLICY",
        resource_id=body.policy_key,
        outcome="SUCCESS",
        correlation_id=correlation_id,
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="policy version conflict",
        ) from exc

    await session.refresh(record)
    return AbacPolicyRead.model_validate(record)


@router.get(
    "/abac/policies",
    response_model=Page,
    summary="List ABAC policies for an organization",
)
async def list_policies(
    organization_id: UUID = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Viewer")
    ),
) -> Page:
    enforce_organization(context, organization_id)

    count_stmt = (
        select(func.count())
        .select_from(AbacPolicyRecord)
        .where(AbacPolicyRecord.organization_id == organization_id)
    )
    total = (await session.execute(count_stmt)).scalar() or 0

    stmt = (
        select(AbacPolicyRecord)
        .where(AbacPolicyRecord.organization_id == organization_id)
        .order_by(AbacPolicyRecord.priority, AbacPolicyRecord.created_at)
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()

    return Page(
        items=[AbacPolicyRead.model_validate(r) for r in rows],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.post(
    "/abac/evaluate",
    response_model=AbacEvaluateResponse,
    summary="Evaluate an ABAC access decision",
)
async def evaluate_access(
    body: AbacEvaluateRequest,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Analyst", "Viewer")
    ),
) -> AbacEvaluateResponse:
    org_id = context.require_organization()
    correlation_id = get_correlation_id()

    policies = await _load_policies(session, org_id)
    result = evaluate(
        body.subject_attributes,
        body.resource_attributes,
        body.environment_attributes,
        policies,
    )

    await _record_decision(session, context, result, body, correlation_id)
    await session.commit()

    return AbacEvaluateResponse(
        decision=result.decision,
        reasons=result.reasons,
        contributing_policies=result.contributing_policies,
        evaluation_time_ms=result.evaluation_time_ms,
        policy_version=result.policy_version,
    )


@router.post(
    "/abac/simulate",
    response_model=list[AbacEvaluateResponse],
    summary="Simulate ABAC policy evaluation",
)
async def simulate_access(
    body: AbacSimulateRequest,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin")
    ),
) -> list[AbacEvaluateResponse]:
    org_id = context.require_organization()

    policies = await _load_policies(session, org_id)
    vary = body.vary_subject_attributes if body.vary_subject_attributes else None

    results = simulate(
        body.subject_attributes,
        body.resource_attributes,
        body.environment_attributes,
        policies,
        vary_subject_attrs=vary,
    )

    return [
        AbacEvaluateResponse(
            decision=r.decision,
            reasons=r.reasons,
            contributing_policies=r.contributing_policies,
            evaluation_time_ms=r.evaluation_time_ms,
            policy_version=r.policy_version,
        )
        for r in results
    ]


@router.get(
    "/abac/decisions",
    response_model=Page,
    summary="Audit trail of ABAC decisions",
)
async def list_decisions(
    organization_id: UUID = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin")
    ),
) -> Page:
    enforce_organization(context, organization_id)

    count_stmt = (
        select(func.count())
        .select_from(AbacDecisionRecord)
        .where(AbacDecisionRecord.organization_id == organization_id)
    )
    total = (await session.execute(count_stmt)).scalar() or 0

    stmt = (
        select(AbacDecisionRecord)
        .where(AbacDecisionRecord.organization_id == organization_id)
        .order_by(AbacDecisionRecord.evaluated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()

    return Page(
        items=[AbacDecisionRead.model_validate(r) for r in rows],
        limit=limit,
        offset=offset,
        total=total,
    )
