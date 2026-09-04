from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.dbt_artifacts import (
    DbtArtifactError,
    ParsedDbtResource,
    parse_dbt_catalog,
    parse_dbt_manifest,
    parse_dbt_run_results,
)
from aida.dbt_column_lineage import DependencyResource, extract_column_lineage
from aida.dbt_quality_bridge import reconcile_dbt_test_quality
from aida.events import record_audit, record_outbox
from aida.integration_catalog import transformation_metadata_integration_enabled
from aida.integration_service import ensure_organization_integration_policy
from aida.models import (
    DataSource,
    DbtArtifactImport,
    DbtLineageEdge,
    DbtProject,
    DbtResource,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Project,
)
from aida.parsed_lineage_review_service import (
    resolve_review_status_for_new_edge,
)
from aida.schemas import (
    DbtArtifactImportRead,
    DbtArtifactImportRequest,
    DbtLineageEdgeRead,
    DbtLineageNodeRead,
    DbtLineageRead,
    DbtProjectCreate,
    DbtProjectRead,
    DbtResourceRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["dbt-transformations"])

RELATION_RESOURCE_TYPES = frozenset({"MODEL", "SEED", "SNAPSHOT", "SOURCE"})


def _normalized_identifier(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().strip('`"[]').lower()


async def _project_scope(
    session: AsyncSession,
    project_id: UUID,
    context: SecurityContext,
) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    await _require_dbt_integration(session, project.organization_id)
    return project


async def _dbt_project_scope(
    session: AsyncSession,
    dbt_project_id: UUID,
    context: SecurityContext,
) -> DbtProject:
    dbt_project = await session.get(DbtProject, dbt_project_id)
    if dbt_project is None:
        raise HTTPException(status_code=404, detail="dbt project not found")
    enforce_organization(context, dbt_project.organization_id)
    await _require_dbt_integration(session, dbt_project.organization_id)
    return dbt_project


async def _artifact_scope(
    session: AsyncSession,
    artifact_id: UUID,
    context: SecurityContext,
) -> DbtArtifactImport:
    artifact = await session.get(DbtArtifactImport, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="dbt artifact import not found")
    enforce_organization(context, artifact.organization_id)
    await _require_dbt_integration(session, artifact.organization_id)
    return artifact


async def _catalog_matches(
    session: AsyncSession,
    datasource_id: UUID,
) -> tuple[dict[tuple[str, str, str], UUID], dict[tuple[str, str], UUID]]:
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
    for table, schema, catalog in rows:
        schema_key = _normalized_identifier(schema.name)
        table_key = _normalized_identifier(table.name)
        exact[(_normalized_identifier(catalog.name), schema_key, table_key)] = table.id
        candidates.setdefault((schema_key, table_key), []).append(table.id)
    unambiguous = {key: values[0] for key, values in candidates.items() if len(values) == 1}
    return exact, unambiguous


def _matched_table_id(
    resource: ParsedDbtResource,
    exact: dict[tuple[str, str, str], UUID],
    unambiguous: dict[tuple[str, str], UUID],
) -> UUID | None:
    if resource.resource_type not in RELATION_RESOURCE_TYPES:
        return None
    schema = _normalized_identifier(resource.schema_name)
    name = _normalized_identifier(resource.name)
    database = _normalized_identifier(resource.database_name)
    return exact.get((database, schema, name)) if database else unambiguous.get((schema, name))


async def _require_dbt_integration(session: AsyncSession, organization_id: UUID) -> None:
    policy = await ensure_organization_integration_policy(session, organization_id)
    if not transformation_metadata_integration_enabled(
        policy.transformation_metadata_integrations, "dbt"
    ):
        raise HTTPException(
            status_code=403,
            detail="dbt integration is disabled for this organization",
        )


@router.post(
    "/projects/{project_id}/dbt-projects",
    response_model=DbtProjectRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_dbt_project(
    project_id: UUID,
    body: DbtProjectCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "MetadataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> DbtProject:
    project = await _project_scope(session, project_id, context)
    datasource = await session.get(DataSource, body.datasource_id)
    if datasource is None or datasource.project_id != project.id:
        raise HTTPException(
            status_code=422,
            detail="datasource must belong to the selected project",
        )
    dbt_project = DbtProject(
        organization_id=project.organization_id,
        project_id=project.id,
        datasource_id=datasource.id,
        project_key=body.project_key,
        display_name=body.display_name,
        repository_url=body.repository_url,
        target_name=body.target_name,
        created_by=context.principal_id,
    )
    session.add(dbt_project)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="dbt project key already exists") from exc
    record_audit(
        session,
        replace(context, organization_id=project.organization_id),
        action="dbt_project.create",
        resource_type="dbt_project",
        resource_id=str(dbt_project.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"project_key": dbt_project.project_key, "datasource_id": str(datasource.id)},
    )
    await session.commit()
    await session.refresh(dbt_project)
    return dbt_project


@router.get("/projects/{project_id}/dbt-projects", response_model=Page)
async def list_dbt_projects(
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
    filters = [DbtProject.project_id == project_id]
    total = await session.scalar(select(func.count()).select_from(DbtProject).where(*filters))
    rows = (
        await session.scalars(
            select(DbtProject)
            .where(*filters)
            .order_by(DbtProject.display_name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[DbtProjectRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/dbt-projects/{dbt_project_id}/artifact-imports",
    response_model=DbtArtifactImportRead,
    status_code=status.HTTP_201_CREATED,
)
async def import_dbt_manifest(
    dbt_project_id: UUID,
    body: DbtArtifactImportRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "MetadataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> DbtArtifactImport:
    dbt_project = await _dbt_project_scope(session, dbt_project_id, context)
    datasource = await session.get(DataSource, dbt_project.datasource_id)
    if datasource is None:
        raise HTTPException(status_code=409, detail="registered dbt datasource is unavailable")
    try:
        parsed = parse_dbt_manifest(body.manifest, datasource.dialect)
    except DbtArtifactError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    catalog_types: dict[str, dict[str, str]] = {}
    if body.catalog is not None:
        try:
            catalog_types = parse_dbt_catalog(body.catalog)
        except DbtArtifactError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    test_results = {}
    if body.run_results is not None:
        try:
            test_results = parse_dbt_run_results(body.run_results)
        except DbtArtifactError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing = await session.scalar(
        select(DbtArtifactImport).where(
            DbtArtifactImport.dbt_project_id == dbt_project.id,
            DbtArtifactImport.manifest_fingerprint == parsed.fingerprint,
        )
    )
    if existing is not None:
        return existing
    exact, unambiguous = await _catalog_matches(session, datasource.id)
    relation_resource_count = 0
    matched_resource_count = 0
    resources: list[tuple[ParsedDbtResource, UUID | None]] = []
    for parsed_item in parsed.resources:
        table_id = _matched_table_id(parsed_item, exact, unambiguous)
        if parsed_item.resource_type in RELATION_RESOURCE_TYPES:
            relation_resource_count += 1
            matched_resource_count += int(table_id is not None)
        resources.append((parsed_item, table_id))
    artifact = DbtArtifactImport(
        organization_id=dbt_project.organization_id,
        dbt_project_id=dbt_project.id,
        manifest_fingerprint=parsed.fingerprint,
        dbt_schema_version=parsed.dbt_schema_version,
        dbt_version=parsed.dbt_version,
        invocation_id=parsed.invocation_id,
        generated_at=parsed.generated_at,
        resource_count=len(parsed.resources),
        model_count=sum(item.resource_type == "MODEL" for item in parsed.resources),
        source_count=sum(item.resource_type == "SOURCE" for item in parsed.resources),
        test_count=sum(item.resource_type == "TEST" for item in parsed.resources),
        lineage_edge_count=len(parsed.edges),
        matched_resource_count=matched_resource_count,
        unmatched_resource_count=relation_resource_count - matched_resource_count,
        imported_by=context.principal_id,
    )
    session.add(artifact)
    await session.flush()
    resource_by_unique_id: dict[str, DbtResource] = {}
    for parsed_resource, table_id in resources:
        col_types = dict(parsed_resource.column_types)
        if parsed_resource.unique_id in catalog_types:
            col_types.update(catalog_types[parsed_resource.unique_id])
        test_info = test_results.get(parsed_resource.unique_id)
        dbt_resource = DbtResource(
            organization_id=dbt_project.organization_id,
            artifact_import_id=artifact.id,
            unique_id=parsed_resource.unique_id,
            resource_type=parsed_resource.resource_type,
            package_name=parsed_resource.package_name,
            name=parsed_resource.name,
            database_name=parsed_resource.database_name,
            schema_name=parsed_resource.schema_name,
            relation_name=parsed_resource.relation_name,
            materialization=parsed_resource.materialization,
            original_file_path=parsed_resource.original_file_path,
            description=parsed_resource.description,
            compiled_sql_hash=parsed_resource.compiled_sql_hash,
            compiled_sql_redacted=parsed_resource.compiled_sql_redacted,
            sql_parse_status=parsed_resource.sql_parse_status,
            column_names=parsed_resource.column_names,
            column_descriptions=parsed_resource.column_descriptions,
            column_types=col_types,
            tags=parsed_resource.tags,
            depends_on_unique_ids=parsed_resource.depends_on_unique_ids,
            matched_table_id=table_id,
            test_status=test_info.status if test_info else None,
            test_failures=test_info.failures if test_info else None,
            test_execution_time=test_info.execution_time if test_info else None,
            extra_metadata=parsed_resource.extra_metadata,
        )
        session.add(dbt_resource)
        resource_by_unique_id[dbt_resource.unique_id] = dbt_resource
    await session.flush()
    # P1-05: connector-pushed dbt manifests inherit their trust from the
    # bound datasource. A trusted-for-lineage datasource lands edges
    # ACTIVE straight away; every other datasource follows the shared
    # rule (`auto_active` default → ACTIVE; `require_review` →
    # PROPOSED unless confidence >= threshold).
    settings = get_settings()
    review_mode = settings.lineage_parsed_edges_review_mode
    high_conf = settings.lineage_high_confidence_auto_active_threshold
    source_trusted = bool(getattr(datasource, "trusted_for_lineage", False))
    principal_id = context.principal_id
    for source_unique_id, target_unique_id in parsed.edges:
        session.add(
            DbtLineageEdge(
                organization_id=dbt_project.organization_id,
                artifact_import_id=artifact.id,
                source_resource_id=resource_by_unique_id[source_unique_id].id,
                target_resource_id=resource_by_unique_id[target_unique_id].id,
                edge_type="DEPENDS_ON",
                # Table-level edges have no column granularity -- "" (never
                # NULL) so the widened unique constraint stays meaningful.
                source_column="",
                target_column="",
                # Table-level DEPENDS_ON edges come from the manifest's
                # `depends_on` field directly -- no parser judgement --
                # so they're treated as full-confidence for review
                # purposes. That still leaves them subject to
                # `require_review` for an untrusted datasource.
                review_status=resolve_review_status_for_new_edge(
                    review_mode=review_mode,
                    confidence="FULL",
                    threshold=high_conf,
                    source_trusted=source_trusted,
                ),
                created_by=principal_id,
            )
        )
    column_lineage_edge_count = 0
    for parsed_resource, _table_id in resources:
        if parsed_resource.sql_parse_status != "PARSED":
            continue
        target_resource = resource_by_unique_id[parsed_resource.unique_id]
        dependencies = [
            DependencyResource(
                unique_id=dependency.unique_id,
                relation_name=dependency.relation_name,
                database_name=dependency.database_name,
                schema_name=dependency.schema_name,
                name=dependency.name,
            )
            for dependency_id in parsed_resource.depends_on_unique_ids
            if (dependency := resource_by_unique_id.get(dependency_id)) is not None
        ]
        if not dependencies:
            continue
        column_edges = extract_column_lineage(parsed_resource, dependencies, datasource.dialect)
        for column_edge in column_edges:
            source_resource = resource_by_unique_id.get(column_edge.source_unique_id)
            if source_resource is None:
                continue
            session.add(
                DbtLineageEdge(
                    organization_id=dbt_project.organization_id,
                    artifact_import_id=artifact.id,
                    source_resource_id=source_resource.id,
                    target_resource_id=target_resource.id,
                    edge_type="COLUMN_DEPENDS_ON",
                    source_column=column_edge.source_column,
                    target_column=column_edge.target_column,
                    transformation_type=column_edge.transformation_type,
                    confidence=column_edge.confidence,
                    review_status=resolve_review_status_for_new_edge(
                        review_mode=review_mode,
                        confidence=column_edge.confidence,
                        threshold=high_conf,
                        source_trusted=source_trusted,
                    ),
                    created_by=principal_id,
                )
            )
            column_lineage_edge_count += 1
    if test_results:
        await reconcile_dbt_test_quality(
            session,
            organization_id=dbt_project.organization_id,
            datasource_id=datasource.id,
            dbt_resources=list(resource_by_unique_id.values()),
            context=context,
        )
    record_audit(
        session,
        replace(context, organization_id=dbt_project.organization_id),
        action="dbt_artifact.import",
        resource_type="dbt_artifact_import",
        resource_id=str(artifact.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "dbt_project_id": str(dbt_project.id),
            "resource_count": artifact.resource_count,
            "lineage_edge_count": artifact.lineage_edge_count,
            "column_lineage_edge_count": column_lineage_edge_count,
            "matched_resource_count": artifact.matched_resource_count,
            "raw_artifact_persisted": False,
        },
    )
    record_outbox(
        session,
        organization_id=dbt_project.organization_id,
        aggregate_type="dbt_artifact_import",
        aggregate_id=str(artifact.id),
        event_type="dbt_artifact.imported.v1",
        payload={
            "artifact_import_id": str(artifact.id),
            "dbt_project_id": str(dbt_project.id),
            "resource_count": artifact.resource_count,
            "lineage_edge_count": artifact.lineage_edge_count,
            "column_lineage_edge_count": column_lineage_edge_count,
        },
    )
    await session.commit()
    await session.refresh(artifact)
    return artifact


@router.get("/dbt-projects/{dbt_project_id}/artifact-imports", response_model=Page)
async def list_dbt_artifact_imports(
    dbt_project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "MetadataAdmin", "DataSteward", "Auditor", "Viewer"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    await _dbt_project_scope(session, dbt_project_id, context)
    filters = [DbtArtifactImport.dbt_project_id == dbt_project_id]
    total = await session.scalar(
        select(func.count()).select_from(DbtArtifactImport).where(*filters)
    )
    rows = (
        await session.scalars(
            select(DbtArtifactImport)
            .where(*filters)
            .order_by(DbtArtifactImport.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[DbtArtifactImportRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/dbt-artifact-imports/{artifact_id}/resources", response_model=Page)
async def list_dbt_resources(
    artifact_id: UUID,
    resource_type: str | None = Query(default=None, max_length=30),
    matched: bool | None = Query(default=None),
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
    filters = [DbtResource.artifact_import_id == artifact_id]
    if resource_type:
        filters.append(DbtResource.resource_type == resource_type.upper())
    if matched is True:
        filters.append(DbtResource.matched_table_id.is_not(None))
    elif matched is False:
        filters.append(DbtResource.matched_table_id.is_(None))
    total = await session.scalar(select(func.count()).select_from(DbtResource).where(*filters))
    rows = (
        await session.scalars(
            select(DbtResource)
            .where(*filters)
            .order_by(DbtResource.resource_type, DbtResource.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[DbtResourceRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/dbt-artifact-imports/{artifact_id}/lineage", response_model=DbtLineageRead)
async def get_dbt_lineage(
    artifact_id: UUID,
    limit: int = Query(default=1000, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "DataAdmin", "MetadataAdmin", "DataSteward", "Auditor", "Viewer"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> DbtLineageRead:
    artifact = await _artifact_scope(session, artifact_id, context)
    resources = (
        await session.scalars(
            select(DbtResource)
            .where(DbtResource.artifact_import_id == artifact.id)
            .order_by(DbtResource.resource_type, DbtResource.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    resource_ids = {resource.id for resource in resources}
    edges = (
        await session.scalars(
            select(DbtLineageEdge).where(
                DbtLineageEdge.artifact_import_id == artifact.id,
                DbtLineageEdge.source_resource_id.in_(resource_ids),
                DbtLineageEdge.target_resource_id.in_(resource_ids),
            )
        )
    ).all()
    return DbtLineageRead(
        artifact_import_id=artifact.id,
        nodes=[
            DbtLineageNodeRead(
                id=resource.id,
                unique_id=resource.unique_id,
                label=resource.name,
                resource_type=resource.resource_type,
                materialization=resource.materialization,
                matched_table_id=resource.matched_table_id,
                test_status=resource.test_status,
            )
            for resource in resources
        ],
        edges=[DbtLineageEdgeRead.model_validate(edge) for edge in edges],
        resource_count=artifact.resource_count,
        edge_count=artifact.lineage_edge_count,
        catalog_match_count=artifact.matched_resource_count,
    )
