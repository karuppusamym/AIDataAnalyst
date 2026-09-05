from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.custom_quality_rules import evaluate_rule_pack
from aida.data_quality import DEFAULT_POLICY
from aida.db import get_session
from aida.dq_triage_agent import suggest_triage
from aida.events import record_audit, record_outbox
from aida.external_quality_signals import ingest_external_signal
from aida.freshness import WatermarkConfig, evaluate_freshness
from aida.models import (
    AnalysisRun,
    DataQualityIncident,
    DataQualityObservation,
    DataQualityPolicy,
    DataSource,
    ExternalQualitySignal,
    FreshnessObservation,
    FreshnessWatermarkConfig,
    MetadataColumn,
    MetadataTable,
    QualityRule,
    QualityRulePack,
)
from aida.quality_service import evaluate_analysis_run
from aida.schemas import (
    ApiModel,
    DataQualityIncidentRead,
    DataQualityIncidentTransition,
    DataQualityObservationRead,
    DataQualityPolicyRead,
    DataQualityPolicyUpsert,
    DataQualitySummaryRead,
    ExternalQualitySignalIngest,
    ExternalQualitySignalIngestResult,
    ExternalQualitySignalRead,
    FreshnessConfigRead,
    FreshnessConfigUpsert,
    FreshnessStatusRead,
    Page,
    QualityRulePackRead,
    QualityRulePackUpsert,
    QualityRuleRead,
    QualityRuleUpsert,
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


class DataQualityIncidentTriageRead(ApiModel):
    incident_id: UUID
    anomaly_type: str
    likely_causes: list[str]
    recommended_next_steps: list[str]
    #: The evidence field name(s) each cause/step was derived from, so a
    #: steward can check this against the incident's own `evidence` blob
    #: rather than trust an unattributed sentence.
    basis: list[str]


@router.get(
    "/quality-incidents/{incident_id}/triage",
    response_model=DataQualityIncidentTriageRead,
)
async def get_quality_incident_triage(
    incident_id: UUID,
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "DataSteward", "Operations", "Viewer", "Analyst"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> DataQualityIncidentTriageRead:
    """A deterministic root-cause hint for one incident (`dq_triage_agent`).

    Read-only and computed fresh on every call -- nothing about an incident
    is mutated or persisted by this endpoint (INV-7's read-route gate holds
    trivially here since there is no write at all). See
    `dq_triage_agent`'s own module docstring for why this stays computed
    rather than stored.
    """
    incident = await session.get(DataQualityIncident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="quality incident not found")
    enforce_organization(context, incident.organization_id)
    suggestion = suggest_triage(
        anomaly_type=incident.anomaly_type,
        source=incident.source,
        evidence=dict(incident.evidence or {}),
        occurrence_count=incident.occurrence_count,
    )
    return DataQualityIncidentTriageRead(
        incident_id=incident.id,
        anomaly_type=suggestion.anomaly_type,
        likely_causes=list(suggestion.likely_causes),
        recommended_next_steps=list(suggestion.recommended_next_steps),
        basis=list(suggestion.basis),
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


# --- DQ-2: Freshness Watermark Contracts ----------------------------------------


@router.put(
    "/datasources/{datasource_id}/freshness-config/{table_id}",
    response_model=FreshnessConfigRead,
)
async def upsert_freshness_config(
    datasource_id: UUID,
    table_id: UUID,
    body: FreshnessConfigUpsert,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> FreshnessConfigRead:
    source = await _source(session, context, datasource_id)
    table = await session.get(MetadataTable, table_id)
    if table is None or table.datasource_id != source.id:
        raise HTTPException(status_code=422, detail="table is not part of this datasource")

    config = await session.scalar(
        select(FreshnessWatermarkConfig).where(
            FreshnessWatermarkConfig.table_id == table_id,
        )
    )
    action = (
        "data_quality.freshness_config.update"
        if config
        else "data_quality.freshness_config.create"
    )
    if config is None:
        config = FreshnessWatermarkConfig(
            organization_id=source.organization_id,
            datasource_id=source.id,
            table_id=table_id,
            watermark_column=body.watermark_column,
            classification=body.classification,
            threshold_minutes=body.threshold_minutes,
            retention_days=body.retention_days,
            created_by=context.principal_id,
        )
        session.add(config)
    else:
        config.watermark_column = body.watermark_column
        config.classification = body.classification
        config.threshold_minutes = body.threshold_minutes
        config.retention_days = body.retention_days
        # Reset approval on update (maker-checker)
        config.status = "PENDING_APPROVAL"
        config.approved_by = None
        config.approved_at = None
    await session.flush()
    record_audit(
        session,
        context,
        action=action,
        resource_type="freshness_watermark_config",
        resource_id=str(config.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "datasource_id": str(source.id),
            "table_id": str(table_id),
            "watermark_column": body.watermark_column,
        },
    )
    record_outbox(
        session,
        organization_id=source.organization_id,
        aggregate_type="freshness_watermark_config",
        aggregate_id=str(config.id),
        event_type="data_quality.freshness_config.changed.v1",
        payload={
            "datasource_id": str(source.id),
            "table_id": str(table_id),
            "watermark_column": body.watermark_column,
        },
    )
    await session.commit()
    await session.refresh(config)
    return FreshnessConfigRead.model_validate(config)


# --- GROUP C (DQ-2): maker-checker approval ------------------------------------
#
# `upsert_freshness_config` always leaves a config in PENDING_APPROVAL (or
# resets it there on update) and `evaluate_freshness` refuses to activate
# freshness for anything but ACTIVE -- by design, per this module's own
# docstring ("Configuration requires maker-checker approval"). Until now,
# nothing in the platform ever moved a config out of PENDING_APPROVAL: no
# endpoint set `status`/`approved_by`/`approved_at`, so every configured
# table was permanently stuck reporting AWAITING_APPROVAL, never FRESH/STALE.
# This is the missing checker step.
@router.post(
    "/datasources/{datasource_id}/freshness-config/{table_id}/approve",
    response_model=FreshnessConfigRead,
)
async def approve_freshness_config(
    datasource_id: UUID,
    table_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> FreshnessConfigRead:
    source = await _source(session, context, datasource_id)
    config = await session.scalar(
        select(FreshnessWatermarkConfig).where(
            FreshnessWatermarkConfig.datasource_id == datasource_id,
            FreshnessWatermarkConfig.table_id == table_id,
        )
    )
    if config is None:
        raise HTTPException(status_code=404, detail="freshness configuration not found")
    if config.status != "PENDING_APPROVAL":
        raise HTTPException(
            status_code=409,
            detail=f"freshness configuration is {config.status}, not PENDING_APPROVAL",
        )
    # Maker-checker: the principal approving cannot be the one who last
    # created/edited the configuration -- same self-approval-by-proxy guard
    # PG-4's delegated governance decisions enforce.
    if config.created_by == context.principal_id:
        raise HTTPException(
            status_code=403,
            detail="the configuration's own author cannot approve it",
        )

    config.status = "ACTIVE"
    config.approved_by = context.principal_id
    config.approved_at = datetime.now(UTC)
    await session.flush()
    record_audit(
        session,
        context,
        action="data_quality.freshness_config.approve",
        resource_type="freshness_watermark_config",
        resource_id=str(config.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "datasource_id": str(source.id),
            "table_id": str(table_id),
            "created_by": config.created_by,
        },
    )
    record_outbox(
        session,
        organization_id=source.organization_id,
        aggregate_type="freshness_watermark_config",
        aggregate_id=str(config.id),
        event_type="data_quality.freshness_config.approved.v1",
        payload={"datasource_id": str(source.id), "table_id": str(table_id)},
    )
    await session.commit()
    await session.refresh(config)
    return FreshnessConfigRead.model_validate(config)


@router.get(
    "/datasources/{datasource_id}/freshness",
    response_model=Page,
)
async def list_freshness_configs(
    datasource_id: UUID,
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
    filters = [FreshnessWatermarkConfig.datasource_id == datasource_id]
    total = await session.scalar(
        select(func.count()).select_from(FreshnessWatermarkConfig).where(*filters)
    )
    rows = (
        await session.scalars(
            select(FreshnessWatermarkConfig)
            .where(*filters)
            .order_by(FreshnessWatermarkConfig.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[FreshnessConfigRead.model_validate(r) for r in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/datasources/{datasource_id}/freshness/{table_id}",
    response_model=FreshnessStatusRead,
)
async def get_freshness_status(
    datasource_id: UUID,
    table_id: UUID,
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "DataSteward", "Operations", "Viewer", "Analyst"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> FreshnessStatusRead:
    """Evaluate freshness for a specific table.

    CRITICAL invariant (ADR-0016): scan age is NEVER presented as freshness.
    This endpoint only uses the actual data watermark timestamp.
    """
    await _source(session, context, datasource_id)

    config_row = await session.scalar(
        select(FreshnessWatermarkConfig).where(
            FreshnessWatermarkConfig.datasource_id == datasource_id,
            FreshnessWatermarkConfig.table_id == table_id,
        )
    )

    if config_row is None:
        wm_config = None
    else:
        wm_config = WatermarkConfig(
            table_id=str(config_row.table_id),
            watermark_column=config_row.watermark_column,
            classification=config_row.classification,
            threshold_minutes=config_row.threshold_minutes,
            retention_days=config_row.retention_days,
            approved_by=config_row.approved_by,
            approved_at=config_row.approved_at,
            status=config_row.status,
        )

    latest_observation = await session.scalar(
        select(FreshnessObservation)
        .where(
            FreshnessObservation.table_id == table_id,
            FreshnessObservation.datasource_id == datasource_id,
        )
        .order_by(FreshnessObservation.observed_at.desc())
        .limit(1)
    )

    latest_watermark = (
        latest_observation.watermark_value if latest_observation else None
    )
    now = datetime.now(UTC)
    result = evaluate_freshness(wm_config, latest_watermark, evaluation_time=now)

    return FreshnessStatusRead(
        table_id=table_id,
        status=result.status,
        last_watermark=result.last_watermark,
        age_minutes=result.age_minutes,
        threshold_minutes=result.threshold_minutes,
        evidence=result.evidence,
    )


# --- DQ-4: Custom quality rule packs --------------------------------------------


async def _rule_pack(
    session: AsyncSession, context: SecurityContext, rule_pack_id: UUID
) -> QualityRulePack:
    rule_pack = await session.get(QualityRulePack, rule_pack_id)
    if rule_pack is None:
        raise HTTPException(status_code=404, detail="quality rule pack not found")
    enforce_organization(context, rule_pack.organization_id)
    return rule_pack


@router.post(
    "/datasources/{datasource_id}/quality-rule-packs",
    response_model=QualityRulePackRead,
    status_code=201,
)
async def create_rule_pack(
    datasource_id: UUID,
    body: QualityRulePackUpsert,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> QualityRulePackRead:
    source = await _source(session, context, datasource_id)
    rule_pack = QualityRulePack(
        organization_id=source.organization_id,
        datasource_id=source.id,
        created_by=context.principal_id,
        **body.model_dump(),
    )
    session.add(rule_pack)
    await session.flush()
    record_audit(
        session,
        context,
        action="data_quality.rule_pack.create",
        resource_type="quality_rule_pack",
        resource_id=str(rule_pack.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"datasource_id": str(source.id), "name": rule_pack.name},
    )
    record_outbox(
        session,
        organization_id=source.organization_id,
        aggregate_type="quality_rule_pack",
        aggregate_id=str(rule_pack.id),
        event_type="data_quality.rule_pack.created.v1",
        payload={"datasource_id": str(source.id), "name": rule_pack.name},
    )
    await session.commit()
    await session.refresh(rule_pack)
    return QualityRulePackRead.model_validate(rule_pack)


@router.get("/datasources/{datasource_id}/quality-rule-packs", response_model=Page)
async def list_rule_packs(
    datasource_id: UUID,
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
    filters = [QualityRulePack.datasource_id == datasource_id]
    total = await session.scalar(select(func.count()).select_from(QualityRulePack).where(*filters))
    rows = (
        await session.scalars(
            select(QualityRulePack).where(*filters).order_by(QualityRulePack.name).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[QualityRulePackRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.delete("/quality-rule-packs/{rule_pack_id}", status_code=204)
async def delete_rule_pack(
    rule_pack_id: UUID,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataAdmin")),
    session: AsyncSession = Depends(get_session),
) -> None:
    rule_pack = await _rule_pack(session, context, rule_pack_id)
    await session.delete(rule_pack)
    record_audit(
        session,
        context,
        action="data_quality.rule_pack.delete",
        resource_type="quality_rule_pack",
        resource_id=str(rule_pack_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"name": rule_pack.name},
    )
    await session.commit()


@router.post(
    "/quality-rule-packs/{rule_pack_id}/rules",
    response_model=QualityRuleRead,
    status_code=201,
)
async def create_rule(
    rule_pack_id: UUID,
    body: QualityRuleUpsert,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> QualityRuleRead:
    rule_pack = await _rule_pack(session, context, rule_pack_id)
    table = await session.get(MetadataTable, body.table_id)
    if table is None or table.datasource_id != rule_pack.datasource_id:
        raise HTTPException(
            status_code=422, detail="table is not part of this rule pack's datasource"
        )
    if body.rule_type == "COLUMN_NULL_RATE_MAX":
        if body.column_id is None:
            raise HTTPException(
                status_code=422, detail="column_id is required for COLUMN_NULL_RATE_MAX"
            )
        column = await session.get(MetadataColumn, body.column_id)
        if column is None or column.table_id != body.table_id:
            raise HTTPException(status_code=422, detail="column is not part of the given table")
    elif body.column_id is not None:
        raise HTTPException(
            status_code=422, detail=f"column_id is not applicable to {body.rule_type}"
        )
    rule = QualityRule(
        organization_id=rule_pack.organization_id,
        rule_pack_id=rule_pack.id,
        created_by=context.principal_id,
        **body.model_dump(),
    )
    session.add(rule)
    await session.flush()
    record_audit(
        session,
        context,
        action="data_quality.rule.create",
        resource_type="quality_rule",
        resource_id=str(rule.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"rule_pack_id": str(rule_pack.id), "rule_type": rule.rule_type},
    )
    await session.commit()
    await session.refresh(rule)
    return QualityRuleRead.model_validate(rule)


@router.get("/quality-rule-packs/{rule_pack_id}/rules", response_model=Page)
async def list_rules(
    rule_pack_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "DataSteward", "Operations", "Viewer", "Analyst"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    await _rule_pack(session, context, rule_pack_id)
    filters = [QualityRule.rule_pack_id == rule_pack_id]
    total = await session.scalar(select(func.count()).select_from(QualityRule).where(*filters))
    rows = (
        await session.scalars(
            select(QualityRule).where(*filters).order_by(QualityRule.name).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[QualityRuleRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.delete("/quality-rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> None:
    rule = await session.get(QualityRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="quality rule not found")
    enforce_organization(context, rule.organization_id)
    await session.delete(rule)
    record_audit(
        session,
        context,
        action="data_quality.rule.delete",
        resource_type="quality_rule",
        resource_id=str(rule_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"rule_pack_id": str(rule.rule_pack_id)},
    )
    await session.commit()


@router.post("/quality-rule-packs/{rule_pack_id}/evaluate")
async def evaluate_rule_pack_now(
    rule_pack_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> dict[str, int | str]:
    """On-demand evaluation, independent of the pack's own scheduled cadence --
    same relationship ``replay_quality_evaluation`` has to the profiling-scan
    trigger above."""
    rule_pack = await _rule_pack(session, context, rule_pack_id)
    rules = (
        await session.scalars(select(QualityRule).where(QualityRule.rule_pack_id == rule_pack.id))
    ).all()
    counts = await evaluate_rule_pack(
        session, rule_pack=rule_pack, rules=list(rules), context=context
    )
    await session.commit()
    return {"rule_pack_id": str(rule_pack.id), **counts}


# --- DQ-8: open framework for third-party detector signals ----------------------


@router.post(
    "/datasources/{datasource_id}/quality/external-signals",
    response_model=ExternalQualitySignalIngestResult,
    status_code=201,
)
async def ingest_external_quality_signal(
    datasource_id: UUID,
    body: ExternalQualitySignalIngest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> ExternalQualitySignalIngestResult:
    """Ingest a normalized quality signal from a third-party detector
    (Monte Carlo, Anomalo, ...) and reconcile it into the durable incident
    lifecycle, kept distinguishable from internally-computed signals
    (``source="EXTERNAL"``). Idempotent on (vendor, native id, observed_at)."""
    source = await _source(session, context, datasource_id)
    table = await session.get(MetadataTable, body.table_id)
    if table is None or table.datasource_id != source.id:
        raise HTTPException(status_code=422, detail="table is not part of this datasource")
    if body.column_id is not None:
        column = await session.get(MetadataColumn, body.column_id)
        if column is None or column.table_id != body.table_id:
            raise HTTPException(status_code=422, detail="column is not part of the given table")
    outcome = await ingest_external_signal(
        session,
        organization_id=source.organization_id,
        datasource_id=source.id,
        envelope=body,
        context=context,
    )
    await session.commit()
    await session.refresh(outcome.signal)
    return ExternalQualitySignalIngestResult(
        signal=ExternalQualitySignalRead.model_validate(outcome.signal),
        deduplicated=outcome.deduplicated,
        incident_opened=outcome.incident_opened,
        incident_resolved=outcome.incident_resolved,
    )


@router.get(
    "/datasources/{datasource_id}/quality/external-signals",
    response_model=Page,
)
async def list_external_quality_signals(
    datasource_id: UUID,
    detector_vendor: str | None = Query(default=None, max_length=50),
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
    filters = [ExternalQualitySignal.datasource_id == datasource_id]
    if detector_vendor:
        normalized_vendor = "_".join(detector_vendor.strip().upper().split())
        filters.append(ExternalQualitySignal.detector_vendor == normalized_vendor)
    if table_id:
        filters.append(ExternalQualitySignal.table_id == table_id)
    total = await session.scalar(
        select(func.count()).select_from(ExternalQualitySignal).where(*filters)
    )
    rows = (
        await session.scalars(
            select(ExternalQualitySignal)
            .where(*filters)
            .order_by(ExternalQualitySignal.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[ExternalQualitySignalRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )
