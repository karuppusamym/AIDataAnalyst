import hashlib
import hmac
import json
from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled
from aida.models import (
    DataSource,
    GovernanceReview,
    GovernedTool,
    GovernedToolVersion,
    Project,
    SemanticModelVersion,
    ToolExecution,
)
from aida.query_gateway import GatewayResult, QueryExecutionGateway, QueryRejected
from aida.schemas import (
    GovernanceReviewRead,
    GovernedToolVersionCreate,
    GovernedToolVersionRead,
    Page,
    QueryExecutionResponse,
    ToolExecutionRequest,
    ToolExecutionResponse,
    ToolParameterDefinition,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.sql_guard import SqlGuard
from aida.tool_rendering import ToolParameterError, render_tool_sql, template_placeholders

router = APIRouter(prefix="/v1", tags=["governed-tools"])


def _tool_read(tool: GovernedTool, version: GovernedToolVersion) -> GovernedToolVersionRead:
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
    )


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
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    datasource = await session.get(DataSource, body.datasource_id)
    if (
        datasource is None
        or datasource.project_id != project.id
        or datasource.organization_id != project.organization_id
    ):
        raise HTTPException(status_code=422, detail="datasource is outside this project")
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
    base = (
        select(GovernedToolVersion, GovernedTool)
        .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(GovernedTool.slug, GovernedToolVersion.version.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[_tool_read(tool, version) for version, tool in rows],
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
            "requested_action": review.requested_action,
        },
    )
    await session.commit()
    return review


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
    execution_context = replace(context, organization_id=version.organization_id)
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
    )
