from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.bi_lineage import (
    BiLineageError,
    ParsedBiColumnRef,
    parse_bi_artifact,
)
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.integration_catalog import transformation_metadata_integration_enabled
from aida.integration_service import ensure_organization_integration_policy
from aida.models import (
    BiArtifactImport,
    BiConnection,
    BiMetricColumnEdge,
    BiMetricNode,
    BiReportMetricEdge,
    BiReportNode,
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Project,
)
from aida.schemas import (
    BiArtifactImportRead,
    BiArtifactImportRequest,
    BiConnectionCreate,
    BiConnectionRead,
    BiLineageRead,
    BiMetricColumnEdgeRead,
    BiMetricNodeRead,
    BiReportMetricEdgeRead,
    BiReportNodeRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["bi-lineage"])


def _normalized_identifier(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('`"[]').lower()


async def _require_bi_integration(session: AsyncSession, organization_id: UUID) -> None:
    policy = await ensure_organization_integration_policy(session, organization_id)
    if not transformation_metadata_integration_enabled(
        policy.transformation_metadata_integrations, "bi"
    ):
        raise HTTPException(
            status_code=403,
            detail="bi integration is disabled for this organization",
        )


async def _project_scope(
    session: AsyncSession,
    project_id: UUID,
    context: SecurityContext,
) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    await _require_bi_integration(session, project.organization_id)
    return project


async def _connection_scope(
    session: AsyncSession,
    connection_id: UUID,
    context: SecurityContext,
) -> BiConnection:
    connection = await session.get(BiConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="bi connection not found")
    enforce_organization(context, connection.organization_id)
    await _require_bi_integration(session, connection.organization_id)
    return connection


async def _artifact_scope(
    session: AsyncSession,
    artifact_id: UUID,
    context: SecurityContext,
) -> BiArtifactImport:
    artifact = await session.get(BiArtifactImport, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="bi artifact import not found")
    enforce_organization(context, artifact.organization_id)
    await _require_bi_integration(session, artifact.organization_id)
    return artifact


async def _catalog_matches(
    session: AsyncSession,
    datasource_id: UUID,
) -> tuple[
    dict[tuple[str, str, str], UUID],
    dict[tuple[str, str], UUID],
    dict[UUID, dict[str, UUID]],
]:
    """Return (exact db.schema.table match, unambiguous schema.table match,
    table_id -> {column_name: column_id})."""
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
    candidates: dict[tuple[str, str], list[UUID]] = {}
    table_ids: set[UUID] = set()
    for table, schema, catalog in rows:
        schema_key = _normalized_identifier(schema.name)
        table_key = _normalized_identifier(table.name)
        exact[(_normalized_identifier(catalog.name), schema_key, table_key)] = table.id
        candidates.setdefault((schema_key, table_key), []).append(table.id)
        table_ids.add(table.id)
    unambiguous = {key: values[0] for key, values in candidates.items() if len(values) == 1}

    columns_by_table: dict[UUID, dict[str, UUID]] = {}
    if table_ids:
        column_rows = (
            await session.scalars(
                select(MetadataColumn).where(
                    MetadataColumn.table_id.in_(table_ids),
                    MetadataColumn.status == "ACTIVE",
                )
            )
        ).all()
        for column in column_rows:
            bucket = columns_by_table.setdefault(column.table_id, {})
            bucket[_normalized_identifier(column.name)] = column.id
    return exact, unambiguous, columns_by_table


def _matched_table_id_for_column_ref(
    column_ref: ParsedBiColumnRef,
    exact: dict[tuple[str, str, str], UUID],
    unambiguous: dict[tuple[str, str], UUID],
) -> UUID | None:
    schema = _normalized_identifier(column_ref.schema_name)
    table = _normalized_identifier(column_ref.table_name)
    database = _normalized_identifier(column_ref.database_name)
    if database:
        matched = exact.get((database, schema, table))
        if matched is not None:
            return matched
    return unambiguous.get((schema, table))


@router.post(
    "/projects/{project_id}/bi-connections",
    response_model=BiConnectionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_bi_connection(
    project_id: UUID,
    body: BiConnectionCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "MetadataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> BiConnection:
    project = await _project_scope(session, project_id, context)
    datasource = await session.get(DataSource, body.datasource_id)
    if datasource is None or datasource.project_id != project.id:
        raise HTTPException(
            status_code=422,
            detail="datasource must belong to the selected project",
        )
    connection = BiConnection(
        organization_id=project.organization_id,
        project_id=project.id,
        datasource_id=datasource.id,
        bi_tool=body.bi_tool,
        connection_key=body.connection_key,
        display_name=body.display_name,
        site_or_workspace=body.site_or_workspace,
        created_by=context.principal_id,
    )
    session.add(connection)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="bi connection key already exists") from exc
    record_audit(
        session,
        replace(context, organization_id=project.organization_id),
        action="bi_connection.create",
        resource_type="bi_connection",
        resource_id=str(connection.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "project_id": str(project.id),
            "bi_tool": connection.bi_tool,
            "datasource_id": str(datasource.id),
        },
    )
    await session.commit()
    await session.refresh(connection)
    return connection


@router.get("/projects/{project_id}/bi-connections", response_model=Page)
async def list_bi_connections(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "MetadataAdmin", "DataSteward", "Auditor", "Viewer"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    await _project_scope(session, project_id, context)
    filters = [BiConnection.project_id == project_id]
    total = await session.scalar(select(func.count()).select_from(BiConnection).where(*filters))
    rows = (
        await session.scalars(
            select(BiConnection)
            .where(*filters)
            .order_by(BiConnection.display_name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[BiConnectionRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/bi-connections/{connection_id}/artifact-imports",
    response_model=BiArtifactImportRead,
    status_code=status.HTTP_201_CREATED,
)
async def import_bi_artifact(
    connection_id: UUID,
    body: BiArtifactImportRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "MetadataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> BiArtifactImport:
    connection = await _connection_scope(session, connection_id, context)
    if connection.bi_tool != body.bi_tool:
        raise HTTPException(
            status_code=422,
            detail="artifact bi_tool must match the connection's registered bi_tool",
        )
    datasource = await session.get(DataSource, connection.datasource_id)
    if datasource is None:
        raise HTTPException(status_code=409, detail="registered bi datasource is unavailable")
    try:
        parsed = parse_bi_artifact(body.bi_tool, body.artifact)
    except BiLineageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = await session.scalar(
        select(BiArtifactImport).where(
            BiArtifactImport.connection_id == connection.id,
            BiArtifactImport.artifact_fingerprint == parsed.fingerprint,
        )
    )
    if existing is not None:
        return existing

    exact, unambiguous, columns_by_table = await _catalog_matches(session, datasource.id)
    matched_column_count = 0
    unmatched_column_count = 0
    column_matches: dict[str, tuple[UUID | None, UUID | None]] = {}
    for edge in parsed.metric_column_edges:
        table_id = _matched_table_id_for_column_ref(edge.column, exact, unambiguous)
        column_id = None
        if table_id is not None:
            column_id = columns_by_table.get(table_id, {}).get(
                _normalized_identifier(edge.column.column_name)
            )
        if column_id is not None:
            matched_column_count += 1
        else:
            unmatched_column_count += 1
        key = (
            f"{edge.metric_external_id}\0{edge.column.database_name}\0{edge.column.schema_name}"
            f"\0{edge.column.table_name}\0{edge.column.column_name}"
        )
        column_matches[key] = (table_id, column_id)

    artifact = BiArtifactImport(
        organization_id=connection.organization_id,
        connection_id=connection.id,
        artifact_fingerprint=parsed.fingerprint,
        bi_tool=parsed.bi_tool,
        generated_at=parsed.generated_at,
        report_count=len(parsed.reports),
        metric_count=len(parsed.metrics),
        report_metric_edge_count=len(parsed.report_metric_edges),
        metric_column_edge_count=len(parsed.metric_column_edges),
        matched_column_count=matched_column_count,
        unmatched_column_count=unmatched_column_count,
        imported_by=context.principal_id,
    )
    session.add(artifact)
    await session.flush()

    report_by_external_id: dict[str, BiReportNode] = {}
    for parsed_report in parsed.reports:
        report_by_external_id[parsed_report.external_id] = BiReportNode(
            organization_id=connection.organization_id,
            artifact_import_id=artifact.id,
            external_id=parsed_report.external_id,
            name=parsed_report.name,
            report_type=parsed_report.report_type,
            project_name=parsed_report.project_name,
        )
    # Add parentless reports first is not required since parent_report_id is
    # only resolved after flush; add all, flush, then wire the self-referential FK.
    for report_row in report_by_external_id.values():
        session.add(report_row)
    await session.flush()
    for parsed_report in parsed.reports:
        if parsed_report.parent_external_id is None:
            continue
        parent = report_by_external_id.get(parsed_report.parent_external_id)
        if parent is not None:
            report_by_external_id[parsed_report.external_id].parent_report_id = parent.id
    await session.flush()

    metric_by_external_id: dict[str, BiMetricNode] = {}
    for parsed_metric in parsed.metrics:
        metric_node = BiMetricNode(
            organization_id=connection.organization_id,
            artifact_import_id=artifact.id,
            external_id=parsed_metric.external_id,
            name=parsed_metric.name,
            field_type=parsed_metric.field_type,
            datasource_name=parsed_metric.datasource_name,
            formula_hash=parsed_metric.formula_hash,
            formula_present=parsed_metric.formula_present,
        )
        session.add(metric_node)
        metric_by_external_id[parsed_metric.external_id] = metric_node
    await session.flush()

    for parsed_edge in parsed.report_metric_edges:
        matched_report = report_by_external_id.get(parsed_edge.report_external_id)
        matched_metric = metric_by_external_id.get(parsed_edge.metric_external_id)
        if matched_report is None or matched_metric is None:
            continue
        session.add(
            BiReportMetricEdge(
                organization_id=connection.organization_id,
                artifact_import_id=artifact.id,
                report_id=matched_report.id,
                metric_id=matched_metric.id,
            )
        )

    for edge in parsed.metric_column_edges:
        matched_metric = metric_by_external_id.get(edge.metric_external_id)
        if matched_metric is None:
            continue
        key = (
            f"{edge.metric_external_id}\0{edge.column.database_name}\0{edge.column.schema_name}"
            f"\0{edge.column.table_name}\0{edge.column.column_name}"
        )
        table_id, column_id = column_matches.get(key, (None, None))
        session.add(
            BiMetricColumnEdge(
                organization_id=connection.organization_id,
                artifact_import_id=artifact.id,
                metric_id=matched_metric.id,
                source_database_name=edge.column.database_name,
                source_schema_name=edge.column.schema_name,
                source_table_name=edge.column.table_name,
                source_column_name=edge.column.column_name,
                matched_table_id=table_id,
                matched_column_id=column_id,
            )
        )

    record_audit(
        session,
        replace(context, organization_id=connection.organization_id),
        action="bi_artifact.import",
        resource_type="bi_artifact_import",
        resource_id=str(artifact.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "connection_id": str(connection.id),
            "bi_tool": artifact.bi_tool,
            "report_count": artifact.report_count,
            "metric_count": artifact.metric_count,
            "matched_column_count": artifact.matched_column_count,
            "unmatched_column_count": artifact.unmatched_column_count,
            "raw_artifact_persisted": False,
        },
    )
    record_outbox(
        session,
        organization_id=connection.organization_id,
        aggregate_type="bi_artifact_import",
        aggregate_id=str(artifact.id),
        event_type="bi_artifact.imported.v1",
        payload={
            "artifact_import_id": str(artifact.id),
            "connection_id": str(connection.id),
            "bi_tool": artifact.bi_tool,
            "report_count": artifact.report_count,
            "metric_count": artifact.metric_count,
        },
    )
    await session.commit()
    await session.refresh(artifact)
    return artifact


@router.get("/bi-connections/{connection_id}/artifact-imports", response_model=Page)
async def list_bi_artifact_imports(
    connection_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "MetadataAdmin", "DataSteward", "Auditor", "Viewer"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    await _connection_scope(session, connection_id, context)
    filters = [BiArtifactImport.connection_id == connection_id]
    total = await session.scalar(
        select(func.count()).select_from(BiArtifactImport).where(*filters)
    )
    rows = (
        await session.scalars(
            select(BiArtifactImport)
            .where(*filters)
            .order_by(BiArtifactImport.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[BiArtifactImportRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/bi-artifact-imports/{artifact_id}/reports", response_model=Page)
async def list_bi_reports(
    artifact_id: UUID,
    report_type: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=250, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "MetadataAdmin", "DataSteward", "Auditor", "Viewer"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    await _artifact_scope(session, artifact_id, context)
    filters = [BiReportNode.artifact_import_id == artifact_id]
    if report_type:
        filters.append(BiReportNode.report_type == report_type.upper())
    total = await session.scalar(select(func.count()).select_from(BiReportNode).where(*filters))
    rows = (
        await session.scalars(
            select(BiReportNode)
            .where(*filters)
            .order_by(BiReportNode.report_type, BiReportNode.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[BiReportNodeRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/bi-artifact-imports/{artifact_id}/lineage", response_model=BiLineageRead)
async def get_bi_lineage(
    artifact_id: UUID,
    limit: int = Query(default=1000, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "MetadataAdmin", "DataSteward", "Auditor", "Viewer"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> BiLineageRead:
    artifact = await _artifact_scope(session, artifact_id, context)
    reports = (
        await session.scalars(
            select(BiReportNode)
            .where(BiReportNode.artifact_import_id == artifact.id)
            .order_by(BiReportNode.report_type, BiReportNode.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    metrics = (
        await session.scalars(
            select(BiMetricNode)
            .where(BiMetricNode.artifact_import_id == artifact.id)
            .order_by(BiMetricNode.field_type, BiMetricNode.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    report_ids = {report.id for report in reports}
    metric_ids = {metric.id for metric in metrics}
    report_metric_edges = (
        await session.scalars(
            select(BiReportMetricEdge).where(
                BiReportMetricEdge.artifact_import_id == artifact.id,
                BiReportMetricEdge.report_id.in_(report_ids),
                BiReportMetricEdge.metric_id.in_(metric_ids),
            )
        )
    ).all()
    metric_column_edges = (
        await session.scalars(
            select(BiMetricColumnEdge).where(
                BiMetricColumnEdge.artifact_import_id == artifact.id,
                BiMetricColumnEdge.metric_id.in_(metric_ids),
            )
        )
    ).all()
    return BiLineageRead(
        artifact_import_id=artifact.id,
        reports=[BiReportNodeRead.model_validate(report) for report in reports],
        metrics=[BiMetricNodeRead.model_validate(metric) for metric in metrics],
        report_metric_edges=[
            BiReportMetricEdgeRead.model_validate(edge) for edge in report_metric_edges
        ],
        metric_column_edges=[
            BiMetricColumnEdgeRead.model_validate(edge) for edge in metric_column_edges
        ],
        report_count=artifact.report_count,
        metric_count=artifact.metric_count,
        matched_column_count=artifact.matched_column_count,
        unmatched_column_count=artifact.unmatched_column_count,
    )
