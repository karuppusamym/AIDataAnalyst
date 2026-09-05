import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot import exp, parse_one

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.edition_entitlements import evaluate_entitlement
from aida.events import record_audit, record_outbox
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled
from aida.models import (
    AgentRun,
    DataSource,
    GovernanceReview,
    GovernedTool,
    GovernedToolVersion,
    Project,
    QueryExecution,
    SemanticModelVersion,
    ToolCertificationCase,
    ToolCertificationRun,
    ToolExecution,
)
from aida.multi_table_blueprint import (
    MultiTableBlueprintError,
    UnjoinableTablesError,
    build_multi_table_blueprint,
    resolve_blueprint_tables_and_edges,
)
from aida.quality_coupling import check_tool_gate, fetch_open_incidents, resolve_table_ids
from aida.query_gateway import GatewayResult, QueryExecutionGateway, QueryRejected
from aida.schemas import (
    ApiModel,
    GovernanceReviewRead,
    GovernedToolVersionCreate,
    GovernedToolVersionRead,
    Page,
    QueryExecutionResponse,
    ToolCertificationCaseCreate,
    ToolCertificationCaseRead,
    ToolCertificationDecisionRequest,
    ToolCertificationRunCreate,
    ToolCertificationRunRead,
    ToolCertificationStatusRead,
    ToolDeprecationDependentContextProductRead,
    ToolDeprecationDependentToolRead,
    ToolDeprecationImpactRead,
    ToolExecutionRequest,
    ToolExecutionResponse,
    ToolParameterDefinition,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.sql_guard import SqlGuard
from aida.tool_certification import (
    CERTIFICATION_SUITE_VERSION,
    certification_is_active,
    corpus_fingerprint,
    run_certification_corpus,
)
from aida.tool_impact import DeprecationImpact, compute_deprecation_impact
from aida.tool_rendering import ToolParameterError, render_tool_sql, template_placeholders
from aida.tool_usage import DEFAULT_USAGE_LOOKBACK_DAYS
from aida.view_tool_blueprint import (
    ViewNotEligibleError,
    ViewToolBlueprintError,
    build_view_tool_blueprint,
    resolve_view_tool_source,
)

router = APIRouter(prefix="/v1", tags=["governed-tools"])

CERTIFICATION_MAKER_ROLES = ("PlatformAdmin", "ToolDeveloper", "SemanticAdmin")
CERTIFICATION_CHECKER_ROLES = ("PlatformAdmin", "Reviewer", "SemanticAdmin")
CERTIFICATION_READ_ROLES = (
    "PlatformAdmin",
    "ToolDeveloper",
    "SemanticAdmin",
    "Analyst",
    "AgentDeveloper",
    "Reviewer",
    "Viewer",
    "Auditor",
)


def _tool_read(
    tool: GovernedTool, version: GovernedToolVersion, *, usage_count: int = 0
) -> GovernedToolVersionRead:
    return GovernedToolVersionRead(
        id=version.id,
        tool_id=tool.id,
        organization_id=version.organization_id,
        project_id=tool.project_id,
        slug=tool.slug,
        version=version.version,
        status=version.status,
        name=version.name,
        description=version.description,
        datasource_id=version.datasource_id,
        semantic_model_version_id=version.semantic_model_version_id,
        sql_template=version.sql_template,
        referenced_tables=version.referenced_tables,
        parameters=[
            ToolParameterDefinition.model_validate(value) for value in version.parameter_schema
        ],
        allowed_roles=version.allowed_roles,
        fingerprint=version.fingerprint,
        created_by=version.created_by,
        approved_by=version.approved_by,
        approved_at=version.approved_at,
        created_at=version.created_at,
        updated_at=version.updated_at,
        usage_count=usage_count,
    )


def _impact_read(
    tool: GovernedTool, version: GovernedToolVersion, impact: DeprecationImpact
) -> ToolDeprecationImpactRead:
    return ToolDeprecationImpactRead(
        tool_version_id=version.id,
        tool_id=tool.id,
        slug=tool.slug,
        version=version.version,
        status=version.status,
        dependency_tables=list(impact.dependency_tables),
        downstream_nodes=list(impact.downstream_nodes),
        downstream_truncated=impact.downstream_truncated,
        dependent_tool_versions=[
            ToolDeprecationDependentToolRead(
                tool_version_id=item.tool_version_id,
                tool_id=item.tool_id,
                slug=item.slug,
                version=item.version,
                name=item.name,
                shared_table_count=item.shared_table_count,
            )
            for item in impact.dependent_tool_versions
        ],
        dependent_context_products=[
            ToolDeprecationDependentContextProductRead(
                context_product_version_id=item.context_product_version_id,
                product_id=item.product_id,
                product_key=item.product_key,
                version=item.version,
                name=item.name,
                reason=item.reason,
            )
            for item in impact.dependent_context_products
        ],
        active_consumer_count=impact.active_consumer_count,
        recent_execution_count=impact.recent_execution_count,
        lookback_days=impact.lookback_days,
        requested_depth=impact.requested_depth,
        node_limit=impact.node_limit,
        total_blast_radius=impact.total_blast_radius,
    )


def _impact_summary(impact: DeprecationImpact) -> dict[str, int | bool]:
    """Compact, audit/outbox-safe summary of a `DeprecationImpact` -- counts
    only, no node/table identifiers, so the immutable evidence trail stays
    proportionate to an audit detail payload rather than duplicating the
    full preview response."""
    return {
        "downstream_node_count": len(impact.downstream_nodes),
        "downstream_truncated": impact.downstream_truncated,
        "dependent_tool_version_count": len(impact.dependent_tool_versions),
        "dependent_context_product_count": len(impact.dependent_context_products),
        "active_consumer_count": impact.active_consumer_count,
        "recent_execution_count": impact.recent_execution_count,
        "total_blast_radius": impact.total_blast_radius,
    }


def _query_response(result: GatewayResult) -> QueryExecutionResponse:
    execution = result.execution
    return QueryExecutionResponse(
        execution_id=execution.id,
        status=execution.status,
        normalized_sql=execution.normalized_sql or "",
        referenced_tables=execution.referenced_tables,
        referenced_columns=execution.referenced_columns,
        column_lineage=execution.column_lineage,
        plan_cost=execution.plan_cost or 0.0,
        warehouse_query_id=execution.warehouse_query_id,
        row_count=execution.row_count or 0,
        elapsed_ms=execution.elapsed_ms or 0,
        masked_columns=list(result.masked_columns),
        rows=list(result.rows),
    )


async def _persist_tool_version_draft(
    project: Project,
    datasource: DataSource,
    body: GovernedToolVersionCreate,
    *,
    context: SecurityContext,
    session: AsyncSession,
    settings: Settings,
) -> GovernedToolVersionRead:
    """The shared draft-creation tail: validate `body.sql_template` the same
    way regardless of whether it was hand-authored (`create_tool_version`)
    or generated (`create_multi_table_tool_blueprint`), then persist a new
    `GovernedToolVersion` in ``DRAFT`` status. Publication is unaffected by
    which path created the draft -- both go through the same
    `submit_tool_for_review` maker-checker flow afterwards.
    """
    definitions = body.parameters
    declared = {definition.name for definition in definitions}
    try:
        placeholders = template_placeholders(body.sql_template, dialect=datasource.dialect)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="tool SQL template cannot be parsed") from exc
    if placeholders != declared:
        raise HTTPException(
            status_code=422,
            detail="SQL placeholders must exactly match parameter definitions",
        )
    guard = SqlGuard(
        default_row_limit=settings.default_query_row_limit,
        hard_row_limit=settings.hard_query_row_limit,
    )
    validation = guard.validate(body.sql_template, dialect=datasource.dialect)
    if not validation.valid or not validation.normalized_sql:
        raise HTTPException(
            status_code=422,
            detail=f"invalid governed tool SQL: {', '.join(validation.violations)}",
        )
    gateway = QueryExecutionGateway(settings)
    allowed_tables = await gateway.allowed_tables(session, datasource)
    unauthorized = sorted(
        table for table in validation.referenced_tables if table.lower() not in allowed_tables
    )
    if unauthorized:
        raise HTTPException(
            status_code=422,
            detail=f"unknown or unauthorized tool tables: {', '.join(unauthorized)}",
        )

    tool = await session.scalar(
        select(GovernedTool).where(
            GovernedTool.project_id == project.id,
            GovernedTool.slug == body.slug,
        )
    )
    if tool is None:
        tool = GovernedTool(
            organization_id=project.organization_id,
            project_id=project.id,
            slug=body.slug,
        )
        session.add(tool)
        await session.flush()
    latest = await session.scalar(
        select(func.max(GovernedToolVersion.version)).where(GovernedToolVersion.tool_id == tool.id)
    )
    fingerprint_payload = {
        "name": body.name,
        "description": body.description,
        "datasource_id": str(body.datasource_id),
        "semantic_model_version_id": (
            str(body.semantic_model_version_id) if body.semantic_model_version_id else None
        ),
        "sql_template": validation.normalized_sql,
        "referenced_tables": sorted(validation.referenced_tables),
        "parameters": [definition.model_dump(mode="json") for definition in definitions],
        "allowed_roles": sorted(body.allowed_roles),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    version = GovernedToolVersion(
        organization_id=project.organization_id,
        tool_id=tool.id,
        version=(latest or 0) + 1,
        name=body.name,
        description=body.description,
        datasource_id=datasource.id,
        semantic_model_version_id=body.semantic_model_version_id,
        sql_template=validation.normalized_sql,
        referenced_tables=sorted(validation.referenced_tables),
        parameter_schema=[definition.model_dump(mode="json") for definition in definitions],
        allowed_roles=sorted(body.allowed_roles),
        fingerprint=fingerprint,
        created_by=context.principal_id,
    )
    session.add(version)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=project.organization_id),
        action="tool.version.create",
        resource_type="governed_tool_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"tool_slug": tool.slug, "version": version.version},
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="governed_tool_version",
        aggregate_id=str(version.id),
        event_type="tool.version.draft_created.v1",
        payload={
            "tool_version_id": str(version.id),
            "tool_id": str(tool.id),
            "project_id": str(project.id),
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="tool version conflict; retry") from exc
    return _tool_read(tool, version)


