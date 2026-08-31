"""PG-4: delegation and reassignment of governance authority.

Grant, revoke, and list endpoints for `aida.models.Delegation` -- a
principal (the delegator) hands one or more of its own roles to another
principal (the delegate) for a bounded time window, e.g. a steward or
reviewer going on leave delegates their governance-review decision
authority to a covering colleague.

Both halves of PG-4's exit condition ("time-bounded, audited") are enforced
here and at the one real consumer of a grant, `aida.security
.require_roles_or_delegated`:

  * time-bounded -- `starts_at`/`expires_at` are validated on grant (must be
    ordered, bounded to `MAX_DELEGATION_WINDOW`), and
    `aida.delegation.is_delegation_active` is the single query-time
    projection that decides whether a grant is currently honored; nothing
    flips a status column at expiry (mirrors GL-5/CT-5 certification expiry
    -- the row is retained, evaluation is what enforces the window).
  * audited -- granting and revoking both write an `AuditEvent` and an
    outbox event here (`delegation.granted` / `delegation.revoked`, already
    named in `Docs/30-contracts/04-event-catalog.md`); *using* a delegation
    to decide a governance review is audited too, at
    `semantic_api.decide_governance_review` /
    `bulk_decide_governance_reviews`, which record `via_delegation_id` /
    `via_delegator_principal_id` in that decision's own `AuditEvent` when
    `SecurityContext.active_delegation_id` is set.

A principal can only delegate roles it actually holds
(`aida.delegation.validate_delegated_roles`, checked against the granting
request's own `context.roles`) -- this endpoint cannot be used to
manufacture authority nobody granted the delegator in the first place.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.delegation import validate_delegated_roles
from aida.events import record_audit, record_outbox
from aida.models import Delegation
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["delegation"])

# Roles eligible to hold governance-review decision authority in the first
# place (the same set `semantic_api.decide_governance_review` /
# `bulk_decide_governance_reviews` gate on) plus the metadata/semantic/data
# admin roles that hold other governance-adjacent authority elsewhere in
# this module -- a floor for who may attempt a grant at all;
# `validate_delegated_roles` is what actually stops a grant from exceeding
# what the caller holds.
DELEGATION_ROLES = (
    "PlatformAdmin",
    "DataSteward",
    "Reviewer",
    "MetadataAdmin",
    "DataAdmin",
    "SemanticAdmin",
)

# A delegation is time-bounded by definition (PG-4's own exit condition) --
# this caps how far bounded can stretch, so "time-bounded" cannot mean "for
# a decade" in practice. 180 days comfortably covers parental/medical leave
# and sabbaticals; a longer genuine need is a role-reassignment question,
# not a delegation.
MAX_DELEGATION_WINDOW = timedelta(days=180)


class DelegationCreate(ApiModel):
    delegate_principal_id: str = Field(min_length=1, max_length=255)
    delegated_roles: list[str] = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=10, max_length=2000)
    starts_at: datetime | None = None
    expires_at: datetime

    @model_validator(mode="after")
    def validate_roles_unique(self) -> "DelegationCreate":
        if len(set(self.delegated_roles)) != len(self.delegated_roles):
            raise ValueError("delegated_roles must be unique")
        return self


class DelegationRead(ApiModel):
    id: UUID
    organization_id: UUID
    delegator_principal_id: str
    delegate_principal_id: str
    delegated_roles: list[str]
    reason: str
    starts_at: datetime
    expires_at: datetime
    status: str
    created_by: str
    revoked_by: str | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _audit_context(context: SecurityContext, organization_id: UUID) -> SecurityContext:
    return replace(context, organization_id=organization_id)


@router.post(
    "/organizations/{organization_id}/delegations",
    response_model=DelegationRead,
    status_code=status.HTTP_201_CREATED,
)
async def grant_delegation(
    organization_id: UUID,
    body: DelegationCreate,
    context: SecurityContext = Depends(require_roles(*DELEGATION_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Delegation:
    enforce_organization(context, organization_id)
    if body.delegate_principal_id == context.principal_id:
        raise HTTPException(status_code=422, detail="cannot delegate authority to yourself")
    try:
        validate_delegated_roles(set(body.delegated_roles), set(context.roles))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    now = datetime.now(UTC)
    starts_at = body.starts_at or now
    if body.expires_at <= starts_at:
        raise HTTPException(status_code=422, detail="expires_at must be after starts_at")
    if body.expires_at - starts_at > MAX_DELEGATION_WINDOW:
        raise HTTPException(
            status_code=422,
            detail=f"delegation window exceeds the {MAX_DELEGATION_WINDOW.days}-day cap",
        )
    delegation = Delegation(
        organization_id=organization_id,
        delegator_principal_id=context.principal_id,
        delegate_principal_id=body.delegate_principal_id,
        delegated_roles=sorted(set(body.delegated_roles)),
        reason=body.reason,
        starts_at=starts_at,
        expires_at=body.expires_at,
        created_by=context.principal_id,
    )
    session.add(delegation)
    await session.flush()
    record_audit(
        session,
        _audit_context(context, organization_id),
        action="delegation.grant",
        resource_type="delegation",
        resource_id=str(delegation.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "delegator_principal_id": context.principal_id,
            "delegate_principal_id": body.delegate_principal_id,
            "delegated_roles": delegation.delegated_roles,
            "starts_at": starts_at.isoformat(),
            "expires_at": body.expires_at.isoformat(),
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="delegation",
        aggregate_id=str(delegation.id),
        event_type="delegation.granted",
        payload={
            "delegation_id": str(delegation.id),
            "delegator_principal_id": context.principal_id,
            "delegate_principal_id": body.delegate_principal_id,
            "delegated_roles": delegation.delegated_roles,
            "expires_at": body.expires_at.isoformat(),
        },
    )
    await session.commit()
    return delegation


@router.post(
    "/delegations/{delegation_id}/revoke",
    response_model=DelegationRead,
)
async def revoke_delegation(
    delegation_id: UUID,
    context: SecurityContext = Depends(require_roles(*DELEGATION_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Delegation:
    delegation = await session.get(Delegation, delegation_id)
    if delegation is None:
        raise HTTPException(status_code=404, detail="delegation not found")
    enforce_organization(context, delegation.organization_id)
    if delegation.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="delegation is not active")
    if (
        context.principal_id != delegation.delegator_principal_id
        and "PlatformAdmin" not in context.roles
    ):
        raise HTTPException(
            status_code=403,
            detail="only the delegator or a platform admin may revoke a delegation",
        )
    now = datetime.now(UTC)
    delegation.status = "REVOKED"
    delegation.revoked_by = context.principal_id
    delegation.revoked_at = now
    record_audit(
        session,
        _audit_context(context, delegation.organization_id),
        action="delegation.revoke",
        resource_type="delegation",
        resource_id=str(delegation.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "delegator_principal_id": delegation.delegator_principal_id,
            "delegate_principal_id": delegation.delegate_principal_id,
            "revoked_by": context.principal_id,
        },
    )
    record_outbox(
        session,
        organization_id=delegation.organization_id,
        aggregate_type="delegation",
        aggregate_id=str(delegation.id),
        event_type="delegation.revoked",
        payload={
            "delegation_id": str(delegation.id),
            "delegator_principal_id": delegation.delegator_principal_id,
            "delegate_principal_id": delegation.delegate_principal_id,
            "revoked_by": context.principal_id,
        },
    )
    await session.commit()
    return delegation


@router.get(
    "/organizations/{organization_id}/delegations",
    response_model=Page,
)
async def list_delegations(
    organization_id: UUID,
    delegate_principal_id: str | None = Query(default=None, max_length=255),
    delegator_principal_id: str | None = Query(default=None, max_length=255),
    delegation_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*DELEGATION_ROLES, "Viewer", "Auditor")),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = [Delegation.organization_id == organization_id]
    if delegate_principal_id is not None:
        filters.append(Delegation.delegate_principal_id == delegate_principal_id)
    if delegator_principal_id is not None:
        filters.append(Delegation.delegator_principal_id == delegator_principal_id)
    if delegation_status is not None:
        filters.append(Delegation.status == delegation_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(Delegation).where(*filters)
    )
    rows = (
        await session.scalars(
            select(Delegation)
            .where(*filters)
            .order_by(Delegation.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[DelegationRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )
