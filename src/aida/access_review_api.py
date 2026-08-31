"""Self-service entitlement reporting API (OB-7).

`POST /v1/access-review/entitlements/generate` is deliberately reachable by
any authenticated principal with no role restriction at all -- the module 20
exit bar is "self-service" in the literal sense: a principal must be able to
pull their own report without asking an admin. Naming a *different*
`principal_id` in the body narrows that: it requires an elevated role and is
always audited as pulled "on behalf of" that principal (see
`aida.access_review`'s module docstring for why that report is grant-data
only, with no ABAC overlay).

Every generated report is persisted (`AccessReviewReportRecord`, append-only)
before the response is returned, following OB-5's compliance-pack precedent:
a bank's access-review process needs to point at a specific report as the
record of what was disclosed, not trust that a live query would reproduce
the same answer later.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.access_review import build_entitlement_report, persist_entitlement_report
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.models import AccessReviewReportRecord
from aida.schemas import (
    ClassificationDecisionRead,
    EntitlementReportRead,
    GenerateEntitlementReportRequest,
    Page,
    SourceEntitlementRead,
    WorkspaceEntitlementRead,
)
from aida.security import SecurityContext, enforce_organization, get_security_context

router = APIRouter(prefix="/v1", tags=["access-review"])

# Roles allowed to pull an entitlement report for a *different* principal.
# Self-service (no principal_id given) needs none of these -- see module docstring.
_ON_BEHALF_OF_ROLES = frozenset({"PlatformAdmin", "DataAdmin", "ComplianceOfficer"})


def _to_read(record: AccessReviewReportRecord) -> EntitlementReportRead:
    entitlements = record.entitlements
    return EntitlementReportRead(
        id=record.id,
        organization_id=record.organization_id,
        subject_principal_id=record.subject_principal_id,
        subject_principal_type=record.subject_principal_type,
        is_self_service=record.is_self_service,
        requested_by=record.requested_by,
        workspace_memberships=[
            WorkspaceEntitlementRead(**m) for m in entitlements.get("workspace_memberships", [])
        ],
        source_entitlements=[
            SourceEntitlementRead(**s) for s in entitlements.get("source_entitlements", [])
        ],
        abac_classification_decisions=[
            ClassificationDecisionRead(**d)
            for d in entitlements.get("abac_classification_decisions", [])
        ],
        abac_note=entitlements.get("abac_note", ""),
        checksum=record.checksum,
        generated_at=record.generated_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


@router.post(
    "/access-review/entitlements/generate",
    response_model=EntitlementReportRead,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a self-service entitlement report",
)
async def generate_entitlement_report(
    body: GenerateEntitlementReportRequest,
    context: SecurityContext = Depends(get_security_context),
    session: AsyncSession = Depends(get_session),
) -> EntitlementReportRead:
    org_id = context.require_organization()
    correlation_id = get_correlation_id()

    is_self_service = body.principal_id is None or body.principal_id == context.principal_id
    if is_self_service:
        subject_principal_id = context.principal_id
        subject_principal_type = context.principal_type
        requester_roles: frozenset[str] | None = context.roles
    else:
        if context.roles.isdisjoint(_ON_BEHALF_OF_ROLES):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "one of these roles is required to pull an entitlement report for "
                    f"another principal: {', '.join(sorted(_ON_BEHALF_OF_ROLES))}"
                ),
            )
        assert body.principal_id is not None  # narrows for mypy: is_self_service guarantees this
        subject_principal_id = body.principal_id
        subject_principal_type = body.principal_type
        requester_roles = None

    report = await build_entitlement_report(
        session,
        organization_id=org_id,
        subject_principal_id=subject_principal_id,
        subject_principal_type=subject_principal_type,
        requested_by=context.principal_id,
        is_self_service=is_self_service,
        requester_roles=requester_roles,
    )
    record = persist_entitlement_report(session, organization_id=org_id, report=report)
    await session.flush()

    record_audit(
        session,
        context,
        action="access_review.entitlement_report.generate",
        resource_type="access_review_report",
        resource_id=str(record.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={
            "subject_principal_id": subject_principal_id,
            "is_self_service": is_self_service,
            "checksum": report.checksum,
        },
    )

    await session.commit()
    await session.refresh(record)
    return _to_read(record)


@router.get(
    "/access-review/reports",
    response_model=Page,
    summary="List persisted entitlement reports",
)
async def list_entitlement_reports(
    subject_principal_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(get_security_context),
    session: AsyncSession = Depends(get_session),
) -> Page:
    org_id = context.require_organization()

    # Self-service by default: a principal with no elevated role only ever
    # sees their own report history, never another principal's.
    effective_subject = subject_principal_id
    if effective_subject is None and context.roles.isdisjoint(_ON_BEHALF_OF_ROLES):
        effective_subject = context.principal_id
    elif (
        effective_subject is not None
        and effective_subject != context.principal_id
        and context.roles.isdisjoint(_ON_BEHALF_OF_ROLES)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="an elevated role is required to list another principal's reports",
        )

    filters = [AccessReviewReportRecord.organization_id == org_id]
    if effective_subject is not None:
        filters.append(AccessReviewReportRecord.subject_principal_id == effective_subject)

    total = await session.scalar(
        select(func.count()).select_from(AccessReviewReportRecord).where(*filters)
    )
    rows = (
        await session.scalars(
            select(AccessReviewReportRecord)
            .where(*filters)
            .order_by(AccessReviewReportRecord.generated_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return Page(
        items=[_to_read(r) for r in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/access-review/reports/{report_id}",
    response_model=EntitlementReportRead,
    summary="Get a persisted entitlement report",
)
async def get_entitlement_report(
    report_id: UUID,
    context: SecurityContext = Depends(get_security_context),
    session: AsyncSession = Depends(get_session),
) -> EntitlementReportRead:
    record = await session.get(AccessReviewReportRecord, report_id)
    if record is None:
        raise HTTPException(status_code=404, detail="entitlement report not found")
    enforce_organization(context, record.organization_id)

    if (
        record.subject_principal_id != context.principal_id
        and context.roles.isdisjoint(_ON_BEHALF_OF_ROLES)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="an elevated role is required to read another principal's report",
        )

    return _to_read(record)
