"""AI decision lineage API: query decision edges for runs, assets, and refusals."""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.ai_decision_lineage import (
    get_decisions_for_asset,
    get_decisions_for_run,
    get_refusals,
)
from aida.db import get_session
from aida.models import AiDecisionRecord
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["ai-decision-lineage"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AiDecisionRead(ApiModel):
    id: UUID
    organization_id: UUID
    run_id: UUID
    decision_type: str
    source_node: str
    target_node: str
    reason: str
    evidence: dict[str, Any]
    control_version: str | None
    decided_at: datetime


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/ai-decisions/refusals",
    response_model=Page,
    summary="List all AI refusal decisions for audit",
)
async def list_refusals(
    organization_id: UUID = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataAdmin")),
) -> Page:
    """List refusals before the dynamic run route can match ``refusals`` as a UUID."""
    enforce_organization(context, organization_id)

    count_stmt = (
        select(func.count())
        .select_from(AiDecisionRecord)
        .where(
            AiDecisionRecord.organization_id == organization_id,
            AiDecisionRecord.decision_type == "REFUSAL",
        )
    )
    total = (await session.execute(count_stmt)).scalar() or 0
    records = await get_refusals(session, organization_id, limit=limit, offset=offset)
    return Page(
        items=[AiDecisionRead.model_validate(record) for record in records],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.get(
    "/ai-decisions/{run_id}",
    response_model=list[AiDecisionRead],
    summary="Get all AI decisions for a specific run",
)
async def get_run_decisions(
    run_id: UUID,
    organization_id: UUID = Query(...),
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Analyst", "Viewer")
    ),
) -> list[AiDecisionRead]:
    enforce_organization(context, organization_id)
    records = await get_decisions_for_run(session, run_id)
    return [AiDecisionRead.model_validate(r) for r in records]
@router.get(
    "/ai-decisions/asset/{asset_id}",
    response_model=list[AiDecisionRead],
    summary="Get AI decisions involving a specific asset",
)
async def get_asset_decisions(
    asset_id: str,
    organization_id: UUID = Query(...),
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Analyst", "Viewer")
    ),
) -> list[AiDecisionRead]:
    enforce_organization(context, organization_id)
    records = await get_decisions_for_asset(session, asset_id, limit=limit)
    return [AiDecisionRead.model_validate(r) for r in records]