async def _load_project_and_datasource(
    session: AsyncSession,
    context: SecurityContext,
    project_id: UUID,
    datasource_id: UUID,
) -> tuple[Project, DataSource]:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    datasource = await session.get(DataSource, datasource_id)
    if (
        datasource is None
        or datasource.project_id != project.id
        or datasource.organization_id != project.organization_id
    ):
        raise HTTPException(status_code=422, detail="datasource is outside this project")
    return project, datasource


@router.post(
    "/projects/{project_id}/tools",
    response_model=GovernedToolVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_tool_version(
    project_id: UUID,
    body: GovernedToolVersionCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "SemanticAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GovernedToolVersionRead:
    project, datasource = await _load_project_and_datasource(
        session, context, project_id, body.datasource_id
    )
    if body.semantic_model_version_id:
        semantic_model = await session.get(SemanticModelVersion, body.semantic_model_version_id)
        if (
            semantic_model is None
            or semantic_model.project_id != project.id
            or semantic_model.status != "PUBLISHED"
        ):
            raise HTTPException(
                status_code=422,
                detail="tool semantic model must be published and belong to this project",
            )
    return await _persist_tool_version_draft(
        project, datasource, body, context=context, session=session, settings=settings
    )


class MultiTableToolBlueprintRequest(ApiModel):
    """SM-5: request a deterministically-rendered multi-table JOIN tool
    draft instead of hand-authoring `sql_template`. Everything below mirrors
    `GovernedToolVersionCreate`'s equivalent fields exactly (same
    slug/name/description/allowed_roles constraints) -- only `sql_template`
    and `parameters` are replaced with `table_ids`, since those two are
    generated from the tables' declared/approved relationships rather than
    written by hand.
    """

    slug: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=3, max_length=4000)
    datasource_id: UUID
    semantic_model_version_id: UUID | None = None
    table_ids: list[UUID] = Field(min_length=2, max_length=8)
    allowed_roles: list[str] = Field(min_length=1, max_length=100)


@router.post(
    "/projects/{project_id}/tool-blueprints/multi-table",
    response_model=GovernedToolVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_multi_table_tool_blueprint(
    project_id: UUID,
    body: MultiTableToolBlueprintRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "SemanticAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GovernedToolVersionRead:
    """SM-5: deterministically render a candidate multi-table JOIN tool from
    `table_ids` and the relationships already declared/approved between them
    (database foreign keys, or reviewer-approved `RelationshipCandidate`
    rows -- see `multi_table_blueprint.py`), then persist it as an ordinary
    ``DRAFT`` `GovernedToolVersion` through the exact same validation and
    persistence path `create_tool_version` uses. Never guesses a join: two
    selected tables with no declared relationship between them make the
    whole request fail with 422 rather than fabricating a join key.
    Publication still requires `submit_tool_for_review` and independent
    approval, unchanged.
    """
    # PG-5: multi-table blueprint authoring is "Studio (semantic + tool
    # authoring)" in Docs/00-product/07-packaging-and-editions.md §3 --
    # Enterprise floor. Checked before any DB work, same as the role gate
    # above it (`require_roles` already refused an ineligible role before this
    # line runs at all).
    entitlement = evaluate_entitlement(
        organization_edition=settings.edition,
        capability="studio_semantic_and_tool_authoring",
    )
    if not entitlement.allowed:
        record_audit(
            session,
            context,
            action="tool_blueprint.entitlement_denied",
            resource_type="governed_tool_version",
            resource_id=None,
            outcome="DENIED",
            correlation_id=get_correlation_id(),
            details=entitlement.snapshot(),
        )
        await session.commit()
        raise HTTPException(status_code=403, detail=entitlement.reason_code)

    project, datasource = await _load_project_and_datasource(
        session, context, project_id, body.datasource_id
    )
    if body.semantic_model_version_id:
        semantic_model = await session.get(SemanticModelVersion, body.semantic_model_version_id)
        if (
            semantic_model is None
            or semantic_model.project_id != project.id
            or semantic_model.status != "PUBLISHED"
        ):
            raise HTTPException(
                status_code=422,
                detail="tool semantic model must be published and belong to this project",
            )
    try:
        tables, edges = await resolve_blueprint_tables_and_edges(
            session,
            organization_id=project.organization_id,
            datasource_id=datasource.id,
            table_ids=body.table_ids,
        )
        blueprint = build_multi_table_blueprint(tables, edges, dialect=datasource.dialect)
    except UnjoinableTablesError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "cannot join the selected tables: no declared or approved relationship "
                f"connects: {', '.join(exc.unreachable_tables)}"
            ),
        ) from exc
    except MultiTableBlueprintError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    create_body = GovernedToolVersionCreate(
        slug=body.slug,
        name=body.name,
        description=body.description,
        datasource_id=body.datasource_id,
        semantic_model_version_id=body.semantic_model_version_id,
        sql_template=blueprint.sql_template,
        parameters=list(blueprint.parameters),
        allowed_roles=body.allowed_roles,
    )
    return await _persist_tool_version_draft(
        project, datasource, create_body, context=context, session=session, settings=settings
    )


class ViewToolBlueprintRequest(ApiModel):
    """N11: request a deterministically-rendered single-view tool draft
    instead of hand-authoring `sql_template`. A database VIEW is already a
    human-authored, pre-curated query, which makes it the highest-quality-
    per-unit-of-effort source for auto-generating a governed tool -- unlike
    SM-5's `MultiTableToolBlueprintRequest` (a mechanical FK-join, not a
    curated one), this path never re-derives the view's own SELECT/JOIN/
    aggregation logic; it only adds a governed, parameterized read surface
    on top of it (`SELECT <view's own column list> FROM <view> WHERE
    <optional equality filters>` -- see `view_tool_blueprint.py`). Mirrors
    `MultiTableToolBlueprintRequest`'s fields exactly, replacing
    `table_ids`/generated-join with a single `table_id` naming the view.
    """

    slug: str = Field(pattern=r"^[a-z][a-z0-9_]{1,99}$")
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=3, max_length=4000)
    datasource_id: UUID
    semantic_model_version_id: UUID | None = None
    table_id: UUID
    allowed_roles: list[str] = Field(min_length=1, max_length=100)


