from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.connector_health import ConnectorHealthScore
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.fleet import datasource_health, fleet_health, tool_first_execution_rate
from aida.models import (
    AnalysisRun,
    AuditEvent,
    DataDomain,
    DataSource,
    Organization,
    OutboxEvent,
    Project,
    ScanPolicy,
)
from aida.schemas import (
    AnalysisRunRead,
    AuditEventRead,
    DataDomainRead,
    DataSourceSummaryRead,
    FleetSummaryRead,
    OutboxEventRead,
    Page,
    ProjectRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.tool_first_rate import DEFAULT_WINDOW_DAYS, ToolFirstRate

router = APIRouter(prefix="/v1", tags=["operations"])


# --- CN-7: per-connector health scoring ---------------------------------------
#
# Local response models, not `aida.schemas` -- ST-05/ST-06 (see
# `Docs/40-engineering/06-refactor-plan.md`) is actively splitting that file on
# this same branch, and `aida.policy_native_sync_api` / `aida.sql_validation_api`
# already establish the pattern of a locally-scoped `ApiModel` for new API
# surface that shouldn't contend with that split.
class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class ConnectorHealthFactorRead(ApiModel):
    name: str
    score: float
    maximum: float
    reason: str
    evidence: dict[str, Any]


class ConnectorHealthScoreRead(ApiModel):
    datasource_id: UUID
    score: int
    status: str
    factors: list[ConnectorHealthFactorRead]
    blockers: list[str]
    computed_at: datetime

    @classmethod
    def from_score(cls, score: ConnectorHealthScore) -> "ConnectorHealthScoreRead":
        return cls(
            datasource_id=score.datasource_id,
            score=score.score,
            status=score.status,
            factors=[
                ConnectorHealthFactorRead(
                    name=factor.name,
                    score=factor.score,
                    maximum=factor.maximum,
                    reason=factor.reason,
                    evidence=factor.evidence,
                )
                for factor in score.factors
            ],
            blockers=score.blockers,
            computed_at=score.computed_at,
        )


# --- TL-6: tool-first execution rate ------------------------------------------
#
# Same locally-scoped `ApiModel` rationale as CN-7 above.
class ToolFirstRateRead(ApiModel):
    organization_id: UUID
    window_days: int
    tool_first_executions: int
    freeform_executions: int
    total_executions: int
    rate: float | None
    by_source: dict[str, int]
    target_rate: float
    meets_target: bool | None
    computed_at: datetime

    @classmethod
    def from_rate(cls, rate: ToolFirstRate) -> "ToolFirstRateRead":
        return cls(
            organization_id=rate.organization_id,
            window_days=rate.window_days,
            tool_first_executions=rate.tool_first_executions,
            freeform_executions=rate.freeform_executions,
            total_executions=rate.total_executions,
            rate=rate.rate,
            by_source=rate.by_source,
            target_rate=rate.target_rate,
            meets_target=rate.meets_target,
            computed_at=rate.computed_at,
        )


async def _require_organization(
    session: AsyncSession, context: SecurityContext, organization_id: UUID
) -> Organization:
    enforce_organization(context, organization_id)
    organization = await session.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")
    return organization


@router.get("/organizations/{organization_id}/data-domains", response_model=Page)
async def list_organization_data_domains(
    organization_id: UUID,
    line_of_business_id: UUID | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
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
    """List a tenant's data domains without an N+1 traversal through every LOB --
    same pattern as list_organization_projects, feeding the workspace tree
    (Org -> LOB -> Domain -> Project -> Source) instead of a flat source picker.
    """
    await _require_organization(session, context, organization_id)
    filters = [DataDomain.organization_id == organization_id]
    if line_of_business_id:
        filters.append(DataDomain.line_of_business_id == line_of_business_id)
    total = await session.scalar(select(func.count()).select_from(DataDomain).where(*filters))
    rows = (
        await session.scalars(
            select(DataDomain)
            .where(*filters)
            .order_by(DataDomain.name, DataDomain.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[DataDomainRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


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


@router.get(
    "/datasources/{datasource_id}/health",
    response_model=ConnectorHealthScoreRead,
)
async def get_datasource_health(
    datasource_id: UUID,
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
) -> ConnectorHealthScoreRead:
    """CN-7: one connector's explainable health score.

    Derived entirely from existing `AnalysisRun`/`ScanPolicy` history --
    see `aida.connector_health` for the five scored factors and
    `aida.fleet.datasource_health` for how they're gathered.
    """
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    score = await datasource_health(session, datasource_id)
    if score is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    return ConnectorHealthScoreRead.from_score(score)


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


@router.get("/organizations/{organization_id}/fleet-health", response_model=Page)
async def organization_fleet_health(
    organization_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "Auditor", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """CN-7: per-connector health scores for every datasource in the fleet.

    The batch counterpart to `GET /v1/datasources/{id}/health` -- powers the
    fleet view's per-connector health column without one request per
    connector. Each item carries the same explainable factor breakdown.
    """
    await _require_organization(session, context, organization_id)
    scores = await fleet_health(session, organization_id)
    items = [ConnectorHealthScoreRead.from_score(score) for score in scores]
    return Page(items=items, limit=len(items), offset=0, total=len(items))


@router.get(
    "/organizations/{organization_id}/tool-first-rate",
    response_model=ToolFirstRateRead,
)
async def organization_tool_first_rate(
    organization_id: UUID,
    window_days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=1, le=365),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "Auditor", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> ToolFirstRateRead:
    """TL-6: what share of this organization's completed agent-run executions
    went through a certified governed tool ("tool-first") rather than ad-hoc
    generated SQL, over a rolling window.

    Derived entirely from existing `AgentRun.generation_source` history --
    see `aida.tool_first_rate` for the pure ratio computation and
    `aida.fleet.tool_first_execution_rate` for how it's gathered. The
    numerator (`tool_first_executions`), denominator (`total_executions`),
    and the full per-source breakdown (`by_source`) are always returned
    alongside the ratio so the number is never opaque.
    """
    await _require_organization(session, context, organization_id)
    rate = await tool_first_execution_rate(session, organization_id, window_days=window_days)
    return ToolFirstRateRead.from_rate(rate)


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
