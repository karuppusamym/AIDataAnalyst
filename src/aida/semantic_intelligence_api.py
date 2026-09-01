import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot import exp

from aida.business_annotation_versions import current_version_alias
from aida.classification import SENSITIVE_CLASSES
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.fleet import ensure_datasource_enabled
from aida.models import (
    AnalysisRun,
    BusinessDomain,
    BusinessEntity,
    DataSource,
    GovernanceReview,
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    MetadataColumn,
    MetadataConstraint,
    MetadataEnrichmentProposal,
    MetadataSchema,
    MetadataTable,
    Project,
    SemanticInferenceRun,
)
from aida.schemas import (
    BusinessMapEdgeRead,
    BusinessMapNodeRead,
    BusinessMapRead,
    GovernedToolVersionCreate,
    GovernedToolVersionRead,
    MetadataBusinessAnnotationRead,
    MetadataEnrichmentProposalRead,
    Page,
    SemanticInferenceRequest,
    SemanticInferenceRunRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.semantic_inference import (
    SEMANTIC_INFERENCE_VERSION,
    TableSemanticOutput,
    enrich_with_optional_model,
)
from aida.tool_api import create_tool_version

router = APIRouter(prefix="/v1", tags=["business-semantics"])


def _proposal_read(
    proposal: MetadataEnrichmentProposal,
    table: MetadataTable,
    schema: MetadataSchema,
) -> MetadataEnrichmentProposalRead:
    return MetadataEnrichmentProposalRead(
        id=proposal.id,
        organization_id=proposal.organization_id,
        datasource_id=proposal.datasource_id,
        inference_run_id=proposal.inference_run_id,
        table_id=proposal.table_id,
        governance_review_id=proposal.governance_review_id,
        schema_name=schema.name,
        table_name=table.name,
        proposal_type=proposal.proposal_type,
        status=proposal.status,
        engine_type=proposal.engine_type,
        engine_version=proposal.engine_version,
        confidence=proposal.confidence,
        payload=proposal.payload,
        evidence=proposal.evidence,
        fingerprint=proposal.fingerprint,
        proposed_by=proposal.proposed_by,
        reviewed_by=proposal.reviewed_by,
        review_reason=proposal.review_reason,
        reviewed_at=proposal.reviewed_at,
        promoted_tool_version_id=proposal.promoted_tool_version_id,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
    )


def _annotation_read(
    annotation: MetadataBusinessAnnotation,
    version: MetadataBusinessAnnotationVersion,
    table: MetadataTable,
    schema: MetadataSchema,
    domain: BusinessDomain,
    entity: BusinessEntity,
) -> MetadataBusinessAnnotationRead:
    """AT-6: content comes from `version`, the current (`APPROVED`)
    `MetadataBusinessAnnotationVersion` -- `annotation` itself is identity
    only. See `business_annotation_versions.py`.
    """
    return MetadataBusinessAnnotationRead(
        id=annotation.id,
        organization_id=annotation.organization_id,
        datasource_id=annotation.datasource_id,
        table_id=annotation.table_id,
        schema_name=schema.name,
        table_name=table.name,
        domain_id=domain.id,
        domain_key=domain.domain_key,
        domain_name=domain.display_name,
        entity_id=entity.id,
        entity_key=entity.entity_key,
        entity_name=entity.display_name,
        source_proposal_id=annotation.source_proposal_id,
        version=version.version,
        business_name=version.business_name,
        business_description=version.business_description,
        table_role=version.table_role,
        grain_statement=version.grain_statement,
        synonyms=version.synonyms,
        suggested_questions=version.suggested_questions,
        tags=version.tags,
        confidence=version.confidence,
        approved_by=version.approved_by,
        approved_at=version.approved_at,
        created_at=annotation.created_at,
        updated_at=version.updated_at,
    )


@router.post(
    "/datasources/{datasource_id}/semantic-inference-runs",
    response_model=SemanticInferenceRunRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_semantic_inference_run(
    datasource_id: UUID,
    body: SemanticInferenceRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "SemanticAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SemanticInferenceRun:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        ensure_datasource_enabled(datasource)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    analysis_run = await session.scalar(
        select(AnalysisRun)
        .where(
            AnalysisRun.datasource_id == datasource.id,
            AnalysisRun.status == "COMPLETED",
        )
        .order_by(AnalysisRun.updated_at.desc())
        .limit(1)
    )
    if analysis_run is None:
        raise HTTPException(
            status_code=409,
            detail="a completed metadata scan is required before business inference",
        )
    table_rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataSchema.name, MetadataTable.name)
            .limit(body.max_tables)
        )
    ).all()
    if not table_rows:
        raise HTTPException(status_code=409, detail="no active tables are available for inference")
    table_ids = [table.id for table, _schema in table_rows]
    columns = list(
        await session.scalars(
            select(MetadataColumn)
            .where(MetadataColumn.table_id.in_(table_ids), MetadataColumn.status == "ACTIVE")
            .order_by(MetadataColumn.table_id, MetadataColumn.ordinal_position)
        )
    )
    constraints = list(
        await session.scalars(
            select(MetadataConstraint).where(
                MetadataConstraint.table_id.in_(table_ids),
                MetadataConstraint.status == "ACTIVE",
            )
        )
    )
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    constraints_by_table: dict[UUID, list[MetadataConstraint]] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)
    for constraint in constraints:
        constraints_by_table.setdefault(constraint.table_id, []).append(constraint)
    entries = [
        (
            table,
            schema.name,
            columns_by_table.get(table.id, []),
            constraints_by_table.get(table.id, []),
        )
        for table, schema in table_rows
    ]
    suggestions, model_route = await enrich_with_optional_model(
        session=session,
        settings=settings,
        organization_id=datasource.organization_id,
        entries=entries,
        use_model=body.use_model,
    )
    model_count = sum(engine == "LLM_ASSISTED" for _output, engine, _evidence in suggestions)
    run = SemanticInferenceRun(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        analysis_run_id=analysis_run.id,
        status="COMPLETED",
        engine_mode=("HYBRID" if model_count else "RULES_ONLY"),
        engine_version=SEMANTIC_INFERENCE_VERSION,
        model_route=model_route,
        table_count=len(entries),
        proposal_count=len(suggestions),
        model_enriched_count=model_count,
        rule_only_count=len(suggestions) - model_count,
        created_by=context.principal_id,
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    for output, engine_type, evidence in suggestions:
        proposal_id = uuid4()
        review_id = uuid4()
        payload = output.model_dump(mode="json")
        evidence = {
            **evidence,
            "analysis_run_id": str(analysis_run.id),
            "evidence_ids": output.evidence_ids,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {"payload": payload, "evidence": evidence},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        session.add(
            GovernanceReview(
                id=review_id,
                organization_id=datasource.organization_id,
                object_type="METADATA_ENRICHMENT_PROPOSAL",
                object_id=str(proposal_id),
                requested_action="APPLY_BUSINESS_SEMANTICS",
                requested_by=context.principal_id,
            )
        )
        session.add(
            MetadataEnrichmentProposal(
                id=proposal_id,
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                inference_run_id=run.id,
                table_id=output.table_id,
                governance_review_id=review_id,
                engine_type=engine_type,
                engine_version=SEMANTIC_INFERENCE_VERSION,
                confidence=output.confidence,
                payload=payload,
                evidence=evidence,
                fingerprint=fingerprint,
                proposed_by=context.principal_id,
            )
        )
    audit_context = replace(context, organization_id=datasource.organization_id)
    record_audit(
        session,
        audit_context,
        action="business_semantics.inference.complete",
        resource_type="semantic_inference_run",
        resource_id=str(run.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "engine_mode": run.engine_mode,
            "table_count": run.table_count,
            "proposal_count": run.proposal_count,
            "value_scope": "METADATA_ONLY",
        },
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="semantic_inference_run",
        aggregate_id=str(run.id),
        event_type="business_semantics.proposals_created.v1",
        payload={
            "semantic_inference_run_id": str(run.id),
            "datasource_id": str(datasource.id),
            "proposal_count": len(suggestions),
        },
    )
    await session.commit()
    return run


@router.get(
    "/datasources/{datasource_id}/semantic-inference-runs",
    response_model=Page,
)
async def list_semantic_inference_runs(
    datasource_id: UUID,
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "MetadataAdmin",
            "DataAdmin",
            "SemanticAdmin",
            "DataSteward",
            "Analyst",
            "Viewer",
            "Auditor",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = (SemanticInferenceRun.datasource_id == datasource.id,)
    total = await session.scalar(
        select(func.count()).select_from(SemanticInferenceRun).where(*filters)
    )
    rows = list(
        await session.scalars(
            select(SemanticInferenceRun)
            .where(*filters)
            .order_by(SemanticInferenceRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return Page(
        items=[SemanticInferenceRunRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/datasources/{datasource_id}/metadata-enrichment-proposals",
    response_model=Page,
)
async def list_metadata_enrichment_proposals(
    datasource_id: UUID,
    proposal_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "MetadataAdmin",
            "DataAdmin",
            "SemanticAdmin",
            "DataSteward",
            "Reviewer",
            "Analyst",
            "Viewer",
            "Auditor",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = [MetadataEnrichmentProposal.datasource_id == datasource.id]
    if proposal_status:
        filters.append(MetadataEnrichmentProposal.status == proposal_status.upper())
    base = (
        select(MetadataEnrichmentProposal, MetadataTable, MetadataSchema)
        .join(MetadataTable, MetadataTable.id == MetadataEnrichmentProposal.table_id)
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(MetadataEnrichmentProposal.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[_proposal_read(proposal, table, schema) for proposal, table, schema in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/datasources/{datasource_id}/business-annotations",
    response_model=Page,
)
async def list_business_annotations(
    datasource_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "MetadataAdmin",
            "DataAdmin",
            "SemanticAdmin",
            "DataSteward",
            "Reviewer",
            "Analyst",
            "Viewer",
            "Auditor",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    version_alias, version_ranked = current_version_alias()
    base = (
        select(
            MetadataBusinessAnnotation,
            version_alias,
            MetadataTable,
            MetadataSchema,
            BusinessDomain,
            BusinessEntity,
        )
        .join(version_alias, version_alias.annotation_id == MetadataBusinessAnnotation.id)
        .join(MetadataTable, MetadataTable.id == MetadataBusinessAnnotation.table_id)
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .join(BusinessDomain, BusinessDomain.id == MetadataBusinessAnnotation.domain_id)
        .join(BusinessEntity, BusinessEntity.id == MetadataBusinessAnnotation.entity_id)
        .where(
            MetadataBusinessAnnotation.datasource_id == datasource.id,
            version_ranked.c.rn == 1,
        )
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(BusinessDomain.display_name, BusinessEntity.display_name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[_annotation_read(*row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/metadata/tables/{table_id}/business-annotation",
    response_model=MetadataBusinessAnnotationRead,
)
async def get_table_business_annotation(
    table_id: UUID,
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "MetadataAdmin",
            "DataAdmin",
            "SemanticAdmin",
            "DataSteward",
            "Reviewer",
            "Analyst",
            "Viewer",
            "Auditor",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> MetadataBusinessAnnotationRead:
    version_alias, version_ranked = current_version_alias()
    row = (
        await session.execute(
            select(
                MetadataBusinessAnnotation,
                version_alias,
                MetadataTable,
                MetadataSchema,
                BusinessDomain,
                BusinessEntity,
            )
            .join(version_alias, version_alias.annotation_id == MetadataBusinessAnnotation.id)
            .join(MetadataTable, MetadataTable.id == MetadataBusinessAnnotation.table_id)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(BusinessDomain, BusinessDomain.id == MetadataBusinessAnnotation.domain_id)
            .join(BusinessEntity, BusinessEntity.id == MetadataBusinessAnnotation.entity_id)
            .where(MetadataBusinessAnnotation.table_id == table_id, version_ranked.c.rn == 1)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="approved business annotation not found")
    annotation, version, table, schema, domain, entity = row
    enforce_organization(context, annotation.organization_id)
    return _annotation_read(annotation, version, table, schema, domain, entity)


@router.get(
    "/organizations/{organization_id}/business-map",
    response_model=BusinessMapRead,
)
async def get_business_map(
    organization_id: UUID,
    limit: int = Query(default=500, ge=1, le=2000),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "MetadataAdmin",
            "DataAdmin",
            "SemanticAdmin",
            "DataSteward",
            "Reviewer",
            "Analyst",
            "Viewer",
            "Auditor",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> BusinessMapRead:
    enforce_organization(context, organization_id)
    version_alias, version_ranked = current_version_alias()
    rows = (
        await session.execute(
            select(
                MetadataBusinessAnnotation,
                version_alias,
                MetadataTable,
                MetadataSchema,
                BusinessDomain,
                BusinessEntity,
            )
            .join(version_alias, version_alias.annotation_id == MetadataBusinessAnnotation.id)
            .join(MetadataTable, MetadataTable.id == MetadataBusinessAnnotation.table_id)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(BusinessDomain, BusinessDomain.id == MetadataBusinessAnnotation.domain_id)
            .join(BusinessEntity, BusinessEntity.id == MetadataBusinessAnnotation.entity_id)
            .where(
                MetadataBusinessAnnotation.organization_id == organization_id,
                version_ranked.c.rn == 1,
            )
            .order_by(BusinessDomain.display_name, BusinessEntity.display_name)
            .limit(limit)
        )
    ).all()
    nodes: dict[str, BusinessMapNodeRead] = {}
    edges: dict[str, BusinessMapEdgeRead] = {}
    annotations_by_table: dict[UUID, tuple[MetadataBusinessAnnotation, BusinessDomain]] = {}
    for annotation, version, table, schema, domain, entity in rows:
        domain_node = f"domain:{domain.id}"
        entity_node = f"entity:{entity.id}"
        table_node = f"table:{table.id}"
        nodes[domain_node] = BusinessMapNodeRead(
            id=domain_node,
            node_type="DOMAIN",
            label=domain.display_name,
            parent_id=None,
            metadata={"domain_key": domain.domain_key},
        )
        nodes[entity_node] = BusinessMapNodeRead(
            id=entity_node,
            node_type="ENTITY",
            label=entity.display_name,
            parent_id=domain_node,
            metadata={"entity_key": entity.entity_key},
        )
        nodes[table_node] = BusinessMapNodeRead(
            id=table_node,
            node_type="TABLE",
            label=f"{schema.name}.{table.name}",
            parent_id=entity_node,
            metadata={
                "datasource_id": str(annotation.datasource_id),
                "table_role": version.table_role,
                "grain": version.grain_statement,
            },
        )
        edges[f"contains:{domain.id}:{entity.id}"] = BusinessMapEdgeRead(
            id=f"contains:{domain.id}:{entity.id}",
            edge_type="DOMAIN_CONTAINS_ENTITY",
            source_node_id=domain_node,
            target_node_id=entity_node,
            evidence={"status": "APPROVED"},
        )
        edges[f"represents:{entity.id}:{table.id}"] = BusinessMapEdgeRead(
            id=f"represents:{entity.id}:{table.id}",
            edge_type="ENTITY_REPRESENTED_BY_TABLE",
            source_node_id=entity_node,
            target_node_id=table_node,
            evidence={"annotation_version": version.version},
        )
        annotations_by_table[table.id] = (annotation, domain)
    if annotations_by_table:
        foreign_keys = list(
            await session.scalars(
                select(MetadataConstraint).where(
                    MetadataConstraint.organization_id == organization_id,
                    MetadataConstraint.status == "ACTIVE",
                    MetadataConstraint.constraint_type == "FOREIGN_KEY",
                    MetadataConstraint.table_id.in_(annotations_by_table),
                    MetadataConstraint.referenced_table_id.in_(annotations_by_table),
                )
            )
        )
        for constraint in foreign_keys:
            target_id = constraint.referenced_table_id
            if target_id is None:
                continue
            source_annotation, source_domain = annotations_by_table[constraint.table_id]
            target_annotation, target_domain = annotations_by_table[target_id]
            if source_domain.id == target_domain.id:
                continue
            edge_id = f"cross-domain:{constraint.id}"
            edges[edge_id] = BusinessMapEdgeRead(
                id=edge_id,
                edge_type="CROSS_DOMAIN_FOREIGN_KEY",
                source_node_id=f"table:{source_annotation.table_id}",
                target_node_id=f"table:{target_annotation.table_id}",
                evidence={
                    "constraint_id": str(constraint.id),
                    "source_domain": source_domain.domain_key,
                    "target_domain": target_domain.domain_key,
                    "source_columns": constraint.columns,
                    "target_columns": constraint.referenced_columns,
                },
            )
    node_values = list(nodes.values())
    edge_values = list(edges.values())
    return BusinessMapRead(
        organization_id=organization_id,
        nodes=node_values,
        edges=edge_values,
        domain_count=sum(node.node_type == "DOMAIN" for node in node_values),
        entity_count=sum(node.node_type == "ENTITY" for node in node_values),
        table_count=sum(node.node_type == "TABLE" for node in node_values),
        cross_domain_edge_count=sum(
            edge.edge_type == "CROSS_DOMAIN_FOREIGN_KEY" for edge in edge_values
        ),
        truncated=len(rows) == limit,
    )


@router.post(
    "/metadata-enrichment-proposals/{proposal_id}/promote-tool",
    response_model=GovernedToolVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def promote_enrichment_tool_blueprint(
    proposal_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "SemanticAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GovernedToolVersionRead:
    proposal = await session.get(MetadataEnrichmentProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="metadata enrichment proposal not found")
    enforce_organization(context, proposal.organization_id)
    if proposal.status != "APPROVED":
        raise HTTPException(status_code=409, detail="proposal must be approved before promotion")
    if proposal.promoted_tool_version_id:
        raise HTTPException(status_code=409, detail="proposal tool blueprint is already promoted")
    output = TableSemanticOutput.model_validate(proposal.payload)
    blueprint = output.tool_blueprint
    if not blueprint.recommended:
        raise HTTPException(status_code=422, detail="proposal does not recommend a tool")
    row = (
        await session.execute(
            select(MetadataTable, MetadataSchema)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.id == proposal.table_id,
                MetadataTable.datasource_id == proposal.datasource_id,
                MetadataTable.status == "ACTIVE",
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail="proposal table is no longer active")
    table, schema = row
    available = {
        column.name: column
        for column in await session.scalars(
            select(MetadataColumn).where(
                MetadataColumn.table_id == table.id,
                MetadataColumn.status == "ACTIVE",
            )
        )
    }
    if not blueprint.output_columns or any(
        name not in available or available[name].classification in SENSITIVE_CLASSES
        for name in blueprint.output_columns
    ):
        raise HTTPException(
            status_code=409,
            detail="tool blueprint contains missing or sensitive columns",
        )
    datasource = await session.get(DataSource, proposal.datasource_id)
    if datasource is None:
        raise HTTPException(status_code=409, detail="proposal datasource is unavailable")
    project = await session.get(Project, datasource.project_id)
    if project is None:
        raise HTTPException(status_code=409, detail="proposal project is unavailable")
    table_expression = exp.Table(
        this=exp.to_identifier(table.name), db=exp.to_identifier(schema.name)
    )
    sql = (
        exp.select(*(exp.column(column_name) for column_name in blueprint.output_columns))
        .from_(table_expression)
        .sql(dialect=datasource.dialect)
    )
    tool_version = await create_tool_version(
        project_id=project.id,
        body=GovernedToolVersionCreate(
            slug=blueprint.slug,
            name=blueprint.name,
            description=(
                f"{blueprint.description} Generated from approved business-semantic proposal "
                f"{proposal.id}; SQL was rendered deterministically by Atlas."
            ),
            datasource_id=datasource.id,
            semantic_model_version_id=None,
            sql_template=sql,
            parameters=[],
            allowed_roles=list(blueprint.allowed_roles),
        ),
        context=context,
        session=session,
        settings=settings,
    )
    proposal.promoted_tool_version_id = tool_version.id
    record_audit(
        session,
        replace(context, organization_id=proposal.organization_id),
        action="business_semantics.tool_blueprint.promote",
        resource_type="metadata_enrichment_proposal",
        resource_id=str(proposal.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"governed_tool_version_id": str(tool_version.id), "status": "DRAFT"},
    )
    record_outbox(
        session,
        organization_id=proposal.organization_id,
        aggregate_type="metadata_enrichment_proposal",
        aggregate_id=str(proposal.id),
        event_type="business_semantics.tool_blueprint_promoted.v1",
        payload={
            "proposal_id": str(proposal.id),
            "governed_tool_version_id": str(tool_version.id),
        },
    )
    await session.commit()
    return tool_version
