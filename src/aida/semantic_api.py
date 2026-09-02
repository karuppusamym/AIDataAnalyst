import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aida.agent_eval_gate import (
    DEFAULT_AGENT_EVAL_GATE_THRESHOLD,
    compute_agent_eval_gate,
    record_agent_eval_gate_evidence,
    stored_steward_verdicts,
)
from aida.asset_description_service import (
    apply_asset_description_draft,
    reject_asset_description_draft,
)
from aida.classification_propagation import apply_classification_promotion
from aida.config import Settings, get_settings
from aida.consumer_footer import ConsumerFooterRead, compose_consumer_footer
from aida.context import get_correlation_id
from aida.db import get_session
from aida.document_ingestion import apply_document_claim, reject_document_claim
from aida.events import record_audit, record_outbox
from aida.metric_formula_signature import find_formula_collisions
from aida.metric_suggestion_service import (
    apply_metric_suggestion_proposal,
    reject_metric_suggestion_proposal,
)
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
    DocumentClaim,
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
    SemanticMetricProposal,
    SemanticMetricVersion,
    SemanticModelVersion,
    TermSemanticBinding,
)
from aida.product_marketplace_api import approve_access_request
from aida.retrieval import hybrid_retrieve_cross_source
from aida.schemas import (
    GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS,
    ApiModel,
    GlobalSearchHitRead,
    GlobalSearchResponse,
    GlossaryConflictRead,
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
from aida.semantic_diff import ChangeKind, diff_semantic_object
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


@router.get(
    "/semantic-model-versions/{model_id}/consumers",
    response_model=ConsumerFooterRead,
)
async def get_semantic_model_version_consumers(
    model_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> ConsumerFooterRead:
    """UX-18: the consumer footer for one semantic model version -- who/what
    currently consumes *this exact version*, from CX-4 consumption lineage
    (`aida.consumer_footer`), so a steward opening it for edit is never
    blind to its downstream impact. `resource_id` is `model_id` itself
    (each version is its own row), matching the `resource_type=
    "semantic_model_version"` convention this module's own `record_audit`
    calls already use.
    """
    model = await session.get(SemanticModelVersion, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="semantic model version not found")
    enforce_organization(context, model.organization_id)
    return await compose_consumer_footer(
        session,
        organization_id=model.organization_id,
        resource_type="semantic_model_version",
        resource_id=str(model.id),
        version=model.version,
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


@router.get(
    "/semantic-metric-versions/{version_id}/consumers",
    response_model=ConsumerFooterRead,
)
async def get_semantic_metric_version_consumers(
    version_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> ConsumerFooterRead:
    """UX-18: the consumer footer for one semantic metric version. See
    `get_semantic_model_version_consumers` above -- same composition, same
    version-specific `resource_id` convention, scoped here to
    `resource_type="semantic_metric_version"`.
    """
    version = await session.get(SemanticMetricVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="semantic metric version not found")
    enforce_organization(context, version.organization_id)
    return await compose_consumer_footer(
        session,
        organization_id=version.organization_id,
        resource_type="semantic_metric_version",
        resource_id=str(version.id),
        version=version.version,
    )


METRIC_FORMULA_COLLISION_TYPE = "METRIC_FORMULA_COLLISION"
# Scan/creation caps mirror `stewardship_api.detect_glossary_conflicts`
# exactly (same bounded-batch rationale: a governance-queue detector must
# never turn one call into an unbounded write).
_METRIC_COLLISION_SCAN_LIMIT = 5000
_METRIC_COLLISION_CREATE_LIMIT = 100


@router.post(
    "/organizations/{organization_id}/metric-conflicts/detect",
    response_model=Page,
)
async def detect_metric_formula_collisions(
    organization_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """AT-17: raise a governance conflict for two *different* published
    metrics that compute the same thing via the same (or grain-normalized
    same) formula -- GL-3's own detect-on-demand pattern
    (`stewardship_api.detect_glossary_conflicts`), mirrored for metric
    formulas instead of glossary-term synonyms.

    Reuses `GlossaryConflict` as-is rather than adding a parallel
    conflict-tracking table: `term_id` is already nullable (checked in
    `aida.models` before writing this), and neither
    `stewardship_service.apply_conflict_resolution` nor
    `reject_conflict_resolution` -- the maker-checker resolution GL-3 built,
    reached identically through `semantic_api.decide_governance_review`'s
    existing `GLOSSARY_CONFLICT` branch -- reference `term_id` at all. A
    metric-formula collision is stored with `term_id=None`,
    `conflict_type="METRIC_FORMULA_COLLISION"`, and both metrics' identity
    and formula fields in `position_a`/`position_b`, so "losing position
    retained" holds for metrics exactly as it does for glossary terms: a
    resolution never deletes either side's recorded position.

    The comparison itself is `aida.metric_formula_signature`
    (`find_formula_collisions`), a pure DB-free function -- see its
    docstring for precisely what "collision" means here and its honest
    limit (exact/grain-normalized structural duplication of `(aggregation,
    source_table_id, measure_column_id, default_time_column_id, grain)`,
    not general algebraic formula equivalence, which this schema's
    single-aggregation metric shape cannot even pose).
    """
    enforce_organization(context, organization_id)
    rows = (
        await session.execute(
            select(SemanticMetricVersion, SemanticMetric)
            .join(SemanticMetric, SemanticMetric.id == SemanticMetricVersion.metric_id)
            .where(
                SemanticMetricVersion.organization_id == organization_id,
                SemanticMetricVersion.status == "PUBLISHED",
            )
            .limit(_METRIC_COLLISION_SCAN_LIMIT)
        )
    ).all()
    versions_by_metric_version_id = {str(version.id): version for version, _metric in rows}
    snapshots = [
        {
            "metric_version_id": version.id,
            "metric_id": metric.id,
            "metric_name": version.name,
            "aggregation": version.aggregation,
            "source_table_id": version.source_table_id,
            "measure_column_id": version.measure_column_id,
            "default_time_column_id": version.default_time_column_id,
            "grain": version.grain,
        }
        for version, metric in rows
    ]
    existing_rows = (
        await session.scalars(
            select(GlossaryConflict).where(
                GlossaryConflict.organization_id == organization_id,
                GlossaryConflict.status.in_(("OPEN", "REVIEW_REQUIRED")),
                GlossaryConflict.conflict_type == METRIC_FORMULA_COLLISION_TYPE,
            )
        )
    ).all()
    existing_pairs = {
        tuple(sorted((row.position_a.get("metric_id", ""), row.position_b.get("metric_id", ""))))
        for row in existing_rows
    }
    created: list[GlossaryConflict] = []
    for collision in find_formula_collisions(snapshots):
        pair = tuple(sorted((collision.left.metric_id, collision.right.metric_id)))
        if pair in existing_pairs:
            continue
        left_version = versions_by_metric_version_id[collision.left.metric_version_id]
        right_version = versions_by_metric_version_id[collision.right.metric_version_id]
        conflict = GlossaryConflict(
            organization_id=organization_id,
            term_id=None,
            conflict_type=METRIC_FORMULA_COLLISION_TYPE,
            position_a={
                "metric_id": collision.left.metric_id,
                "metric_version_id": collision.left.metric_version_id,
                "metric_name": collision.left.metric_name,
                "aggregation": collision.left.aggregation,
                "source_table_id": collision.left.source_table_id,
                "measure_column_id": collision.left.measure_column_id,
                "default_time_column_id": collision.left.default_time_column_id,
                "grain": collision.left.grain_raw,
                "created_by": left_version.created_by,
                "match_kind": collision.match_kind,
            },
            position_b={
                "metric_id": collision.right.metric_id,
                "metric_version_id": collision.right.metric_version_id,
                "metric_name": collision.right.metric_name,
                "aggregation": collision.right.aggregation,
                "source_table_id": collision.right.source_table_id,
                "measure_column_id": collision.right.measure_column_id,
                "default_time_column_id": collision.right.default_time_column_id,
                "grain": collision.right.grain_raw,
                "created_by": right_version.created_by,
                "match_kind": collision.match_kind,
            },
            assigned_owner=left_version.created_by,
            raised_by=context.principal_id,
        )
        session.add(conflict)
        created.append(conflict)
        existing_pairs.add(pair)
        if len(created) == _METRIC_COLLISION_CREATE_LIMIT:
            break
    await session.flush()
    for conflict in created:
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="glossary_conflict",
            aggregate_id=str(conflict.id),
            event_type="semantic.metric_conflict_raised.v1",
            payload={"conflict_id": str(conflict.id), "conflict_type": conflict.conflict_type},
        )
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="semantic.metric_conflict.detect",
        resource_type="glossary_conflict",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"published_metrics_scanned": len(rows), "conflicts_created": len(created)},
    )
    await session.commit()
    return Page(
        items=[GlossaryConflictRead.model_validate(row) for row in created],
        limit=_METRIC_COLLISION_CREATE_LIMIT,
        offset=0,
        total=len(created),
    )


@router.get("/organizations/{organization_id}/metric-conflicts", response_model=Page)
async def list_metric_formula_collisions(
    organization_id: UUID,
    conflict_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """AT-17 read side -- `stewardship_api.list_glossary_conflicts`, scoped to
    `conflict_type="METRIC_FORMULA_COLLISION"` rows only.
    """
    enforce_organization(context, organization_id)
    filters = [
        GlossaryConflict.organization_id == organization_id,
        GlossaryConflict.conflict_type == METRIC_FORMULA_COLLISION_TYPE,
    ]
    if conflict_status:
        filters.append(GlossaryConflict.status == conflict_status.upper())
    total = await session.scalar(select(func.count()).select_from(GlossaryConflict).where(*filters))
    rows = (
        await session.scalars(
            select(GlossaryConflict)
            .where(*filters)
            .order_by(GlossaryConflict.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[GlossaryConflictRead.model_validate(row) for row in rows],
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


# ---------------------------------------------------------------------------
# SM-7: structured version diffs for the governance review queue
#
# `diff_semantic_object` (aida.semantic_diff) is the pure, DB-free diff
# engine -- see its module docstring. Everything below is the DB-facing half:
# turning a pending review's target row(s) into plain-dict snapshots and
# finding the currently published sibling version to diff against. Only the
# object types with an established DRAFT/PUBLISHED-style version lineage are
# supported today (SEMANTIC_MODEL_VERSION, GLOSSARY_TERM_VERSION); the
# governance queue itself spans many more object types (see
# `_apply_governance_review_decision` below), and a review for one of those
# still returns 200 with `diffable=False` and an explanatory `message` rather
# than a 404 or 422 -- the endpoint is meant to be safe to call for *any*
# pending review a reviewer is looking at, not just the two supported kinds.
# ---------------------------------------------------------------------------


class SemanticFieldDeltaRead(ApiModel):
    """One field-level difference, as returned to a reviewer.

    Mirrors `aida.semantic_diff.FieldDelta` field-for-field; kept as its own
    response model (rather than reusing the dataclass directly) so this
    endpoint's wire shape is independent of the pure module's internals.
    """

    field: str
    change: ChangeKind
    before: Any = None
    after: Any = None


class GovernanceReviewDiffRead(ApiModel):
    """Structured version delta for one pending (or decided) governance review."""

    review_id: UUID
    object_type: str
    object_id: str
    diffable: bool
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    entries: list[SemanticFieldDeltaRead] = Field(default_factory=list)
    message: str | None = None


async def _semantic_model_version_snapshot(
    session: AsyncSession, model_id: UUID
) -> dict[str, Any] | None:
    """Flatten one `SemanticModelVersion` plus its `SemanticMetricVersion`s
    into a plain-dict snapshot, keyed so a reviewer can see exactly what
    changed at the model level and per metric (see `diff_semantic_object`'s
    docstring on why metrics are keyed by slug rather than listed).
    """
    model = await session.get(SemanticModelVersion, model_id)
    if model is None:
        return None
    rows = (
        await session.execute(
            select(SemanticMetricVersion, SemanticMetric)
            .join(SemanticMetric, SemanticMetricVersion.metric_id == SemanticMetric.id)
            .where(SemanticMetricVersion.semantic_model_version_id == model.id)
        )
    ).all()
    return {
        "name": model.name,
        "change_summary": model.change_summary,
        "metrics": {
            metric.slug: {
                "name": metric_version.name,
                "description": metric_version.description,
                "aggregation": metric_version.aggregation,
                "grain": metric_version.grain,
                "source_table_id": str(metric_version.source_table_id),
                "measure_column_id": (
                    str(metric_version.measure_column_id)
                    if metric_version.measure_column_id
                    else None
                ),
                "default_time_column_id": (
                    str(metric_version.default_time_column_id)
                    if metric_version.default_time_column_id
                    else None
                ),
                "allowed_dimension_column_ids": sorted(
                    metric_version.allowed_dimension_column_ids
                ),
            }
            for metric_version, metric in rows
        },
    }


async def _published_semantic_model_version_id(
    session: AsyncSession, model: SemanticModelVersion
) -> UUID | None:
    published = await session.scalar(
        select(SemanticModelVersion.id).where(
            SemanticModelVersion.project_id == model.project_id,
            SemanticModelVersion.status == "PUBLISHED",
            SemanticModelVersion.id != model.id,
        )
    )
    return published


async def _glossary_term_version_snapshot(
    session: AsyncSession, version_id: UUID
) -> dict[str, Any] | None:
    version = await session.get(GlossaryTermVersion, version_id)
    if version is None:
        return None
    return {
        "display_name": version.display_name,
        "definition": version.definition,
        "synonyms": sorted(version.synonyms),
        "owner_principal": version.owner_principal,
    }


async def _published_glossary_term_version_id(
    session: AsyncSession, version: GlossaryTermVersion
) -> UUID | None:
    published = await session.scalar(
        select(GlossaryTermVersion.id).where(
            GlossaryTermVersion.term_id == version.term_id,
            GlossaryTermVersion.status == "APPROVED",
            GlossaryTermVersion.id != version.id,
        )
    )
    return published


async def compose_governance_review_diff(
    session: AsyncSession, review: GovernanceReview
) -> GovernanceReviewDiffRead:
    """The DB-facing half of SM-7 for one already-fetched review: turns its
    target row(s) into plain-dict snapshots and calls `diff_semantic_object`
    (`aida.semantic_diff`) directly -- no reimplementation of the diff itself.

    Factored out of `get_governance_review_diff` (below) so a caller that
    already has a batch of reviews on hand -- UX-17's `review_queue_read_model`
    composing a whole run's proposals in one response -- can reuse the exact
    same object-type dispatch and fallback wording per review, rather than
    forking it, so the two surfaces cannot disagree on what is diffable and
    what a diff looks like for a given review.
    """
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    message: str | None = None

    if review.object_type == "SEMANTIC_MODEL_VERSION":
        model = await session.get(SemanticModelVersion, UUID(review.object_id))
        if model is None:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        after = await _semantic_model_version_snapshot(session, model.id)
        published_id = await _published_semantic_model_version_id(session, model)
        before = (
            await _semantic_model_version_snapshot(session, published_id)
            if published_id is not None
            else {}
        )
    elif review.object_type == "GLOSSARY_TERM_VERSION":
        term_version = await session.get(GlossaryTermVersion, UUID(review.object_id))
        if term_version is None:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        after = await _glossary_term_version_snapshot(session, term_version.id)
        published_id = await _published_glossary_term_version_id(session, term_version)
        before = (
            await _glossary_term_version_snapshot(session, published_id)
            if published_id is not None
            else {}
        )
    else:
        message = (
            f"structured diffs are not yet available for {review.object_type}; "
            "the raw proposed object is still reachable through its own read endpoint"
        )

    diff = diff_semantic_object(before, after) if after is not None else None
    return GovernanceReviewDiffRead(
        review_id=review.id,
        object_type=review.object_type,
        object_id=review.object_id,
        diffable=diff is not None,
        before=before,
        after=after,
        entries=[
            SemanticFieldDeltaRead(
                field=entry.field,
                change=entry.change,
                before=entry.before,
                after=entry.after,
            )
            for entry in (diff.entries if diff is not None else [])
        ],
        message=message,
    )


@router.get(
    "/governance/reviews/{review_id}/diff",
    response_model=GovernanceReviewDiffRead,
)
async def get_governance_review_diff(
    review_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward", "Reviewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReviewDiffRead:
    """The structured version delta for one governance review, alongside
    (not instead of) the raw proposed content -- SM-7, "reviewers see version
    deltas". `before` is the currently published version's snapshot (`{}` if
    the object has never been published before, e.g. a brand-new metric),
    `after` is the proposed version's snapshot, and `entries` is the
    field-level diff between them.
    """
    review = await session.get(GovernanceReview, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="governance review not found")
    enforce_organization(context, review.organization_id)
    return await compose_governance_review_diff(session, review)


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
            # AT-7(a): explicit early retirement -- a steward can retire a
            # still-current PUBLISHED version, or cut a SUPPORTED version's
            # support window short, rather than waiting it out.
            if product_version.status not in ("PUBLISHED", "SUPPORTED"):
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
            # AT-7(a)/AT-D1: the version being replaced does not jump straight
            # to fully-hidden SUPERSEDED in this same transaction -- it enters
            # SUPPORTED for its own configured support window (that version's
            # own `support_window_days`; `None` means supported until someone
            # explicitly retires it), during which a version-pinned consumer
            # can still read it. Discovery/`tools_list` keeps surfacing only
            # the new PUBLISHED version as current -- unchanged, since those
            # paths already filter to status == "PUBLISHED" only.
            prior_support_window_days = await session.scalar(
                select(ContextProductVersion.support_window_days).where(
                    ContextProductVersion.product_id == product_version.product_id,
                    ContextProductVersion.status == "PUBLISHED",
                    ContextProductVersion.id != product_version.id,
                )
            )
            support_window_ends_at = (
                None
                if prior_support_window_days is None
                else now + timedelta(days=prior_support_window_days)
            )
            await session.execute(
                update(ContextProductVersion)
                .where(
                    ContextProductVersion.product_id == product_version.product_id,
                    ContextProductVersion.status == "PUBLISHED",
                    ContextProductVersion.id != product_version.id,
                )
                .values(
                    status="SUPPORTED",
                    updated_at=now,
                    superseded_at=now,
                    superseded_by_version_id=product_version.id,
                    support_window_ends_at=support_window_ends_at,
                )
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
        eval_gate_verdict: str | None = None
        if decision == "APPROVE":
            # N15: an AGENT-kind AiAssetVersion may not move to APPROVED
            # (its published/production state) unless its evaluation gate
            # currently shows PASS -- see aida.agent_eval_gate's module
            # docstring for the full design and the honest org-wide scoping
            # this reuses from UX-19. Runs live, on every APPROVE decision
            # (single or bulk -- both paths call this function), so a stale
            # or manufactured evidence blob can never let a publish through:
            # the CONFIRMED_RUN half is always recomputed fresh here from the
            # organization's real, current confirmed-run corpus.
            if ai_asset.asset_kind == "AGENT":
                gate_result = await compute_agent_eval_gate(
                    session,
                    organization_id=ai_version.organization_id,
                    extra_verdicts=stored_steward_verdicts(ai_version),
                    threshold=DEFAULT_AGENT_EVAL_GATE_THRESHOLD,
                )
                eval_gate_verdict = gate_result.verdict
                if gate_result.verdict != "PASS":
                    # Deliberately *not* persisted here: a raise this deep
                    # in `_apply_governance_review_decision` unwinds without
                    # a commit in the single-decision path, and rolls back
                    # inside a SAVEPOINT in the bulk path (see
                    # `bulk_decide_governance_reviews`'s own docstring) --
                    # exactly like every other precondition failure already
                    # raised elsewhere in this function. The blocked-attempt
                    # reason is still fully evidenced in this exception's own
                    # detail (verdict, pass rate, named failing exemplars);
                    # `GET .../eval-gate` recomputes the identical live result
                    # for a steward to inspect before retrying, with no
                    # side effect and nothing lost by not persisting a
                    # rolled-back write.
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "agent evaluation gate did not pass "
                            f"({gate_result.verdict}): {gate_result.reason}"
                        ),
                    )
                # Only a PASS survives to the final commit -- recorded here,
                # right alongside the approval it justified, into the exact
                # `evaluation_evidence` field `ai_registry.
                # compute_ai_trust_score` already reads, via the existing
                # `record_audit` trail (never a parallel one).
                record_agent_eval_gate_evidence(
                    session,
                    ai_version,
                    gate_result,
                    context=replace(context, organization_id=ai_version.organization_id),
                    stage="PUBLISH",
                )
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
            "eval_gate_verdict": eval_gate_verdict,
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
    elif review.object_type == "DOCUMENT_CLAIM":
        claim = await session.get(DocumentClaim, UUID(review.object_id))
        if claim is None or claim.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if decision == "APPROVE":
            event_type = await apply_document_claim(claim, reviewer=context.principal_id, now=now)
        else:
            event_type = await reject_document_claim(claim, reviewer=context.principal_id, now=now)
        aggregate_type = "document_claim"
        aggregate_id = str(claim.id)
        payload = {
            "claim_id": str(claim.id),
            "document_section_id": str(claim.document_section_id),
            "subject_type": claim.subject_type,
            "subject_id": claim.subject_id,
            "review_id": str(review.id),
        }
    elif review.object_type == "SEMANTIC_METRIC_PROPOSAL":
        metric_proposal = await session.get(SemanticMetricProposal, UUID(review.object_id))
        if metric_proposal is None or metric_proposal.organization_id != review.organization_id:
            raise HTTPException(status_code=409, detail="review target is unavailable")
        if decision == "APPROVE":
            # SM-4: this is the only call site that publishes a proposed
            # metric definition, and it only runs after the maker-checker
            # guard above (status PENDING, independent reviewer) has
            # already passed -- no evidence score, however high, reaches
            # this line without an independent decision.
            event_type, published_metric_version = await apply_metric_suggestion_proposal(
                session,
                metric_proposal,
                reviewer=context.principal_id,
                now=now,
            )
            published_metric_version_id: str | None = str(published_metric_version.id)
        else:
            event_type = await reject_metric_suggestion_proposal(
                metric_proposal,
                reviewer=context.principal_id,
                now=now,
            )
            published_metric_version_id = None
        aggregate_type = "semantic_metric_proposal"
        aggregate_id = str(metric_proposal.id)
        payload = {
            "proposal_id": str(metric_proposal.id),
            "table_id": str(metric_proposal.table_id),
            "measure_column_id": str(metric_proposal.measure_column_id),
            "overall_score": metric_proposal.overall_score,
            "published_metric_version_id": published_metric_version_id,
            "review_id": str(review.id),
        }
    elif review.object_type == "COLUMN_CLASSIFICATION_PROMOTION":
        # AT-11: promoting a lineage-derived classification to the asserted
        # (policy-enforced) value. The apply function re-checks the raise-only
        # guard and appends the derived provenance as ClassificationEvidence;
        # the maker != checker / PENDING-only guards above already ran, so no
        # derived value reaches assertion without an independent decision.
        event_type, aggregate_type, aggregate_id, payload = await apply_classification_promotion(
            session, review, decision=decision, context=context, now=now
        )
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


# ---------------------------------------------------------------------------
# --- GROUP A: RT-5 (API half) / RT-9 -- cross-source global search ---
# ---------------------------------------------------------------------------
#
# `search_api.py`'s `GET /v1/search` (RT-4/RT-5, module 12/21) is the
# lightweight, lexical-only, command-palette-facing global search -- it
# already spans every datasource in an org, but only on a `ts_query`-style
# text match, with a single flat "lexical" evidence factor. It stays exactly
# as-is here (that module belongs to a different owner in this delivery
# slice; not edited).
#
# `aida.retrieval.hybrid_retrieve_enhanced` (RT-1/RT-2/RT-3, this same
# module's neighbour) is the richer engine -- lexical + vector + graph +
# quality/usage fusion with a fully inspectable per-factor evidence trail --
# but until now it only ever took a single `DataSource`, so nothing exposed
# it as a genuine *cross-source* search. `hybrid_retrieve_cross_source`
# (RT-9) closes that gap in `retrieval.py`; this endpoint is its API surface
# (RT-5's "API half" -- the palette UI itself is a different group's row).
# The two endpoints are complementary, not duplicates: this one trades
# `/v1/search`'s lower latency for a fully-ranked, evidence-rich answer.


@router.get(
    "/organizations/{organization_id}/global-search",
    response_model=GlobalSearchResponse,
)
async def global_semantic_search(
    organization_id: UUID,
    q: str = Query(min_length=1, max_length=500, description="Search query"),
    project_id: UUID | None = Query(default=None, description="Optional project filter"),
    datasource_ids: list[UUID] | None = Query(
        default=None, description="Optional explicit datasource scope"
    ),
    limit: int = Query(default=25, ge=1, le=100),
    fusion_method: Literal["rrf", "weighted_linear"] = Query(default="rrf"),
    include_vector: bool = Query(default=True),
    include_graph: bool = Query(default=True),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "SemanticAdmin", "DataSteward", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GlobalSearchResponse:
    """RT-9: one query, genuinely spanning every datasource in scope, ranked
    by the same lexical + vector + graph + quality/usage fusion pipeline
    `hybrid_retrieve_enhanced` uses within one datasource (RT-1/RT-2/RT-3).

    Policy filtering happens before any ranking runs: `enforce_organization`
    gates the whole call, and the candidate datasource set itself is always
    a query scoped to `organization_id` (narrowed further by `project_id`/
    `datasource_ids` when supplied) -- never a caller-supplied source list
    taken on faith.
    """
    enforce_organization(context, organization_id)

    datasource_stmt = select(DataSource).where(DataSource.organization_id == organization_id)
    if project_id is not None:
        datasource_stmt = datasource_stmt.where(DataSource.project_id == project_id)
    if datasource_ids:
        datasource_stmt = datasource_stmt.where(DataSource.id.in_(datasource_ids))
    datasources = (await session.scalars(datasource_stmt.limit(200))).all()

    hits = await hybrid_retrieve_cross_source(
        session,
        organization_id=organization_id,
        datasources=list(datasources),
        question=q,
        settings=settings,
        fusion_method=fusion_method,
        include_vector=include_vector,
        include_graph=include_graph,
        limit=limit,
    )

    items = [
        GlobalSearchHitRead(
            object_type=hit.object_type,
            object_id=hit.object_id,
            display_name=hit.display_name,
            score=hit.score,
            datasource_id=UUID(hit.metadata["datasource_id"]),
            reason_codes=hit.reason_codes,
            evidence=hit.metadata.get("retrieval_evidence", {}),
            metadata={k: v for k, v in hit.metadata.items() if k != "retrieval_evidence"},
        )
        for hit in hits
    ]

    return GlobalSearchResponse(
        items=items,
        total=len(items),
        datasource_count=len(datasources),
        limit=limit,
        fusion_method=fusion_method,
        vector_enabled=include_vector,
        graph_enabled=include_graph,
    )
