from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.data_quality import DEFAULT_POLICY
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    AnalysisRun,
    DataQualityIncident,
    DataQualityObservation,
    DataQualityPolicy,
    DataSource,
    MetadataTable,
)
from aida.quality_service import evaluate_analysis_run
from aida.schemas import (
    DataQualityIncidentRead,
    DataQualityIncidentTransition,
    DataQualityObservationRead,
    DataQualityPolicyRead,
    DataQualityPolicyUpsert,
    DataQualitySummaryRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["data-quality"])


async def _source(
    session: AsyncSession, context: SecurityContext, datasource_id: UUID
) -> DataSource:
    source = await session.get(DataSource, datasource_id)
    if source is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, source.organization_id)
    return source


@router.put(
    "/datasources/{datasource_id}/quality-policies",
    response_model=DataQualityPolicyRead,
)
async def upsert_quality_policy(
    datasource_id: UUID,
    body: DataQualityPolicyUpsert,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> DataQualityPolicyRead:
    source = await _source(session, context, datasource_id)
    if body.table_id:
        table = await session.get(MetadataTable, body.table_id)
        if table is None or table.datasource_id != source.id:
            raise HTTPException(status_code=422, detail="table is not part of this datasource")
    scope_key = str(body.table_id) if body.table_id else "*"
    policy = await session.scalar(
        select(DataQualityPolicy).where(
            DataQualityPolicy.datasource_id == source.id,
            DataQualityPolicy.scope_key == scope_key,
        )
    )
    action = "data_quality.policy.update" if policy else "data_quality.policy.create"
    if policy is None:
        policy = DataQualityPolicy(
            organization_id=source.organization_id,
            datasource_id=source.id,
            table_id=body.table_id,
            scope_key=scope_key,
            created_by=context.principal_id,
            **body.model_dump(exclude={"table_id"}),
        )
        session.add(policy)
    else:
        for key, value in body.model_dump(exclude={"table_id"}).items():
            setattr(policy, key, value)
    await session.flush()
    record_audit(
        session,
        context,
        action=action,
        resource_type="data_quality_policy",
        resource_id=str(policy.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "datasource_id": str(source.id),
            "scope_key": scope_key,
            "enabled": policy.enabled,
        },
    )
    record_outbox(
        session,
        organization_id=source.organization_id,
        aggregate_type="data_quality_policy",
        aggregate_id=str(policy.id),
        event_type="data_quality.policy.changed.v1",
        payload={
            "datasource_id": str(source.id),
            "scope_key": scope_key,
            "enabled": policy.enabled,
        },
    )
    await session.commit()
    await session.refresh(policy)
    return DataQualityPolicyRead.model_validate(policy)


@router.get("/datasources/{datasource_id}/quality-policies", response_model=Page)
async def list_quality_policies(
    datasource_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Operations", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    await _source(session, context, datasource_id)
    filters = [DataQualityPolicy.datasource_id == datasource_id]
    total = await session.scalar(
        select(func.count()).select_from(DataQualityPolicy).where(*filters)
    )
    rows = (
        await session.scalars(
            select(DataQualityPolicy)
            .where(*filters)
            .order_by(DataQualityPolicy.scope_key)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[DataQualityPolicyRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post("/analysis-runs/{analysis_run_id}/quality-evaluation")
async def replay_quality_evaluation(
    analysis_run_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int | str]:
    run = await session.get(AnalysisRun, analysis_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="analysis run not found")
    enforce_organization(context, run.organization_id)
    if run.status != "COMPLETED":
        raise HTTPException(status_code=409, detail="only completed analysis runs can be evaluated")
    counts = await evaluate_analysis_run(
        session,
        analysis_run_id=run.id,
        organization_id=run.organization_id,
        datasource_id=run.datasource_id,
        context=context,
    )
    await session.commit()
    return {"analysis_run_id": str(run.id), **counts}


@router.get("/datasources/{datasource_id}/quality-observations", response_model=Page)
async def list_quality_observations(
    datasource_id: UUID,
    observation_status: str | None = Query(default=None, alias="status", max_length=30),
    table_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "DataSteward", "Operations", "Viewer", "Analyst"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    await _source(session, context, datasource_id)
    filters = [DataQualityObservation.datasource_id == datasource_id]
    if observation_status:
        filters.append(DataQualityObservation.status == observation_status.upper())
    if table_id:
        filters.append(DataQualityObservation.table_id == table_id)
    total = await session.scalar(
        select(func.count()).select_from(DataQualityObservation).where(*filters)
    )
    rows = (
        await session.execute(
            select(DataQualityObservation, MetadataTable.name)
            .join(MetadataTable, MetadataTable.id == DataQualityObservation.table_id)
            .where(*filters)
            .order_by(DataQualityObservation.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [
        DataQualityObservationRead.model_validate(
            {
                **{
                    column.name: getattr(observation, column.name)
                    for column in DataQualityObservation.__table__.columns
                },
                "table_name": name,
            }
        )
        for observation, name in rows
    ]
    return Page(items=items, limit=limit, offset=offset, total=total or 0)


@router.get("/datasources/{datasource_id}/quality-incidents", response_model=Page)
async def list_quality_incidents(
    datasource_id: UUID,
    incident_status: str | None = Query(default=None, alias="status", max_length=30),
    severity: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "DataSteward", "Operations", "Viewer", "Analyst"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    await _source(session, context, datasource_id)
    filters = [DataQualityIncident.datasource_id == datasource_id]
    if incident_status:
        filters.append(DataQualityIncident.status == incident_status.upper())
    if severity:
        filters.append(DataQualityIncident.severity == severity.upper())
    total = await session.scalar(
        select(func.count()).select_from(DataQualityIncident).where(*filters)
    )
    rows = (
        await session.execute(
            select(DataQualityIncident, MetadataTable.name)
            .join(MetadataTable, MetadataTable.id == DataQualityIncident.table_id)
            .where(*filters)
            .order_by(DataQualityIncident.last_observed_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [
        DataQualityIncidentRead.model_validate(
            {
                **{
                    column.name: getattr(incident, column.name)
                    for column in DataQualityIncident.__table__.columns
                },
                "table_name": name,
            }
        )
        for incident, name in rows
    ]
    return Page(items=items, limit=limit, offset=offset, total=total or 0)


@router.post("/quality-incidents/{incident_id}/transition", response_model=DataQualityIncidentRead)
async def transition_quality_incident(
    incident_id: UUID,
    body: DataQualityIncidentTransition,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> DataQualityIncidentRead:
    incident = await session.get(DataQualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="quality incident not found")
    enforce_organization(context, incident.organization_id)
    if incident.status == "RESOLVED":
        raise HTTPException(status_code=409, detail="resolved incidents cannot be transitioned")
    now = datetime.now(UTC)
    incident.status = body.status
    if body.status == "ACKNOWLEDGED":
        incident.acknowledged_by = context.principal_id
        incident.acknowledged_at = now
    else:
        incident.resolved_by = context.principal_id
        incident.resolved_at = now
        incident.resolution_reason = body.reason
    record_audit(
        session,
        context,
        action=f"data_quality.incident.{body.status.lower()}",
        resource_type="data_quality_incident",
        resource_id=str(incident.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"reason": body.reason, "anomaly_type": incident.anomaly_type},
    )
    record_outbox(
        session,
        organization_id=incident.organization_id,
        aggregate_type="data_quality_incident",
        aggregate_id=str(incident.id),
        event_type="data_quality.incident.transitioned.v1",
        payload={"status": body.status, "anomaly_type": incident.anomaly_type},
    )
    await session.commit()
    row = (
        await session.execute(
            select(DataQualityIncident, MetadataTable.name)
            .join(MetadataTable, MetadataTable.id == DataQualityIncident.table_id)
            .where(DataQualityIncident.id == incident.id)
        )
    ).one()
    return DataQualityIncidentRead.model_validate(
        {
            **{
                column.name: getattr(row[0], column.name)
                for column in DataQualityIncident.__table__.columns
            },
            "table_name": row[1],
        }
    )


@router.get("/datasources/{datasource_id}/quality-summary", response_model=DataQualitySummaryRead)
async def quality_summary(
    datasource_id: UUID,
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "DataSteward", "Operations", "Viewer", "Analyst"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> DataQualitySummaryRead:
    source = await _source(session, context, datasource_id)
    ranked = (
        select(
            DataQualityObservation.status.label("status"),
            DataQualityObservation.quality_score.label("score"),
            DataQualityObservation.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=DataQualityObservation.table_id,
                order_by=DataQualityObservation.created_at.desc(),
            )
            .label("position"),
        )
        .join(MetadataTable, MetadataTable.id == DataQualityObservation.table_id)
        .where(
            DataQualityObservation.datasource_id == datasource_id,
            MetadataTable.status == "ACTIVE",
            MetadataTable.object_type == "BASE_TABLE",
        )
        .subquery()
    )
    latest = (
        select(ranked.c.status, ranked.c.score, ranked.c.created_at)
        .where(ranked.c.position == 1)
        .subquery()
    )
    status_rows = (
        await session.execute(select(latest.c.status, func.count()).group_by(latest.c.status))
    ).all()
    summary_row = (
        await session.execute(
            select(func.count(), func.avg(latest.c.score), func.max(latest.c.created_at))
        )
    ).one()
    incident_row = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(DataQualityIncident.severity == "CRITICAL"),
            ).where(
                DataQualityIncident.datasource_id == datasource_id,
                DataQualityIncident.status.in_(("OPEN", "ACKNOWLEDGED")),
            )
        )
    ).one()
    table_count = await session.scalar(
        select(func.count())
        .select_from(MetadataTable)
        .where(
            MetadataTable.datasource_id == datasource_id,
            MetadataTable.status == "ACTIVE",
            MetadataTable.object_type == "BASE_TABLE",
        )
    )
    policy = await session.scalar(
        select(DataQualityPolicy).where(
            DataQualityPolicy.datasource_id == datasource_id,
            DataQualityPolicy.scope_key == "*",
            DataQualityPolicy.enabled.is_(True),
        )
    )
    last_observed = summary_row[2]
    age = (datetime.now(UTC) - last_observed).total_seconds() / 60 if last_observed else None
    max_age = (
        policy.metadata_scan_max_age_minutes
        if policy
        else int(DEFAULT_POLICY["metadata_scan_max_age_minutes"])
    )
    scan_status = "NOT_OBSERVED" if age is None else "STALE" if age > max_age else "CURRENT"
    return DataQualitySummaryRead(
        datasource_id=source.id,
        table_count=table_count or 0,
        observed_table_count=summary_row[0] or 0,
        status_counts={status: count for status, count in status_rows},
        open_incident_count=incident_row[0] or 0,
        critical_incident_count=incident_row[1] or 0,
        average_quality_score=round(float(summary_row[1]), 2)
        if summary_row[1] is not None
        else None,
        last_observed_at=last_observed,
        metadata_scan_age_minutes=round(age, 2) if age is not None else None,
        metadata_scan_status=scan_status,
        source_freshness_status="NOT_CONFIGURED",
    )
