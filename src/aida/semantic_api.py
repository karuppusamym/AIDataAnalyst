import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aida.asset_description_service import (
    apply_asset_description_draft,
    reject_asset_description_draft,
)
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    AiAsset,
    AiAssetVersion,
    AssetDescriptionDraft,
    AssetDocumentationVersion,
    BulkStewardshipOperation,
    ContextProductVersion,
    CrossBoundaryGrant,
    DataContractVersion,
    DataProduct,
    DataProductAccessRequest,
    DataProductVersion,
    DataSource,
    GlossaryConflict,
    GlossaryLinkProposal,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    GovernedToolVersion,
    MetadataColumn,
    MetadataEnrichmentProposal,
    MetadataTable,
    ModelRouteConfiguration,
    Project,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
    TermSemanticBinding,
)
from aida.product_marketplace_api import approve_access_request
from aida.schemas import (
    GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS,
    GovernanceDecisionRequest,
    GovernanceReviewBulkDecisionItemRead,
    GovernanceReviewBulkDecisionRequest,
    GovernanceReviewBulkDecisionResultRead,
    GovernanceReviewBulkSelectionFilter,
    GovernanceReviewRead,
    Page,
    SemanticMetricCreate,
    SemanticMetricVersionRead,
    SemanticModelCloneRequest,
    SemanticModelVersionCreate,
    SemanticModelVersionRead,
    TermSemanticBindingCreate,
    TermSemanticBindingRead,
)
from aida.security import (
    SecurityContext,
    enforce_organization,
    require_roles,
    require_roles_or_delegated,
)
from aida.semantic_inference import apply_enrichment_proposal
from aida.stewardship_service import (
    apply_bulk_operation,
    apply_conflict_resolution,
    apply_link_proposal,
    reject_conflict_resolution,
    reject_link_proposal,
)

router = APIRouter(prefix="/v1", tags=["semantic-governance"])


def _metric_fingerprint(body: SemanticMetricCreate) -> str:
    payload = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metric_read(
    version: SemanticMetricVersion, metric: SemanticMetric
) -> SemanticMetricVersionRead:
    return SemanticMetricVersionRead(
        id=version.id,
        semantic_model_version_id=version.semantic_model_version_id,
        metric_id=metric.id,
        metric_slug=metric.slug,
        metric_name=version.name,
        version=version.version,
        status=version.status,
        description=version.description,
        aggregation=version.aggregation,
        grain=version.grain,
        source_table_id=version.source_table_id,
        measure_column_id=version.measure_column_id,
        default_time_column_id=version.default_time_column_id,
        allowed_dimension_column_ids=[
            UUID(value) for value in version.allowed_dimension_column_ids
        ],
        fingerprint=version.fingerprint,
        created_by=version.created_by,
        created_at=version.created_at,
    )


