"""
Multi-Step Tool Plans API (Phase E - EE.6 / AG-4)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.edition_entitlements import evaluate_entitlement
from aida.events import record_audit, record_outbox
from aida.models import (
    ToolPlanExecutionRecord,
    ToolPlanRecord,
    ToolPlanStepRecord,
)
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.tool_plans import (
    PlanBudget,
    PlanStep,
    ToolPlan,
    execute_plan,
    persist_execution,
    persist_plan,
    validate_plan,
)

router = APIRouter(prefix="/v1", tags=["tool-plans"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class PlanStepCreate(ApiModel):
    sequence: int = Field(ge=1)
    tool_id: str = Field(min_length=1, max_length=255)
    tool_version: str = Field(min_length=1, max_length=50)
    parameters: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[int] = Field(default_factory=list)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    expected_cost: float = Field(default=0.0, ge=0.0)


class PlanBudgetCreate(ApiModel):
    max_steps: int = Field(default=20, ge=1, le=100)
    max_time_seconds: int = Field(default=600, ge=1, le=86400)
    max_tokens: int = Field(default=100_000, ge=0)
    max_cost_units: float = Field(default=100.0, ge=0.0)


class ToolPlanCreate(ApiModel):
    name: str = Field(min_length=2, max_length=200)
    steps: list[PlanStepCreate] = Field(min_length=1, max_length=100)
    budget: PlanBudgetCreate = Field(default_factory=PlanBudgetCreate)


class ToolPlanRead(ApiModel):
    id: UUID
    organization_id: UUID
    name: str
    budget: dict[str, Any]
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class ToolPlanStepRead(ApiModel):
    id: UUID
    plan_id: UUID
    sequence: int
    tool_id: str
    tool_version: str
    parameters: dict[str, Any]
    dependencies: list[int]
    timeout_seconds: int
    expected_cost: float
    status: str
    started_at: datetime | None
    completed_at: datetime | None
    evidence: dict[str, Any]
    error_message: str | None


class ToolPlanDetailRead(ApiModel):
    id: UUID
    organization_id: UUID
    name: str
    budget: dict[str, Any]
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    steps: list[ToolPlanStepRead]


class ValidationIssueRead(ApiModel):
    step_sequence: int
    issue: str
    severity: str


class ValidationResponse(ApiModel):
    valid: bool
    issues: list[ValidationIssueRead]


class ExecutionRead(ApiModel):
    id: UUID
    organization_id: UUID
    plan_id: UUID
    started_at: datetime
    completed_at: datetime | None
    budget_consumed: dict[str, Any]
    status: str
    executed_by: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def _deny_unless_entitled(
    session: AsyncSession,
    context: SecurityContext,
    *,
    settings: Settings,
    action: str,
    resource_id: str | None,
) -> None:
    """PG-5: multi-step tool plans are "Multi-step tool plans" in
    `Docs/00-product/07-packaging-and-editions.md` §3 -- Enterprise floor,
    and this router had no entitlement check at all before PG-5 (only the
    `require_roles` above each endpoint). Raises `HTTPException(403)` and
    commits an audit record of the denial (mirroring
    `query_gateway.py`'s `AuthorizationDenied` handling) if the
    organization's edition does not include the capability; otherwise
    returns normally and the caller proceeds.
    """
    entitlement = evaluate_entitlement(
        organization_edition=settings.edition, capability="multi_step_tool_plans"
    )
    if entitlement.allowed:
        return
    record_audit(
        session,
        context,
        action=action,
        resource_type="ToolPlan",
        resource_id=resource_id,
        outcome="DENIED",
        correlation_id=get_correlation_id(),
        details=entitlement.snapshot(),
    )
    await session.commit()
    raise HTTPException(status_code=403, detail=entitlement.reason_code)


@router.post(
    "/tool-plans",
    response_model=ToolPlanRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_tool_plan(
    body: ToolPlanCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "DataEngineer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ToolPlanRead:
    """Create a new tool plan."""
    org_id = context.require_organization()
    await _deny_unless_entitled(
        session, context, settings=settings, action="tool_plan.entitlement_denied", resource_id=None
    )

    plan = ToolPlan(
        id=None,
        name=body.name,
        steps=[
            PlanStep(
                sequence=s.sequence,
                tool_id=s.tool_id,
                tool_version=s.tool_version,
                parameters=s.parameters,
                dependencies=s.dependencies,
                timeout_seconds=s.timeout_seconds,
                expected_cost=s.expected_cost,
            )
            for s in body.steps
        ],
        budget=PlanBudget(
            max_steps=body.budget.max_steps,
            max_time_seconds=body.budget.max_time_seconds,
            max_tokens=body.budget.max_tokens,
            max_cost_units=body.budget.max_cost_units,
        ),
    )

    record = await persist_plan(session, org_id, plan, context.principal_id)

    record_audit(
        session,
        context,
        action="tool_plan.create",
        resource_type="ToolPlan",
        resource_id=str(record.id),
        outcome="success",
        correlation_id=get_correlation_id(),
        details={"name": body.name, "step_count": len(body.steps)},
    )

    await session.commit()
    return ToolPlanRead.model_validate(record)


@router.get(
    "/tool-plans/{plan_id}",
    response_model=ToolPlanDetailRead,
)
async def get_tool_plan(
    plan_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "DataEngineer", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> ToolPlanDetailRead:
    """Get tool plan with steps."""
    # Called for its refusal, not its value: a request with no tenant claim is a 400
    # before anything is loaded. The row's own organization is what scopes the read below.
    context.require_organization()

    plan_record = await session.get(ToolPlanRecord, plan_id)
    if plan_record is None:
        raise HTTPException(status_code=404, detail="tool plan not found")
    enforce_organization(context, plan_record.organization_id)

    steps_stmt = (
        select(ToolPlanStepRecord)
        .where(ToolPlanStepRecord.plan_id == plan_id)
        .order_by(ToolPlanStepRecord.sequence)
    )
    steps_result = await session.execute(steps_stmt)
    step_rows = steps_result.scalars().all()

    return ToolPlanDetailRead(
        id=plan_record.id,
        organization_id=plan_record.organization_id,
        name=plan_record.name,
        budget=plan_record.budget,
        status=plan_record.status,
        created_by=plan_record.created_by,
        created_at=plan_record.created_at,
        updated_at=plan_record.updated_at,
        steps=[ToolPlanStepRead.model_validate(s) for s in step_rows],
    )


@router.post(
    "/tool-plans/{plan_id}/validate",
    response_model=ValidationResponse,
)
async def validate_tool_plan(
    plan_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "DataEngineer")
    ),
    session: AsyncSession = Depends(get_session),
) -> ValidationResponse:
    """Validate a tool plan."""
    # Called for its refusal, not its value: a request with no tenant claim is a 400
    # before anything is loaded. The row's own organization is what scopes the read below.
    context.require_organization()

    plan_record = await session.get(ToolPlanRecord, plan_id)
    if plan_record is None:
        raise HTTPException(status_code=404, detail="tool plan not found")
    enforce_organization(context, plan_record.organization_id)

    steps_stmt = (
        select(ToolPlanStepRecord)
        .where(ToolPlanStepRecord.plan_id == plan_id)
        .order_by(ToolPlanStepRecord.sequence)
    )
    steps_result = await session.execute(steps_stmt)
    step_rows = steps_result.scalars().all()

    plan = ToolPlan(
        id=plan_record.id,
        name=plan_record.name,
        steps=[
            PlanStep(
                sequence=s.sequence,
                tool_id=s.tool_id,
                tool_version=s.tool_version,
                parameters=s.parameters,
                dependencies=s.dependencies,
                timeout_seconds=s.timeout_seconds,
                expected_cost=s.expected_cost,
            )
            for s in step_rows
        ],
        budget=PlanBudget(**plan_record.budget) if plan_record.budget else PlanBudget(),
    )

    result = validate_plan(plan)

    if result.valid:
        plan_record.status = "VALIDATED"
        record_audit(
            session,
            context,
            action="tool_plan.validate",
            resource_type="tool_plan",
            resource_id=str(plan_id),
            outcome="VALID" if result.valid else "INVALID",
            correlation_id=get_correlation_id(),
        )
        await session.commit()

    return ValidationResponse(
        valid=result.valid,
        issues=[
            ValidationIssueRead(
                step_sequence=i.step_sequence,
                issue=i.issue,
                severity=i.severity,
            )
            for i in result.issues
        ],
    )


@router.post(
    "/tool-plans/{plan_id}/execute",
    response_model=ExecutionRead,
    status_code=status.HTTP_201_CREATED,
)
async def execute_tool_plan(
    plan_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "DataEngineer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ExecutionRead:
    """Execute a validated tool plan."""
    org_id = context.require_organization()
    # PG-5: checked again at execute, not only at create -- a plan created
    # while the deployment held an Enterprise+ edition must not still be
    # executable after a downgrade.
    await _deny_unless_entitled(
        session,
        context,
        settings=settings,
        action="tool_plan.entitlement_denied",
        resource_id=str(plan_id),
    )

    plan_record = await session.get(ToolPlanRecord, plan_id)
    if plan_record is None:
        raise HTTPException(status_code=404, detail="tool plan not found")
    enforce_organization(context, plan_record.organization_id)

    if plan_record.status not in ("VALIDATED", "DRAFT"):
        raise HTTPException(
            status_code=409,
            detail=f"plan is in state {plan_record.status}, cannot execute",
        )

    steps_stmt = (
        select(ToolPlanStepRecord)
        .where(ToolPlanStepRecord.plan_id == plan_id)
        .order_by(ToolPlanStepRecord.sequence)
    )
    steps_result = await session.execute(steps_stmt)
    step_rows = steps_result.scalars().all()

    plan = ToolPlan(
        id=plan_record.id,
        name=plan_record.name,
        steps=[
            PlanStep(
                sequence=s.sequence,
                tool_id=s.tool_id,
                tool_version=s.tool_version,
                parameters=s.parameters,
                dependencies=s.dependencies,
                timeout_seconds=s.timeout_seconds,
                expected_cost=s.expected_cost,
            )
            for s in step_rows
        ],
        budget=PlanBudget(**plan_record.budget) if plan_record.budget else PlanBudget(),
    )

    plan_record.status = "EXECUTING"
    await session.flush()

    plan_result = await execute_plan(
        plan, org_id, session, context.principal_id
    )

    # Update plan status
    plan_record.status = plan_result.status

    # Update step records
    step_by_seq = {s.sequence: s for s in step_rows}
    for sr in plan_result.step_results:
        step_record = step_by_seq.get(sr.sequence)
        if step_record:
            step_record.status = sr.status
            step_record.started_at = sr.started_at
            step_record.completed_at = sr.completed_at
            step_record.evidence = sr.evidence
            step_record.error_message = sr.error_message

    execution_record = await persist_execution(
        session, org_id, plan_result, context.principal_id
    )

    record_audit(
        session,
        context,
        action="tool_plan.execute",
        resource_type="ToolPlan",
        resource_id=str(plan_id),
        outcome="success",
        correlation_id=get_correlation_id(),
        details={
            "status": plan_result.status,
            "steps_executed": plan_result.budget_consumed.steps_executed,
        },
    )

    record_outbox(
        session,
        organization_id=org_id,
        aggregate_type="ToolPlan",
        aggregate_id=str(plan_id),
        event_type="tool_plan.execution_completed",
        payload={
            "plan_id": str(plan_id),
            "status": plan_result.status,
            "steps_executed": plan_result.budget_consumed.steps_executed,
        },
    )

    await session.commit()
    return ExecutionRead.model_validate(execution_record)


@router.post(
    "/tool-plans/{plan_id}/cancel",
    response_model=ToolPlanRead,
)
async def cancel_tool_plan(
    plan_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper")
    ),
    session: AsyncSession = Depends(get_session),
) -> ToolPlanRead:
    """Cancel a running or draft tool plan."""
    # Called for its refusal, not its value: a request with no tenant claim is a 400
    # before anything is loaded. The row's own organization is what scopes the read below.
    context.require_organization()

    plan_record = await session.get(ToolPlanRecord, plan_id)
    if plan_record is None:
        raise HTTPException(status_code=404, detail="tool plan not found")
    enforce_organization(context, plan_record.organization_id)

    if plan_record.status in ("COMPLETED", "CANCELLED"):
        raise HTTPException(
            status_code=409,
            detail=f"plan is already {plan_record.status}",
        )

    plan_record.status = "CANCELLED"

    record_audit(
        session,
        context,
        action="tool_plan.cancel",
        resource_type="ToolPlan",
        resource_id=str(plan_id),
        outcome="success",
        correlation_id=get_correlation_id(),
    )

    await session.commit()
    return ToolPlanRead.model_validate(plan_record)


@router.get(
    "/tool-plans/{plan_id}/evidence",
    response_model=Page,
)
async def get_plan_evidence(
    plan_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "DataEngineer", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """Get execution evidence for a tool plan."""
    org_id = context.require_organization()

    plan_record = await session.get(ToolPlanRecord, plan_id)
    if plan_record is None:
        raise HTTPException(status_code=404, detail="tool plan not found")
    enforce_organization(context, plan_record.organization_id)

    stmt = (
        select(ToolPlanExecutionRecord)
        .where(
            and_(
                ToolPlanExecutionRecord.plan_id == plan_id,
                ToolPlanExecutionRecord.organization_id == org_id,
            )
        )
        .order_by(ToolPlanExecutionRecord.started_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    items = [ExecutionRead.model_validate(row) for row in rows]
    return Page(items=items, total=len(items), limit=limit, offset=offset)
