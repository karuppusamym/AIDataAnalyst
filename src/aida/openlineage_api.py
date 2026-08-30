from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.integration_catalog import transformation_metadata_integration_enabled
from aida.integration_service import ensure_organization_integration_policy
from aida.models import (
    DataSource,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    OpenLineageColumnEdge,
    OpenLineageDataset,
    OpenLineageRunEvent,
    OpenLineageTableEdge,
)
from aida.openlineage import (
    OpenLineageError,
    ParsedOpenLineageDataset,
    parse_openlineage_run_event,
)
from aida.schemas import (
    OpenLineageColumnEdgeRead,
    OpenLineageDatasetRead,
    OpenLineageIngestRequest,
    OpenLineageRunEventRead,
    OpenLineageTableEdgeRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["openlineage"])


def _normalized_identifier(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('`"[]').lower()


def _dataset_key(dataset: ParsedOpenLineageDataset) -> tuple[str, str]:
    return (_normalized_identifier(dataset.namespace), _normalized_identifier(dataset.name))


async def _require_openlineage_integration(
    session: AsyncSession, organization_id: UUID
) -> None:
    policy = await ensure_organization_integration_policy(session, organization_id)
    if not transformation_metadata_integration_enabled(
        policy.transformation_metadata_integrations, "openlineage"
    ):
        raise HTTPException(
            status_code=403,
            detail="openlineage integration is disabled for this organization",
        )


async def _datasource_scope(
    session: AsyncSession,
    datasource_id: UUID,
    context: SecurityContext,
) -> DataSource:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    await _require_openlineage_integration(session, datasource.organization_id)
    return datasource


async def _event_scope(
    session: AsyncSession,
    event_id: UUID,
    context: SecurityContext,
) -> OpenLineageRunEvent:
    event = await session.get(OpenLineageRunEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="openlineage event not found")
    enforce_organization(context, event.organization_id)
    await _require_openlineage_integration(session, event.organization_id)
    return event


async def _catalog_matches(
    session: AsyncSession,
    datasource_id: UUID,
) -> tuple[
    dict[tuple[str, str, str], UUID],
    dict[tuple[str, str], UUID],
    dict[str, UUID],
]:
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataTable.datasource_id == datasource_id,
                MetadataTable.status == "ACTIVE",
                MetadataSchema.status == "ACTIVE",
                MetadataCatalog.status == "ACTIVE",
            )
        )
    ).all()
    exact: dict[tuple[str, str, str], UUID] = {}
    schema_table_candidates: dict[tuple[str, str], list[UUID]] = {}
    table_only_candidates: dict[str, list[UUID]] = {}
    for table, schema, catalog in rows:
        catalog_key = _normalized_identifier(catalog.name)
        schema_key = _normalized_identifier(schema.name)
        table_key = _normalized_identifier(table.name)
        exact[(catalog_key, schema_key, table_key)] = table.id
        schema_table_candidates.setdefault((schema_key, table_key), []).append(table.id)
        table_only_candidates.setdefault(table_key, []).append(table.id)
    schema_table = {
        key: values[0] for key, values in schema_table_candidates.items() if len(values) == 1
    }
    table_only = {
        key: values[0] for key, values in table_only_candidates.items() if len(values) == 1
    }
    return exact, schema_table, table_only


def _matched_table_id_for_dataset(
    dataset: ParsedOpenLineageDataset,
    exact: dict[tuple[str, str, str], UUID],
    schema_table: dict[tuple[str, str], UUID],
    table_only: dict[str, UUID],
) -> UUID | None:
    parts = [
        part for part in (_normalized_identifier(item) for item in dataset.name.split(".")) if part
    ]
    if len(parts) >= 3:
        return exact.get((parts[-3], parts[-2], parts[-1])) or schema_table.get(
            (parts[-2], parts[-1])
        )
    if len(parts) == 2:
        return schema_table.get((parts[0], parts[1]))
    if len(parts) == 1:
        return table_only.get(parts[0])
    return None


