import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    AssetDocumentationVersion,
    BulkStewardshipOperation,
    ContextProductVersion,
    DataSource,
    GlossaryConflict,
    GlossaryLinkProposal,
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
)
from aida.schemas import (
    GovernanceDecisionRequest,
    GovernanceReviewRead,
    Page,
    SemanticMetricCreate,
    SemanticMetricVersionRead,
    SemanticModelCloneRequest,
    SemanticModelVersionCreate,
    SemanticModelVersionRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles
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


@router.post("/governance/reviews/{review_id}/decision", response_model=GovernanceReviewRead)
async def decide_governance_review(
    review_id: UUID,
    body: GovernanceDecisionRequest,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataSteward", "Reviewer")),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    review = await session.get(GovernanceReview, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="governance review not found")
    enforce_organization(context, review.organization_id)
    if review.status != "PENDING":
        raise HTTPException(status_code=409, detail="governance review is already decided")
    if review.requested_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker-checker separation is required")
    now = datetime.now(UTC)
    review.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    review.decided_by = context.principal_id
    review.decision_reason = body.reason
    review.decided_at = now
    if review.object_type == "SEMANTIC_MODEL_VERSION":
        model = await session.get(SemanticModelVersion, UUID(review.object_id))
        if model is None or model.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if body.decision == "APPROVE":
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
            if body.decision == "APPROVE":
                tool_version.status = "DEPRECATED"
                event_type = "tool.version.deprecated.v1"
            else:
                event_type = "tool.version.deprecation_rejected.v1"
        elif body.decision == "APPROVE":
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
        if body.decision == "APPROVE":
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
        if (
            product_version is None
            or product_version.organization_id != review.organization_id
        ):
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if product_version.status != "REVIEW_REQUIRED":
            raise HTTPException(status_code=409, detail="context product is no longer pending")
        if body.decision == "APPROVE":
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
    elif review.object_type == "METADATA_ENRICHMENT_PROPOSAL":
        proposal = await session.get(MetadataEnrichmentProposal, UUID(review.object_id))
        if proposal is None or proposal.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if proposal.status != "PENDING_REVIEW":
            raise HTTPException(status_code=409, detail="business semantics are no longer pending")
        proposal.reviewed_by = context.principal_id
        proposal.review_reason = body.reason
        proposal.reviewed_at = now
        if body.decision == "APPROVE":
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
        if body.decision == "APPROVE":
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
        if body.decision == "APPROVE":
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
        if body.decision == "APPROVE":
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
        if body.decision == "APPROVE":
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
        if body.decision == "APPROVE":
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
    else:
        raise HTTPException(status_code=422, detail="unsupported governance object type")
    audit_context = replace(context, organization_id=review.organization_id)
    record_audit(
        session,
        audit_context,
        action="governance.review.decide",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": body.decision, "object_id": review.object_id},
    )
    record_outbox(
        session,
        organization_id=review.organization_id,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
    )
    await session.commit()
    return review
