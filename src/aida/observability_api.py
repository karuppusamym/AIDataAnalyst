"""Observability API (OB-1 through OB-4).

SLO definitions CRUD, error budget consumption, and audit archive
status endpoints.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import AuditArchiveRecord, SloDefinition, SloMeasurement
from aida.schemas import (
    ArchiveStatusRead,
    Page,
    SloBudgetRead,
    SloDefinitionCreate,
    SloDefinitionRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["observability"])


@router.post("/observability/slo", response_model=SloDefinitionRead, status_code=201)
async def create_slo_definition(
    body: SloDefinitionCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> SloDefinitionRead:
    org_id = context.require_organization()

    existing = await session.scalar(
        select(SloDefinition).where(
            SloDefinition.organization_id == org_id,
            SloDefinition.slo_key == body.slo_key,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="slo_key already exists")

    slo = SloDefinition(
        organization_id=org_id,
        slo_key=body.slo_key,
        name=body.name,
        target=body.target,
        window_days=body.window_days,
        threshold=body.threshold,
        created_by=context.principal_id,
    )
    session.add(slo)
    await session.flush()
    record_audit(
        session,
        context,
        action="observability.slo.create",
        resource_type="slo_definition",
        resource_id=str(slo.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"slo_key": body.slo_key, "target": body.target},
    )
    record_outbox(
        session,
        organization_id=org_id,
        aggregate_type="slo_definition",
        aggregate_id=str(slo.id),
        event_type="observability.slo.created.v1",
        payload={"slo_key": body.slo_key, "target": body.target},
    )
    await session.commit()
    await session.refresh(slo)
    return SloDefinitionRead.model_validate(slo)


@router.get("/observability/slo", response_model=Page)
async def list_slo_definitions(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    org_id = context.require_organization()
    filters = [SloDefinition.organization_id == org_id]
    total = await session.scalar(
        select(func.count()).select_from(SloDefinition).where(*filters)
    )
    rows = (
        await session.scalars(
            select(SloDefinition)
            .where(*filters)
            .order_by(SloDefinition.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[SloDefinitionRead.model_validate(r) for r in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/observability/slo/{slo_id}/budget", response_model=SloBudgetRead)
async def get_slo_budget(
    slo_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> SloBudgetRead:
    slo = await session.get(SloDefinition, slo_id)
    if slo is None:
        raise HTTPException(status_code=404, detail="slo definition not found")
    enforce_organization(context, slo.organization_id)

    latest_measurement = await session.scalar(
        select(SloMeasurement)
        .where(SloMeasurement.slo_id == slo.id)
        .order_by(SloMeasurement.measured_at.desc())
        .limit(1)
    )

    current_value = latest_measurement.value if latest_measurement else None
    budget_remaining = latest_measurement.budget_remaining if latest_measurement else None

    if current_value is not None and current_value >= slo.target:
        status = "HEALTHY"
    elif current_value is not None and current_value >= slo.threshold:
        status = "AT_RISK"
    elif current_value is not None:
        status = "BREACHED"
    else:
        status = "NO_DATA"

    return SloBudgetRead(
        slo_id=slo.id,
        slo_key=slo.slo_key,
        name=slo.name,
        target=slo.target,
        current_value=current_value,
        budget_remaining=budget_remaining,
        window_days=slo.window_days,
        status=status,
    )


@router.get("/observability/archive/status", response_model=ArchiveStatusRead)
async def get_archive_status(
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> ArchiveStatusRead:
    org_id = context.require_organization()
    filters = [AuditArchiveRecord.organization_id == org_id]

    stats = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(AuditArchiveRecord.event_count), 0),
                func.count().filter(AuditArchiveRecord.legal_hold.is_(True)),
            ).where(*filters)
        )
    ).one()

    latest = await session.scalar(
        select(AuditArchiveRecord)
        .where(*filters)
        .order_by(AuditArchiveRecord.created_at.desc())
        .limit(1)
    )

    total_archives = stats[0] or 0
    total_events_archived = int(stats[1])
    legal_hold_count = stats[2] or 0

    if total_archives == 0:
        status = "NO_ARCHIVES"
    elif legal_hold_count > 0:
        status = "LEGAL_HOLD_ACTIVE"
    else:
        status = "HEALTHY"

    return ArchiveStatusRead(
        total_archives=total_archives,
        total_events_archived=total_events_archived,
        latest_archive_id=latest.archive_id if latest else None,
        latest_checksum=latest.checksum if latest else None,
        legal_hold_count=legal_hold_count,
        status=status,
    )