async def _event_read(
    session: AsyncSession,
    event: OpenLineageRunEvent,
) -> OpenLineageRunEventRead:
    datasets = (
        await session.scalars(
            select(OpenLineageDataset)
            .where(OpenLineageDataset.run_event_id == event.id)
            .order_by(
                OpenLineageDataset.direction,
                OpenLineageDataset.namespace,
                OpenLineageDataset.name,
            )
        )
    ).all()
    table_edges = (
        await session.scalars(
            select(OpenLineageTableEdge)
            .where(OpenLineageTableEdge.run_event_id == event.id)
            .order_by(
                OpenLineageTableEdge.input_dataset_name,
                OpenLineageTableEdge.output_dataset_name,
            )
        )
    ).all()
    column_edges = (
        await session.scalars(
            select(OpenLineageColumnEdge)
            .where(OpenLineageColumnEdge.run_event_id == event.id)
            .order_by(
                OpenLineageColumnEdge.output_dataset_name,
                OpenLineageColumnEdge.output_column_name,
                OpenLineageColumnEdge.input_dataset_name,
                OpenLineageColumnEdge.input_column_name,
            )
            .limit(2000)
        )
    ).all()
    return OpenLineageRunEventRead(
        **OpenLineageRunEventRead.model_validate(event).model_dump(),
        datasets=[OpenLineageDatasetRead.model_validate(item) for item in datasets],
        table_edges=[OpenLineageTableEdgeRead.model_validate(item) for item in table_edges],
        column_edges=[OpenLineageColumnEdgeRead.model_validate(item) for item in column_edges],
    )


