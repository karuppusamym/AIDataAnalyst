from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from aida.config import Settings, get_settings
from aida.connectors.registry import connector_registry
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.ingestion import (
    CERTIFICATION_SUITE_VERSION,
    catalog_counts,
    chunk_fingerprint,
    connector_certification_evidence,
    connector_definition_payload,
    default_capabilities,
    envelope_counts,
    envelope_fingerprint,
    envelope_to_discovery,
)
from aida.models import (
    AnalysisRun,
    ConnectorCertificationRun,
    DataSource,
    MetadataCatalog,
    MetadataIngestionBatch,
    MetadataIngestionChunk,
    MetadataIngestionJob,
    MetadataTable,
)
from aida.schemas import (
    ConnectorCapabilityRead,
    ConnectorCertificationRead,
    MetadataIngestionBatchCreate,
    MetadataIngestionBatchRead,
    MetadataIngestionChunkCreate,
    MetadataIngestionChunkRead,
    MetadataIngestionCreate,
    MetadataIngestionRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.workflows.activities import persist_discovery_snapshot
from aida.workflows.ingestion import MetadataBatchIngestionWorkflow

router = APIRouter(prefix="/v1", tags=["enterprise ingestion"])


@router.get("/connectors/capability-matrix", response_model=list[ConnectorCapabilityRead])
async def connector_capability_matrix(
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Viewer", "Auditor")
    ),
) -> list[ConnectorCapabilityRead]:
    del context
    return [
        ConnectorCapabilityRead.model_validate(
            connector_definition_payload(definition, default_capabilities(definition))
        )
        for definition in connector_registry.definitions
    ]


@router.post(
    "/datasources/{datasource_id}/connector-certifications",
    response_model=ConnectorCertificationRead,
    status_code=status.HTTP_201_CREATED,
)
async def certify_datasource_connector(
    datasource_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> ConnectorCertificationRun:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    definition = connector_registry.definition(datasource.connector_type)
    active_catalogs = int(
        await session.scalar(
            select(func.count())
            .select_from(MetadataCatalog)
            .where(
                MetadataCatalog.datasource_id == datasource.id,
                MetadataCatalog.status == "ACTIVE",
            )
        )
        or 0
    )
    active_tables = int(
        await session.scalar(
            select(func.count())
            .select_from(MetadataTable)
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.status == "ACTIVE",
            )
        )
        or 0
    )
    certification_status, score, checks = connector_certification_evidence(
        datasource,
        definition,
        active_catalogs=active_catalogs,
        active_tables=active_tables,
    )
    certification = ConnectorCertificationRun(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        connector_type=datasource.connector_type,
        connector_version=definition.version,
        suite_version=CERTIFICATION_SUITE_VERSION,
        status=certification_status,
        score=score,
        checks=checks,
        initiated_by=context.principal_id,
        completed_at=datetime.now(UTC),
    )
    session.add(certification)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="connector.certify",
        resource_type="connector_certification_run",
        resource_id=str(certification.id),
        outcome="SUCCESS" if certification_status == "CERTIFIED" else certification_status,
        correlation_id=get_correlation_id(),
        details={
            "datasource_id": str(datasource.id),
            "connector_type": datasource.connector_type,
            "status": certification_status,
            "score": score,
        },
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="connector_certification_run",
        aggregate_id=str(certification.id),
        event_type="connector.certification.completed.v1",
        payload={
            "certification_id": str(certification.id),
            "datasource_id": str(datasource.id),
            "status": certification_status,
            "score": score,
        },
    )
    await session.commit()
    return certification


