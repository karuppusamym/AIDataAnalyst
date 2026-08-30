"""
REST API for consumption lineage (CX-4).

Three read-only endpoints expose the consumption edges recorded by the MCP
server and the Context Product REST API whenever a resource read passes
policy checks.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import Field

from aida.consumption_lineage import (
    get_consumption_by_consumer,
    get_consumption_for_resource,
    get_consumption_graph,
)
from aida.db import AsyncSession, get_session
from aida.schemas import ApiModel
from aida.security import SecurityContext, get_security_context

router = APIRouter(prefix="/api/v1", tags=["consumption-lineage"])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class ConsumptionRecordRead(ApiModel):
    id: UUID
    organization_id: UUID
    consumer_id: str
    consumer_type: str
    resource_type: str
    resource_id: str
    channel: str
    correlation_id: str
    policy_decision: str
    business_purpose: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    consumed_at: datetime


class ConsumptionRecordPage(ApiModel):
    items: list[ConsumptionRecordRead]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get(
    "/organizations/{organization_id}/consumption-lineage/by-resource",
    response_model=ConsumptionRecordPage,
    summary="Consumption edges for a resource",
)
async def list_consumption_for_resource(
    organization_id: UUID,
    resource_type: str = Query(..., min_length=1, max_length=100),
    resource_id: str = Query(..., min_length=1, max_length=255),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    context: SecurityContext = Depends(get_security_context),
    session: AsyncSession = Depends(get_session),
) -> ConsumptionRecordPage:
    context.require_organization()
    items, total = await get_consumption_for_resource(
        session,
        organization_id=organization_id,
        resource_type=resource_type,
        resource_id=resource_id,
        limit=limit,
        offset=offset,
    )
    return ConsumptionRecordPage(
        items=[ConsumptionRecordRead.model_validate(r) for r in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/organizations/{organization_id}/consumption-lineage/by-consumer",
    response_model=ConsumptionRecordPage,
    summary="Consumption edges for a consumer",
)
async def list_consumption_by_consumer(
    organization_id: UUID,
    consumer_id: str = Query(..., min_length=1, max_length=255),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    context: SecurityContext = Depends(get_security_context),
    session: AsyncSession = Depends(get_session),
) -> ConsumptionRecordPage:
    context.require_organization()
    items, total = await get_consumption_by_consumer(
        session,
        organization_id=organization_id,
        consumer_id=consumer_id,
        limit=limit,
        offset=offset,
    )
    return ConsumptionRecordPage(
        items=[ConsumptionRecordRead.model_validate(r) for r in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/organizations/{organization_id}/consumption-lineage/graph",
    response_model=ConsumptionRecordPage,
    summary="Full consumption graph for an organization",
)
async def list_consumption_graph(
    organization_id: UUID,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    context: SecurityContext = Depends(get_security_context),
    session: AsyncSession = Depends(get_session),
) -> ConsumptionRecordPage:
    context.require_organization()
    items, total = await get_consumption_graph(
        session,
        organization_id=organization_id,
        limit=limit,
        offset=offset,
    )
    return ConsumptionRecordPage(
        items=[ConsumptionRecordRead.model_validate(r) for r in items],
        total=total,
        limit=limit,
        offset=offset,
    )
