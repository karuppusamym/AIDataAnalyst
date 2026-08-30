"""
Compliance Pack Generation API (Phase E - EE.4 / OB-5)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.compliance_packs import (
    CompliancePack,
    Framework,
    generate_pack,
    persist_pack,
    _section_to_dict,
)
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.models import CompliancePackRecord
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["compliance"])


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class GeneratePackRequest(ApiModel):
    framework: Literal["MODEL_RISK", "BCBS_239", "ACCESS_REVIEW", "AI_USAGE", "CHANGE_CONTROL"]
    period_start: datetime
    period_end: datetime
    name: str | None = None


class CompliancePackRead(ApiModel):
    id: UUID
    organization_id: UUID
    name: str
    framework: str
    period_start: datetime
    period_end: datetime
    sections: list[dict[str, Any]]
    status: str
    checksum: str
    generated_by: str
    generated_at: datetime
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/compliance/packs/generate",
    response_model=CompliancePackRead,
    status_code=status.HTTP_201_CREATED,
)
async def generate_compliance_pack(
    body: GeneratePackRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ComplianceOfficer", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> CompliancePackRead:
    """Generate an audit-ready compliance pack from runtime evidence."""
    org_id = context.require_organization()

    if body.period_end <= body.period_start:
        raise HTTPException(
            status_code=422,
            detail="period_end must be after period_start",
        )

    pack = await generate_pack(
        framework=body.framework,
        period_start=body.period_start,
        period_end=body.period_end,
        org_id=org_id,
        session=session,
        generated_by=context.principal_id,
    )

    record = await persist_pack(
        session=session,
        org_id=org_id,
        pack=pack,
        generated_by=context.principal_id,
    )

    record_audit(
        session,
        context,
        action="compliance_pack.generate",
        resource_type="CompliancePack",
        resource_id=str(record.id),
        outcome="success",
        correlation_id=get_correlation_id(),
        details={
            "framework": body.framework,
            "period_start": body.period_start.isoformat(),
            "period_end": body.period_end.isoformat(),
            "checksum": pack.checksum,
        },
    )

    await session.commit()
    return CompliancePackRead.model_validate(record)


@router.get(
    "/compliance/packs",
    response_model=Page[CompliancePackRead],
)
async def list_compliance_packs(
    framework: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ComplianceOfficer", "DataSteward", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page[CompliancePackRead]:
    """List generated compliance packs."""
    org_id = context.require_organization()

    stmt = (
        select(CompliancePackRecord)
        .where(CompliancePackRecord.organization_id == org_id)
    )
    if framework:
        stmt = stmt.where(CompliancePackRecord.framework == framework)

    stmt = stmt.order_by(CompliancePackRecord.generated_at.desc()).offset(offset).limit(limit)
    result = await session.execute(stmt)
    rows = result.scalars().all()

    items = [CompliancePackRead.model_validate(row) for row in rows]
    return Page(items=items, total=len(items), limit=limit, offset=offset)


@router.get(
    "/compliance/packs/{pack_id}",
    response_model=CompliancePackRead,
)
async def get_compliance_pack(
    pack_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ComplianceOfficer", "DataSteward", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> CompliancePackRead:
    """Get compliance pack detail."""
    org_id = context.require_organization()

    record = await session.get(CompliancePackRecord, pack_id)
    if record is None:
        raise HTTPException(status_code=404, detail="compliance pack not found")
    enforce_organization(context, record.organization_id)

    return CompliancePackRead.model_validate(record)


@router.get(
    "/compliance/packs/{pack_id}/download",
    response_model=dict[str, Any],
)
async def download_compliance_pack(
    pack_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ComplianceOfficer", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Download compliance pack as structured JSON."""
    org_id = context.require_organization()

    record = await session.get(CompliancePackRecord, pack_id)
    if record is None:
        raise HTTPException(status_code=404, detail="compliance pack not found")
    enforce_organization(context, record.organization_id)

    return {
        "id": str(record.id),
        "name": record.name,
        "framework": record.framework,
        "period_start": record.period_start.isoformat(),
        "period_end": record.period_end.isoformat(),
        "sections": record.sections,
        "checksum": record.checksum,
        "generated_by": record.generated_by,
        "generated_at": record.generated_at.isoformat(),
        "status": record.status,
    }
