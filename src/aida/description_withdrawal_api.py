"""Request that an approved description be retired.

    POST /v1/descriptions/withdrawals        raise one (reviewed, never applied here)
    GET  /v1/descriptions/withdrawals        what has been asked for, and decided

Deliberately only these two. There is no approve endpoint, because approving is
`POST /v1/governance/reviews/{id}/decision` like every other governed object --
a second decision surface would either duplicate that one or route around its
maker-checker guard, and removing a description an agent may be grounding on is
not a smaller decision than adding one.

Authorization mirrors `column_documentation_api`'s read gate on the way in: a
caller who cannot read a table's columns must not be able to act on their
descriptions. The write population is the stewardship one that can already
propose description changes elsewhere.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import gate_read
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.description_withdrawal import request_description_withdrawal
from aida.events import record_audit, record_outbox
from aida.models import DescriptionWithdrawal, MetadataColumn, MetadataTable
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["description-withdrawal"])

# Same population that may upload a data dictionary or a model workbook: all
# three propose a description change that a reviewer then decides.
_WITHDRAW_WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")
_WITHDRAW_READ_ROLES = (*_WITHDRAW_WRITE_ROLES, "Reviewer", "Analyst", "Viewer", "Auditor")


class DescriptionWithdrawalCreate(ApiModel):
    subject_type: str = Field(pattern="^(TABLE|COLUMN)$")
    subject_id: UUID
    #: WITHDRAW retires the current approved description; REINSTATE republishes
    #: a previously withdrawn one as a new version. Defaults to WITHDRAW so
    #: every existing caller is unchanged.
    request_type: str = Field(default="WITHDRAW", pattern="^(WITHDRAW|REINSTATE)$")
    #: Required, and not merely for the audit trail: a reviewer deciding a
    #: retraction needs to know what was wrong with the text, which the text
    #: itself cannot tell them.
    reason: str = Field(min_length=3, max_length=2000)


class DescriptionWithdrawalRead(ApiModel):
    id: UUID
    organization_id: UUID
    request_type: str
    subject_type: str
    subject_id: str
    subject_label: str
    version_id: UUID
    withdrawn_text: str
    reason: str
    status: str
    governance_review_id: UUID | None = None
    requested_by: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


async def _authorize_subject(
    body: DescriptionWithdrawalCreate,
    context: SecurityContext,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Run the same read gate the description's own read endpoint runs.

    Loaded and gated here rather than inside the service so the service stays
    a pure state transition, matching how every other publish path in this
    codebase splits authorization from application.
    """
    if body.subject_type == "COLUMN":
        column = await session.get(MetadataColumn, body.subject_id)
        if column is None:
            raise HTTPException(status_code=404, detail="column not found")
        enforce_organization(context, column.organization_id)
        table = await session.get(MetadataTable, column.table_id)
        if table is None:
            raise HTTPException(status_code=404, detail="table not found")
    else:
        table = await session.get(MetadataTable, body.subject_id)
        if table is None:
            raise HTTPException(status_code=404, detail="table not found")
        enforce_organization(context, table.organization_id)

    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="table",
        resource_id=str(table.id),
        datasource_id=table.datasource_id,
    )


@router.post(
    "/descriptions/withdrawals",
    response_model=DescriptionWithdrawalRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_description_withdrawal(
    body: DescriptionWithdrawalCreate,
    context: SecurityContext = Depends(require_roles(*_WITHDRAW_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DescriptionWithdrawal:
    """Ask for a description to be retired, or a retired one brought back.

    202, not 201: nothing has happened to the description yet. It is still
    exactly what every reader resolves, until a different principal approves
    the review this creates.
    """
    await _authorize_subject(body, context, session, settings)
    if context.organization_id is None:
        # `enforce_organization` in `_authorize_subject` already narrowed this
        # for every real caller; the check keeps the type honest rather than
        # asserting past it.
        raise HTTPException(status_code=403, detail="ORGANIZATION_SCOPE_REQUIRED")
    withdrawal, review = await request_description_withdrawal(
        session,
        organization_id=context.organization_id,
        subject_type=body.subject_type,
        subject_id=body.subject_id,
        reason=body.reason,
        requested_by=context.principal_id,
        request_type=body.request_type,
    )
    record_audit(
        session,
        context,
        action="description.withdrawal.request",
        resource_type="description_withdrawal",
        resource_id=str(withdrawal.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "request_type": withdrawal.request_type,
            "subject_type": withdrawal.subject_type,
            "subject_id": withdrawal.subject_id,
            "version_id": str(withdrawal.version_id),
            "review_id": str(review.id),
        },
    )
    record_outbox(
        session,
        organization_id=withdrawal.organization_id,
        aggregate_type="description_withdrawal",
        aggregate_id=str(withdrawal.id),
        event_type="description.withdrawal.requested.v1",
        payload={
            "withdrawal_id": str(withdrawal.id),
            "request_type": withdrawal.request_type,
            "subject_type": withdrawal.subject_type,
            "subject_id": withdrawal.subject_id,
            "review_id": str(review.id),
        },
    )
    await session.commit()
    return withdrawal


@router.get("/descriptions/withdrawals", response_model=Page)
async def list_description_withdrawals(
    subject_id: UUID | None = Query(default=None),
    withdrawal_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_WITHDRAW_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """Withdrawals in this organization, newest first.

    Includes rejected and superseded ones: a steward asking "why is this still
    described?" is best answered by the request that was turned down, not by
    silence.
    """
    filters = [DescriptionWithdrawal.organization_id == context.organization_id]
    if subject_id is not None:
        filters.append(DescriptionWithdrawal.subject_id == str(subject_id))
    if withdrawal_status:
        filters.append(DescriptionWithdrawal.status == withdrawal_status)
    total = await session.scalar(
        select(func.count()).select_from(DescriptionWithdrawal).where(*filters)
    )
    rows = (
        (
            await session.execute(
                select(DescriptionWithdrawal)
                .where(*filters)
                .order_by(DescriptionWithdrawal.created_at.desc(), DescriptionWithdrawal.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return Page(
        items=[DescriptionWithdrawalRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )
