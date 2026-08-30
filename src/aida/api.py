from collections.abc import Sequence
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from neo4j import AsyncGraphDatabase
from sqlalchemy import func, or_, select
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
from aida.asset_certification import asset_certification_is_active, current_asset_certification
from aida.authorization_gate import AuthorizationDenied, gate
from aida.catalog_bulk_actions import (
    CATALOG_BULK_ACTION_MAX_ITEMS,
    CATALOG_BULK_FILTER_SCAN_CAP,
    BulkPlan,
    dedupe_preserving_order,
    match_columns_by_pattern,
    match_tables_by_filter,
    plan_certify,
    plan_classify,
    plan_own,
    plan_tag,
)
from aida.classification import SENSITIVE_CLASSES
from aida.classification_feed import ExternalClassificationRecord, ingest_classification_feed
from aida.config import Settings, get_settings
from aida.connectors.registry import connector_registry
from aida.context import get_correlation_id
from aida.db import get_session
from aida.domain_service import ensure_default_domain, resolve_domain
from aida.events import record_audit, record_outbox
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled, reserve_analysis_run
from aida.integration_service import ensure_organization_integration_policy
from aida.model_gateway import SUPPORTED_MODEL_PROVIDERS
from aida.models import (
    AgentEvaluationRun,
    AgentRun,
    AnalysisRun,
    AnalysisTask,
    AssetCertification,
    AssetTag,
    CatalogBulkActionRun,
    ColumnProfile,
    CrossBoundaryGrant,
    DataDomain,
    DataSource,
    GovernanceReview,
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
    OwnershipAssignment,
    ProfilingExceptionPolicy,
    Project,
    QueryExecution,
    ScanPolicy,
    TableProfile,
)
from aida.pagination import InvalidCursor, apply_keyset, decode_cursor, encode_cursor
from aida.prompt_risk import DeterministicPromptRiskClassifier
from aida.query_gateway import (
    AuthorizationRejected,
    GatewayResult,
    QueryExecutionGateway,
    QueryRejected,
)
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
    AnalysisTaskRead,
    ApiModel,
    AssetCertificationRead,
    CatalogBulkActionRunRead,
    CatalogBulkCertifyRequest,
    CatalogBulkClassifyRequest,
    CatalogBulkOwnRequest,
    CatalogBulkSelectionFilter,
    CatalogBulkTagRequest,
    CertificationDecisionRequest,
    ClassificationFeedIngestRequest,
    ClassificationFeedIngestResponse,
    ColumnProfileRead,
    CrossBoundaryGrantCreate,
    CrossBoundaryGrantRead,
    CursorPage,
    DataDomainCreate,
    DataDomainRead,
    DataSourceBulkOnboardItemRead,
    DataSourceBulkOnboardRequest,
    DataSourceBulkOnboardResultRead,
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
    ProfilingExceptionDecisionRequest,
    ProfilingExceptionPolicyCreate,
    ProfilingExceptionPolicyRead,
    ProfilingExceptionRevokeRequest,
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

CATALOG_BULK_ACTION_WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")
CATALOG_BULK_ACTION_READ_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "DataSteward",
    "Analyst",
    "Viewer",
)
_CATALOG_BULK_ACTION_EVENT_TYPES = {
    "TAG": "catalog.asset_tag.applied.v1",
    "CLASSIFY": "catalog.column.classified.v1",
    "OWN": "ownership.assigned.v1",
    "CERTIFY": "certification.granted.v1",
}

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
    await gate_read(
        session,
        context,
        settings,
        # Not READ_METADATA: this returns the assembled retrieval evidence an agent
        # would be handed, which is the context product, not the catalog.
        action="CONSUME_CONTEXT",
        resource_type="datasource",
        resource_id=str(datasource.id),
        datasource_id=datasource.id,
    )
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


