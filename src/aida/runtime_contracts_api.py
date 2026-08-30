"""
Runtime Data Contract Enforcement API (Phase E - EE.1)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    ContractSlaRecord,
    ContractViolationRecord,
    DataContractVersion,
    DataQualityObservation,
    MetadataColumn,
    MetadataTable,
    TableProfile,
)
from aida.runtime_contracts import (
    ContractViolation,
    EnforcementResult,
    SlaStatus,
    contract_from_db,
    enforce_at_query_time,
    evaluate_contract,
    persist_violations,
    record_sla_status,
)
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["runtime-contracts"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ViolationRead(ApiModel):
    id: UUID
    organization_id: UUID
    contract_id: UUID
    violation_type: str
    severity: str
    evidence: dict[str, Any]
    detected_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None
    created_at: datetime
    updated_at: datetime


class EvaluationResponse(ApiModel):
    contract_id: UUID
    violations: list[dict[str, Any]]
    enforcement_action: str
    allowed: bool
    reason: str | None = None


class SlaStatusResponse(ApiModel):
    contract_id: UUID
    compliant: bool
    uptime_percent: float
    violations_in_period: int
    breach_minutes: int
    period_start: datetime
    period_end: datetime


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/data-contracts/{contract_id}/evaluate",
    response_model=EvaluationResponse,
    status_code=status.HTTP_200_OK,
)
async def evaluate_data_contract(
    contract_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataSteward", "DataEngineer", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> EvaluationResponse:
    """Evaluate a data contract against current state."""
    org_id = context.require_organization()

    contract_row = await session.get(DataContractVersion, contract_id)
    if contract_row is None:
        raise HTTPException(status_code=404, detail="contract not found")
    enforce_organization(context, contract_row.organization_id)

    contract = contract_from_db(contract_row)

    # Gather current columns from schema definition (the contract itself defines expected)
    current_columns = [
        {"name": field_def["name"], "physical_type": field_def["data_type"]}
        for field_def in (contract_row.schema_definition or [])
    ]

    # Gather recent quality observations
    quality_stmt = (
        select(DataQualityObservation)
        .where(
            and_(
                DataQualityObservation.organization_id == org_id,
            )
        )
        .order_by(DataQualityObservation.created_at.desc())
        .limit(10)
    )
    quality_result = await session.execute(quality_stmt)
    quality_rows = quality_result.scalars().all()
    quality_observations = [
        {
            "quality_score": row.quality_score,
            "anomaly_types": row.anomaly_types,
            "status": row.status,
        }
        for row in quality_rows
    ]

    # Get last profile time
    profile_stmt = (
        select(TableProfile.created_at)
        .where(TableProfile.organization_id == org_id)
        .order_by(TableProfile.created_at.desc())
        .limit(1)
    )
    profile_result = await session.execute(profile_stmt)
    last_profile_at = profile_result.scalar_one_or_none()

    violations = evaluate_contract(
        contract, current_columns, quality_observations, last_profile_at
    )
    enforcement = enforce_at_query_time(contract, violations)

    # Persist violations
    if violations:
        await persist_violations(session, org_id, violations)
        record_outbox(
            session,
            organization_id=org_id,
            aggregate_type="DataContract",
            aggregate_id=str(contract_id),
            event_type="contract.violations_detected",
            payload={
                "contract_id": str(contract_id),
                "violation_count": len(violations),
                "enforcement_action": enforcement.enforcement_action,
            },
        )

    record_audit(
        session,
        context,
        action="data_contract.evaluate",
        resource_type="DataContractVersion",
        resource_id=str(contract_id),
        outcome="success",
        correlation_id=get_correlation_id(),
        details={
            "violation_count": len(violations),
            "enforcement_action": enforcement.enforcement_action,
        },
    )

    await session.commit()

    return EvaluationResponse(
        contract_id=contract_id,
        violations=[
            {
                "violation_type": v.violation_type,
                "severity": v.severity,
                "evidence": v.evidence,
                "detected_at": v.detected_at.isoformat(),
            }
            for v in violations
        ],
        enforcement_action=enforcement.enforcement_action,
        allowed=enforcement.allowed,
        reason=enforcement.reason,
    )


@router.get(
    "/data-contracts/{contract_id}/violations",
    response_model=Page,
)
async def list_contract_violations(
    contract_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataSteward", "DataEngineer", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """List violations for a contract."""
    org_id = context.require_organization()

    contract_row = await session.get(DataContractVersion, contract_id)
    if contract_row is None:
        raise HTTPException(status_code=404, detail="contract not found")
    enforce_organization(context, contract_row.organization_id)

    stmt = (
        select(ContractViolationRecord)
        .where(
            and_(
                ContractViolationRecord.contract_id == contract_id,
                ContractViolationRecord.organization_id == org_id,
            )
        )
        .order_by(ContractViolationRecord.detected_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    items = [ViolationRead.model_validate(row) for row in rows]
    return Page(items=items, total=len(items), limit=limit, offset=offset)


@router.get(
    "/data-contracts/{contract_id}/sla-status",
    response_model=SlaStatusResponse,
)
async def get_sla_status(
    contract_id: UUID,
    period_days: int = Query(default=30, ge=1, le=365),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataSteward", "DataEngineer", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> SlaStatusResponse:
    """Get SLA compliance status for a contract."""
    org_id = context.require_organization()

    contract_row = await session.get(DataContractVersion, contract_id)
    if contract_row is None:
        raise HTTPException(status_code=404, detail="contract not found")
    enforce_organization(context, contract_row.organization_id)

    now = datetime.now(UTC)
    period_start = now - timedelta(days=period_days)
    period_end = now

    sla_record = await record_sla_status(
        session, org_id, contract_id, period_start, period_end
    )
    record_audit(
        session,
        context,
        action="data_contract.sla_status",
        resource_type="data_contract",
        resource_id=str(contract_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    await session.commit()

    return SlaStatusResponse(
        contract_id=contract_id,
        compliant=sla_record.uptime_percent >= (contract_row.availability_sla_percent or 99.0),
        uptime_percent=sla_record.uptime_percent,
        violations_in_period=sla_record.violations_count,
        breach_minutes=sla_record.breach_minutes,
        period_start=period_start,
        period_end=period_end,
    )
