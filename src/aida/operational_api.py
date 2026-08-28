from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.models import (
    AnalysisRun,
    AuditEvent,
    DataSource,
    Organization,
    OutboxEvent,
    Project,
    ScanPolicy,
)
from aida.schemas import (
    AnalysisRunRead,
    AuditEventRead,
    DataSourceSummaryRead,
    FleetSummaryRead,
    OutboxEventRead,
    Page,
    ProjectRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["operations"])


async def _require_organization(
    session: AsyncSession, context: SecurityContext, organization_id: UUID
) -> Organization:
    enforce_organization(context, organization_id)
    organization = await session.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return organization


@router.get("/organizations/{organization_id}/projects", response_model=Page)
async def list_organization_projects(
    organization_id: UUID,
    line_of_business_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "OrganizationAdmin",
            "ProjectAdmin",
            "DataAdmin",
            "Operations",
            "Viewer",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """List a tenant's projects without an N+1 traversal through every LOB."""
    await _require_organization(session, context, organization_id)
    filters = [Project.organization_id == organization_id]
    if line_of_business_id:
        filters.append(Project.line_of_business_id == line_of_business_id)
    total = await session.scalar(select(func.count()).select_from(Project).where(*filters))
    rows = (
        await session.scalars(
            select(Project)
            .where(*filters)
            .order_by(Project.name, Project.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[ProjectRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/organizations/{organization_id}/datasources", response_model=Page)
async def list_organization_datasources(
    organization_id: UUID,
    project_id: UUID | None = None,
    datasource_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "OrganizationAdmin",
            "ProjectAdmin",
            "MetadataAdmin",
            "DataAdmin",
            "Operations",
            "Analyst",
            "Viewer",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """List source summaries directly at tenant scope for large fleet consoles."""
    await _require_organization(session, context, organization_id)
    filters = [DataSource.organization_id == organization_id]
    if project_id:
        filters.append(DataSource.project_id == project_id)
    if datasource_status:
        filters.append(DataSource.status == datasource_status.upper())
    total = await session.scalar(select(func.count()).select_from(DataSource).where(*filters))
    rows = (
        await session.scalars(
            select(DataSource)
            .where(*filters)
            .order_by(DataSource.name, DataSource.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[DataSourceSummaryRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/organizations/{organization_id}/analysis-runs", response_model=Page)
async def list_organization_analysis_runs(
    organization_id: UUID,
    run_status: str | None = Query(default=None, max_length=30),
    datasource_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "OrganizationAdmin", "MetadataAdmin", "DataAdmin", "Operations"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    await _require_organization(session, context, organization_id)
    filters = [AnalysisRun.organization_id == organization_id]
    if run_status:
        filters.append(AnalysisRun.status == run_status.upper())
    if datasource_id:
        filters.append(AnalysisRun.datasource_id == datasource_id)
    total = await session.scalar(select(func.count()).select_from(AnalysisRun).where(*filters))
    rows = (
        await session.scalars(
            select(AnalysisRun)
            .where(*filters)
            .order_by(AnalysisRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[AnalysisRunRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/organizations/{organization_id}/audit-events", response_model=Page)
async def list_audit_events(
    organization_id: UUID,
    action: str | None = Query(default=None, max_length=150),
    resource_type: str | None = Query(default=None, max_length=100),
    correlation_id: str | None = Query(default=None, max_length=100),
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "Auditor", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    await _require_organization(session, context, organization_id)
    if since is not None and since.tzinfo is None:
        raise HTTPException(status_code=422, detail="since must include a timezone")
    if until is not None and until.tzinfo is None:
        raise HTTPException(status_code=422, detail="until must include a timezone")
    if since is not None and until is not None and since > until:
        raise HTTPException(status_code=422, detail="since cannot be later than until")
    filters = [AuditEvent.organization_id == organization_id]
    if action:
        filters.append(AuditEvent.action == action)
    if resource_type:
        filters.append(AuditEvent.resource_type == resource_type)
    if correlation_id:
        filters.append(AuditEvent.correlation_id == correlation_id)
    if since:
        filters.append(AuditEvent.occurred_at >= since)
    if until:
        filters.append(AuditEvent.occurred_at <= until)
    total = await session.scalar(select(func.count()).select_from(AuditEvent).where(*filters))
    rows = (
        await session.scalars(
            select(AuditEvent)
            .where(*filters)
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[AuditEventRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


async def _status_counts(
    session: AsyncSession, model: type[DataSource] | type[AnalysisRun], organization_id: UUID
) -> dict[str, int]:
    rows = (
        await session.execute(
            select(model.status, func.count())
            .where(model.organization_id == organization_id)
            .group_by(model.status)
        )
    ).all()
    return {str(status): int(count) for status, count in rows}


@router.get("/organizations/{organization_id}/fleet-summary", response_model=FleetSummaryRead)
async def fleet_summary(
    organization_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "Auditor", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> FleetSummaryRead:
    await _require_organization(session, context, organization_id)
    now = datetime.now(UTC)
    policies_enabled = await session.scalar(
        select(func.count())
        .select_from(ScanPolicy)
        .where(
            ScanPolicy.organization_id == organization_id,
            ScanPolicy.enabled.is_(True),
        )
    )
    policies_due = await session.scalar(
        select(func.count())
        .select_from(ScanPolicy)
        .where(
            ScanPolicy.organization_id == organization_id,
            ScanPolicy.enabled.is_(True),
            ScanPolicy.next_run_at <= now,
        )
    )
    pending_outbox = await session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(
            OutboxEvent.organization_id == organization_id,
            OutboxEvent.status == "PENDING",
        )
    )
    dead_letter_outbox = await session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(
            OutboxEvent.organization_id == organization_id,
            OutboxEvent.status == "DEAD_LETTER",
        )
    )
    return FleetSummaryRead(
        organization_id=organization_id,
        datasource_statuses=await _status_counts(session, DataSource, organization_id),
        analysis_run_statuses=await _status_counts(session, AnalysisRun, organization_id),
        scan_policies_enabled=policies_enabled or 0,
        scan_policies_due=policies_due or 0,
        pending_outbox_events=pending_outbox or 0,
        dead_letter_outbox_events=dead_letter_outbox or 0,
        generated_at=now,
    )


@router.get("/organizations/{organization_id}/outbox-events", response_model=Page)
async def list_outbox_events(
    organization_id: UUID,
    event_status: str | None = Query(default=None, alias="status", max_length=30),
    event_type: str | None = Query(default=None, max_length=150),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "Auditor", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """Return bounded, tenant-scoped delivery evidence without exposing event payloads."""
    await _require_organization(session, context, organization_id)
    filters = [OutboxEvent.organization_id == organization_id]
    if event_status:
        filters.append(OutboxEvent.status == event_status.upper())
    if event_type:
        filters.append(OutboxEvent.event_type == event_type)
    total = await session.scalar(select(func.count()).select_from(OutboxEvent).where(*filters))
    rows = (
        await session.scalars(
            select(OutboxEvent)
            .where(*filters)
            .order_by(OutboxEvent.occurred_at.desc(), OutboxEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[OutboxEventRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post("/outbox-events/{event_id}/requeue", response_model=OutboxEventRead)
async def requeue_outbox_event(
    event_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> OutboxEvent:
    event = await session.get(OutboxEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="outbox event not found")
    if event.organization_id is None:
        if "PlatformAdmin" not in context.roles:
            raise HTTPException(status_code=403, detail="platform event access denied")
    else:
        enforce_organization(context, event.organization_id)
    if event.status != "DEAD_LETTER":
        raise HTTPException(status_code=409, detail="only dead-letter events can be requeued")
    event.status = "PENDING"
    event.attempt_count = 0
    event.next_attempt_at = datetime.now(UTC)
    event.last_error = None
    record_audit(
        session,
        replace(context, organization_id=event.organization_id),
        action="outbox_event.requeue",
        resource_type="outbox_event",
        resource_id=str(event.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    await session.commit()
    return event