@router.get("/lines-of-business/{lob_id}/data-domains", response_model=Page)
async def list_data_domains(
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
    await ensure_default_domain(session, lob)
    await session.commit()
    filters = (DataDomain.line_of_business_id == lob.id,)
    total = await session.scalar(select(func.count()).select_from(DataDomain).where(*filters))
    rows = (
        await session.scalars(
            select(DataDomain).where(*filters).order_by(DataDomain.name).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[DataDomainRead.model_validate(row) for row in rows],
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
    "/lines-of-business/{lob_id}/data-domains",
    response_model=DataDomainRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_data_domain(
    lob_id: UUID,
    body: DataDomainCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> DataDomain:
    lob = await session.get(LineOfBusiness, lob_id)
    if lob is None:
        raise HTTPException(status_code=404, detail="line of business not found")
    enforce_organization(context, lob.organization_id)
    parent = None
    if body.parent_domain_id is not None:
        parent = await session.get(DataDomain, body.parent_domain_id)
        if parent is None or parent.line_of_business_id != lob.id:
            raise HTTPException(
                status_code=422,
                detail=(
                    "parent_domain_id must reference an existing domain "
                    "in the same line of business"
                ),
            )
    domain = DataDomain(
        organization_id=lob.organization_id,
        line_of_business_id=lob.id,
        parent_domain_id=body.parent_domain_id,
        name=body.name,
        code=body.code,
    )
    session.add(domain)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=lob.organization_id),
        action="data_domain.create",
        resource_type="data_domain",
        resource_id=str(domain.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"parent_domain_id": str(body.parent_domain_id) if body.parent_domain_id else None},
    )
    record_outbox(
        session,
        organization_id=lob.organization_id,
        aggregate_type="data_domain",
        aggregate_id=str(domain.id),
        event_type="data_domain.created.v1",
        payload={
            "data_domain_id": str(domain.id),
            "line_of_business_id": str(lob.id),
            "parent_domain_id": str(body.parent_domain_id) if body.parent_domain_id else None,
        },
    )
    await _commit_or_conflict(session, "data domain code already exists in this line of business")
    return domain


@router.get("/data-domains/{domain_id}/cross-boundary-grants", response_model=Page)
async def list_cross_boundary_grants(
    domain_id: UUID,
    grant_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    domain = await session.get(DataDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="data domain not found")
    enforce_organization(context, domain.organization_id)
    filters = [
        or_(
            CrossBoundaryGrant.source_data_domain_id == domain.id,
            CrossBoundaryGrant.target_data_domain_id == domain.id,
        )
    ]
    if grant_status is not None:
        filters.append(CrossBoundaryGrant.status == grant_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(CrossBoundaryGrant).where(*filters)
    )
    rows = (
        await session.scalars(
            select(CrossBoundaryGrant)
            .where(*filters)
            .order_by(CrossBoundaryGrant.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[CrossBoundaryGrantRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/data-domains/{domain_id}/cross-boundary-grants",
    response_model=CrossBoundaryGrantRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_cross_boundary_grant(
    domain_id: UUID,
    body: CrossBoundaryGrantCreate,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> CrossBoundaryGrant:
    """Request permission for `body.target_data_domain_id` to see across the
    boundary into `domain_id` (the source, owning domain). Creates the grant in
    PENDING_APPROVAL and files it into the same governance review queue every
    other governed object here uses (ADR-0017 SS4) — it only becomes ACTIVE once
    a *different* principal approves it via POST /governance/reviews/{id}/decision.
    """
    source_domain = await session.get(DataDomain, domain_id)
    if source_domain is None:
        raise HTTPException(status_code=404, detail="data domain not found")
    enforce_organization(context, source_domain.organization_id)
    if body.target_data_domain_id == source_domain.id:
        raise HTTPException(
            status_code=422, detail="target_data_domain_id must differ from the source domain"
        )
    target_domain = await session.get(DataDomain, body.target_data_domain_id)
    if target_domain is None or target_domain.organization_id != source_domain.organization_id:
        raise HTTPException(status_code=422, detail="target_data_domain_id not found")
    grant = CrossBoundaryGrant(
        organization_id=source_domain.organization_id,
        source_data_domain_id=source_domain.id,
        target_data_domain_id=target_domain.id,
        edge_kinds=body.edge_kinds,
        reason=body.reason,
        requested_by=context.principal_id,
        expires_at=body.expires_at,
    )
    session.add(grant)
    await session.flush()
    review = GovernanceReview(
        organization_id=source_domain.organization_id,
        object_type="CROSS_BOUNDARY_GRANT",
        object_id=str(grant.id),
        requested_action="GRANT",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=source_domain.organization_id),
        action="cross_boundary_grant.request",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "cross_boundary_grant_id": str(grant.id),
            "source_data_domain_id": str(source_domain.id),
            "target_data_domain_id": str(target_domain.id),
        },
    )
    record_outbox(
        session,
        organization_id=source_domain.organization_id,
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
    return grant


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
    if body.data_domain_id is not None:
        explicit_domain = await session.get(DataDomain, body.data_domain_id)
        if explicit_domain is None or explicit_domain.line_of_business_id != lob.id:
            raise HTTPException(
                status_code=422,
                detail="data_domain_id must reference an existing domain in this line of business",
            )
    domain = await resolve_domain(session, lob, body.data_domain_id)
    project = Project(
        organization_id=lob.organization_id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
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


def _validate_datasource_create(body: DataSourceCreate, settings: Settings) -> None:
    """Shared, DB-free registration validation for a single datasource spec.

    Used by both `create_datasource` and `bulk_onboard_datasources` so the two
    paths cannot drift: credential-reference provider check and connector-type
    support are exactly the same rule either way (IN-1).
    """
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


def _build_datasource(project: Project, body: DataSourceCreate) -> DataSource:
    return DataSource(
        organization_id=project.organization_id,
        line_of_business_id=project.line_of_business_id,
        data_domain_id=project.data_domain_id,
        project_id=project.id,
        **body.model_dump(),
    )


def _record_datasource_registration_events(
    session: AsyncSession,
    audit_context: SecurityContext,
    project: Project,
    datasource: DataSource,
) -> None:
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
    _validate_datasource_create(body, settings)
    datasource = _build_datasource(project, body)
    session.add(datasource)
    await session.flush()
    audit_context = replace(context, organization_id=project.organization_id)
    _record_datasource_registration_events(session, audit_context, project, datasource)
    await _commit_or_conflict(session, "datasource name already exists in this project")
    return datasource


@router.post(
    "/projects/{project_id}/datasources/bulk-onboard",
    response_model=DataSourceBulkOnboardResultRead,
)
async def bulk_onboard_datasources(
    project_id: UUID,
    body: DataSourceBulkOnboardRequest,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataAdmin")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DataSourceBulkOnboardResultRead:
    """IN-1: register up to DATASOURCE_BULK_ONBOARD_MAX_ITEMS datasources in one call.

    Every item goes through exactly the same registration path a single
    `create_datasource` call would -- `_validate_datasource_create` and
    `_build_datasource` are the identical functions that endpoint calls, so
    there is no bulk-only shortcut on credential-reference validation,
    connector-type support, or per-project name uniqueness. A bad item (an
    unapproved credential reference, an unsupported connector type, or a name
    that collides with an existing datasource or an earlier item in this same
    batch) fails only that item -- CT-1/RL-6's partial-success precedent, not
    an all-or-nothing transaction. Each item's insert runs inside its own
    SAVEPOINT (`session.begin_nested()`) so a `DataSource.project_id+name`
    uniqueness violation caught at flush time rolls back only that item, never
    the datasources already staged from earlier in the batch.

    No connectivity probe runs here, in or out of a Temporal workflow: the
    single-item path doesn't run one either at registration time (that is
    `test_datasource`, POST `/datasources/{id}/test`, a separate step the
    caller invokes per source after registration), so there is nothing
    per-item-slow to defer for the bulk path either -- 200 items is 200 bounded
    DB writes, not 200 outbound connections.
    """
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)

    existing_names = set(
        await session.scalars(select(DataSource.name).where(DataSource.project_id == project.id))
    )
    audit_context = replace(context, organization_id=project.organization_id)

    results: list[DataSourceBulkOnboardItemRead] = []
    succeeded = 0
    for index, item in enumerate(body.datasources):
        try:
            _validate_datasource_create(item, settings)
            if item.name in existing_names:
                raise HTTPException(
                    status_code=422,
                    detail="datasource name already exists in this project",
                )
        except HTTPException as exc:
            results.append(
                DataSourceBulkOnboardItemRead(
                    index=index,
                    name=item.name,
                    status="FAILED",
                    reason=str(exc.detail),
                )
            )
            continue

        datasource = _build_datasource(project, item)
        try:
            async with session.begin_nested():
                session.add(datasource)
                await session.flush()
        except IntegrityError:
            results.append(
                DataSourceBulkOnboardItemRead(
                    index=index,
                    name=item.name,
                    status="FAILED",
                    reason="datasource name already exists in this project",
                )
            )
            continue

        existing_names.add(item.name)
        _record_datasource_registration_events(session, audit_context, project, datasource)
        results.append(
            DataSourceBulkOnboardItemRead(
                index=index,
                name=item.name,
                status="SUCCEEDED",
                datasource_id=datasource.id,
                reason=None,
            )
        )
        succeeded += 1

    failed = len(results) - succeeded
    record_audit(
        session,
        audit_context,
        action="datasource.bulk_register",
        resource_type="datasource",
        resource_id=None,
        outcome="SUCCESS" if not failed else "PARTIAL_SUCCESS" if succeeded else "FAILURE",
        correlation_id=get_correlation_id(),
        details={
            "project_id": str(project.id),
            "requested_count": len(results),
            "succeeded_count": succeeded,
            "failed_count": failed,
        },
    )
    await session.commit()
    return DataSourceBulkOnboardResultRead(
        requested_count=len(results),
        succeeded_count=succeeded,
        failed_count=failed,
        results=results,
    )


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
    # base_priority tracks the admin's own explicit choice separately from the
    # scheduler-visible `priority` column, so a later usage-weighted rebalance
    # (workflows/scheduler.rebalance_usage_weighted_priorities) always computes
    # from what the admin actually asked for, never from a previously-boosted
    # value (ADR-0017 SS8).
    values["base_priority"] = body.priority
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
        if datasource.status != "ACTIVE":
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
    "/datasources/{datasource_id}/classification-feed/ingest",
    response_model=ClassificationFeedIngestResponse,
)
async def ingest_datasource_classification_feed(
    datasource_id: UUID,
    body: ClassificationFeedIngestRequest,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> ClassificationFeedIngestResponse:
    """Ingest one batch from an authoritative external classification feed.

    Module 05 sec 9, PR-3: a bank's own classification feed is the
    highest-accuracy source and *overrides* deterministic rule-based
    classification -- ``aida.classification_feed.ingest_classification_feed``
    is the single decision point for that override, and every applied record
    appends a ``classification_evidence`` row with
    ``source_type="EXTERNAL_AUTHORITATIVE"`` so provenance (inferred vs.
    externally authoritative) is always inspectable, never silently merged.
    """
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    records = [
        ExternalClassificationRecord(
            schema_name=record.schema_name,
            table_name=record.table_name,
            column_name=record.column_name,
            classification=record.classification,
            source=body.source,
            confidence=record.confidence,
            note=record.note,
        )
        for record in body.records
    ]
    result = await ingest_classification_feed(
        session,
        datasource=datasource,
        records=records,
        created_by=context.principal_id,
    )
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="classification_feed.ingest",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "source": body.source,
            "total": result.total,
            "matched": result.matched,
            "changed": result.changed,
            "unmatched_count": len(result.unmatched),
        },
    )
    if result.changed_column_ids:
        # Matches the catalog's already-documented `classification.assigned` row
        # (Docs/30-contracts/04-event-catalog.md) -- an authoritative-feed
        # override is still "a column classified", just with a different
        # source_type in the payload than a rule-based one.
        record_outbox(
            session,
            organization_id=datasource.organization_id,
            aggregate_type="datasource",
            aggregate_id=str(datasource.id),
            event_type="classification.assigned",
            payload={
                "datasource_id": str(datasource.id),
                "source": body.source,
                "source_type": "EXTERNAL_AUTHORITATIVE",
                "column_ids": list(result.changed_column_ids),
            },
        )
    await session.commit()
    return ClassificationFeedIngestResponse(
        source=body.source,
        total=result.total,
        matched=result.matched,
        changed=result.changed,
        unmatched=list(result.unmatched),
    )


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


@router.get("/analysis-runs/{run_id}/tasks", response_model=Page)
async def list_analysis_run_tasks(
    run_id: UUID,
    task_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Auditor", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """Per-task attempt/heartbeat/failure drill-down for one analysis run.

    Module 05 sec 6/10, PR-4: Temporal already tracks retries and heartbeats
    for every activity, but only inside the cluster. ``analysis_task`` is the
    operator-facing mirror ``aida.task_tracking`` writes at the start, on
    heartbeat, and at the end of every task, so a stuck or failing run can be
    drilled into here without reaching into Temporal directly.
    """
    run = await session.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="analysis run not found")
    enforce_organization(context, run.organization_id)
    filters = [AnalysisTask.analysis_run_id == run.id]
    if task_status:
        filters.append(AnalysisTask.status == task_status.upper())
    total = await session.scalar(select(func.count()).select_from(AnalysisTask).where(*filters))
    rows = (
        await session.scalars(
            select(AnalysisTask)
            .where(*filters)
            .order_by(AnalysisTask.started_at.asc().nulls_last(), AnalysisTask.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[AnalysisTaskRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/analysis-runs/{run_id}/tasks/{task_id}", response_model=AnalysisTaskRead)
async def get_analysis_run_task(
    run_id: UUID,
    task_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Auditor", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> AnalysisTask:
    run = await session.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="analysis run not found")
    enforce_organization(context, run.organization_id)
    task = await session.get(AnalysisTask, task_id)
    if task is None or task.analysis_run_id != run.id:
        raise HTTPException(status_code=404, detail="analysis task not found")
    return task


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


async def gate_read(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    datasource_id: UUID,
) -> None:
    """Authorize a catalog read, or answer 403 with a reason code.

    A helper rather than a dependency because the resource is not known until the
    handler has loaded it: `list_columns` is authorized against the *table*, whose
    datasource it does not learn until after the row is fetched. A dependency would
    have to re-fetch it, and the version that avoids re-fetching is the version that
    authorizes against the path parameter instead of the object -- which is how a
    check ends up describing something other than what the handler returns.

    403 with the bare reason code: enough for a caller to know whether to ask for a
    grant or stop asking, and nothing about the resource or the policy (INV-6).
    """
    try:
        await gate(
            session,
            context,
            settings=settings,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            datasource_id=datasource_id,
        )
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403, detail=exc.reason_code) from exc


async def _list_page(
    session: AsyncSession,
    *,
    model: type[Any],
    filters: Sequence[Any],
    order_columns: tuple[Any, ...],
    coercers: tuple[type, ...],
    read_schema: type[ApiModel],
    limit: int,
    offset: int,
    cursor: str | None,
) -> CursorPage:
    """Shared list-endpoint body: keyset pagination when `cursor` is given, plain
    offset pagination otherwise -- both return a `next_cursor` so a caller can
    fetch page one by `offset` (and see a `total`) and then walk every page after
    it purely by cursor, never paying for another `COUNT(*)` or a growing `OFFSET`.

    This is CT-2: the high-volume catalog list endpoints (tables, columns, and the
    CT-3 indexes/partitions/constraints endpoints that share the same shape) need
    response cost that stays flat regardless of page depth at 1M-tables-by-30M-
    columns scale, which OFFSET cannot give -- the database must walk and discard
    every prior row before it can return anything.

    The keyset branch's `WHERE`/`ORDER BY` use exactly `order_columns` (which
    callers pair with a composite index whose leading columns match `filters`),
    so its cost is bounded by `limit` alone -- independent of how many pages a
    caller has already walked, unlike `offset`.
    """
    total: int | None = None
    if cursor is not None:
        try:
            raw_values = decode_cursor(cursor, arity=len(order_columns))
            last_values = tuple(
                coerce(value) for coerce, value in zip(coercers, raw_values, strict=True)
            )
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
    return CursorPage(
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


@router.get("/datasources/{datasource_id}/tables", response_model=CursorPage)
async def list_tables(
    datasource_id: UUID,
    q: str | None = Query(default=None, min_length=2, max_length=200),
    object_type: str | None = Query(default=None, max_length=30),
    table_status: str = Query(default="ACTIVE", alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CursorPage:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="datasource",
        resource_id=str(datasource.id),
        datasource_id=datasource.id,
    )
    filters: list[Any] = [
        MetadataTable.organization_id == datasource.organization_id,
        MetadataTable.datasource_id == datasource.id,
    ]
    if table_status != "ALL":
        filters.append(MetadataTable.status == table_status)
    if object_type and object_type != "ALL":
        filters.append(MetadataTable.object_type == object_type)
    if q:
        normalized_query = q.strip().lower()
        filters.append(
            or_(
                func.lower(MetadataTable.name).contains(normalized_query),
                func.lower(func.coalesce(MetadataTable.source_description, "")).contains(
                    normalized_query
                ),
            )
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


@router.get("/tables/{table_id}/columns", response_model=CursorPage)
async def list_columns(
    table_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CursorPage:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="table",
        resource_id=str(table.id),
        datasource_id=table.datasource_id,
    )
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


@router.get("/tables/{table_id}/constraints", response_model=CursorPage)
async def list_constraints(
    table_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CursorPage:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="table",
        resource_id=str(table.id),
        datasource_id=table.datasource_id,
    )
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


@router.get("/tables/{table_id}/indexes", response_model=CursorPage)
async def list_indexes(
    table_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CursorPage:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="table",
        resource_id=str(table.id),
        datasource_id=table.datasource_id,
    )
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


@router.get("/tables/{table_id}/partitions", response_model=CursorPage)
async def list_partitions(
    table_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CursorPage:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="table",
        resource_id=str(table.id),
        datasource_id=table.datasource_id,
    )
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
    settings: Settings = Depends(get_settings),
) -> TableProfileRead:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="table",
        resource_id=str(table.id),
        datasource_id=table.datasource_id,
    )
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


# ---------------------------------------------------------------------------
# PR-2: policy-approved range/top-value profiling by classification.
#
# Mirrors `request_cross_boundary_grant`/`decide_governance_review`'s
# maker-checker shape (a different principal must decide than the one who
# requested), but keeps its own denormalized status fields rather than filing
# into the shared `governance_review` queue -- see `ProfilingExceptionPolicy`'s
# docstring in `models.py` for why. The actual value-capture gate this policy
# unlocks lives in `workflows.activities.profile_table_task`, which additionally
# requires the connector to report `capabilities.value_range_profiling`.
# ---------------------------------------------------------------------------

PROFILING_EXCEPTION_REQUEST_ROLES = ("PlatformAdmin", "DataAdmin", "DataSteward")
PROFILING_EXCEPTION_READ_ROLES = (
    "PlatformAdmin",
    "DataAdmin",
    "DataSteward",
    "Reviewer",
    "Viewer",
)
PROFILING_EXCEPTION_DECIDE_ROLES = ("PlatformAdmin", "DataSteward", "Reviewer")


@router.post(
    "/datasources/{datasource_id}/profiling-exception-policies",
    response_model=ProfilingExceptionPolicyRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_profiling_exception_policy(
    datasource_id: UUID,
    body: ProfilingExceptionPolicyCreate,
    context: SecurityContext = Depends(require_roles(*PROFILING_EXCEPTION_REQUEST_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ProfilingExceptionPolicy:
    """Request the value-bearing profiling exception for one classification.

    Created `PENDING`; only becomes `APPROVED` -- and only then eligible for
    `profile_table_task` to act on -- once a *different* principal decides it
    via `POST /profiling-exception-policies/{id}/decision`.
    """
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    classification = body.classification.upper()
    if classification not in SENSITIVE_CLASSES:
        raise HTTPException(
            status_code=422,
            detail=(
                "classification must be one of the sensitive classes: "
                f"{sorted(SENSITIVE_CLASSES)}"
            ),
        )
    existing = await session.scalar(
        select(ProfilingExceptionPolicy).where(
            ProfilingExceptionPolicy.organization_id == datasource.organization_id,
            ProfilingExceptionPolicy.datasource_id == datasource.id,
            ProfilingExceptionPolicy.classification == classification,
            ProfilingExceptionPolicy.status.in_(("PENDING", "APPROVED")),
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="a pending or approved profiling exception policy already covers this scope",
        )
    policy = ProfilingExceptionPolicy(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        classification=classification,
        status="PENDING",
        retention_days=body.retention_days,
        requested_by=context.principal_id,
        request_reason=body.reason,
    )
    session.add(policy)
    await session.flush()
    audit_context = replace(context, organization_id=datasource.organization_id)
    record_audit(
        session,
        audit_context,
        action="profiling_exception_policy.request",
        resource_type="profiling_exception_policy",
        resource_id=str(policy.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"datasource_id": str(datasource.id), "classification": classification},
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="profiling_exception_policy",
        aggregate_id=str(policy.id),
        event_type="profiling_exception_policy.requested.v1",
        payload={
            "policy_id": str(policy.id),
            "datasource_id": str(datasource.id),
            "classification": classification,
        },
    )
    await session.commit()
    return policy


@router.get(
    "/datasources/{datasource_id}/profiling-exception-policies",
    response_model=Page,
)
async def list_profiling_exception_policies(
    datasource_id: UUID,
    policy_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*PROFILING_EXCEPTION_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = [
        ProfilingExceptionPolicy.organization_id == datasource.organization_id,
        ProfilingExceptionPolicy.datasource_id == datasource.id,
    ]
    if policy_status:
        filters.append(ProfilingExceptionPolicy.status == policy_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(ProfilingExceptionPolicy).where(*filters)
    )
    rows = (
        await session.scalars(
            select(ProfilingExceptionPolicy)
            .where(*filters)
            .order_by(ProfilingExceptionPolicy.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[ProfilingExceptionPolicyRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/profiling-exception-policies/{policy_id}/decision",
    response_model=ProfilingExceptionPolicyRead,
)
async def decide_profiling_exception_policy(
    policy_id: UUID,
    body: ProfilingExceptionDecisionRequest,
    context: SecurityContext = Depends(require_roles(*PROFILING_EXCEPTION_DECIDE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ProfilingExceptionPolicy:
    policy = await session.scalar(
        select(ProfilingExceptionPolicy)
        .where(ProfilingExceptionPolicy.id == policy_id)
        .with_for_update()
    )
    if policy is None:
        raise HTTPException(status_code=404, detail="profiling exception policy not found")
    enforce_organization(context, policy.organization_id)
    if policy.status != "PENDING":
        raise HTTPException(
            status_code=409, detail="profiling exception policy is already decided"
        )
    if policy.requested_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker-checker separation is required")
    now = datetime.now(UTC)
    policy.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    policy.decided_by = context.principal_id
    policy.decision_reason = body.reason
    policy.decided_at = now
    audit_context = replace(context, organization_id=policy.organization_id)
    record_audit(
        session,
        audit_context,
        action="profiling_exception_policy.decide",
        resource_type="profiling_exception_policy",
        resource_id=str(policy.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": body.decision, "classification": policy.classification},
    )
    record_outbox(
        session,
        organization_id=policy.organization_id,
        aggregate_type="profiling_exception_policy",
        aggregate_id=str(policy.id),
        event_type="profiling_exception_policy.decided.v1",
        payload={
            "policy_id": str(policy.id),
            "datasource_id": str(policy.datasource_id),
            "classification": policy.classification,
            "status": policy.status,
        },
    )
    await session.commit()
    return policy


@router.post(
    "/profiling-exception-policies/{policy_id}/revoke",
    response_model=ProfilingExceptionPolicyRead,
)
async def revoke_profiling_exception_policy(
    policy_id: UUID,
    body: ProfilingExceptionRevokeRequest,
    context: SecurityContext = Depends(require_roles(*PROFILING_EXCEPTION_DECIDE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ProfilingExceptionPolicy:
    """Immediately withdraws an APPROVED policy's authority to capture values.

    `profile_table_task`'s gate (`profiling_exceptions.approved_policy_for`)
    only ever matches `status == "APPROVED"`, so a revoked policy stops
    authorizing new captures from the moment this commits -- already-captured
    `ColumnValueProfileArtifact` rows are unaffected (they still expire on
    their own pinned schedule; revoking the policy is not itself a retention
    action).
    """
    policy = await session.scalar(
        select(ProfilingExceptionPolicy)
        .where(ProfilingExceptionPolicy.id == policy_id)
        .with_for_update()
    )
    if policy is None:
        raise HTTPException(status_code=404, detail="profiling exception policy not found")
    enforce_organization(context, policy.organization_id)
    if policy.status != "APPROVED":
        raise HTTPException(
            status_code=409, detail="only an approved profiling exception policy can be revoked"
        )
    now = datetime.now(UTC)
    policy.status = "REVOKED"
    policy.revoked_by = context.principal_id
    policy.revoked_at = now
    policy.revocation_reason = body.reason
    audit_context = replace(context, organization_id=policy.organization_id)
    record_audit(
        session,
        audit_context,
        action="profiling_exception_policy.revoke",
        resource_type="profiling_exception_policy",
        resource_id=str(policy.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"classification": policy.classification, "reason": body.reason},
    )
    record_outbox(
        session,
        organization_id=policy.organization_id,
        aggregate_type="profiling_exception_policy",
        aggregate_id=str(policy.id),
        event_type="profiling_exception_policy.revoked.v1",
        payload={
            "policy_id": str(policy.id),
            "datasource_id": str(policy.datasource_id),
            "classification": policy.classification,
        },
    )
    await session.commit()
    return policy


# ---------------------------------------------------------------------------
# CT-5: asset certification lifecycle with expiry (table or column)
# ---------------------------------------------------------------------------


def _asset_certification_read(
    certification: AssetCertification, *, is_active: bool
) -> AssetCertificationRead:
    return AssetCertificationRead(
        id=certification.id,
        organization_id=certification.organization_id,
        table_id=certification.table_id,
        column_id=certification.column_id,
        asset_type=certification.asset_type,
        status=certification.status,
        rationale=certification.rationale,
        certified_by=certification.certified_by,
        expires_at=certification.expires_at,
        is_active=is_active,
        created_at=certification.created_at,
        updated_at=certification.updated_at,
    )


@router.post(
    "/tables/{table_id}/certification",
    response_model=AssetCertificationRead,
    status_code=status.HTTP_201_CREATED,
)
async def certify_table_asset(
    table_id: UUID,
    body: CertificationDecisionRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> AssetCertificationRead:
    """Module 04's public interface: ``certify_asset(scope, table_id, decision)``.

    Certifies the table itself, or -- module 04's scale note names column as
    the dominant catalog entity -- one specific column of it. Immediate and
    role-gated, the same as CT-1's bulk certify action on this same table:
    a single deliberate certification by an authorized steward, not a batch,
    so there is no maker-checker review to wait on.
    """
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    if table.status != "ACTIVE":
        raise HTTPException(status_code=409, detail=f"table status is {table.status}, not ACTIVE")
    now = datetime.now(UTC)
    if body.expires_at <= now:
        raise HTTPException(status_code=422, detail="certification expiry must be in the future")
    column: MetadataColumn | None = None
    if body.asset_type == "COLUMN":
        column = await session.get(MetadataColumn, body.column_id)
        if (
            column is None
            or column.organization_id != table.organization_id
            or column.table_id != table.id
        ):
            raise HTTPException(status_code=404, detail="column not found on this table")
        if column.status != "ACTIVE":
            raise HTTPException(
                status_code=409, detail=f"column status is {column.status}, not ACTIVE"
            )
    prior_rows = (
        await session.scalars(
            select(AssetCertification).where(
                AssetCertification.table_id == table.id,
                AssetCertification.asset_type == body.asset_type,
                AssetCertification.column_id == (column.id if column else None),
                AssetCertification.status == "ACTIVE",
            )
        )
    ).all()
    for prior in prior_rows:
        prior.status = "SUPERSEDED"
    certification = AssetCertification(
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=column.id if column else None,
        asset_type=body.asset_type,
        rationale=body.rationale,
        certified_by=context.principal_id,
        expires_at=body.expires_at,
    )
    session.add(certification)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=table.organization_id),
        action="catalog.asset.certify",
        resource_type="asset_certification",
        resource_id=str(certification.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "table_id": str(table.id),
            "column_id": str(column.id) if column else None,
            "asset_type": body.asset_type,
            "expires_at": body.expires_at.isoformat(),
            "superseded_count": len(prior_rows),
        },
    )
    record_outbox(
        session,
        organization_id=table.organization_id,
        aggregate_type="asset_certification",
        aggregate_id=str(certification.id),
        event_type="catalog.asset.certified.v1",
        payload={
            "certification_id": str(certification.id),
            "table_id": str(table.id),
            "column_id": str(column.id) if column else None,
            "asset_type": body.asset_type,
            "expires_at": body.expires_at.isoformat(),
        },
    )
    await session.commit()
    return _asset_certification_read(
        certification, is_active=asset_certification_is_active(certification, at=now)
    )


@router.get("/tables/{table_id}/certification", response_model=AssetCertificationRead)
async def get_table_certification(
    table_id: UUID,
    column_id: UUID | None = Query(default=None),
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AssetCertificationRead:
    """The currently *active* certification for a table, or one of its columns.

    Expiry is enforced here rather than trusted from ``status``: a certification
    row keeps reading back ``status == "ACTIVE"`` after ``expires_at`` passes
    (see ``aida.asset_certification``), so this 404s once the active one has
    expired, even though the row itself is still sitting there as evidence.
    """
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="table",
        resource_id=str(table.id),
        datasource_id=table.datasource_id,
    )
    asset_type = "TABLE"
    if column_id is not None:
        column = await session.get(MetadataColumn, column_id)
        if (
            column is None
            or column.organization_id != table.organization_id
            or column.table_id != table.id
        ):
            raise HTTPException(status_code=404, detail="column not found on this table")
        asset_type = "COLUMN"
    rows = (
        await session.scalars(
            select(AssetCertification)
            .where(
                AssetCertification.table_id == table.id,
                AssetCertification.asset_type == asset_type,
                AssetCertification.column_id == column_id,
            )
            .order_by(AssetCertification.created_at.desc())
            .limit(20)
        )
    ).all()
    active = current_asset_certification(list(rows), at=datetime.now(UTC))
    if active is None:
        raise HTTPException(status_code=404, detail="no active certification found")
    return _asset_certification_read(active, is_active=True)


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
            workspace_id=body.workspace_id,
        )
    except AuthorizationRejected as exc:
        # Before `QueryRejected`, which it subclasses. 403 rather than 422 because the
        # statement was never the problem -- resubmitting a corrected one changes
        # nothing, and 422 would send the caller off to fix their SQL.
        raise HTTPException(status_code=403, detail=exc.reason_code) from exc
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


# ---------------------------------------------------------------------------
# CT-1: Catalog bulk actions (tag, classify, own, certify)
# ---------------------------------------------------------------------------


async def _resolve_bulk_table_subjects(
    session: AsyncSession,
    *,
    organization_id: UUID,
    table_ids: list[UUID] | None,
    selection_filter: CatalogBulkSelectionFilter | None,
) -> tuple[list[UUID], str, bool]:
    if table_ids is not None:
        return dedupe_preserving_order(table_ids), "EXPLICIT", False
    assert selection_filter is not None
    datasource = await session.get(DataSource, selection_filter.datasource_id)
    if datasource is None or datasource.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="data source not found")
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.datasource_id == selection_filter.datasource_id,
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataTable.id)
            .limit(CATALOG_BULK_FILTER_SCAN_CAP)
        )
    ).all()
    candidates = [(row[0], row[1]) for row in rows]
    matched, truncated = match_tables_by_filter(
        candidates,
        match_field=selection_filter.match_field,
        match_pattern=selection_filter.match_pattern,
        cap=CATALOG_BULK_ACTION_MAX_ITEMS,
    )
    if not matched:
        raise HTTPException(status_code=409, detail="filter matched no active tables")
    return matched, "FILTER", truncated


async def _fetch_bulk_tables(
    session: AsyncSession, *, organization_id: UUID, table_ids: list[UUID]
) -> dict[UUID, MetadataTable]:
    rows = (
        await session.scalars(
            select(MetadataTable).where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.id.in_(table_ids),
            )
        )
    ).all()
    return {row.id: row for row in rows}


async def _persist_catalog_bulk_action_run(
    session: AsyncSession,
    *,
    context: SecurityContext,
    organization_id: UUID,
    action: str,
    selection_mode: str,
    parameters: dict[str, Any],
    plan: BulkPlan,
) -> CatalogBulkActionRun:
    run = CatalogBulkActionRun(
        organization_id=organization_id,
        action=action,
        selection_mode=selection_mode,
        parameters=parameters,
        requested_count=len(plan.results),
        succeeded_count=plan.succeeded_count,
        failed_count=plan.failed_count,
        results=[item.as_dict() for item in plan.results],
        requested_by=context.principal_id,
    )
    session.add(run)
    await session.flush()
    if plan.succeeded_count and plan.failed_count:
        outcome = "PARTIAL_SUCCESS"
    elif plan.succeeded_count:
        outcome = "SUCCESS"
    else:
        outcome = "FAILURE"
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action=f"catalog.bulk_{action.lower()}",
        resource_type="catalog_bulk_action_run",
        resource_id=str(run.id),
        outcome=outcome,
        correlation_id=get_correlation_id(),
        details={
            "requested_count": run.requested_count,
            "succeeded_count": run.succeeded_count,
            "failed_count": run.failed_count,
            "selection_mode": selection_mode,
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="catalog_bulk_action_run",
        aggregate_id=str(run.id),
        event_type=_CATALOG_BULK_ACTION_EVENT_TYPES[action],
        payload={
            "run_id": str(run.id),
            "action": action,
            "succeeded_count": run.succeeded_count,
            "failed_count": run.failed_count,
        },
    )
    return run


@router.post(
    "/organizations/{organization_id}/tables/bulk-tag",
    response_model=CatalogBulkActionRunRead,
)
async def bulk_tag_tables(
    organization_id: UUID,
    body: CatalogBulkTagRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CatalogBulkActionRun:
    enforce_organization(context, organization_id)
    subject_ids, selection_mode, truncated = await _resolve_bulk_table_subjects(
        session,
        organization_id=organization_id,
        table_ids=body.table_ids,
        selection_filter=body.filter,
    )
    tables = await _fetch_bulk_tables(
        session, organization_id=organization_id, table_ids=subject_ids
    )
    existing_tags = (
        await session.scalars(
            select(AssetTag).where(
                AssetTag.table_id.in_(subject_ids),
                AssetTag.tag_key == body.tag_key,
            )
        )
    ).all()
    plan = plan_tag(
        subject_ids,
        tables=tables,
        existing_tags={row.table_id: row for row in existing_tags},
        organization_id=organization_id,
        tag_key=body.tag_key,
        tag_value=body.tag_value,
        applied_by=context.principal_id,
    )
    for row in plan.new_rows:
        session.add(row)
    run = await _persist_catalog_bulk_action_run(
        session,
        context=context,
        organization_id=organization_id,
        action="TAG",
        selection_mode=selection_mode,
        parameters={
            "tag_key": body.tag_key,
            "tag_value": body.tag_value,
            "selection_truncated": truncated,
        },
        plan=plan,
    )
    await session.commit()
    return run


@router.post(
    "/organizations/{organization_id}/tables/bulk-classify",
    response_model=CatalogBulkActionRunRead,
)
async def bulk_classify_columns(
    organization_id: UUID,
    body: CatalogBulkClassifyRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CatalogBulkActionRun:
    enforce_organization(context, organization_id)
    truncated = False
    if body.column_ids is not None:
        subject_ids = dedupe_preserving_order(body.column_ids)
        selection_mode = "EXPLICIT"
    else:
        table_ids, _, table_truncated = await _resolve_bulk_table_subjects(
            session,
            organization_id=organization_id,
            table_ids=body.table_ids,
            selection_filter=body.filter,
        )
        selection_mode = "EXPLICIT" if body.table_ids is not None else "FILTER"
        truncated = truncated or table_truncated
        column_rows = (
            await session.scalars(
                select(MetadataColumn)
                .where(
                    MetadataColumn.organization_id == organization_id,
                    MetadataColumn.table_id.in_(table_ids),
                    MetadataColumn.status == "ACTIVE",
                )
                .order_by(MetadataColumn.id)
                .limit(CATALOG_BULK_FILTER_SCAN_CAP)
            )
        ).all()
        subject_ids, column_truncated = match_columns_by_pattern(
            column_rows,
            name_pattern=body.column_name_pattern,
            cap=CATALOG_BULK_ACTION_MAX_ITEMS,
        )
        truncated = truncated or column_truncated
        if not subject_ids:
            raise HTTPException(status_code=409, detail="selection matched no active columns")
    rows = (
        await session.execute(
            select(MetadataColumn, MetadataTable)
            .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
            .where(
                MetadataColumn.organization_id == organization_id,
                MetadataColumn.id.in_(subject_ids),
            )
        )
    ).all()
    columns = {row[0].id: (row[0], row[1]) for row in rows}
    plan = plan_classify(
        subject_ids,
        columns=columns,
        classification=body.classification,
    )
    run = await _persist_catalog_bulk_action_run(
        session,
        context=context,
        organization_id=organization_id,
        action="CLASSIFY",
        selection_mode=selection_mode,
        parameters={
            "classification": body.classification,
            "column_name_pattern": body.column_name_pattern,
            "selection_truncated": truncated,
        },
        plan=plan,
    )
    await session.commit()
    return run


@router.post(
    "/organizations/{organization_id}/tables/bulk-own",
    response_model=CatalogBulkActionRunRead,
)
async def bulk_assign_ownership(
    organization_id: UUID,
    body: CatalogBulkOwnRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CatalogBulkActionRun:
    enforce_organization(context, organization_id)
    subject_ids, selection_mode, truncated = await _resolve_bulk_table_subjects(
        session,
        organization_id=organization_id,
        table_ids=body.table_ids,
        selection_filter=body.filter,
    )
    tables = await _fetch_bulk_tables(
        session, organization_id=organization_id, table_ids=subject_ids
    )
    existing_assignments = (
        await session.scalars(
            select(OwnershipAssignment).where(
                OwnershipAssignment.organization_id == organization_id,
                OwnershipAssignment.subject_type == "TABLE",
                OwnershipAssignment.subject_id.in_([str(value) for value in subject_ids]),
                OwnershipAssignment.owner_type == body.owner_type,
                OwnershipAssignment.owner_principal == body.owner_principal,
            )
        )
    ).all()
    plan = plan_own(
        subject_ids,
        tables=tables,
        existing_assignments={UUID(row.subject_id): row for row in existing_assignments},
        organization_id=organization_id,
        owner_type=body.owner_type,
        owner_principal=body.owner_principal,
        assigned_by=context.principal_id,
    )
    for row in plan.new_rows:
        session.add(row)
    run = await _persist_catalog_bulk_action_run(
        session,
        context=context,
        organization_id=organization_id,
        action="OWN",
        selection_mode=selection_mode,
        parameters={
            "owner_type": body.owner_type,
            "owner_principal": body.owner_principal,
            "selection_truncated": truncated,
        },
        plan=plan,
    )
    await session.commit()
    return run


@router.post(
    "/organizations/{organization_id}/tables/bulk-certify",
    response_model=CatalogBulkActionRunRead,
)
async def bulk_certify_tables(
    organization_id: UUID,
    body: CatalogBulkCertifyRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CatalogBulkActionRun:
    enforce_organization(context, organization_id)
    if body.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="certification expiry must be in the future")
    subject_ids, selection_mode, truncated = await _resolve_bulk_table_subjects(
        session,
        organization_id=organization_id,
        table_ids=body.table_ids,
        selection_filter=body.filter,
    )
    tables = await _fetch_bulk_tables(
        session, organization_id=organization_id, table_ids=subject_ids
    )
    active_certifications = (
        await session.scalars(
            select(AssetCertification).where(
                AssetCertification.table_id.in_(subject_ids),
                # CT-5: certification is now also column-scoped (`asset_type ==
                # "COLUMN"`), with `table_id` still denormalized onto those rows
                # for lookup. Table-level bulk certify must only ever supersede a
                # prior *table*-level certification, never a column's.
                AssetCertification.asset_type == "TABLE",
                AssetCertification.status == "ACTIVE",
            )
        )
    ).all()
    grouped_certifications: dict[UUID, list[AssetCertification]] = {}
    for row in active_certifications:
        grouped_certifications.setdefault(row.table_id, []).append(row)
    plan = plan_certify(
        subject_ids,
        tables=tables,
        active_certifications=grouped_certifications,
        organization_id=organization_id,
        rationale=body.rationale,
        expires_at=body.expires_at,
        certified_by=context.principal_id,
    )
    for row in plan.new_rows:
        session.add(row)
    run = await _persist_catalog_bulk_action_run(
        session,
        context=context,
        organization_id=organization_id,
        action="CERTIFY",
        selection_mode=selection_mode,
        parameters={
            "rationale": body.rationale,
            "expires_at": body.expires_at.isoformat(),
            "selection_truncated": truncated,
        },
        plan=plan,
    )
    await session.commit()
    return run


@router.get(
    "/organizations/{organization_id}/catalog-bulk-actions",
    response_model=Page,
)
async def list_catalog_bulk_action_runs(
    organization_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = (CatalogBulkActionRun.organization_id == organization_id,)
    total = await session.scalar(
        select(func.count()).select_from(CatalogBulkActionRun).where(*filters)
    )
    rows = (
        await session.scalars(
            select(CatalogBulkActionRun)
            .where(*filters)
            .order_by(CatalogBulkActionRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[CatalogBulkActionRunRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/organizations/{organization_id}/catalog-bulk-actions/{run_id}",
    response_model=CatalogBulkActionRunRead,
)
async def get_catalog_bulk_action_run(
    organization_id: UUID,
    run_id: UUID,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CatalogBulkActionRun:
    enforce_organization(context, organization_id)
    run = await session.get(CatalogBulkActionRun, run_id)
    if run is None or run.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="catalog bulk action run not found")
    return run
