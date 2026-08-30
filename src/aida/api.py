from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from neo4j import AsyncGraphDatabase
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from aida.agent_evals import run_control_evaluation
from aida.agent_intelligence import GovernedPlanner, GovernedRetriever
from aida.agent_orchestrator import (
    AgentClarificationRequired,
    AgentPolicyRejected,
    GovernedAgentOrchestrator,
    ModelRouteUnavailable,
)
from aida.config import Settings, get_settings
from aida.connectors.registry import connector_registry
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled, reserve_analysis_run
from aida.integration_service import ensure_organization_integration_policy
from aida.model_gateway import SUPPORTED_MODEL_PROVIDERS
from aida.pagination import InvalidCursor, apply_keyset, decode_cursor, encode_cursor
from aida.models import (
    AgentEvaluationRun,
    AgentRun,
    AnalysisRun,
    ColumnProfile,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataIndex,
    MetadataPartition,
    MetadataSchema,
    MetadataTable,
    Organization,
    OrganizationIntegrationPolicy,
    Project,
    QueryExecution,
    ScanPolicy,
    TableProfile,
)
from aida.prompt_risk import DeterministicPromptRiskClassifier
from aida.query_gateway import GatewayResult, QueryExecutionGateway, QueryRejected
from aida.schemas import (
    AgentAnalysisRequest,
    AgentAnalysisResponse,
    AgentEvaluationRunRead,
    AgentRetrievalPreviewRead,
    AgentRetrievalPreviewRequest,
    AgentRunRead,
    AiRuntimeStatusRead,
    AnalysisRunCreate,
    AnalysisRunRead,
    ColumnProfileRead,
    DataSourceCreate,
    DataSourceRead,
    DataSourceSummaryRead,
    DataSourceUpdate,
    GraphSummaryRead,
    LineOfBusinessCreate,
    LineOfBusinessRead,
    MetadataColumnRead,
    MetadataConstraintRead,
    MetadataIndexRead,
    MetadataPartitionRead,
    MetadataTableRead,
    OrganizationCreate,
    OrganizationIntegrationPolicyRead,
    OrganizationIntegrationPolicyWrite,
    OrganizationRead,
    Page,
    ProjectCreate,
    ProjectRead,
    QueryExecutionRequest,
    QueryExecutionResponse,
    QueryLineageRead,
    ScanPolicyRead,
    ScanPolicyUpsert,
    SqlValidationRequest,
    SqlValidationResponse,
    TableProfileRead,
)
from aida.secrets import SecretResolver
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.sql_guard import SqlGuard
from aida.workflows.discovery import DatasourceDiscoveryWorkflow

router = APIRouter(prefix="/v1")


@router.get("/ai/runtime-status", response_model=AiRuntimeStatusRead)
async def ai_runtime_status(
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "Analyst", "AgentDeveloper", "Viewer", "Auditor")
    ),
    settings: Settings = Depends(get_settings),
) -> AiRuntimeStatusRead:
    del context
    credential_provider_available = SecretResolver(settings).provider_available()
    oidc_configured = bool(
        settings.identity_provider == "oidc"
        and settings.oidc_issuer
        and settings.oidc_audience
        and (settings.oidc_jwks_url or settings.oidc_jwks_json)
    )
    return AiRuntimeStatusRead(
        orchestration_mode="HYBRID",
        runtime="FRAMEWORK_NEUTRAL_TYPED_STATE_MACHINE",
        runtime_version="v2",
        model_route_status=(
            "CONFIGURED"
            if settings.model_generation_enabled and settings.model_route
            else "NOT_CONFIGURED"
        ),
        model_generation_enabled=settings.model_generation_enabled,
        available_model_providers=sorted(SUPPORTED_MODEL_PROVIDERS),
        development_sql_override_enabled=settings.allow_development_sql_override,
        identity_provider=settings.identity_provider.upper(),
        identity_verification=(
            "SIGNED_JWT_ISSUER_AUDIENCE_JWKS"
            if settings.identity_provider == "oidc"
            else "DEVELOPMENT_HEADERS_ONLY"
        ),
        oidc_configured=oidc_configured,
        credential_provider=settings.credential_provider.upper(),
        credential_provider_available=credential_provider_available,
        enterprise_security_ready=(oidc_configured and credential_provider_available),
        deterministic_controls=[
            "authorization",
            "prompt_risk_classification",
            "governed_metadata_retrieval",
            "approved_tool_first_planning",
            "metadata_resolution",
            "semantic_version_resolution",
            "sql_ast_validation",
            "catalog_allowlisting",
            "query_cost_gate",
            "row_limit",
            "sensitive_data_masking",
            "audit_evidence",
            "repeatable_control_evaluation",
        ],
        optional_framework_adapters=["LangGraph", "Google ADK"],
        data_retention_statement=(
            "Raw analyst questions are not persisted; only an HMAC digest and bounded "
            "evidence are retained."
        ),
    )


@router.post(
    "/datasources/{datasource_id}/agent-retrieval-preview",
    response_model=AgentRetrievalPreviewRead,
)
async def preview_agent_retrieval(
    datasource_id: UUID,
    body: AgentRetrievalPreviewRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "Analyst", "AgentDeveloper", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AgentRetrievalPreviewRead:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    prompt_risk = DeterministicPromptRiskClassifier().assess(body.question)
    hits = (
        []
        if prompt_risk.decision == "BLOCK"
        else await GovernedRetriever(settings).retrieve(
            session, datasource=datasource, question=body.question
        )
    )
    plan = GovernedPlanner(settings).plan(
        retrieval_hits=hits,
        roles=context.roles,
        candidate_sql_available=body.candidate_sql_available,
        tool_parameters={},
        prompt_risk=prompt_risk,
    )
    return AgentRetrievalPreviewRead(
        datasource_id=datasource.id,
        retrieval_evidence=[hit.evidence() for hit in hits],
        plan_evidence=plan.evidence(),
    )


@router.post(
    "/organizations/{organization_id}/agent-evaluations",
    response_model=AgentEvaluationRunRead,
    status_code=status.HTTP_201_CREATED,
)
async def run_agent_evaluation(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "AgentDeveloper", "Auditor")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AgentEvaluationRun:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    summary = run_control_evaluation(settings)
    evaluation = AgentEvaluationRun(
        organization_id=organization_id,
        principal_id=context.principal_id,
        suite_version=summary.suite_version,
        status="PASSED" if summary.failed_count == 0 else "FAILED",
        scenario_count=summary.scenario_count,
        passed_count=summary.passed_count,
        failed_count=summary.failed_count,
        pass_rate=summary.pass_rate,
        findings=summary.findings,
    )
    session.add(evaluation)
    await session.flush()
    audit_context = replace(context, organization_id=organization_id)
    record_audit(
        session,
        audit_context,
        action="agent.evaluation.run",
        resource_type="agent_evaluation_run",
        resource_id=str(evaluation.id),
        outcome="SUCCESS" if summary.failed_count == 0 else "FAILED",
        correlation_id=get_correlation_id(),
        details={
            "suite_version": summary.suite_version,
            "scenario_count": summary.scenario_count,
            "failed_count": summary.failed_count,
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="agent_evaluation_run",
        aggregate_id=str(evaluation.id),
        event_type="agent.evaluation.completed.v1",
        payload={
            "evaluation_run_id": str(evaluation.id),
            "status": evaluation.status,
            "suite_version": evaluation.suite_version,
        },
    )
    await session.commit()
    return evaluation


@router.get("/organizations/{organization_id}/agent-evaluations", response_model=Page)
async def list_agent_evaluations(
    organization_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "AgentDeveloper", "Auditor", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = (AgentEvaluationRun.organization_id == organization_id,)
    total = await session.scalar(
        select(func.count()).select_from(AgentEvaluationRun).where(*filters)
    )
    rows = (
        await session.scalars(
            select(AgentEvaluationRun)
            .where(*filters)
            .order_by(AgentEvaluationRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[AgentEvaluationRunRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


def _query_execution_response(result: GatewayResult) -> QueryExecutionResponse:
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


async def _commit_or_conflict(session: AsyncSession, detail: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc


async def _submit_analysis_workflow(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    run: AnalysisRun,
) -> None:
    if not settings.temporal_enabled:
        return
    client: Client = request.app.state.temporal_client
    try:
        await client.start_workflow(
            DatasourceDiscoveryWorkflow.run,
            str(run.id),
            id=run.temporal_workflow_id or f"discovery-{run.datasource_id}-{run.id}",
            task_queue=settings.temporal_task_queue,
        )
    except WorkflowAlreadyStartedError:
        return
    except Exception as exc:
        run.status = "SUBMISSION_FAILED"
        run.error_class = type(exc).__name__
        run.error_message = "workflow submission failed"
        await session.commit()
        raise HTTPException(status_code=503, detail="workflow service unavailable") from exc


@router.get("/organizations", response_model=Page)
async def list_organizations(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "Auditor", "Operations")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    filters = []
    if "PlatformAdmin" not in context.roles:
        filters.append(Organization.id == context.require_organization())
    total = await session.scalar(select(func.count()).select_from(Organization).where(*filters))
    rows = (
        await session.scalars(
            select(Organization)
            .where(*filters)
            .order_by(Organization.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[OrganizationRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/organizations/{organization_id}/lines-of-business", response_model=Page)
async def list_lines_of_business(
    organization_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    filters = (LineOfBusiness.organization_id == organization_id,)
    total = await session.scalar(select(func.count()).select_from(LineOfBusiness).where(*filters))
    rows = (
        await session.scalars(
            select(LineOfBusiness)
            .where(*filters)
            .order_by(LineOfBusiness.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[LineOfBusinessRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/lines-of-business/{lob_id}/projects", response_model=Page)
async def list_projects(
    lob_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    lob = await session.get(LineOfBusiness, lob_id)
    if lob is None:
        raise HTTPException(status_code=404, detail="line of business not found")
    enforce_organization(context, lob.organization_id)
    filters = (Project.line_of_business_id == lob.id,)
    total = await session.scalar(select(func.count()).select_from(Project).where(*filters))
    rows = (
        await session.scalars(
            select(Project).where(*filters).order_by(Project.name).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[ProjectRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/projects/{project_id}/datasources", response_model=Page)
async def list_datasources(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    filters = (DataSource.project_id == project.id,)
    total = await session.scalar(select(func.count()).select_from(DataSource).where(*filters))
    rows = (
        await session.scalars(
            select(DataSource).where(*filters).order_by(DataSource.name).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[DataSourceSummaryRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post("/organizations", response_model=OrganizationRead, status_code=status.HTTP_201_CREATED)
async def create_organization(
    body: OrganizationCreate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin")),
    session: AsyncSession = Depends(get_session),
) -> Organization:
    organization = Organization(name=body.name, slug=body.slug)
    session.add(organization)
    await session.flush()
    session.add(OrganizationIntegrationPolicy(organization_id=organization.id))
    audit_context = replace(context, organization_id=organization.id)
    record_audit(
        session,
        audit_context,
        action="organization.create",
        resource_type="organization",
        resource_id=str(organization.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    record_outbox(
        session,
        organization_id=organization.id,
        aggregate_type="organization",
        aggregate_id=str(organization.id),
        event_type="organization.created.v1",
        payload={"organization_id": str(organization.id), "slug": organization.slug},
    )
    await _commit_or_conflict(session, "organization slug already exists")
    return organization


@router.get(
    "/organizations/{organization_id}/integration-policy",
    response_model=OrganizationIntegrationPolicyRead,
)
async def get_organization_integration_policy(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "OrganizationAdmin")),
    session: AsyncSession = Depends(get_session),
) -> OrganizationIntegrationPolicy:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    policy = await ensure_organization_integration_policy(session, organization_id)
    await session.commit()
    return policy


@router.put(
    "/organizations/{organization_id}/integration-policy",
    response_model=OrganizationIntegrationPolicyRead,
)
async def update_organization_integration_policy(
    organization_id: UUID,
    body: OrganizationIntegrationPolicyWrite,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "OrganizationAdmin")),
    session: AsyncSession = Depends(get_session),
) -> OrganizationIntegrationPolicy:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    policy = await ensure_organization_integration_policy(session, organization_id)
    policy.transformation_metadata_integrations = body.transformation_metadata_integrations
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="organization_integration_policy.update",
        resource_type="organization_integration_policy",
        resource_id=str(policy.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "transformation_metadata_integrations": policy.transformation_metadata_integrations
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="organization_integration_policy",
        aggregate_id=str(policy.id),
        event_type="organization.integration_policy.updated.v1",
        payload={
            "organization_id": str(organization_id),
            "transformation_metadata_integrations": policy.transformation_metadata_integrations,
        },
    )
    await session.commit()
    await session.refresh(policy)
    return policy


@router.post(
    "/organizations/{organization_id}/lines-of-business",
    response_model=LineOfBusinessRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_line_of_business(
    organization_id: UUID,
    body: LineOfBusinessCreate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "OrganizationAdmin")),
    session: AsyncSession = Depends(get_session),
) -> LineOfBusiness:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    lob = LineOfBusiness(organization_id=organization_id, name=body.name, code=body.code)
    session.add(lob)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="line_of_business.create",
        resource_type="line_of_business",
        resource_id=str(lob.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="line_of_business",
        aggregate_id=str(lob.id),
        event_type="line_of_business.created.v1",
        payload={"line_of_business_id": str(lob.id), "code": lob.code},
    )
    await _commit_or_conflict(session, "line-of-business code already exists")
    return lob


@router.post(
    "/lines-of-business/{lob_id}/projects",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    lob_id: UUID,
    body: ProjectCreate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "ProjectAdmin")),
    session: AsyncSession = Depends(get_session),
) -> Project:
    lob = await session.get(LineOfBusiness, lob_id)
    if lob is None:
        raise HTTPException(status_code=404, detail="line of business not found")
    enforce_organization(context, lob.organization_id)
    project = Project(
        organization_id=lob.organization_id,
        line_of_business_id=lob.id,
        name=body.name,
        slug=body.slug,
    )
    session.add(project)
    await session.flush()
    audit_context = replace(context, organization_id=lob.organization_id)
    record_audit(
        session,
        audit_context,
        action="project.create",
        resource_type="project",
        resource_id=str(project.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    record_outbox(
        session,
        organization_id=lob.organization_id,
        aggregate_type="project",
        aggregate_id=str(project.id),
        event_type="project.created.v1",
        payload={"project_id": str(project.id), "lob_id": str(lob.id)},
    )
    await _commit_or_conflict(session, "project slug already exists")
    return project


@router.post(
    "/projects/{project_id}/datasources",
    response_model=DataSourceRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_datasource(
    project_id: UUID,
    body: DataSourceCreate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataAdmin")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DataSource:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    approved_reference_prefix = f"{settings.credential_provider}://"
    if not body.credential_reference.startswith(approved_reference_prefix):
        raise HTTPException(
            status_code=422,
            detail=(
                "credential_reference must use the configured secret provider, "
                "never a connection string or unapproved provider"
            ),
        )
    if body.connector_type not in connector_registry.supported_types:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported connector type: {body.connector_type}",
        )
    datasource = DataSource(
        organization_id=project.organization_id,
        line_of_business_id=project.line_of_business_id,
        project_id=project.id,
        **body.model_dump(),
    )
    session.add(datasource)
    await session.flush()
    audit_context = replace(context, organization_id=project.organization_id)
    record_audit(
        session,
        audit_context,
        action="datasource.register",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "connector_type": datasource.connector_type,
            "network_zone": datasource.network_zone,
        },
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="datasource",
        aggregate_id=str(datasource.id),
        event_type="datasource.registered.v1",
        payload={
            "datasource_id": str(datasource.id),
            "project_id": str(project.id),
            "connector_type": datasource.connector_type,
        },
    )
    await _commit_or_conflict(session, "datasource name already exists in this project")
    return datasource


@router.patch("/datasources/{datasource_id}", response_model=DataSourceRead)
async def update_datasource(
    datasource_id: UUID,
    body: DataSourceUpdate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataAdmin")),
    session: AsyncSession = Depends(get_session),
) -> DataSource:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    changes = body.model_dump(exclude_unset=True)
    enabled = changes.pop("enabled", None)
    if enabled is not None:
        datasource.status = (
            "CONNECTION_VERIFIED" if enabled and datasource.capabilities else "REGISTERED"
        )
        if not enabled:
            datasource.status = "DISABLED"
    for field, value in changes.items():
        setattr(datasource, field, value)
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="datasource.update",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"updated_fields": sorted(body.model_fields_set)},
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="datasource",
        aggregate_id=str(datasource.id),
        event_type="datasource.updated.v1",
        payload={
            "datasource_id": str(datasource.id),
            "status": datasource.status,
            "max_concurrency": datasource.max_concurrency,
        },
    )
    await session.commit()
    return datasource


@router.put("/datasources/{datasource_id}/scan-policy", response_model=ScanPolicyRead)
async def upsert_scan_policy(
    datasource_id: UUID,
    body: ScanPolicyUpsert,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> ScanPolicy:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    now = datetime.now(UTC)
    if body.start_at is not None and body.start_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="start_at must include a timezone")
    next_run_at = body.start_at.astimezone(UTC) if body.start_at else now
    policy = await session.scalar(
        select(ScanPolicy).where(ScanPolicy.datasource_id == datasource.id)
    )
    values = body.model_dump(exclude={"start_at"})
    if policy is None:
        policy = ScanPolicy(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            next_run_at=next_run_at,
            created_by=context.principal_id,
            **values,
        )
        session.add(policy)
    else:
        for field, value in values.items():
            setattr(policy, field, value)
        if body.start_at is not None or policy.next_run_at < now:
            policy.next_run_at = next_run_at
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="scan_policy.upsert",
        resource_type="scan_policy",
        resource_id=str(policy.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"enabled": policy.enabled, "interval_minutes": policy.interval_minutes},
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="scan_policy",
        aggregate_id=str(policy.id),
        event_type="scan_policy.updated.v1",
        payload={
            "scan_policy_id": str(policy.id),
            "datasource_id": str(datasource.id),
            "enabled": policy.enabled,
        },
    )
    await session.commit()
    return policy


@router.get("/datasources/{datasource_id}/scan-policy", response_model=ScanPolicyRead)
async def get_scan_policy(
    datasource_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> ScanPolicy:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    policy = await session.scalar(
        select(ScanPolicy).where(ScanPolicy.datasource_id == datasource.id)
    )
    if policy is None:
        raise HTTPException(status_code=404, detail="scan policy not found")
    return policy


@router.post("/datasources/{datasource_id}/test", response_model=DataSourceRead)
async def test_datasource(
    datasource_id: UUID,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataAdmin")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DataSource:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        dsn = SecretResolver(settings).resolve(datasource.credential_reference)
        connector = connector_registry.create(datasource.connector_type, dsn)
        await connector.test_connection()
        datasource.status = "CONNECTION_VERIFIED"
        datasource.capabilities = asdict(connector.capabilities)
        outcome = "SUCCESS"
    except Exception as exc:
        datasource.status = "CONNECTION_FAILED"
        outcome = "FAILURE"
        record_audit(
            session,
            replace(context, organization_id=datasource.organization_id),
            action="datasource.test",
            resource_type="datasource",
            resource_id=str(datasource.id),
            outcome=outcome,
            correlation_id=get_correlation_id(),
            details={"error_class": type(exc).__name__},
        )
        await session.commit()
        raise HTTPException(status_code=424, detail="datasource connection test failed") from exc
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="datasource.test",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome=outcome,
        correlation_id=get_correlation_id(),
    )
    await session.commit()
    return datasource


@router.post(
    "/datasources/{datasource_id}/analysis-runs",
    response_model=AnalysisRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_analysis_run(
    datasource_id: UUID,
    body: AnalysisRunCreate,
    request: Request,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AnalysisRun:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        run = await reserve_analysis_run(
            session,
            settings,
            datasource_id=datasource.id,
            mode=body.mode,
            trigger_type="MANUAL",
        )
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="analysis_run.create",
        resource_type="analysis_run",
        resource_id=str(run.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="analysis_run",
        aggregate_id=str(run.id),
        event_type="analysis_run.requested.v1",
        payload={"run_id": str(run.id), "datasource_id": str(datasource.id), "mode": run.mode},
    )
    await session.commit()

    await _submit_analysis_workflow(request, session, settings, run)
    return run


@router.get("/datasources/{datasource_id}/analysis-runs", response_model=Page)
async def list_analysis_runs(
    datasource_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = (
        AnalysisRun.organization_id == datasource.organization_id,
        AnalysisRun.datasource_id == datasource.id,
    )
    total = await session.scalar(select(func.count()).select_from(AnalysisRun).where(*filters))
    rows = (
        await session.scalars(
            select(AnalysisRun)
            .where(*filters)
            .order_by(AnalysisRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[AnalysisRunRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/analysis-runs/{run_id}", response_model=AnalysisRunRead)
async def get_analysis_run(
    run_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> AnalysisRun:
    run = await session.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="analysis run not found")
    enforce_organization(context, run.organization_id)
    return run


@router.post("/analysis-runs/{run_id}/cancel", response_model=AnalysisRunRead)
async def cancel_analysis_run(
    run_id: UUID,
    request: Request,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AnalysisRun:
    run = await session.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="analysis run not found")
    enforce_organization(context, run.organization_id)
    if run.status in {"COMPLETED", "FAILED", "CANCELLED"}:
        return run
    if settings.temporal_enabled and run.temporal_workflow_id:
        try:
            client: Client = request.app.state.temporal_client
            handle = client.get_workflow_handle(run.temporal_workflow_id)
            await handle.cancel(reason=f"cancelled by {context.principal_id}")
        except Exception as exc:
            raise HTTPException(status_code=503, detail="workflow cancellation failed") from exc
    run.status = "CANCELLATION_REQUESTED"
    record_audit(
        session,
        replace(context, organization_id=run.organization_id),
        action="analysis_run.cancel",
        resource_type="analysis_run",
        resource_id=str(run.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
    )
    record_outbox(
        session,
        organization_id=run.organization_id,
        aggregate_type="analysis_run",
        aggregate_id=str(run.id),
        event_type="analysis_run.cancellation_requested.v1",
        payload={"run_id": str(run.id), "datasource_id": str(run.datasource_id)},
    )
    await session.commit()
    return run


@router.post(
    "/analysis-runs/{run_id}/resume",
    response_model=AnalysisRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_analysis_run(
    run_id: UUID,
    request: Request,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AnalysisRun:
    previous = await session.get(AnalysisRun, run_id)
    if previous is None:
        raise HTTPException(status_code=404, detail="analysis run not found")
    enforce_organization(context, previous.organization_id)
    if previous.status not in {
        "FAILED",
        "CANCELLED",
        "CANCELLATION_REQUESTED",
        "SUBMISSION_FAILED",
    }:
        raise HTTPException(status_code=409, detail="only interrupted or failed runs can resume")
    try:
        resumed = await reserve_analysis_run(
            session,
            settings,
            datasource_id=previous.datasource_id,
            mode=previous.mode,
            trigger_type="RESUME",
            priority=previous.priority,
            resumed_from_run_id=previous.id,
        )
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record_audit(
        session,
        replace(context, organization_id=previous.organization_id),
        action="analysis_run.resume",
        resource_type="analysis_run",
        resource_id=str(resumed.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"resumed_from_run_id": str(previous.id)},
    )
    record_outbox(
        session,
        organization_id=previous.organization_id,
        aggregate_type="analysis_run",
        aggregate_id=str(resumed.id),
        event_type="analysis_run.resumed.v1",
        payload={
            "run_id": str(resumed.id),
            "resumed_from_run_id": str(previous.id),
            "datasource_id": str(previous.datasource_id),
        },
    )
    await session.commit()
    await _submit_analysis_workflow(request, session, settings, resumed)
    return resumed


async def _list_page(
    session: AsyncSession,
    *,
    model: type,
    filters: tuple,
    order_columns: tuple,
    coercers: tuple,
    read_schema: type,
    limit: int,
    offset: int,
    cursor: str | None,
) -> Page:
    """Shared list-endpoint body: keyset pagination when `cursor` is given, plain
    offset pagination otherwise -- both return a `next_cursor` so a caller can
    fetch page one by `offset` (and see a `total`) and then walk every page after
    it purely by cursor, never paying for another `COUNT(*)` or a growing `OFFSET`.

    The keyset branch's `WHERE`/`ORDER BY` use exactly `order_columns` (which
    callers pair with a composite index whose leading columns match `filters`),
    so its cost is bounded by `limit` alone -- independent of how many pages a
    caller has already walked, unlike `offset`, which the database must walk
    and discard before it can return anything.
    """
    total: int | None = None
    if cursor is not None:
        try:
            raw_values = decode_cursor(cursor, arity=len(order_columns))
            last_values = tuple(coerce(value) for coerce, value in zip(coercers, raw_values))
        except (InvalidCursor, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid cursor") from exc
        statement = apply_keyset(
            select(model).where(*filters).order_by(*order_columns),
            order_columns,
            last_values,
        ).limit(limit)
    else:
        total = await session.scalar(select(func.count()).select_from(model).where(*filters)) or 0
        statement = (
            select(model).where(*filters).order_by(*order_columns).limit(limit).offset(offset)
        )

    rows = (await session.scalars(statement)).all()
    next_cursor = (
        encode_cursor(*(getattr(rows[-1], column.key) for column in order_columns))
        if len(rows) == limit
        else None
    )
    return Page(
        items=[read_schema.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total,
        next_cursor=next_cursor,
    )


_CURSOR_DESCRIPTION = (
    "Opaque keyset cursor from a previous page's next_cursor. When supplied, "
    "offset is ignored, no total is computed, and the response cost stays "
    "bounded by limit no matter how many pages precede it."
)


@router.get("/datasources/{datasource_id}/tables", response_model=Page)
async def list_tables(
    datasource_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = (
        MetadataTable.organization_id == datasource.organization_id,
        MetadataTable.datasource_id == datasource.id,
        MetadataTable.status == "ACTIVE",
    )
    return await _list_page(
        session,
        model=MetadataTable,
        filters=filters,
        order_columns=(MetadataTable.name, MetadataTable.id),
        coercers=(str, UUID),
        read_schema=MetadataTableRead,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )


@router.get("/tables/{table_id}/columns", response_model=Page)
async def list_columns(
    table_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    filters = (
        MetadataColumn.organization_id == table.organization_id,
        MetadataColumn.table_id == table.id,
        MetadataColumn.status == "ACTIVE",
    )
    return await _list_page(
        session,
        model=MetadataColumn,
        filters=filters,
        order_columns=(MetadataColumn.ordinal_position, MetadataColumn.id),
        coercers=(int, UUID),
        read_schema=MetadataColumnRead,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )


@router.get("/tables/{table_id}/constraints", response_model=Page)
async def list_constraints(
    table_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    filters = (
        MetadataConstraint.organization_id == table.organization_id,
        MetadataConstraint.table_id == table.id,
        MetadataConstraint.status == "ACTIVE",
    )
    return await _list_page(
        session,
        model=MetadataConstraint,
        filters=filters,
        order_columns=(MetadataConstraint.name, MetadataConstraint.id),
        coercers=(str, UUID),
        read_schema=MetadataConstraintRead,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )


@router.get("/tables/{table_id}/indexes", response_model=Page)
async def list_indexes(
    table_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    filters = (
        MetadataIndex.organization_id == table.organization_id,
        MetadataIndex.table_id == table.id,
        MetadataIndex.status == "ACTIVE",
    )
    return await _list_page(
        session,
        model=MetadataIndex,
        filters=filters,
        order_columns=(MetadataIndex.name, MetadataIndex.id),
        coercers=(str, UUID),
        read_schema=MetadataIndexRead,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )


@router.get("/tables/{table_id}/partitions", response_model=Page)
async def list_partitions(
    table_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    filters = (
        MetadataPartition.organization_id == table.organization_id,
        MetadataPartition.table_id == table.id,
        MetadataPartition.status == "ACTIVE",
    )
    return await _list_page(
        session,
        model=MetadataPartition,
        filters=filters,
        order_columns=(MetadataPartition.ordinal_position, MetadataPartition.id),
        coercers=(int, UUID),
        read_schema=MetadataPartitionRead,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )


@router.get("/tables/{table_id}/profile", response_model=TableProfileRead)
async def get_latest_table_profile(
    table_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> TableProfileRead:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    profile = await session.scalar(
        select(TableProfile)
        .where(
            TableProfile.organization_id == table.organization_id,
            TableProfile.table_id == table.id,
            TableProfile.status == "COMPLETED",
        )
        .order_by(TableProfile.created_at.desc())
        .limit(1)
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="table profile not found")
    profile_rows = (
        await session.execute(
            select(ColumnProfile, MetadataColumn)
            .join(MetadataColumn, MetadataColumn.id == ColumnProfile.column_id)
            .where(ColumnProfile.table_profile_id == profile.id)
            .order_by(MetadataColumn.ordinal_position)
        )
    ).all()
    return TableProfileRead(
        id=profile.id,
        analysis_run_id=profile.analysis_run_id,
        table_id=profile.table_id,
        row_count_estimate=profile.row_count_estimate,
        sampled_row_count=profile.sampled_row_count,
        profile_version=profile.profile_version,
        status=profile.status,
        created_at=profile.created_at,
        columns=[
            ColumnProfileRead(
                column_id=column.id,
                column_name=column.name,
                classification=column.classification,
                null_count=column_profile.null_count,
                non_null_count=column_profile.non_null_count,
                approximate_distinct_count=column_profile.approximate_distinct_count,
                min_length=column_profile.min_length,
                max_length=column_profile.max_length,
            )
            for column_profile, column in profile_rows
        ],
    )


@router.get(
    "/datasources/{datasource_id}/graph-summary",
    response_model=GraphSummaryRead,
)
async def get_graph_summary(
    datasource_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GraphSummaryRead:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        record = await driver.execute_query(
            """
            MATCH (catalog:Catalog {
              datasource_id: $datasource_id,
              organization_id: $organization_id
            })
            OPTIONAL MATCH (catalog)-[:HAS_SCHEMA]->(schema:Schema)
            OPTIONAL MATCH (schema)-[:HAS_TABLE]->(table:Table)
            OPTIONAL MATCH (table)-[:HAS_COLUMN]->(column:Column)
            OPTIONAL MATCH (table)-[:HAS_CONSTRAINT]->(constraint:Constraint)
            OPTIONAL MATCH (constraint)-[reference:REFERENCES]->(:Table)
            RETURN count(DISTINCT catalog) AS catalogs,
                   count(DISTINCT schema) AS schemas,
                   count(DISTINCT table) AS tables,
                   count(DISTINCT column) AS columns,
                   count(DISTINCT CASE
                     WHEN column.classification IN ['PII', 'PCI', 'PHI', 'SECRET', 'CONFIDENTIAL']
                     THEN column
                   END) AS sensitive_columns
                   ,count(DISTINCT constraint) AS constraints
                   ,count(DISTINCT reference) AS foreign_key_relationships
            """,
            datasource_id=str(datasource.id),
            organization_id=str(datasource.organization_id),
            database_="neo4j",
        )
        summary = record.records[0]
    except Exception as exc:
        raise HTTPException(status_code=503, detail="metadata graph unavailable") from exc
    finally:
        await driver.close()
    catalogs = int(summary["catalogs"])
    authoritative = {
        "catalogs": int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataCatalog)
                .where(MetadataCatalog.datasource_id == datasource.id)
            )
            or 0
        ),
        "schemas": int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataSchema)
                .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
                .where(MetadataCatalog.datasource_id == datasource.id)
            )
            or 0
        ),
        "tables": int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataTable)
                .where(MetadataTable.datasource_id == datasource.id)
            )
            or 0
        ),
        "columns": int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataColumn)
                .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
                .where(MetadataTable.datasource_id == datasource.id)
            )
            or 0
        ),
        "constraints": int(
            await session.scalar(
                select(func.count())
                .select_from(MetadataConstraint)
                .where(MetadataConstraint.datasource_id == datasource.id)
            )
            or 0
        ),
    }
    projected = {
        "catalogs": catalogs,
        "schemas": int(summary["schemas"]),
        "tables": int(summary["tables"]),
        "columns": int(summary["columns"]),
        "constraints": int(summary["constraints"]),
    }
    projection_lag = {name: max(authoritative[name] - projected[name], 0) for name in authoritative}
    if not catalogs:
        projection_status = "NOT_PROJECTED"
    elif any(projection_lag.values()):
        projection_status = "LAGGING"
    else:
        projection_status = "CURRENT"
    return GraphSummaryRead(
        datasource_id=datasource.id,
        catalogs=catalogs,
        schemas=projected["schemas"],
        tables=projected["tables"],
        columns=projected["columns"],
        sensitive_columns=int(summary["sensitive_columns"]),
        constraints=projected["constraints"],
        foreign_key_relationships=int(summary["foreign_key_relationships"]),
        projection_status=projection_status,
        projection_lag=projection_lag,
    )


@router.post("/query/validate", response_model=SqlValidationResponse)
async def validate_query(
    body: SqlValidationRequest,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "Analyst", "AgentDeveloper")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SqlValidationResponse:
    organization_id = context.require_organization()
    guard = SqlGuard(
        default_row_limit=settings.default_query_row_limit,
        hard_row_limit=settings.hard_query_row_limit,
    )
    result = guard.validate(body.sql, dialect=body.dialect, requested_limit=body.max_rows)
    record_audit(
        session,
        context,
        action="query.validate",
        resource_type="sql_query",
        resource_id=None,
        outcome="SUCCESS" if result.valid else "DENIED",
        correlation_id=get_correlation_id(),
        details={
            "organization_id": str(organization_id),
            "dialect": body.dialect,
            "referenced_tables": list(result.referenced_tables),
            "violations": list(result.violations),
        },
    )
    await session.commit()
    return SqlValidationResponse(
        valid=result.valid,
        normalized_sql=result.normalized_sql,
        referenced_tables=list(result.referenced_tables),
        referenced_columns=list(result.referenced_columns),
        violations=list(result.violations),
        applied_row_limit=result.applied_row_limit,
    )


@router.post(
    "/datasources/{datasource_id}/query-executions",
    response_model=QueryExecutionResponse,
)
async def execute_query(
    datasource_id: UUID,
    body: QueryExecutionRequest,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "Analyst")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> QueryExecutionResponse:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        ensure_datasource_enabled(datasource)
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    gateway = QueryExecutionGateway(settings)
    try:
        result = await gateway.execute(
            session,
            datasource=datasource,
            context=replace(context, organization_id=datasource.organization_id),
            correlation_id=get_correlation_id(),
            sql=body.sql,
            requested_limit=body.max_rows,
            semantic_version=body.semantic_version,
        )
    except QueryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="source query execution failed") from exc

    return _query_execution_response(result)


@router.get("/query-executions/{execution_id}/lineage", response_model=QueryLineageRead)
async def get_query_lineage(
    execution_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "AgentDeveloper", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
) -> QueryLineageRead:
    execution = await session.get(QueryExecution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="query execution not found")
    enforce_organization(context, execution.organization_id)
    if "PlatformAdmin" not in context.roles and "Auditor" not in context.roles:
        if execution.principal_id != context.principal_id:
            raise HTTPException(
                status_code=403, detail="query execution belongs to another principal"
            )
    return QueryLineageRead(
        execution_id=execution.id,
        datasource_id=execution.datasource_id,
        status=execution.status,
        referenced_tables=execution.referenced_tables,
        referenced_columns=execution.referenced_columns,
        column_lineage=execution.column_lineage,
        semantic_version=execution.semantic_version,
        policy_version=execution.policy_version,
    )


@router.post(
    "/datasources/{datasource_id}/agent-analyses",
    response_model=AgentAnalysisResponse,
)
async def run_agent_analysis(
    datasource_id: UUID,
    body: AgentAnalysisRequest,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "Analyst", "AgentDeveloper")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AgentAnalysisResponse:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        ensure_datasource_enabled(datasource)
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    orchestrator = GovernedAgentOrchestrator(settings)
    try:
        result = await orchestrator.run(
            session,
            datasource=datasource,
            context=replace(context, organization_id=datasource.organization_id),
            correlation_id=get_correlation_id(),
            question=body.question,
            candidate_sql=body.candidate_sql,
            preferred_tool_version_id=body.preferred_tool_version_id,
            tool_parameters=body.tool_parameters,
            requested_limit=body.max_rows,
        )
    except AgentClarificationRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AgentPolicyRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModelRouteUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except QueryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="agent analysis execution failed") from exc
    return AgentAnalysisResponse(
        agent_run_id=result.agent_run.id,
        status=result.agent_run.status,
        generation_source=result.agent_run.generation_source,
        semantic_version=result.agent_run.semantic_version,
        policy_version=result.agent_run.policy_version,
        step_trace=result.agent_run.step_trace,
        retrieval_evidence=result.agent_run.retrieval_evidence,
        plan_evidence=result.agent_run.plan_evidence,
        execution=_query_execution_response(result.gateway_result),
        explanation=result.explanation,
    )


@router.get("/datasources/{datasource_id}/agent-runs", response_model=Page)
async def list_agent_runs(
    datasource_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "Analyst", "AgentDeveloper", "Viewer", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = (
        AgentRun.datasource_id == datasource.id,
        AgentRun.organization_id == datasource.organization_id,
    )
    total = await session.scalar(select(func.count()).select_from(AgentRun).where(*filters))
    rows = (
        await session.scalars(
            select(AgentRun)
            .where(*filters)
            .order_by(AgentRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[AgentRunRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/agent-runs/{agent_run_id}", response_model=AgentRunRead)
async def get_agent_run(
    agent_run_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "Analyst", "AgentDeveloper", "Viewer", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
) -> AgentRunRead:
    agent_run = await session.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    enforce_organization(context, agent_run.organization_id)
    if "PlatformAdmin" not in context.roles and "Auditor" not in context.roles:
        if agent_run.principal_id != context.principal_id:
            raise HTTPException(status_code=403, detail="agent run belongs to another principal")
    return AgentRunRead.model_validate(agent_run)