async def _project_for_model(
    session: AsyncSession, model: SemanticModelVersion, context: SecurityContext
) -> Project:
    project = await session.get(Project, model.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    return project


@router.post(
    "/projects/{project_id}/semantic-model-versions",
    response_model=SemanticModelVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_semantic_model_version(
    project_id: UUID,
    body: SemanticModelVersionCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> SemanticModelVersion:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    if body.based_on_version_id:
        base = await session.get(SemanticModelVersion, body.based_on_version_id)
        if base is None or base.project_id != project.id:
            raise HTTPException(status_code=422, detail="base semantic version is invalid")
    latest = await session.scalar(
        select(func.max(SemanticModelVersion.version)).where(
            SemanticModelVersion.project_id == project.id
        )
    )
    model = SemanticModelVersion(
        organization_id=project.organization_id,
        project_id=project.id,
        version=(latest or 0) + 1,
        name=body.name,
        change_summary=body.change_summary,
        created_by=context.principal_id,
        based_on_version_id=body.based_on_version_id,
    )
    session.add(model)
    await session.flush()
    audit_context = replace(context, organization_id=project.organization_id)
    record_audit(
        session,
        audit_context,
        action="semantic_model.create",
        resource_type="semantic_model_version",
        resource_id=str(model.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"version": model.version},
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="semantic_model_version",
        aggregate_id=str(model.id),
        event_type="semantic_model.draft_created.v1",
        payload={"semantic_model_version_id": str(model.id), "project_id": str(project.id)},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="semantic version allocation conflict; retry the request",
        ) from exc
    return model


@router.get("/projects/{project_id}/semantic-model-versions", response_model=Page)
async def list_semantic_model_versions(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    filters = (
        SemanticModelVersion.organization_id == project.organization_id,
        SemanticModelVersion.project_id == project.id,
    )
    total = await session.scalar(
        select(func.count()).select_from(SemanticModelVersion).where(*filters)
    )
    rows = (
        await session.scalars(
            select(SemanticModelVersion)
            .where(*filters)
            .order_by(SemanticModelVersion.version.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[SemanticModelVersionRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/semantic-model-versions/{source_model_id}/clone",
    response_model=SemanticModelVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def clone_semantic_model_version(
    source_model_id: UUID,
    body: SemanticModelCloneRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> SemanticModelVersion:
    source = await session.get(SemanticModelVersion, source_model_id)
    if source is None:
        raise HTTPException(status_code=404, detail="semantic model version not found")
    project = await _project_for_model(session, source, context)
    latest_model_version = await session.scalar(
        select(func.max(SemanticModelVersion.version)).where(
            SemanticModelVersion.project_id == project.id
        )
    )
    clone = SemanticModelVersion(
        organization_id=project.organization_id,
        project_id=project.id,
        version=(latest_model_version or 0) + 1,
        name=body.name,
        change_summary=body.change_summary,
        created_by=context.principal_id,
        based_on_version_id=source.id,
    )
    session.add(clone)
    await session.flush()
    source_metric_versions = (
        await session.scalars(
            select(SemanticMetricVersion).where(
                SemanticMetricVersion.semantic_model_version_id == source.id
            )
        )
    ).all()
    for source_metric in source_metric_versions:
        latest_metric_version = await session.scalar(
            select(func.max(SemanticMetricVersion.version)).where(
                SemanticMetricVersion.metric_id == source_metric.metric_id
            )
        )
        session.add(
            SemanticMetricVersion(
                organization_id=project.organization_id,
                semantic_model_version_id=clone.id,
                metric_id=source_metric.metric_id,
                version=(latest_metric_version or 0) + 1,
                name=source_metric.name,
                description=source_metric.description,
                aggregation=source_metric.aggregation,
                grain=source_metric.grain,
                source_table_id=source_metric.source_table_id,
                measure_column_id=source_metric.measure_column_id,
                default_time_column_id=source_metric.default_time_column_id,
                allowed_dimension_column_ids=list(source_metric.allowed_dimension_column_ids),
                fingerprint=source_metric.fingerprint,
                created_by=context.principal_id,
            )
        )
    record_audit(
        session,
        replace(context, organization_id=project.organization_id),
        action="semantic_model.clone",
        resource_type="semantic_model_version",
        resource_id=str(clone.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "based_on_version_id": str(source.id),
            "copied_metrics": len(source_metric_versions),
        },
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="semantic_model_version",
        aggregate_id=str(clone.id),
        event_type="semantic_model.cloned.v1",
        payload={
            "semantic_model_version_id": str(clone.id),
            "based_on_version_id": str(source.id),
            "project_id": str(project.id),
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="semantic clone conflict; retry") from exc
    return clone


@router.post(
    "/semantic-model-versions/{model_id}/metrics",
    response_model=SemanticMetricVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_metric_version(
    model_id: UUID,
    body: SemanticMetricCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> SemanticMetricVersionRead:
    model = await session.get(SemanticModelVersion, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="semantic model version not found")
    project = await _project_for_model(session, model, context)
    if model.status != "DRAFT":
        raise HTTPException(status_code=409, detail="metrics can only change in a draft version")

    table = await session.get(MetadataTable, body.source_table_id)
    if (
        table is None
        or table.organization_id != project.organization_id
        or table.status != "ACTIVE"
    ):
        raise HTTPException(status_code=422, detail="source table is invalid or inactive")
    datasource = await session.get(DataSource, table.datasource_id)
    if datasource is None or datasource.project_id != project.id:
        raise HTTPException(status_code=422, detail="source table is outside this project")
    referenced_ids = set(body.allowed_dimension_column_ids)
    if body.measure_column_id:
        referenced_ids.add(body.measure_column_id)
    if body.default_time_column_id:
        referenced_ids.add(body.default_time_column_id)
    columns = (
        await session.scalars(select(MetadataColumn).where(MetadataColumn.id.in_(referenced_ids)))
    ).all()
    if len(columns) != len(referenced_ids) or any(
        column.table_id != table.id or column.status != "ACTIVE" for column in columns
    ):
        raise HTTPException(
            status_code=422,
            detail="metric columns must be active columns of the selected source table",
        )

    metric = await session.scalar(
        select(SemanticMetric).where(
            SemanticMetric.project_id == project.id,
            SemanticMetric.slug == body.slug,
        )
    )
    if metric is None:
        metric = SemanticMetric(
            organization_id=project.organization_id,
            project_id=project.id,
            slug=body.slug,
        )
        session.add(metric)
        await session.flush()
    existing_in_model = await session.scalar(
        select(SemanticMetricVersion).where(
            SemanticMetricVersion.semantic_model_version_id == model.id,
            SemanticMetricVersion.metric_id == metric.id,
        )
    )
    if existing_in_model is not None:
        raise HTTPException(status_code=409, detail="metric already exists in this model version")
    latest_metric_version = await session.scalar(
        select(func.max(SemanticMetricVersion.version)).where(
            SemanticMetricVersion.metric_id == metric.id
        )
    )
    version = SemanticMetricVersion(
        organization_id=project.organization_id,
        semantic_model_version_id=model.id,
        metric_id=metric.id,
        version=(latest_metric_version or 0) + 1,
        name=body.name,
        description=body.description,
        aggregation=body.aggregation,
        grain=body.grain,
        source_table_id=table.id,
        measure_column_id=body.measure_column_id,
        default_time_column_id=body.default_time_column_id,
        allowed_dimension_column_ids=[str(value) for value in body.allowed_dimension_column_ids],
        fingerprint=_metric_fingerprint(body),
        created_by=context.principal_id,
    )
    session.add(version)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=project.organization_id),
        action="semantic_metric.create",
        resource_type="semantic_metric_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"metric_slug": metric.slug, "version": version.version},
    )
    await session.commit()
    return _metric_read(version, metric)


@router.get("/semantic-model-versions/{model_id}/metrics", response_model=Page)
async def list_metric_versions(
    model_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    model = await session.get(SemanticModelVersion, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="semantic model version not found")
    await _project_for_model(session, model, context)
    total = await session.scalar(
        select(func.count())
        .select_from(SemanticMetricVersion)
        .where(SemanticMetricVersion.semantic_model_version_id == model.id)
    )
    rows = (
        await session.execute(
            select(SemanticMetricVersion, SemanticMetric)
            .join(SemanticMetric, SemanticMetric.id == SemanticMetricVersion.metric_id)
            .where(SemanticMetricVersion.semantic_model_version_id == model.id)
            .order_by(SemanticMetric.slug)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[_metric_read(version, metric) for version, metric in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


async def _semantic_metric_and_project(
    session: AsyncSession, metric_id: UUID, context: SecurityContext
) -> tuple[SemanticMetric, Project]:
    metric = await session.get(SemanticMetric, metric_id)
    if metric is None:
        raise HTTPException(status_code=404, detail="semantic metric not found")
    project = await session.get(Project, metric.project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    return metric, project


async def _approved_glossary_term(
    session: AsyncSession, term_id: UUID, organization_id: UUID
) -> tuple[GlossaryTerm, GlossaryTermVersion]:
    term = await session.get(GlossaryTerm, term_id)
    if term is None or term.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="glossary term not found")
    approved = await session.scalar(
        select(GlossaryTermVersion)
        .where(
            GlossaryTermVersion.term_id == term.id,
            GlossaryTermVersion.status == "APPROVED",
        )
        .order_by(GlossaryTermVersion.version.desc())
        .limit(1)
    )
    if approved is None:
        raise HTTPException(status_code=409, detail="only approved glossary terms can be bound")
    return term, approved


def _term_semantic_binding_read(
    binding: TermSemanticBinding,
    term: GlossaryTerm,
    term_version: GlossaryTermVersion,
    semantic_object_name: str,
) -> TermSemanticBindingRead:
    return TermSemanticBindingRead(
        id=binding.id,
        organization_id=binding.organization_id,
        term_id=term.id,
        term_key=term.term_key,
        term_display_name=term_version.display_name,
        term_definition=term_version.definition,
        semantic_object_type=binding.semantic_object_type,
        semantic_object_id=binding.semantic_object_id,
        semantic_object_name=semantic_object_name,
        status=binding.status,
        requested_by=binding.requested_by,
        approved_by=binding.approved_by,
        approved_at=binding.approved_at,
        governance_review_id=binding.governance_review_id,
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


@router.post(
    "/semantic-metrics/{metric_id}/glossary-bindings",
    response_model=TermSemanticBindingRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_term_semantic_binding(
    metric_id: UUID,
    body: TermSemanticBindingCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> TermSemanticBindingRead:
    """SM-2: bind `body.term_id` to the metric at `metric_id`.

    Mirrors `request_cross_boundary_grant` (api.py): the binding is created
    `PENDING_APPROVAL` and filed into the same shared governance review queue
    every other governed object here uses -- it only becomes `ACTIVE`, and
    only then eligible to participate in retrieval, once a *different*
    principal approves it via `POST /governance/reviews/{id}/decision`.
    """
    metric, project = await _semantic_metric_and_project(session, metric_id, context)
    if body.semantic_object_type != "METRIC" or body.semantic_object_id != metric.id:
        raise HTTPException(
            status_code=422,
            detail="semantic_object_type/semantic_object_id must identify the metric in the path",
        )
    term, _term_version = await _approved_glossary_term(
        session, body.term_id, project.organization_id
    )
    existing = await session.scalar(
        select(TermSemanticBinding).where(
            TermSemanticBinding.term_id == term.id,
            TermSemanticBinding.semantic_object_type == body.semantic_object_type,
            TermSemanticBinding.semantic_object_id == body.semantic_object_id,
            TermSemanticBinding.status.in_(("PENDING_APPROVAL", "ACTIVE")),
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409, detail="a binding for this term and semantic object already exists"
        )
    binding = TermSemanticBinding(
        organization_id=project.organization_id,
        term_id=term.id,
        semantic_object_type=body.semantic_object_type,
        semantic_object_id=body.semantic_object_id,
        requested_by=context.principal_id,
    )
    session.add(binding)
    await session.flush()
    review = GovernanceReview(
        organization_id=project.organization_id,
        object_type="TERM_SEMANTIC_BINDING",
        object_id=str(binding.id),
        requested_action="BIND",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    binding.governance_review_id = review.id
    audit_context = replace(context, organization_id=project.organization_id)
    record_audit(
        session,
        audit_context,
        action="semantic.term_binding.request",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "binding_id": str(binding.id),
            "term_id": str(term.id),
            "semantic_object_type": binding.semantic_object_type,
            "semantic_object_id": str(binding.semantic_object_id),
        },
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": review.object_id,
        },
    )
    await session.commit()
    return _term_semantic_binding_read(binding, term, _term_version, metric.slug)


@router.get("/semantic-metrics/{metric_id}/glossary-bindings", response_model=Page)
async def list_metric_glossary_bindings(
    metric_id: UUID,
    binding_status: str | None = Query(default="ACTIVE", alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """SM-2, object -> term direction: which glossary terms are bound to this metric."""
    metric, _project = await _semantic_metric_and_project(session, metric_id, context)
    filters = [
        TermSemanticBinding.semantic_object_type == "METRIC",
        TermSemanticBinding.semantic_object_id == metric.id,
    ]
    if binding_status:
        filters.append(TermSemanticBinding.status == binding_status.upper())
    base = (
        select(TermSemanticBinding, GlossaryTerm, GlossaryTermVersion)
        .join(GlossaryTerm, GlossaryTerm.id == TermSemanticBinding.term_id)
        .join(
            GlossaryTermVersion,
            (GlossaryTermVersion.term_id == GlossaryTerm.id)
            & (GlossaryTermVersion.status == "APPROVED"),
        )
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(TermSemanticBinding.created_at).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[
            _term_semantic_binding_read(binding, term, term_version, metric.slug)
            for binding, term, term_version in rows
        ],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/glossary-terms/{term_id}/semantic-bindings", response_model=Page)
async def list_term_semantic_bindings(
    term_id: UUID,
    binding_status: str | None = Query(default="ACTIVE", alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """SM-2, term -> object direction: which semantic objects this term is bound to."""
    term = await session.get(GlossaryTerm, term_id)
    if term is None:
        raise HTTPException(status_code=404, detail="glossary term not found")
    enforce_organization(context, term.organization_id)
    term_version = await session.scalar(
        select(GlossaryTermVersion)
        .where(GlossaryTermVersion.term_id == term.id, GlossaryTermVersion.status == "APPROVED")
        .order_by(GlossaryTermVersion.version.desc())
        .limit(1)
    )
    if term_version is None:
        raise HTTPException(status_code=409, detail="glossary term has no approved version")
    filters = [TermSemanticBinding.term_id == term.id]
    if binding_status:
        filters.append(TermSemanticBinding.status == binding_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(select(TermSemanticBinding).where(*filters).subquery())
    )
    bindings = (
        await session.scalars(
            select(TermSemanticBinding)
            .where(*filters)
            .order_by(TermSemanticBinding.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items: list[TermSemanticBindingRead] = []
    for binding in bindings:
        semantic_object_name = ""
        if binding.semantic_object_type == "METRIC":
            metric = await session.get(SemanticMetric, binding.semantic_object_id)
            semantic_object_name = metric.slug if metric is not None else ""
        items.append(_term_semantic_binding_read(binding, term, term_version, semantic_object_name))
    return Page(items=items, limit=limit, offset=offset, total=total or 0)


@router.delete("/term-semantic-bindings/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_term_semantic_binding(
    binding_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> Response:
    binding = await session.get(TermSemanticBinding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="term-semantic binding not found")
    enforce_organization(context, binding.organization_id)
    await session.delete(binding)
    record_audit(
        session,
        replace(context, organization_id=binding.organization_id),
        action="semantic.term_binding.delete",
        resource_type="term_semantic_binding",
        resource_id=str(binding.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "term_id": str(binding.term_id),
            "semantic_object_type": binding.semantic_object_type,
            "semantic_object_id": str(binding.semantic_object_id),
        },
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/semantic-model-versions/{model_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_semantic_model_for_review(
    model_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    model = await session.get(SemanticModelVersion, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="semantic model version not found")
    project = await _project_for_model(session, model, context)
    if model.status == "REVIEW_REQUIRED":
        existing = await session.scalar(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "SEMANTIC_MODEL_VERSION",
                GovernanceReview.object_id == str(model.id),
                GovernanceReview.status == "PENDING",
            )
        )
        if existing:
            return existing
    if model.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only a draft model can be submitted")
    metric_count = await session.scalar(
        select(func.count())
        .select_from(SemanticMetricVersion)
        .where(SemanticMetricVersion.semantic_model_version_id == model.id)
    )
    if not metric_count:
        raise HTTPException(
            status_code=422,
            detail="semantic model must contain at least one metric",
        )
    review = GovernanceReview(
        organization_id=project.organization_id,
        object_type="SEMANTIC_MODEL_VERSION",
        object_id=str(model.id),
        requested_action="PUBLISH",
        requested_by=context.principal_id,
    )
    session.add(review)
    model.status = "REVIEW_REQUIRED"
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=project.organization_id),
        action="semantic_model.submit",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"semantic_model_version_id": str(model.id)},
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": review.object_id,
        },
    )
    await session.commit()
    return review


@router.get("/governance/reviews", response_model=Page)
async def list_governance_reviews(
    review_status: str = Query(default="PENDING", alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward", "Reviewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    organization_id = context.require_organization()
    filters = (
        GovernanceReview.organization_id == organization_id,
        GovernanceReview.status == review_status.upper(),
    )
    total = await session.scalar(select(func.count()).select_from(GovernanceReview).where(*filters))
    rows = (
        await session.scalars(
            select(GovernanceReview)
            .where(*filters)
            .order_by(GovernanceReview.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[GovernanceReviewRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


async def _apply_governance_review_decision(
    session: AsyncSession,
    review: GovernanceReview,
    *,
    decision: str,
    reason: str | None,
    context: SecurityContext,
    now: datetime,
) -> tuple[str, str, str, dict[str, Any]]:
    """Apply one governance decision's object-type-specific side effects.

    This is the single core `decide_governance_review` (single item) and
    `bulk_decide_governance_reviews` (PG-3) both call, so the two paths
    cannot drift: every object type the unified review queue supports is
    dispatched exactly once, here. Mutates `review` itself
    (status/decided_by/decision_reason/decided_at) plus the target object,
    and returns `(event_type, aggregate_type, aggregate_id, payload)` for the
    caller to record as an outbox event. Raises `HTTPException` (409 for a
    target no longer in a decidable state, 422 for an unsupported object
    type) -- it does not catch or convert those; callers are responsible for
    the maker != checker, PENDING-only, and organization-boundary
    preconditions *before* calling this, and for deciding what a raised
    exception means for their own path (abort the single decision, or fail
    just this one item of a bulk batch).
    """
    review.status = "APPROVED" if decision == "APPROVE" else "REJECTED"
    review.decided_by = context.principal_id
    review.decision_reason = reason
    review.decided_at = now
    if review.object_type == "SEMANTIC_MODEL_VERSION":
        model = await session.get(SemanticModelVersion, UUID(review.object_id))
        if model is None or model.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if decision == "APPROVE":
            await session.execute(
                update(SemanticModelVersion)
                .where(
                    SemanticModelVersion.project_id == model.project_id,
                    SemanticModelVersion.status == "PUBLISHED",
                    SemanticModelVersion.id != model.id,
                )
                .values(status="SUPERSEDED", updated_at=now)
            )
            model.status = "PUBLISHED"
            model.approved_by = context.principal_id
            model.approved_at = now
            model.published_at = now
            await session.execute(
                update(SemanticMetricVersion)
                .where(SemanticMetricVersion.semantic_model_version_id == model.id)
                .values(status="PUBLISHED", updated_at=now)
            )
            event_type = "semantic_model.published.v1"
        else:
            model.status = "REJECTED"
            await session.execute(
                update(SemanticMetricVersion)
                .where(SemanticMetricVersion.semantic_model_version_id == model.id)
                .values(status="REJECTED", updated_at=now)
            )
            event_type = "semantic_model.rejected.v1"
        aggregate_type = "semantic_model_version"
        aggregate_id = str(model.id)
        payload = {
            "semantic_model_version_id": str(model.id),
            "project_id": str(model.project_id),
            "version": model.version,
            "review_id": str(review.id),
        }
    elif review.object_type == "GOVERNED_TOOL_VERSION":
        tool_version = await session.get(GovernedToolVersion, UUID(review.object_id))
        if tool_version is None or tool_version.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if review.requested_action == "DEPRECATE":
            if tool_version.status != "PUBLISHED":
                raise HTTPException(status_code=409, detail="tool is no longer published")
            if decision == "APPROVE":
                tool_version.status = "DEPRECATED"
                event_type = "tool.version.deprecated.v1"
            else:
                event_type = "tool.version.deprecation_rejected.v1"
        elif decision == "APPROVE":
            await session.execute(
                update(GovernedToolVersion)
                .where(
                    GovernedToolVersion.tool_id == tool_version.tool_id,
                    GovernedToolVersion.status == "PUBLISHED",
                    GovernedToolVersion.id != tool_version.id,
                )
                .values(status="SUPERSEDED", updated_at=now)
            )
            tool_version.status = "PUBLISHED"
            tool_version.approved_by = context.principal_id
            tool_version.approved_at = now
            event_type = "tool.version.published.v1"
        else:
            tool_version.status = "REJECTED"
            event_type = "tool.version.rejected.v1"
        aggregate_type = "governed_tool_version"
        aggregate_id = str(tool_version.id)
        payload = {
            "tool_version_id": str(tool_version.id),
            "tool_id": str(tool_version.tool_id),
            "version": tool_version.version,
            "review_id": str(review.id),
        }
    elif review.object_type == "MODEL_ROUTE_CONFIGURATION":
        route = await session.get(ModelRouteConfiguration, UUID(review.object_id))
        if route is None or route.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if route.status != "PENDING_REVIEW":
            raise HTTPException(status_code=409, detail="model route is no longer pending review")
        if decision == "APPROVE":
            await session.execute(
                update(ModelRouteConfiguration)
                .where(
                    ModelRouteConfiguration.organization_id == route.organization_id,
                    ModelRouteConfiguration.route_key == route.route_key,
                    ModelRouteConfiguration.status == "APPROVED",
                    ModelRouteConfiguration.id != route.id,
                )
                .values(status="SUPERSEDED", updated_at=now)
            )
            route.status = "APPROVED"
            route.approved_by = context.principal_id
            route.approved_at = now
            event_type = "model_route.approved.v1"
        else:
            route.status = "REJECTED"
            event_type = "model_route.rejected.v1"
        aggregate_type = "model_route_configuration"
        aggregate_id = str(route.id)
        payload = {
            "model_route_id": str(route.id),
            "route_key": route.route_key,
            "version": route.version,
            "review_id": str(review.id),
        }
    elif review.object_type == "CONTEXT_PRODUCT_VERSION":
        product_version = await session.get(ContextProductVersion, UUID(review.object_id))
        if product_version is None or product_version.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if review.requested_action == "DEPRECATE":
            if product_version.status != "PUBLISHED":
                raise HTTPException(
                    status_code=409, detail="context product is no longer published"
                )
            if decision == "APPROVE":
                product_version.status = "DEPRECATED"
                event_type = "context.product_deprecated.v1"
            else:
                event_type = "context.product_deprecation_rejected.v1"
        elif product_version.status != "REVIEW_REQUIRED":
            raise HTTPException(status_code=409, detail="context product is no longer pending")
        elif decision == "APPROVE":
            await session.execute(
                update(ContextProductVersion)
                .where(
                    ContextProductVersion.product_id == product_version.product_id,
                    ContextProductVersion.status == "PUBLISHED",
                    ContextProductVersion.id != product_version.id,
                )
                .values(status="SUPERSEDED", updated_at=now)
            )
            product_version.status = "PUBLISHED"
            product_version.approved_by = context.principal_id
            product_version.approved_at = now
            product_version.published_at = now
            event_type = "context.product_published.v1"
        else:
            product_version.status = "REJECTED"
            event_type = "context.product_rejected.v1"
        aggregate_type = "context_product_version"
        aggregate_id = str(product_version.id)
        payload = {
            "context_product_version_id": str(product_version.id),
            "context_product_id": str(product_version.product_id),
            "version": product_version.version,
            "review_id": str(review.id),
        }
    elif review.object_type == "DATA_PRODUCT_VERSION":
        data_product_version = await session.get(DataProductVersion, UUID(review.object_id))
        if (
            data_product_version is None
            or data_product_version.organization_id != review.organization_id
        ):
            raise HTTPException(status_code=409, detail="review target is unavailable")
        data_product = await session.get(DataProduct, data_product_version.product_id)
        if data_product is None:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if review.requested_action == "RETIRE":
            if data_product_version.status != "PUBLISHED":
                raise HTTPException(status_code=409, detail="data product is no longer published")
            if decision == "APPROVE":
                data_product_version.status = "RETIRED"
                data_product.lifecycle_status = "RETIRED"
                event_type = "data_product.retired.v1"
            else:
                event_type = "data_product.retirement_rejected.v1"
        elif data_product_version.status != "REVIEW_REQUIRED":
            raise HTTPException(status_code=409, detail="data product is no longer pending")
        elif decision == "APPROVE":
            await session.execute(
                update(DataProductVersion)
                .where(
                    DataProductVersion.product_id == data_product_version.product_id,
                    DataProductVersion.status == "PUBLISHED",
                    DataProductVersion.id != data_product_version.id,
                )
                .values(status="SUPERSEDED", updated_at=now)
            )
            data_product_version.status = "PUBLISHED"
            data_product_version.approved_by = context.principal_id
            data_product_version.approved_at = now
            data_product_version.published_at = now
            data_product.lifecycle_status = "ACTIVE"
            event_type = "data_product.published.v1"
        else:
            data_product_version.status = "REJECTED"
            event_type = "data_product.rejected.v1"
        aggregate_type = "data_product_version"
        aggregate_id = str(data_product_version.id)
        payload = {
            "data_product_version_id": str(data_product_version.id),
            "data_product_id": str(data_product.id),
            "version": data_product_version.version,
            "review_id": str(review.id),
        }
    elif review.object_type == "DATA_CONTRACT_VERSION":
        contract_version = await session.get(DataContractVersion, UUID(review.object_id))
        if contract_version is None or contract_version.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if contract_version.status != "REVIEW_REQUIRED":
            raise HTTPException(status_code=409, detail="data contract is no longer pending")
        if decision == "APPROVE":
            await session.execute(
                update(DataContractVersion)
                .where(
                    DataContractVersion.product_id == contract_version.product_id,
                    DataContractVersion.status == "PUBLISHED",
                    DataContractVersion.id != contract_version.id,
                )
                .values(status="SUPERSEDED", updated_at=now)
            )
            contract_version.status = "PUBLISHED"
            contract_version.approved_by = context.principal_id
            contract_version.approved_at = now
            contract_version.published_at = now
            event_type = (
                "data_contract.breaking_exception_approved.v1"
                if review.requested_action == "PUBLISH_BREAKING_EXCEPTION"
                else "data_contract.published.v1"
            )
        else:
            contract_version.status = "REJECTED"
            event_type = "data_contract.rejected.v1"
        aggregate_type = "data_contract_version"
        aggregate_id = str(contract_version.id)
        payload = {
            "data_contract_version_id": str(contract_version.id),
            "data_product_id": str(contract_version.product_id),
            "version": contract_version.version,
            "compatibility_status": contract_version.compatibility_status,
            "review_id": str(review.id),
        }
    elif review.object_type == "DATA_PRODUCT_ACCESS_REQUEST":
        access_request = await session.get(DataProductAccessRequest, UUID(review.object_id))
        if access_request is None or access_request.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        try:
            approve_access_request(
                access_request,
                reviewer=context.principal_id,
                reason=reason,
                approved=decision == "APPROVE",
                now=now,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        event_type = (
            "data_product.access_granted.v1"
            if decision == "APPROVE"
            else "data_product.access_rejected.v1"
        )
        aggregate_type = "data_product_access_request"
        aggregate_id = str(access_request.id)
        payload = {
            "access_request_id": str(access_request.id),
            "data_product_version_id": str(access_request.data_product_version_id),
            "expires_at": access_request.expires_at.isoformat()
            if access_request.expires_at is not None
            else None,
            "review_id": str(review.id),
        }
    elif review.object_type == "AI_ASSET":
        ai_asset = await session.get(AiAsset, UUID(review.object_id))
        if ai_asset is None or ai_asset.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if ai_asset.lifecycle_status != "ACTIVE":
            raise HTTPException(status_code=409, detail="AI asset is no longer active")
        if decision == "APPROVE":
            ai_asset.lifecycle_status = "RETIRED"
            await session.execute(
                update(AiAssetVersion)
                .where(
                    AiAssetVersion.asset_id == ai_asset.id,
                    AiAssetVersion.status == "APPROVED",
                )
                .values(status="RETIRED", updated_at=now)
            )
            event_type = "ai_registry.asset_retired.v1"
        else:
            event_type = "ai_registry.asset_retirement_rejected.v1"
        aggregate_type = "ai_asset"
        aggregate_id = str(ai_asset.id)
        payload = {
            "ai_asset_id": str(ai_asset.id),
            "asset_kind": ai_asset.asset_kind,
            "review_id": str(review.id),
        }
    elif review.object_type == "AI_ASSET_VERSION":
        ai_version = await session.get(AiAssetVersion, UUID(review.object_id))
        if ai_version is None or ai_version.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        ai_asset = await session.get(AiAsset, ai_version.asset_id)
        if ai_asset is None or ai_version.status != "REVIEW_REQUIRED":
            raise HTTPException(status_code=409, detail="AI asset is no longer pending")
        if decision == "APPROVE":
            await session.execute(
                update(AiAssetVersion)
                .where(
                    AiAssetVersion.asset_id == ai_version.asset_id,
                    AiAssetVersion.status == "APPROVED",
                    AiAssetVersion.id != ai_version.id,
                )
                .values(status="SUPERSEDED", updated_at=now)
            )
            ai_version.status = "APPROVED"
            ai_version.approved_by = context.principal_id
            ai_version.approved_at = now
            event_type = "ai_registry.asset_approved.v1"
        else:
            ai_version.status = "REJECTED"
            event_type = "ai_registry.asset_rejected.v1"
        aggregate_type = "ai_asset_version"
        aggregate_id = str(ai_version.id)
        payload = {
            "ai_asset_version_id": str(ai_version.id),
            "ai_asset_id": str(ai_asset.id),
            "asset_kind": ai_asset.asset_kind,
            "version": ai_version.version,
            "review_id": str(review.id),
        }
    elif review.object_type == "METADATA_ENRICHMENT_PROPOSAL":
        proposal = await session.get(MetadataEnrichmentProposal, UUID(review.object_id))
        if proposal is None or proposal.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if proposal.status != "PENDING_REVIEW":
            raise HTTPException(status_code=409, detail="business semantics are no longer pending")
        proposal.reviewed_by = context.principal_id
        proposal.review_reason = reason
        proposal.reviewed_at = now
        if decision == "APPROVE":
            annotation = await apply_enrichment_proposal(
                session,
                proposal=proposal,
                reviewer=context.principal_id,
                approved_at=now,
            )
            await session.flush()
            event_type = "business_semantics.approved.v1"
            annotation_id = str(annotation.id)
        else:
            proposal.status = "REJECTED"
            event_type = "business_semantics.rejected.v1"
            annotation_id = None
        aggregate_type = "metadata_enrichment_proposal"
        aggregate_id = str(proposal.id)
        payload = {
            "proposal_id": str(proposal.id),
            "table_id": str(proposal.table_id),
            "annotation_id": annotation_id,
            "review_id": str(review.id),
        }
    elif review.object_type == "GLOSSARY_TERM_VERSION":
        term_version = await session.get(GlossaryTermVersion, UUID(review.object_id))
        if term_version is None or term_version.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if term_version.status != "REVIEW_REQUIRED":
            raise HTTPException(status_code=409, detail="glossary term is no longer pending")
        if decision == "APPROVE":
            await session.execute(
                update(GlossaryTermVersion)
                .where(
                    GlossaryTermVersion.term_id == term_version.term_id,
                    GlossaryTermVersion.status == "APPROVED",
                    GlossaryTermVersion.id != term_version.id,
                )
                .values(status="SUPERSEDED", updated_at=now)
            )
            term_version.status = "APPROVED"
            term_version.approved_by = context.principal_id
            term_version.approved_at = now
            event_type = "glossary.term.approved.v1"
        else:
            term_version.status = "REJECTED"
            event_type = "glossary.term.rejected.v1"
        aggregate_type = "glossary_term_version"
        aggregate_id = str(term_version.id)
        payload = {
            "term_version_id": str(term_version.id),
            "term_id": str(term_version.term_id),
            "version": term_version.version,
            "review_id": str(review.id),
        }
    elif review.object_type == "ASSET_DOCUMENTATION_VERSION":
        documentation_version = await session.get(AssetDocumentationVersion, UUID(review.object_id))
        if (
            documentation_version is None
            or documentation_version.organization_id != review.organization_id
        ):
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if documentation_version.status != "REVIEW_REQUIRED":
            raise HTTPException(status_code=409, detail="asset documentation is no longer pending")
        if decision == "APPROVE":
            await session.execute(
                update(AssetDocumentationVersion)
                .where(
                    AssetDocumentationVersion.documentation_id
                    == documentation_version.documentation_id,
                    AssetDocumentationVersion.status == "APPROVED",
                    AssetDocumentationVersion.id != documentation_version.id,
                )
                .values(status="SUPERSEDED", updated_at=now)
            )
            documentation_version.status = "APPROVED"
            documentation_version.approved_by = context.principal_id
            documentation_version.approved_at = now
            event_type = "asset.documentation.approved.v1"
        else:
            documentation_version.status = "REJECTED"
            event_type = "asset.documentation.rejected.v1"
        aggregate_type = "asset_documentation_version"
        aggregate_id = str(documentation_version.id)
        payload = {
            "documentation_version_id": str(documentation_version.id),
            "documentation_id": str(documentation_version.documentation_id),
            "version": documentation_version.version,
            "review_id": str(review.id),
        }
    elif review.object_type == "BULK_STEWARDSHIP_OPERATION":
        operation = await session.get(BulkStewardshipOperation, UUID(review.object_id))
        if operation is None or operation.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if operation.status != "REVIEW_REQUIRED":
            raise HTTPException(status_code=409, detail="bulk operation is no longer pending")
        if decision == "APPROVE":
            event_type, applied_count = await apply_bulk_operation(
                session,
                operation,
                reviewer=context.principal_id,
                now=now,
            )
        else:
            operation.status = "REJECTED"
            applied_count = 0
            event_type = "stewardship.bulk_operation_rejected.v1"
        aggregate_type = "bulk_stewardship_operation"
        aggregate_id = str(operation.id)
        payload = {
            "operation_id": str(operation.id),
            "operation_type": operation.operation_type,
            "subject_count": len(operation.subject_ids),
            "applied_count": applied_count,
            "review_id": str(review.id),
        }
    elif review.object_type == "GLOSSARY_CONFLICT":
        conflict = await session.get(GlossaryConflict, UUID(review.object_id))
        if conflict is None or conflict.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if decision == "APPROVE":
            event_type = await apply_conflict_resolution(
                conflict,
                reviewer=context.principal_id,
                now=now,
            )
        else:
            event_type = await reject_conflict_resolution(conflict)
        aggregate_type = "glossary_conflict"
        aggregate_id = str(conflict.id)
        payload = {
            "conflict_id": str(conflict.id),
            "resolution": conflict.proposed_resolution,
            "review_id": str(review.id),
        }
    elif review.object_type == "GLOSSARY_LINK_PROPOSAL":
        link_proposal = await session.get(GlossaryLinkProposal, UUID(review.object_id))
        if link_proposal is None or link_proposal.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if decision == "APPROVE":
            event_type = await apply_link_proposal(
                session,
                link_proposal,
                reviewer=context.principal_id,
                now=now,
            )
        else:
            event_type = await reject_link_proposal(
                link_proposal,
                reviewer=context.principal_id,
                now=now,
            )
        aggregate_type = "glossary_link_proposal"
        aggregate_id = str(link_proposal.id)
        payload = {
            "proposal_id": str(link_proposal.id),
            "table_id": str(link_proposal.table_id),
            "term_id": str(link_proposal.term_id),
            "confidence": link_proposal.confidence,
            "review_id": str(review.id),
        }
    elif review.object_type == "TERM_SEMANTIC_BINDING":
        binding = await session.get(TermSemanticBinding, UUID(review.object_id))
        if binding is None or binding.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if binding.status != "PENDING_APPROVAL":
            raise HTTPException(status_code=409, detail="binding is no longer pending review")
        if decision == "APPROVE":
            binding.status = "ACTIVE"
            binding.approved_by = context.principal_id
            binding.approved_at = now
            event_type = "semantic.term_binding_approved.v1"
        else:
            binding.status = "REJECTED"
            event_type = "semantic.term_binding_rejected.v1"
        aggregate_type = "term_semantic_binding"
        aggregate_id = str(binding.id)
        payload = {
            "binding_id": str(binding.id),
            "term_id": str(binding.term_id),
            "semantic_object_type": binding.semantic_object_type,
            "semantic_object_id": str(binding.semantic_object_id),
            "review_id": str(review.id),
        }
    elif review.object_type == "CROSS_BOUNDARY_GRANT":
        grant = await session.get(CrossBoundaryGrant, UUID(review.object_id))
        if grant is None or grant.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if grant.status != "PENDING_APPROVAL":
            raise HTTPException(status_code=409, detail="cross-boundary grant is no longer pending")
        if decision == "APPROVE":
            grant.status = "ACTIVE"
            grant.approved_by = context.principal_id
            grant.approved_at = now
            event_type = "cross_boundary_grant.approved.v1"
        else:
            grant.status = "REJECTED"
            event_type = "cross_boundary_grant.rejected.v1"
        aggregate_type = "cross_boundary_grant"
        aggregate_id = str(grant.id)
        payload = {
            "cross_boundary_grant_id": str(grant.id),
            "source_data_domain_id": str(grant.source_data_domain_id),
            "target_data_domain_id": str(grant.target_data_domain_id),
            "review_id": str(review.id),
        }
    elif review.object_type == "ASSET_DESCRIPTION_DRAFT":
        draft = await session.get(AssetDescriptionDraft, UUID(review.object_id))
        if draft is None or draft.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if decision == "APPROVE":
            # GL-9: this is the only call site that publishes a drafted
            # description onto the asset, and it only runs after the
            # maker-checker guard above (status PENDING, independent
            # reviewer) has already passed — no evidence score, however
            # high, reaches this line without an independent decision.
            event_type, published_version = await apply_asset_description_draft(
                session,
                draft,
                reviewer=context.principal_id,
                now=now,
            )
            published_version_id: str | None = str(published_version.id)
        else:
            event_type = await reject_asset_description_draft(
                draft,
                reviewer=context.principal_id,
                now=now,
            )
            published_version_id = None
        aggregate_type = "asset_description_draft"
        aggregate_id = str(draft.id)
        payload = {
            "draft_id": str(draft.id),
            "table_id": str(draft.table_id),
            "overall_score": draft.overall_score,
            "published_version_id": published_version_id,
            "review_id": str(review.id),
        }
    else:
        raise HTTPException(status_code=422, detail="unsupported governance object type")
    return event_type, aggregate_type, aggregate_id, payload


@router.post("/governance/reviews/{review_id}/decision", response_model=GovernanceReviewRead)
async def decide_governance_review(
    review_id: UUID,
    body: GovernanceDecisionRequest,
    context: SecurityContext = Depends(
        require_roles_or_delegated("PlatformAdmin", "DataSteward", "Reviewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    review = await session.scalar(
        select(GovernanceReview).where(GovernanceReview.id == review_id).with_for_update()
    )
    if review is None:
        raise HTTPException(status_code=404, detail="governance review not found")
    enforce_organization(context, review.organization_id)
    if review.status != "PENDING":
        raise HTTPException(status_code=409, detail="governance review is already decided")
    if review.requested_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker-checker separation is required")
    # PG-4: a delegate deciding this review under a delegated role must not be
    # able to rubber-stamp something the *delegator* itself proposed -- that
    # would be self-approval by proxy, defeating INV-8 through the back door
    # a delegation grant would otherwise open.
    if (
        context.active_delegator_principal_id is not None
        and review.requested_by == context.active_delegator_principal_id
    ):
        raise HTTPException(status_code=409, detail="maker-checker separation is required")
    now = datetime.now(UTC)
    event_type, aggregate_type, aggregate_id, payload = await _apply_governance_review_decision(
        session,
        review,
        decision=body.decision,
        reason=body.reason,
        context=context,
        now=now,
    )
    audit_context = replace(context, organization_id=review.organization_id)
    record_audit(
        session,
        audit_context,
        action="governance.review.decide",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "decision": body.decision,
            "object_id": review.object_id,
            "via_delegation_id": (
                str(context.active_delegation_id) if context.active_delegation_id else None
            ),
            "via_delegator_principal_id": context.active_delegator_principal_id,
        },
    )
    record_outbox(
        session,
        organization_id=review.organization_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="governance decision conflicted with concurrent state"
        ) from exc
    return review


# ---------------------------------------------------------------------------
# PG-3: bulk decisions with per-item rationale across the unified review
# queue, at 10,000-item scale.
# ---------------------------------------------------------------------------


async def _resolve_governance_review_bulk_subjects(
    session: AsyncSession,
    *,
    context: SecurityContext,
    review_ids: list[UUID] | None,
    selection_filter: GovernanceReviewBulkSelectionFilter | None,
) -> tuple[list[UUID], Literal["EXPLICIT", "FILTER"], bool]:
    """Resolve the bounded set of review ids a bulk decision applies to.

    Mirrors `_resolve_relationship_candidate_bulk_subjects` (RL-6): an
    explicit list is deduped and returned as-is (already bounded to
    GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS by the request schema); a
    filter reuses `list_governance_reviews`'s own filter shape -- status
    scoped to the caller's organization, plus an optional object_type -- with
    every predicate pushed into the SQL `WHERE` clause (never a Python-side
    scan of the whole table), ordered by `created_at` and capped at the same
    limit, reporting whether the cap actually truncated the match set.
    """
    if review_ids is not None:
        return list(dict.fromkeys(review_ids)), "EXPLICIT", False
    assert selection_filter is not None
    organization_id = context.require_organization()
    filters: list[ColumnElement[bool]] = [
        GovernanceReview.organization_id == organization_id,
        GovernanceReview.status == selection_filter.status.upper(),
    ]
    if selection_filter.object_type is not None:
        filters.append(GovernanceReview.object_type == selection_filter.object_type.upper())
    rows = list(
        (
            await session.scalars(
                select(GovernanceReview.id)
                .where(*filters)
                .order_by(GovernanceReview.created_at)
                .limit(GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS + 1)
            )
        ).all()
    )
    truncated = len(rows) > GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS
    ids = rows[:GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS]
    if not ids:
        raise HTTPException(status_code=409, detail="filter matched no governance reviews")
    return ids, "FILTER", truncated


@router.post(
    "/governance/reviews/bulk-decision",
    response_model=GovernanceReviewBulkDecisionResultRead,
)
async def bulk_decide_governance_reviews(
    body: GovernanceReviewBulkDecisionRequest,
    context: SecurityContext = Depends(
        require_roles_or_delegated("PlatformAdmin", "DataSteward", "Reviewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReviewBulkDecisionResultRead:
    """PG-3: decide up to GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS PENDING
    governance reviews in one call, across whichever object type(s) the
    unified review queue already supports, by explicit id list or by a
    status/object-type filter scoped to the caller's organization.

    Exactly the same maker != checker, PENDING-only, and organization-
    boundary rules as `decide_governance_review` apply per item -- this
    calls `_apply_governance_review_decision`, the same core the
    single-item endpoint calls, so the two paths cannot drift -- but a rule
    violation on one item marks that item FAILED and continues (RL-6/CT-1's
    partial-success precedent) rather than aborting the whole batch. Each
    item's dispatch runs inside its own SAVEPOINT (`session.begin_nested`),
    so a failure partway through one item's (possibly multi-table) side
    effects can never leak a partial write into an item reported FAILED --
    verified directly against a real SAVEPOINT rollback, not assumed.

    Selection is always a single bulk query: an explicit id list is deduped
    in Python then fetched with one `WHERE id IN (...)`, and a filter pushes
    every predicate into SQL via `_resolve_governance_review_bulk_subjects`.
    Nothing here loads the full review table into Python to filter it there.
    """
    subject_ids, selection_mode, truncated = await _resolve_governance_review_bulk_subjects(
        session,
        context=context,
        review_ids=body.review_ids,
        selection_filter=body.filter,
    )
    reviews = {
        row.id: row
        for row in (
            await session.scalars(
                select(GovernanceReview).where(GovernanceReview.id.in_(subject_ids))
            )
        ).all()
    }
    now = datetime.now(UTC)
    results: list[GovernanceReviewBulkDecisionItemRead] = []
    succeeded = 0
    for review_id in subject_ids:
        review = reviews.get(review_id)
        if review is None:
            results.append(
                GovernanceReviewBulkDecisionItemRead(
                    review_id=str(review_id),
                    status="FAILED",
                    reason="governance review not found",
                )
            )
            continue
        try:
            enforce_organization(context, review.organization_id)
        except HTTPException:
            results.append(
                GovernanceReviewBulkDecisionItemRead(
                    review_id=str(review_id),
                    status="FAILED",
                    reason="cross-organization access denied",
                )
            )
            continue
        if review.status != "PENDING":
            results.append(
                GovernanceReviewBulkDecisionItemRead(
                    review_id=str(review_id),
                    status="FAILED",
                    reason=f"governance review is already {review.status.lower()}",
                )
            )
            continue
        if review.requested_by == context.principal_id or (
            context.active_delegator_principal_id is not None
            and review.requested_by == context.active_delegator_principal_id
        ):
            results.append(
                GovernanceReviewBulkDecisionItemRead(
                    review_id=str(review_id),
                    status="FAILED",
                    reason="maker-checker separation is required",
                )
            )
            continue
        item_reason = (
            body.rationale_by_review_id.get(review_id) if body.rationale_by_review_id else None
        )
        if item_reason is None:
            item_reason = body.reason
        if body.decision == "REJECT" and not item_reason:
            results.append(
                GovernanceReviewBulkDecisionItemRead(
                    review_id=str(review_id),
                    status="FAILED",
                    reason="a rationale is required to reject this item",
                )
            )
            continue
        try:
            async with session.begin_nested():
                event_type, aggregate_type, aggregate_id, payload = (
                    await _apply_governance_review_decision(
                        session,
                        review,
                        decision=body.decision,
                        reason=item_reason,
                        context=context,
                        now=now,
                    )
                )
                record_outbox(
                    session,
                    organization_id=review.organization_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    event_type=event_type,
                    payload=payload,
                )
        except HTTPException as exc:
            results.append(
                GovernanceReviewBulkDecisionItemRead(
                    review_id=str(review_id), status="FAILED", reason=str(exc.detail)
                )
            )
            continue
        results.append(
            GovernanceReviewBulkDecisionItemRead(
                review_id=str(review_id), status="SUCCEEDED", reason=None
            )
        )
        succeeded += 1
    failed = len(results) - succeeded
    outcome = "SUCCESS" if not failed else "PARTIAL_SUCCESS" if succeeded else "FAILURE"
    record_audit(
        session,
        context,
        action="governance_review.bulk_decide",
        resource_type="governance_review",
        resource_id=None,
        outcome=outcome,
        correlation_id=get_correlation_id(),
        details={
            "decision": body.decision,
            "selection_mode": selection_mode,
            "requested_count": len(results),
            "succeeded_count": succeeded,
            "failed_count": failed,
            "truncated": truncated,
            "via_delegation_id": (
                str(context.active_delegation_id) if context.active_delegation_id else None
            ),
            "via_delegator_principal_id": context.active_delegator_principal_id,
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="bulk governance decision conflicted with concurrent state"
        ) from exc
    return GovernanceReviewBulkDecisionResultRead(
        decision=body.decision,
        selection_mode=selection_mode,
        requested_count=len(results),
        succeeded_count=succeeded,
        failed_count=failed,
        truncated=truncated,
        results=results,
    )