@router.get(
    "/datasources/{datasource_id}/connector-certifications",
    response_model=Page,
)
async def list_connector_certifications(
    datasource_id: UUID,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Viewer", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = (ConnectorCertificationRun.datasource_id == datasource.id,)
    total = int(
        await session.scalar(
            select(func.count()).select_from(ConnectorCertificationRun).where(*filters)
        )
        or 0
    )
    rows = (
        await session.scalars(
            select(ConnectorCertificationRun)
            .where(*filters)
            .order_by(ConnectorCertificationRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[ConnectorCertificationRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.post(
    "/datasources/{datasource_id}/metadata-ingestions",
    response_model=MetadataIngestionRead,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_metadata_envelope(
    datasource_id: UUID,
    body: MetadataIngestionCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "MetadataIngestor")
    ),
    session: AsyncSession = Depends(get_session),
) -> MetadataIngestionJob:
    if body.emitted_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="emitted_at must include a timezone")
    datasource = await session.scalar(
        select(DataSource).where(DataSource.id == datasource_id).with_for_update()
    )
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    if datasource.status == "DISABLED":
        raise HTTPException(status_code=409, detail="datasource is disabled")

    payload_fingerprint = envelope_fingerprint(body)
    existing = await session.scalar(
        select(MetadataIngestionJob).where(
            MetadataIngestionJob.datasource_id == datasource.id,
            MetadataIngestionJob.idempotency_key == body.idempotency_key,
        )
    )
    if existing is not None:
        if existing.payload_fingerprint != payload_fingerprint:
            raise HTTPException(
                status_code=409,
                detail="idempotency key was already used for a different metadata envelope",
            )
        return existing

    declared_counts = envelope_counts(body)
    run = AnalysisRun(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode=body.snapshot_type,
        trigger_type=body.transport,
        status="RUNNING",
        priority=50,
    )
    session.add(run)
    await session.flush()
    ingestion = MetadataIngestionJob(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        analysis_run_id=run.id,
        idempotency_key=body.idempotency_key,
        envelope_version=body.envelope_version,
        producer=body.producer,
        transport=body.transport,
        snapshot_type=body.snapshot_type,
        payload_fingerprint=payload_fingerprint,
        status="PROCESSING",
        object_counts=declared_counts,
        change_counts={},
        submitted_by=context.principal_id,
    )
    session.add(ingestion)
    await session.flush()

    counts = await persist_discovery_snapshot(
        session,
        run,
        datasource,
        envelope_to_discovery(body),
        deprecate_missing=body.snapshot_type == "FULL",
        connector_capabilities={
            **(datasource.capabilities or {}),
            "canonical_push": True,
        },
    )
    run.status = "COMPLETED"
    ingestion.status = "COMPLETED"
    ingestion.object_counts = {
        key: counts[key] for key in ("catalogs", "schemas", "tables", "columns", "constraints")
    }
    ingestion.change_counts = {
        key: counts[key] for key in ("created_objects", "changed_objects", "deprecated_objects")
    }
    ingestion.completed_at = datetime.now(UTC)
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="metadata.ingestion.complete",
        resource_type="metadata_ingestion_job",
        resource_id=str(ingestion.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "datasource_id": str(datasource.id),
            "transport": body.transport,
            "snapshot_type": body.snapshot_type,
            "producer": body.producer,
            **ingestion.object_counts,
            **ingestion.change_counts,
        },
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="metadata_ingestion_job",
        aggregate_id=str(ingestion.id),
        event_type="metadata.discovery.snapshot.v1",
        payload={
            "ingestion_id": str(ingestion.id),
            "run_id": str(run.id),
            "datasource_id": str(datasource.id),
            **ingestion.object_counts,
            **ingestion.change_counts,
        },
    )
    await session.commit()
    return ingestion


@router.get("/datasources/{datasource_id}/metadata-ingestions", response_model=Page)
async def list_metadata_ingestions(
    datasource_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Viewer", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = (MetadataIngestionJob.datasource_id == datasource.id,)
    total = int(
        await session.scalar(select(func.count()).select_from(MetadataIngestionJob).where(*filters))
        or 0
    )
    rows = (
        await session.scalars(
            select(MetadataIngestionJob)
            .where(*filters)
            .order_by(MetadataIngestionJob.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[MetadataIngestionRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.post(
    "/datasources/{datasource_id}/metadata-ingestion-batches",
    response_model=MetadataIngestionBatchRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_metadata_ingestion_batch(
    datasource_id: UUID,
    body: MetadataIngestionBatchCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "MetadataIngestor")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> MetadataIngestionBatch:
    datasource = await session.scalar(
        select(DataSource).where(DataSource.id == datasource_id).with_for_update()
    )
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    if datasource.status == "DISABLED":
        raise HTTPException(status_code=409, detail="datasource is disabled")
    if body.expected_chunks > settings.metadata_batch_max_chunks:
        raise HTTPException(status_code=422, detail="expected_chunks exceeds the configured limit")

    existing = await session.scalar(
        select(MetadataIngestionBatch).where(
            MetadataIngestionBatch.datasource_id == datasource.id,
            MetadataIngestionBatch.batch_key == body.batch_key,
        )
    )
    if existing is not None:
        manifest = (
            existing.envelope_version,
            existing.producer,
            existing.snapshot_type,
            existing.expected_chunks,
        )
        requested = (
            body.envelope_version,
            body.producer,
            body.snapshot_type,
            body.expected_chunks,
        )
        if manifest != requested:
            raise HTTPException(status_code=409, detail="batch key was used for another manifest")
        return existing

    batch = MetadataIngestionBatch(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        batch_key=body.batch_key,
        envelope_version=body.envelope_version,
        producer=body.producer,
        snapshot_type=body.snapshot_type,
        expected_chunks=body.expected_chunks,
        submitted_by=context.principal_id,
    )
    session.add(batch)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="metadata.ingestion.batch.create",
        resource_type="metadata_ingestion_batch",
        resource_id=str(batch.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "datasource_id": str(datasource.id),
            "expected_chunks": body.expected_chunks,
            "snapshot_type": body.snapshot_type,
            "producer": body.producer,
        },
    )
    await session.commit()
    return batch


@router.get(
    "/datasources/{datasource_id}/metadata-ingestion-batches",
    response_model=Page,
)
async def list_metadata_ingestion_batches(
    datasource_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Viewer", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = (MetadataIngestionBatch.datasource_id == datasource.id,)
    total = int(
        await session.scalar(
            select(func.count()).select_from(MetadataIngestionBatch).where(*filters)
        )
        or 0
    )
    rows = (
        await session.scalars(
            select(MetadataIngestionBatch)
            .where(*filters)
            .order_by(MetadataIngestionBatch.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[MetadataIngestionBatchRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total,
    )


async def _load_batch(
    session: AsyncSession,
    batch_id: UUID,
    context: SecurityContext,
    *,
    for_update: bool = False,
) -> MetadataIngestionBatch:
    statement = select(MetadataIngestionBatch).where(MetadataIngestionBatch.id == batch_id)
    if for_update:
        statement = statement.with_for_update()
    batch = await session.scalar(statement)
    if batch is None:
        raise HTTPException(status_code=404, detail="metadata ingestion batch not found")
    enforce_organization(context, batch.organization_id)
    return batch


@router.get(
    "/metadata-ingestion-batches/{batch_id}",
    response_model=MetadataIngestionBatchRead,
)
async def get_metadata_ingestion_batch(
    batch_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Viewer", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
) -> MetadataIngestionBatch:
    return await _load_batch(session, batch_id, context)


@router.post(
    "/metadata-ingestion-batches/{batch_id}/chunks",
    response_model=MetadataIngestionChunkRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_metadata_ingestion_chunk(
    batch_id: UUID,
    body: MetadataIngestionChunkCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "MetadataIngestor")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> MetadataIngestionChunk:
    batch = await _load_batch(session, batch_id, context, for_update=True)
    if batch.status != "DRAFT":
        raise HTTPException(status_code=409, detail="batch manifest has already been finalized")
    if body.chunk_number > batch.expected_chunks:
        raise HTTPException(status_code=422, detail="chunk_number exceeds expected_chunks")
    fingerprint = chunk_fingerprint(body)
    existing = await session.scalar(
        select(MetadataIngestionChunk).where(
            MetadataIngestionChunk.batch_id == batch.id,
            (
                (MetadataIngestionChunk.chunk_number == body.chunk_number)
                | (MetadataIngestionChunk.chunk_key == body.chunk_key)
            ),
        )
    )
    if existing is not None:
        if (
            existing.chunk_number != body.chunk_number
            or existing.chunk_key != body.chunk_key
            or existing.payload_fingerprint != fingerprint
        ):
            raise HTTPException(
                status_code=409, detail="chunk number or key has conflicting content"
            )
        return existing

    declared_counts = catalog_counts(body.catalogs)
    existing_counts = (
        await session.scalars(
            select(MetadataIngestionChunk.object_counts).where(
                MetadataIngestionChunk.batch_id == batch.id
            )
        )
    ).all()
    total_tables = declared_counts["tables"] + sum(
        int(counts.get("tables", 0)) for counts in existing_counts
    )
    total_columns = declared_counts["columns"] + sum(
        int(counts.get("columns", 0)) for counts in existing_counts
    )
    if total_tables > settings.metadata_batch_max_tables:
        raise HTTPException(status_code=422, detail="batch exceeds the configured table limit")
    if total_columns > settings.metadata_batch_max_columns:
        raise HTTPException(status_code=422, detail="batch exceeds the configured column limit")

    chunk = MetadataIngestionChunk(
        organization_id=batch.organization_id,
        datasource_id=batch.datasource_id,
        batch_id=batch.id,
        chunk_number=body.chunk_number,
        chunk_key=body.chunk_key,
        emitted_at=body.emitted_at,
        payload_fingerprint=fingerprint,
        payload=body.model_dump(mode="json"),
        object_counts=declared_counts,
    )
    session.add(chunk)
    batch.received_chunks += 1
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=batch.organization_id),
        action="metadata.ingestion.chunk.receive",
        resource_type="metadata_ingestion_chunk",
        resource_id=str(chunk.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "batch_id": str(batch.id),
            "chunk_number": chunk.chunk_number,
            **chunk.object_counts,
        },
    )
    await session.commit()
    return chunk


@router.get(
    "/metadata-ingestion-batches/{batch_id}/chunks",
    response_model=Page,
)
async def list_metadata_ingestion_chunks(
    batch_id: UUID,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Viewer", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    batch = await _load_batch(session, batch_id, context)
    filters = (MetadataIngestionChunk.batch_id == batch.id,)
    total = int(
        await session.scalar(
            select(func.count()).select_from(MetadataIngestionChunk).where(*filters)
        )
        or 0
    )
    rows = (
        await session.scalars(
            select(MetadataIngestionChunk)
            .where(*filters)
            .order_by(MetadataIngestionChunk.chunk_number)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[MetadataIngestionChunkRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.post(
    "/metadata-ingestion-batches/{batch_id}/finalize",
    response_model=MetadataIngestionBatchRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def finalize_metadata_ingestion_batch(
    batch_id: UUID,
    request: Request,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "MetadataIngestor")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> MetadataIngestionBatch:
    if not settings.temporal_enabled or request.app.state.temporal_client is None:
        raise HTTPException(status_code=503, detail="durable workflow service is unavailable")
    batch = await _load_batch(session, batch_id, context, for_update=True)
    if batch.status == "COMPLETED":
        return batch
    if batch.status not in {"DRAFT", "FAILED", "SUBMISSION_FAILED"}:
        raise HTTPException(status_code=409, detail="batch is already queued or processing")
    numbers = list(
        await session.scalars(
            select(MetadataIngestionChunk.chunk_number)
            .where(MetadataIngestionChunk.batch_id == batch.id)
            .order_by(MetadataIngestionChunk.chunk_number)
        )
    )
    if numbers != list(range(1, batch.expected_chunks + 1)):
        raise HTTPException(
            status_code=409,
            detail="all expected chunks must be uploaded before finalization",
        )
    previous_run_id = batch.analysis_run_id
    run = AnalysisRun(
        organization_id=batch.organization_id,
        datasource_id=batch.datasource_id,
        resumed_from_run_id=previous_run_id,
        mode=batch.snapshot_type,
        trigger_type="BATCH_PUSH",
        status="QUEUED",
        priority=50,
    )
    session.add(run)
    await session.flush()
    workflow_id = f"metadata-batch-{batch.id}-{uuid4()}"
    run.temporal_workflow_id = workflow_id
    batch.analysis_run_id = run.id
    batch.temporal_workflow_id = workflow_id
    batch.status = "QUEUED"
    batch.finalized_at = datetime.now(UTC)
    batch.error_class = None
    batch.error_message = None
    record_audit(
        session,
        replace(context, organization_id=batch.organization_id),
        action="metadata.ingestion.batch.finalize",
        resource_type="metadata_ingestion_batch",
        resource_id=str(batch.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "run_id": str(run.id),
            "expected_chunks": batch.expected_chunks,
            "snapshot_type": batch.snapshot_type,
        },
    )
    record_outbox(
        session,
        organization_id=batch.organization_id,
        aggregate_type="metadata_ingestion_batch",
        aggregate_id=str(batch.id),
        event_type="metadata.ingestion.batch.queued.v1",
        payload={
            "batch_id": str(batch.id),
            "run_id": str(run.id),
            "datasource_id": str(batch.datasource_id),
        },
    )
    await session.commit()

    client: Client = request.app.state.temporal_client
    try:
        await client.start_workflow(
            MetadataBatchIngestionWorkflow.run,
            str(batch.id),
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )
    except WorkflowAlreadyStartedError:
        pass
    except Exception as exc:
        batch.status = "SUBMISSION_FAILED"
        batch.error_class = type(exc).__name__
        batch.error_message = "workflow submission failed"
        run.status = "SUBMISSION_FAILED"
        run.error_class = type(exc).__name__
        run.error_message = "workflow submission failed"
        await session.commit()
        raise HTTPException(status_code=503, detail="workflow service unavailable") from exc
    await session.refresh(batch)
    return batch