@router.post(
    "/lineage/openlineage",
    response_model=OpenLineageRunEventRead,
    status_code=status.HTTP_201_CREATED,
)
async def ingest_openlineage_run_event(
    body: OpenLineageIngestRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "MetadataAdmin", "MetadataIngestor")
    ),
    session: AsyncSession = Depends(get_session),
) -> OpenLineageRunEventRead:
    datasource = await _datasource_scope(session, body.datasource_id, context)
    try:
        parsed = parse_openlineage_run_event(body.event)
    except OpenLineageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing = await session.scalar(
        select(OpenLineageRunEvent).where(
            OpenLineageRunEvent.datasource_id == datasource.id,
            OpenLineageRunEvent.event_fingerprint == parsed.fingerprint,
        )
    )
    if existing is not None:
        return await _event_read(session, existing)

    exact, schema_table, table_only = await _catalog_matches(session, datasource.id)
    inputs_with_matches = [
        (dataset, _matched_table_id_for_dataset(dataset, exact, schema_table, table_only))
        for dataset in parsed.inputs
    ]
    outputs_with_matches = [
        (dataset, _matched_table_id_for_dataset(dataset, exact, schema_table, table_only))
        for dataset in parsed.outputs
    ]
    input_lookup = {
        _dataset_key(dataset): matched_table_id for dataset, matched_table_id in inputs_with_matches
    }
    output_lookup = {
        _dataset_key(dataset): matched_table_id
        for dataset, matched_table_id in outputs_with_matches
    }
    unresolved_dataset_count = sum(
        matched_table_id is None
        for _, matched_table_id in [*inputs_with_matches, *outputs_with_matches]
    )

    event = OpenLineageRunEvent(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        event_fingerprint=parsed.fingerprint,
        event_type=parsed.event_type,
        event_time=parsed.event_time,
        producer=parsed.producer,
        schema_url=parsed.schema_url,
        job_namespace=parsed.job_namespace,
        job_name=parsed.job_name,
        run_id=parsed.run_id,
        input_dataset_count=len(parsed.inputs),
        output_dataset_count=len(parsed.outputs),
        table_edge_count=len(parsed.table_edges),
        column_edge_count=len(parsed.column_edges),
        unresolved_dataset_count=unresolved_dataset_count,
        imported_by=context.principal_id,
    )
    session.add(event)
    await session.flush()

    for dataset, matched_table_id in inputs_with_matches:
        session.add(
            OpenLineageDataset(
                organization_id=datasource.organization_id,
                run_event_id=event.id,
                direction="INPUT",
                namespace=dataset.namespace,
                name=dataset.name,
                matched_table_id=matched_table_id,
                schema_fields=dataset.schema_fields,
            )
        )
    for dataset, matched_table_id in outputs_with_matches:
        session.add(
            OpenLineageDataset(
                organization_id=datasource.organization_id,
                run_event_id=event.id,
                direction="OUTPUT",
                namespace=dataset.namespace,
                name=dataset.name,
                matched_table_id=matched_table_id,
                schema_fields=dataset.schema_fields,
            )
        )

    for edge in parsed.table_edges:
        session.add(
            OpenLineageTableEdge(
                organization_id=datasource.organization_id,
                run_event_id=event.id,
                input_dataset_namespace=edge.input_dataset_namespace,
                input_dataset_name=edge.input_dataset_name,
                input_table_id=input_lookup.get(
                    (
                        _normalized_identifier(edge.input_dataset_namespace),
                        _normalized_identifier(edge.input_dataset_name),
                    )
                ),
                output_dataset_namespace=edge.output_dataset_namespace,
                output_dataset_name=edge.output_dataset_name,
                output_table_id=output_lookup.get(
                    (
                        _normalized_identifier(edge.output_dataset_namespace),
                        _normalized_identifier(edge.output_dataset_name),
                    )
                ),
            )
        )

    for column_edge in parsed.column_edges:
        session.add(
            OpenLineageColumnEdge(
                organization_id=datasource.organization_id,
                run_event_id=event.id,
                input_dataset_namespace=column_edge.input_dataset_namespace,
                input_dataset_name=column_edge.input_dataset_name,
                input_table_id=input_lookup.get(
                    (
                        _normalized_identifier(column_edge.input_dataset_namespace),
                        _normalized_identifier(column_edge.input_dataset_name),
                    )
                ),
                input_column_name=column_edge.input_column_name,
                output_dataset_namespace=column_edge.output_dataset_namespace,
                output_dataset_name=column_edge.output_dataset_name,
                output_table_id=output_lookup.get(
                    (
                        _normalized_identifier(column_edge.output_dataset_namespace),
                        _normalized_identifier(column_edge.output_dataset_name),
                    )
                ),
                output_column_name=column_edge.output_column_name,
                transformation_type=column_edge.transformation_type,
                transformation_subtype=column_edge.transformation_subtype,
            )
        )

    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="openlineage_run_event.import",
        resource_type="openlineage_run_event",
        resource_id=str(event.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "datasource_id": str(datasource.id),
            "event_type": event.event_type,
            "job_namespace": event.job_namespace,
            "job_name": event.job_name,
            "run_id": event.run_id,
            "unresolved_dataset_count": event.unresolved_dataset_count,
            "table_edge_count": event.table_edge_count,
            "column_edge_count": event.column_edge_count,
        },
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="openlineage_run_event",
        aggregate_id=str(event.id),
        event_type="openlineage.run_event.ingested.v1",
        payload={
            "openlineage_run_event_id": str(event.id),
            "datasource_id": str(datasource.id),
            "event_type": event.event_type,
            "table_edge_count": event.table_edge_count,
            "column_edge_count": event.column_edge_count,
            "unresolved_dataset_count": event.unresolved_dataset_count,
        },
    )
    await session.commit()
    await session.refresh(event)
    return await _event_read(session, event)


@router.get("/datasources/{datasource_id}/openlineage-events", response_model=Page)
async def list_openlineage_run_events(
    datasource_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "DataAdmin",
            "MetadataAdmin",
            "DataSteward",
            "Auditor",
            "Viewer",
            "Operations",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await _datasource_scope(session, datasource_id, context)
    filters = (OpenLineageRunEvent.datasource_id == datasource.id,)
    total = await session.scalar(
        select(func.count()).select_from(OpenLineageRunEvent).where(*filters)
    )
    rows = (
        await session.scalars(
            select(OpenLineageRunEvent)
            .where(*filters)
            .order_by(OpenLineageRunEvent.event_time.desc(), OpenLineageRunEvent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[OpenLineageRunEventRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/openlineage-events/{event_id}", response_model=OpenLineageRunEventRead)
async def get_openlineage_run_event(
    event_id: UUID,
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "DataAdmin",
            "MetadataAdmin",
            "DataSteward",
            "Auditor",
            "Viewer",
            "Operations",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> OpenLineageRunEventRead:
    event = await _event_scope(session, event_id, context)
    return await _event_read(session, event)
