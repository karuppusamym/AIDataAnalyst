from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
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
from aida.agent_run_replay import resolve_grounding
from aida.authorization_gate import gate_read
from aida.classification import SENSITIVE_CLASSES
from aida.classification_feed import ExternalClassificationRecord, ingest_classification_feed
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled, reserve_analysis_run
from aida.graph_store import GraphStoreUnavailable, build_graph_store, resolve_graph_store_backend
from aida.integration_service import ensure_organization_integration_policy
from aida.model_gateway import SUPPORTED_MODEL_PROVIDERS
from aida.models import (
    AgentEvaluationRun,
    AgentRun,
    AnalysisRun,
    AnalysisTask,
    ColumnProfile,
    DataSource,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataIndex,
    MetadataPartition,
    MetadataSchema,
    MetadataTable,
    Organization,
    OrganizationIntegrationPolicy,
    ProfilingExceptionPolicy,
    QueryExecution,
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
    AgentRunGroundingReceiptsRead,
    AgentRunRead,
    AiRuntimeStatusRead,
    AnalysisRunCreate,
    AnalysisRunRead,
    AnalysisTaskRead,
    ApiModel,
    ClassificationFeedIngestRequest,
    ClassificationFeedIngestResponse,
    ColumnProfileRead,
    CursorPage,
    GraphSummaryRead,
    GroundingFragmentReceiptRead,
    MetadataColumnRead,
    MetadataConstraintRead,
    MetadataIndexRead,
    MetadataPartitionRead,
    MetadataTableRead,
    OrganizationIntegrationPolicyRead,
    OrganizationIntegrationPolicyWrite,
    Page,
    ProfilingExceptionDecisionRequest,
    ProfilingExceptionPolicyCreate,
    ProfilingExceptionPolicyRead,
    ProfilingExceptionRevokeRequest,
    QueryExecutionRequest,
    QueryExecutionResponse,
    QueryLineageRead,
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
                f"classification must be one of the sensitive classes: {sorted(SENSITIVE_CLASSES)}"
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
        raise HTTPException(status_code=409, detail="profiling exception policy is already decided")
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
    """Reconciles the configured graph store's projection against PostgreSQL's
    authoritative counts (C7 / ADR-0020 amendment, Group J: the backend is now
    a per-organization setting, resolved through `aida.graph_store`, rather
    than always Neo4j). A `disabled` organization gets an explicit 503, not a
    zeroed or degraded summary (INV-4)."""
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)

    backend = await resolve_graph_store_backend(session, datasource.organization_id, settings)
    if backend == "disabled":
        raise HTTPException(status_code=503, detail="graph store disabled for this organization")
    try:
        summary = await build_graph_store(backend, settings).graph_summary(session, datasource)
    except GraphStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="metadata graph unavailable") from exc
    catalogs = summary.catalogs
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
        "schemas": summary.schemas,
        "tables": summary.tables,
        "columns": summary.columns,
        "constraints": summary.constraints,
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
        sensitive_columns=summary.sensitive_columns,
        constraints=projected["constraints"],
        foreign_key_relationships=summary.foreign_key_relationships,
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
        # 2026-09-03: a 429 from the provider surfaces as HTTP 429 so the
        # client can render "provider throttled, try again" rather than the
        # same "no model route" message shown for a genuinely-unconfigured
        # route. Everything else stays 503.
        if exc.provider_status_code == 429:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
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


@router.get(
    "/agent-runs/{agent_run_id}/grounding-receipts",
    response_model=AgentRunGroundingReceiptsRead,
)
async def get_agent_run_grounding_receipts(
    agent_run_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "Analyst", "AgentDeveloper", "Viewer", "Auditor")
    ),
    session: AsyncSession = Depends(get_session),
) -> AgentRunGroundingReceiptsRead:
    """AT-6 replay proof: resolve every grounding-fragment digest stored on this
    run back to the exact content it was hashed from. A `BUSINESS_ANNOTATION`
    fragment resolves to its `MetadataBusinessAnnotationVersion` -- which, being
    append-only (`business_annotation_versions.py`), is still exactly the
    content this run saw even if a later approval has since superseded it.
    `digest_verified` recomputes the digest from that stored content and
    confirms it still matches what was recorded on the run at grounding time.
    """
    agent_run = await session.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise HTTPException(status_code=404, detail="agent run not found")
    enforce_organization(context, agent_run.organization_id)
    if "PlatformAdmin" not in context.roles and "Auditor" not in context.roles:
        if agent_run.principal_id != context.principal_id:
            raise HTTPException(status_code=403, detail="agent run belongs to another principal")
    resolved = await resolve_grounding(session, agent_run)
    return AgentRunGroundingReceiptsRead(
        agent_run_id=agent_run.id,
        fragment_count=len(resolved),
        fragments=[
            GroundingFragmentReceiptRead(
                object_type=fragment.object_type,
                object_id=fragment.object_id,
                fragment_digest=fragment.fragment_digest,
                annotation_version_id=(
                    UUID(fragment.annotation_version_id) if fragment.annotation_version_id else None
                ),
                annotation_version=(
                    fragment.resolved_annotation_version.version
                    if fragment.resolved_annotation_version is not None
                    else None
                ),
                annotation_status=fragment.current_status,
                business_name=(
                    fragment.resolved_annotation_version.business_name
                    if fragment.resolved_annotation_version is not None
                    else None
                ),
                business_description=(
                    fragment.resolved_annotation_version.business_description
                    if fragment.resolved_annotation_version is not None
                    else None
                ),
                digest_verified=fragment.digest_verified,
            )
            for fragment in resolved
        ],
    )
