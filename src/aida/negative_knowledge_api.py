"""
Negative Knowledge Surface API (Phase E - EE.3)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.models import NegativeAssertionRecord
from aida.negative_knowledge import (
    lift_suppression,
    query_negatives,
    search_negatives,
)
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["negative-knowledge"])


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class NegativeAssertionRead(ApiModel):
    id: UUID
    organization_id: UUID
    assertion_type: str
    subject_id: str
    predicate: dict[str, Any]
    evidence: dict[str, Any]
    rejected_by: str
    rejected_at: datetime
    suppression_active: bool
    material_change_hash: str | None
    suppression_lifted_at: datetime | None
    suppression_lifted_by: str | None
    lift_reason: str | None
    created_at: datetime
    updated_at: datetime


class LiftSuppressionRequest(ApiModel):
    reason: str = Field(min_length=3, max_length=2000)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/negative-knowledge/{subject_id}",
    response_model=Page,
)
async def get_subject_assertions(
    subject_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataSteward", "DataEngineer", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """Get negative assertions for a specific subject."""
    org_id = context.require_organization()

    records = await query_negatives(session, org_id, subject_id)

    # Apply pagination
    paginated = records[offset : offset + limit]
    items = [NegativeAssertionRead.model_validate(r) for r in paginated]
    return Page(items=items, total=len(records), limit=limit, offset=offset)


@router.get(
    "/negative-knowledge/search",
    response_model=Page,
)
async def search_negative_assertions(
    assertion_type: str | None = Query(default=None),
    suppression_active: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataSteward", "DataEngineer", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """Search negative assertions with optional filters."""
    org_id = context.require_organization()

    records = await search_negatives(
        session,
        org_id,
        assertion_type=assertion_type,
        suppression_active=suppression_active,
        limit=limit,
        offset=offset,
    )

    items = [NegativeAssertionRead.model_validate(r) for r in records]
    return Page(items=items, total=len(items), limit=limit, offset=offset)


@router.post(
    "/negative-knowledge/{assertion_id}/lift-suppression",
    response_model=NegativeAssertionRead,
)
async def lift_assertion_suppression(
    assertion_id: UUID,
    body: LiftSuppressionRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> NegativeAssertionRead:
    """Manually lift suppression on a negative assertion."""
    org_id = context.require_organization()

    record = await lift_suppression(
        session,
        org_id,
        assertion_id,
        lifted_by=context.principal_id,
        reason=body.reason,
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="negative assertion not found",
        )

    record_audit(
        session,
        context,
        action="negative_assertion.lift_suppression",
        resource_type="NegativeAssertion",
        resource_id=str(assertion_id),
        outcome="success",
        correlation_id=get_correlation_id(),
        details={"reason": body.reason},
    )

    await session.commit()
    return NegativeAssertionRead.model_validate(record)