@router.post(
    "/projects/{project_id}/tool-blueprints/from-view",
    response_model=GovernedToolVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_view_tool_blueprint(
    project_id: UUID,
    body: ViewToolBlueprintRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "SemanticAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GovernedToolVersionRead:
    """N11: view -> tool ("tool generator B"). Resolves `table_id` as a
    view -- refusing outright (422) unless it carries a captured, `PARSED`,
    non-quarantined `MetadataViewDefinition`, mirroring `mcp_server.py`'s
    AT-19 `_view_definition_transformation_detail` gate exactly: a tool
    whose underlying logic cannot be shown to a reviewer cannot responsibly
    be published, even in draft. Then builds the candidate SQL template
    deterministically from the view's own output columns
    (`view_tool_blueprint.build_view_tool_blueprint`) and persists it as an
    ordinary ``DRAFT`` `GovernedToolVersion` through the exact same
    validation and persistence path `create_tool_version` and SM-5's
    `create_multi_table_tool_blueprint` use. Publication still requires
    `submit_tool_for_review` and independent approval, unchanged.
    """
    # PG-5: generative tool-blueprint authoring is "Studio (semantic + tool
    # authoring)" in Docs/00-product/07-packaging-and-editions.md §3 --
    # Enterprise floor, same gate `create_multi_table_tool_blueprint` applies.
    entitlement = evaluate_entitlement(
        organization_edition=settings.edition,
        capability="studio_semantic_and_tool_authoring",
    )
    if not entitlement.allowed:
        record_audit(
            session,
            context,
            action="tool_blueprint.entitlement_denied",
            resource_type="governed_tool_version",
            resource_id=None,
            outcome="DENIED",
            correlation_id=get_correlation_id(),
            details=entitlement.snapshot(),
        )
        await session.commit()
        raise HTTPException(status_code=403, detail=entitlement.reason_code)

    project, datasource = await _load_project_and_datasource(
        session, context, project_id, body.datasource_id
    )
    if body.semantic_model_version_id:
        semantic_model = await session.get(SemanticModelVersion, body.semantic_model_version_id)
        if (
            semantic_model is None
            or semantic_model.project_id != project.id
            or semantic_model.status != "PUBLISHED"
        ):
            raise HTTPException(
                status_code=422,
                detail="tool semantic model must be published and belong to this project",
            )
    try:
        view_source = await resolve_view_tool_source(
            session,
            organization_id=project.organization_id,
            datasource_id=datasource.id,
            table_id=body.table_id,
        )
        blueprint = build_view_tool_blueprint(view_source, dialect=datasource.dialect)
    except ViewNotEligibleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ViewToolBlueprintError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    create_body = GovernedToolVersionCreate(
        slug=body.slug,
        name=body.name,
        description=body.description,
        datasource_id=body.datasource_id,
        semantic_model_version_id=body.semantic_model_version_id,
        sql_template=blueprint.sql_template,
        parameters=list(blueprint.parameters),
        allowed_roles=body.allowed_roles,
    )
    return await _persist_tool_version_draft(
        project, datasource, create_body, context=context, session=session, settings=settings
    )


@router.get("/projects/{project_id}/tools", response_model=Page)
async def list_tools(
    project_id: UUID,
    tool_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin",
            "ToolDeveloper",
            "SemanticAdmin",
            "Analyst",
            "AgentDeveloper",
            "Viewer",
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    filters = [
        GovernedTool.organization_id == project.organization_id,
        GovernedTool.project_id == project.id,
    ]
    if tool_status:
        filters.append(GovernedToolVersion.status == tool_status.upper())

    # TL-4: usage-weighted ranking. Popularity is counted per *tool* (every
    # version's completed executions over a bounded lookback window, the
    # same signal `aida.tool_usage.get_tool_usage_counts` exposes to the MCP
    # `tools/list` catalog) rather than per version, so a newly-published
    # version of a heavily-used tool doesn't fall to the bottom of the list.
    # Joined in and ordered on at the SQL level -- not resolved in Python
    # after the fact -- so the ranking holds across pages, not just within
    # one already-fetched page.
    since = datetime.now(UTC) - timedelta(days=DEFAULT_USAGE_LOOKBACK_DAYS)
    usage_subquery = (
        select(
            GovernedToolVersion.tool_id.label("tool_id"),
            func.count(ToolExecution.id).label("usage_count"),
        )
        .join(ToolExecution, ToolExecution.tool_version_id == GovernedToolVersion.id)
        .where(
            GovernedToolVersion.organization_id == project.organization_id,
            ToolExecution.organization_id == project.organization_id,
            ToolExecution.status == "COMPLETED",
            ToolExecution.created_at >= since,
        )
        .group_by(GovernedToolVersion.tool_id)
        .subquery()
    )
    usage_count_column = func.coalesce(usage_subquery.c.usage_count, 0)
    base = (
        select(GovernedToolVersion, GovernedTool, usage_count_column)
        .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
        .outerjoin(usage_subquery, usage_subquery.c.tool_id == GovernedTool.id)
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(
                usage_count_column.desc(),
                GovernedTool.slug,
                GovernedToolVersion.version.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[
            _tool_read(tool, version, usage_count=usage_count)
            for version, tool, usage_count in rows
        ],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/tool-versions/{version_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_tool_for_review(
    version_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "SemanticAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    version = await session.get(GovernedToolVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="tool version not found")
    enforce_organization(context, version.organization_id)
    if version.status == "REVIEW_REQUIRED":
        existing = await session.scalar(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "GOVERNED_TOOL_VERSION",
                GovernanceReview.object_id == str(version.id),
                GovernanceReview.status == "PENDING",
            )
        )
        if existing:
            return existing
    if version.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only a draft tool can be submitted")
    review = GovernanceReview(
        organization_id=version.organization_id,
        object_type="GOVERNED_TOOL_VERSION",
        object_id=str(version.id),
        requested_action="PUBLISH",
        requested_by=context.principal_id,
    )
    session.add(review)
    version.status = "REVIEW_REQUIRED"
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action="tool.version.submit",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"tool_version_id": str(version.id)},
    )
    record_outbox(
        session,
        organization_id=version.organization_id,
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


@router.post(
    "/tool-versions/{version_id}/deprecation-submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_tool_deprecation(
    version_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "SemanticAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GovernanceReview:
    version = await session.get(GovernedToolVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="tool version not found")
    enforce_organization(context, version.organization_id)
    if version.status == "DEPRECATED":
        raise HTTPException(status_code=409, detail="tool version is already deprecated")
    if version.status != "PUBLISHED":
        raise HTTPException(status_code=409, detail="only a published tool can be deprecated")
    existing = await session.scalar(
        select(GovernanceReview).where(
            GovernanceReview.object_type == "GOVERNED_TOOL_VERSION",
            GovernanceReview.object_id == str(version.id),
            GovernanceReview.requested_action == "DEPRECATE",
            GovernanceReview.status == "PENDING",
        )
    )
    if existing:
        return existing

    # TL-7: compute the deprecation blast radius *before* the deprecation
    # review is even created, and record it as immutable evidence on this
    # real submission -- not a standalone report nobody consults. The
    # checker deciding this review (`semantic_api.py`'s generic governance
    # decision handler) can pull this audit row; a maker submitting the
    # deprecation sees the same evidence in this endpoint's own response.
    tool = await session.get(GovernedTool, version.tool_id)
    datasource = await session.get(DataSource, version.datasource_id)
    if tool is None or datasource is None:
        raise HTTPException(status_code=409, detail="tool dependency is unavailable")
    impact = await compute_deprecation_impact(
        session,
        tool=tool,
        version=version,
        datasource=datasource,
        settings=settings,
    )

    review = GovernanceReview(
        organization_id=version.organization_id,
        object_type="GOVERNED_TOOL_VERSION",
        object_id=str(version.id),
        requested_action="DEPRECATE",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action="tool.version.deprecation.submit",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "tool_version_id": str(version.id),
            "deprecation_impact": _impact_summary(impact),
        },
    )
    record_outbox(
        session,
        organization_id=version.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": review.object_id,
            "deprecation_impact": _impact_summary(impact),
            "requested_action": review.requested_action,
        },
    )
    await session.commit()
    return review


@router.get(
    "/tool-versions/{version_id}/deprecation-impact",
    response_model=ToolDeprecationImpactRead,
)
async def get_tool_deprecation_impact(
    version_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "SemanticAdmin", "Reviewer", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ToolDeprecationImpactRead:
    """TL-7: preview the blast radius of deprecating this tool version,
    computed fresh against live data -- read-only, no state change, so a
    maker can call this any number of times before deciding whether to
    submit the deprecation at all.
    """
    version = await session.get(GovernedToolVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="tool version not found")
    enforce_organization(context, version.organization_id)
    tool = await session.get(GovernedTool, version.tool_id)
    datasource = await session.get(DataSource, version.datasource_id)
    if tool is None or datasource is None:
        raise HTTPException(status_code=409, detail="tool dependency is unavailable")
    impact = await compute_deprecation_impact(
        session,
        tool=tool,
        version=version,
        datasource=datasource,
        settings=settings,
    )
    return _impact_read(tool, version, impact)


class AnalysisToolBlueprintRead(ApiModel):
    project_id: UUID
    definition: GovernedToolVersionCreate
    parameter_review_required: bool


@router.post("/agent-runs/{run_id}/tool-blueprint", response_model=AnalysisToolBlueprintRead)
async def prepare_analysis_tool(
    run_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "ToolDeveloper", "SemanticAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AnalysisToolBlueprintRead:
    run = await session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Analysis run not found")
    enforce_organization(context, run.organization_id)
    if "PlatformAdmin" not in context.roles and run.principal_id != context.principal_id:
        raise HTTPException(status_code=403, detail="Only the analysis author can prepare its tool")
    query = (
        await session.get(QueryExecution, run.query_execution_id)
        if run.query_execution_id
        else None
    )
    if (
        run.status != "COMPLETED"
        or query is None
        or query.status != "COMPLETED"
        or not query.normalized_sql
    ):
        raise HTTPException(
            status_code=409, detail="A successful analysis with stored SQL is required"
        )
    datasource = await session.get(DataSource, run.datasource_id)
    if datasource is None:
        raise HTTPException(status_code=409, detail="Analysis datasource is unavailable")
    statement = parse_one(query.normalized_sql, read=datasource.dialect)
    # Stored SQL contains redacted literals, including LIMIT. Never invent their values.
    # The ordinary gateway will enforce its row cap on the reusable operation.
    statement.set("limit", None)
    parameters = []
    for index, placeholder in enumerate(list(statement.find_all(exp.Placeholder)), 1):
        name = f"value_{index}"
        placeholder.replace(exp.Placeholder(this=name))
        parameters.append(
            ToolParameterDefinition(name=name, parameter_type="STRING", required=True)
        )
    return AnalysisToolBlueprintRead(
        project_id=datasource.project_id,
        definition=GovernedToolVersionCreate(
            slug=f"analysis_{run.id.hex[:12]}",
            name="Reusable analysis",
            description=(
                f"Draft from completed analysis {run.id}. Parameter types require author review."
            ),
            datasource_id=datasource.id,
            sql_template=statement.sql(dialect=datasource.dialect),
            parameters=parameters,
            allowed_roles=["Analyst", "ToolConsumer"],
        ),
        parameter_review_required=bool(parameters),
    )


@router.post("/tool-versions/{version_id}/execute", response_model=ToolExecutionResponse)
async def execute_tool(
    version_id: UUID,
    body: ToolExecutionRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "Analyst", "AgentDeveloper", "ToolConsumer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ToolExecutionResponse:
    return await execute_tool_version(version_id, body, context, session, settings)


async def execute_tool_version(
    version_id: UUID,
    body: ToolExecutionRequest,
    context: SecurityContext,
    session: AsyncSession,
    settings: Settings,
) -> ToolExecutionResponse:
    """Shared governed execution path for HTTP callers and persisted tool plans."""
    if context.roles.isdisjoint({"PlatformAdmin", "Analyst", "AgentDeveloper", "ToolConsumer"}):
        raise HTTPException(status_code=403, detail="tool execution role is required")
    version = await session.get(GovernedToolVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="tool version not found")
    enforce_organization(context, version.organization_id)
    if version.status != "PUBLISHED":
        raise HTTPException(status_code=409, detail="only a published tool can execute")
    if "PlatformAdmin" not in context.roles and context.roles.isdisjoint(version.allowed_roles):
        raise HTTPException(status_code=403, detail="tool role binding denied execution")
    tool = await session.get(GovernedTool, version.tool_id)
    datasource = await session.get(DataSource, version.datasource_id)
    if tool is None or datasource is None:
        raise HTTPException(status_code=409, detail="tool dependency is unavailable")
    try:
        ensure_datasource_enabled(datasource)
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # TL-3: gate execution on open quality incidents against the tool's own
    # declared dependencies (`version.referenced_tables`, authorised at
    # tool-version creation time) -- resolved to this datasource's tables and
    # checked before a single row of SQL is rendered or sent to the warehouse.
    execution_context = replace(context, organization_id=version.organization_id)
    dependency_table_ids = await resolve_table_ids(
        session, datasource=datasource, table_names=version.referenced_tables
    )
    dependency_incidents = await fetch_open_incidents(
        session, datasource=datasource, table_ids=list(dependency_table_ids.values())
    )
    quality_gate = check_tool_gate(
        tool_id=str(tool.id),
        dependency_asset_ids=[str(table_id) for table_id in dependency_table_ids.values()],
        incidents=dependency_incidents,
    )
    if quality_gate.action == "BLOCK":
        record_audit(
            session,
            execution_context,
            action="tool.execute",
            resource_type="governed_tool_version",
            resource_id=str(version.id),
            outcome="DENIED",
            correlation_id=get_correlation_id(),
            details={
                "reason": "QUALITY_INCIDENT_BLOCK",
                "message": quality_gate.message,
                "affected_assets": quality_gate.affected_assets,
            },
        )
        await session.commit()
        raise HTTPException(status_code=409, detail=quality_gate.message)

    try:
        rendered = render_tool_sql(
            version.sql_template,
            dialect=datasource.dialect,
            definitions=[
                ToolParameterDefinition.model_validate(value) for value in version.parameter_schema
            ],
            values=body.parameters,
        )
    except ToolParameterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    parameter_fingerprint = hmac.new(
        settings.audit_hmac_key.encode("utf-8"),
        json.dumps(
            rendered.normalized_parameters,
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    tool_execution = ToolExecution(
        organization_id=version.organization_id,
        tool_version_id=version.id,
        principal_id=context.principal_id,
        parameter_fingerprint=parameter_fingerprint,
    )
    session.add(tool_execution)
    await session.flush()
    semantic_version = (
        f"semantic-model:{version.semantic_model_version_id}"
        if version.semantic_model_version_id
        else None
    )
    gateway = QueryExecutionGateway(settings)
    try:
        result = await gateway.execute(
            session,
            datasource=datasource,
            context=execution_context,
            correlation_id=get_correlation_id(),
            sql=rendered.sql,
            requested_limit=body.max_rows,
            semantic_version=semantic_version,
        )
    except QueryRejected as exc:
        tool_execution.status = "REJECTED"
        tool_execution.query_execution_id = exc.execution_id
        tool_execution.error_message = str(exc)[:1000]
        await session.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        tool_execution.status = "FAILED"
        tool_execution.error_message = "tool query execution failed"
        await session.commit()
        raise HTTPException(status_code=502, detail="tool execution failed") from exc
    tool_execution.status = "COMPLETED"
    tool_execution.query_execution_id = result.execution.id
    record_audit(
        session,
        execution_context,
        action="tool.execute",
        resource_type="tool_execution",
        resource_id=str(tool_execution.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "tool_version_id": str(version.id),
            "query_execution_id": str(result.execution.id),
            "quality_gate_action": quality_gate.action,
        },
    )
    record_outbox(
        session,
        organization_id=version.organization_id,
        aggregate_type="tool_execution",
        aggregate_id=str(tool_execution.id),
        event_type="tool.execution.completed.v1",
        payload={
            "tool_execution_id": str(tool_execution.id),
            "tool_version_id": str(version.id),
            "query_execution_id": str(result.execution.id),
        },
    )
    await session.commit()
    return ToolExecutionResponse(
        tool_execution_id=tool_execution.id,
        tool_version_id=version.id,
        tool_slug=tool.slug,
        tool_version=version.version,
        execution=_query_response(result),
        quality_gate=(
            {
                "action": quality_gate.action,
                "affected_assets": quality_gate.affected_assets,
                "message": quality_gate.message,
            }
            if quality_gate.action != "ALLOW"
            else None
        ),
    )


# ---------------------------------------------------------------------------
# TL-1: tool certification corpus and workflow.
#
# A maker executes the tool's certification corpus against a published
# version's real parameter-binding path (deterministic, DB-free evidence);
# an independent checker then countersigns it into an active certification
# with an expiry. Expired certifications stop counting as certified without
# their evidence row ever being mutated, and recertification is simply a new
# run -- prior runs are never deleted, so certification history is complete.
# ---------------------------------------------------------------------------


@router.post(
    "/tools/{tool_id}/certification-cases",
    response_model=ToolCertificationCaseRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_tool_certification_case(
    tool_id: UUID,
    body: ToolCertificationCaseCreate,
    context: SecurityContext = Depends(require_roles(*CERTIFICATION_MAKER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ToolCertificationCase:
    tool = await session.get(GovernedTool, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    enforce_organization(context, tool.organization_id)
    case = ToolCertificationCase(
        organization_id=tool.organization_id,
        tool_id=tool.id,
        case_key=body.case_key,
        description=body.description,
        parameters=body.parameters,
        expectation=body.expectation.model_dump(mode="json"),
        created_by=context.principal_id,
    )
    session.add(case)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=tool.organization_id),
        action="tool.certification_case.create",
        resource_type="tool_certification_case",
        resource_id=str(case.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"tool_id": str(tool.id), "case_key": case.case_key},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="certification case key already exists for this tool"
        ) from exc
    return case


@router.get("/tools/{tool_id}/certification-cases", response_model=Page)
async def list_tool_certification_cases(
    tool_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CERTIFICATION_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    tool = await session.get(GovernedTool, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    enforce_organization(context, tool.organization_id)
    filters = (ToolCertificationCase.tool_id == tool.id,)
    total = await session.scalar(
        select(func.count()).select_from(ToolCertificationCase).where(*filters)
    )
    rows = (
        await session.scalars(
            select(ToolCertificationCase)
            .where(*filters)
            .order_by(ToolCertificationCase.case_key)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[ToolCertificationCaseRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/tool-versions/{version_id}/certification-runs",
    response_model=ToolCertificationRunRead,
    status_code=status.HTTP_201_CREATED,
)
async def execute_tool_certification(
    version_id: UUID,
    body: ToolCertificationRunCreate,
    context: SecurityContext = Depends(require_roles(*CERTIFICATION_MAKER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ToolCertificationRun:
    version = await session.get(GovernedToolVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="tool version not found")
    enforce_organization(context, version.organization_id)
    if version.status != "PUBLISHED":
        raise HTTPException(
            status_code=409, detail="only a published tool version can be certified"
        )
    if body.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="certification expiry must be in the future")
    datasource = await session.get(DataSource, version.datasource_id)
    if datasource is None:
        raise HTTPException(status_code=409, detail="tool datasource is unavailable")
    cases = (
        await session.scalars(
            select(ToolCertificationCase).where(
                ToolCertificationCase.tool_id == version.tool_id,
                ToolCertificationCase.status == "ACTIVE",
            )
        )
    ).all()
    if not cases:
        raise HTTPException(
            status_code=422,
            detail="tool has no active certification corpus cases defined",
        )
    definitions = [
        ToolParameterDefinition.model_validate(value) for value in version.parameter_schema
    ]
    run_status, score, passed, total, results = run_certification_corpus(
        list(cases),
        sql_template=version.sql_template,
        dialect=datasource.dialect,
        definitions=definitions,
    )
    run = ToolCertificationRun(
        organization_id=version.organization_id,
        tool_id=version.tool_id,
        tool_version_id=version.id,
        suite_version=CERTIFICATION_SUITE_VERSION,
        corpus_fingerprint=corpus_fingerprint(list(cases)),
        status=run_status,
        total_cases=total,
        passed_cases=passed,
        score=score,
        results=results,
        rationale=body.rationale,
        executed_by=context.principal_id,
        expires_at=body.expires_at if run_status == "PENDING_REVIEW" else None,
    )
    session.add(run)
    await session.flush()
    if run_status == "PENDING_REVIEW":
        session.add(
            GovernanceReview(
                organization_id=version.organization_id,
                object_type="TOOL_CERTIFICATION_RUN",
                object_id=str(run.id),
                requested_action="CERTIFY",
                requested_by=context.principal_id,
            )
        )
    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action="tool.certification_run.execute",
        resource_type="tool_certification_run",
        resource_id=str(run.id),
        outcome=run_status,
        correlation_id=get_correlation_id(),
        details={
            "tool_version_id": str(version.id),
            "score": score,
            "passed_cases": passed,
            "total_cases": total,
        },
    )
    record_outbox(
        session,
        organization_id=version.organization_id,
        aggregate_type="tool_certification_run",
        aggregate_id=str(run.id),
        event_type="tool.certification_run.executed.v1",
        payload={
            "certification_run_id": str(run.id),
            "tool_id": str(version.tool_id),
            "tool_version_id": str(version.id),
            "status": run_status,
            "score": score,
        },
    )
    await session.commit()
    return run


@router.post(
    "/tool-certification-runs/{run_id}/decision",
    response_model=ToolCertificationRunRead,
)
async def decide_tool_certification(
    run_id: UUID,
    body: ToolCertificationDecisionRequest,
    context: SecurityContext = Depends(require_roles(*CERTIFICATION_CHECKER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ToolCertificationRun:
    run = await session.get(ToolCertificationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="certification run not found")
    enforce_organization(context, run.organization_id)
    if run.status != "PENDING_REVIEW":
        raise HTTPException(status_code=409, detail="certification run is not pending review")
    if run.executed_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker-checker separation is required")
    review = await session.scalar(
        select(GovernanceReview).where(
            GovernanceReview.object_type == "TOOL_CERTIFICATION_RUN",
            GovernanceReview.object_id == str(run.id),
            GovernanceReview.status == "PENDING",
        )
    )
    now = datetime.now(UTC)
    run.certified_by = context.principal_id
    run.decision_reason = body.reason
    if body.decision == "APPROVE":
        run.status = "CERTIFIED"
        run.issued_at = now
        event_type = "tool.certification_completed.v1"
    else:
        run.status = "REJECTED"
        run.expires_at = None
        event_type = "tool.certification_rejected.v1"
    if review is not None:
        review.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
        review.decided_by = context.principal_id
        review.decision_reason = body.reason
        review.decided_at = now
    record_audit(
        session,
        replace(context, organization_id=run.organization_id),
        action="tool.certification_run.decision",
        resource_type="tool_certification_run",
        resource_id=str(run.id),
        outcome=run.status,
        correlation_id=get_correlation_id(),
        details={"tool_version_id": str(run.tool_version_id), "decision": body.decision},
    )
    record_outbox(
        session,
        organization_id=run.organization_id,
        aggregate_type="tool_certification_run",
        aggregate_id=str(run.id),
        event_type=event_type,
        payload={
            "certification_run_id": str(run.id),
            "tool_id": str(run.tool_id),
            "tool_version_id": str(run.tool_version_id),
            "status": run.status,
            "expires_at": run.expires_at.isoformat() if run.expires_at else None,
        },
    )
    await session.commit()
    return run


@router.get("/tools/{tool_id}/certification-runs", response_model=Page)
async def list_tool_certification_runs(
    tool_id: UUID,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CERTIFICATION_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    tool = await session.get(GovernedTool, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    enforce_organization(context, tool.organization_id)
    filters = (ToolCertificationRun.tool_id == tool.id,)
    total = await session.scalar(
        select(func.count()).select_from(ToolCertificationRun).where(*filters)
    )
    rows = (
        await session.scalars(
            select(ToolCertificationRun)
            .where(*filters)
            .order_by(ToolCertificationRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[ToolCertificationRunRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/tools/{tool_id}/certification-status",
    response_model=ToolCertificationStatusRead,
)
async def get_tool_certification_status(
    tool_id: UUID,
    tool_version_id: UUID | None = Query(default=None),
    context: SecurityContext = Depends(require_roles(*CERTIFICATION_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ToolCertificationStatusRead:
    tool = await session.get(GovernedTool, tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="tool not found")
    enforce_organization(context, tool.organization_id)
    filters = [
        ToolCertificationRun.tool_id == tool.id,
        ToolCertificationRun.status == "CERTIFIED",
    ]
    if tool_version_id is not None:
        filters.append(ToolCertificationRun.tool_version_id == tool_version_id)
    # The most recently issued CERTIFIED run is authoritative for "current"
    # certification, whether it is still active or has since expired -- a
    # recertification always supersedes an older run without touching it.
    run = await session.scalar(
        select(ToolCertificationRun)
        .where(*filters)
        .order_by(ToolCertificationRun.issued_at.desc())
        .limit(1)
    )
    if run is not None and certification_is_active(run):
        return ToolCertificationStatusRead(
            tool_id=tool.id,
            tool_version_id=run.tool_version_id,
            certified=True,
            run_id=run.id,
            certified_by=run.certified_by,
            issued_at=run.issued_at,
            expires_at=run.expires_at,
            expired_run_id=None,
            expired_at=None,
        )
    if run is not None:
        return ToolCertificationStatusRead(
            tool_id=tool.id,
            tool_version_id=run.tool_version_id,
            certified=False,
            run_id=None,
            certified_by=None,
            issued_at=None,
            expires_at=None,
            expired_run_id=run.id,
            expired_at=run.expires_at,
        )
    return ToolCertificationStatusRead(
        tool_id=tool.id,
        tool_version_id=tool_version_id,
        certified=False,
        run_id=None,
        certified_by=None,
        issued_at=None,
        expires_at=None,
        expired_run_id=None,
        expired_at=None,
    )
