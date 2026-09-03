import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import select, text
from temporalio.client import Client

from aida import __version__
from aida.access_review_api import router as access_review_router
from aida.agent_roster_api import router as agent_roster_router
from aida.ai_decision_lineage_api import router as ai_decision_lineage_router
from aida.ai_governance_api import router as ai_governance_router
from aida.ai_registry_api import router as ai_registry_router
from aida.api import router
from aida.asset_description_api import router as asset_description_router
from aida.asset_evidence_api import router as asset_evidence_router
from aida.bi_api import router as bi_router
from aida.compliance_api import router as compliance_router
from aida.composite_key_api import router as composite_key_router
from aida.config import Settings, get_settings
from aida.consumption_lineage_api import router as consumption_lineage_router
from aida.context import correlation_id_var
from aida.context_compiler_api import router as context_compiler_router
from aida.context_product_api import router as context_product_router
from aida.db import session_factory
from aida.dbt_api import router as dbt_router
from aida.delegation_api import router as delegation_router
from aida.detokenization_api import router as detokenization_router
from aida.document_ingestion_api import router as document_ingestion_router
from aida.glossary_api import router as glossary_router
from aida.graph_perspectives_api import router as graph_perspectives_router
from aida.ingestion_api import router as ingestion_router
from aida.intelligence_api import router as intelligence_router
from aida.lineage_evidence_export_api import router as lineage_evidence_export_router
from aida.logging import configure_logging
from aida.marketplace_discovery import router as marketplace_discovery_router
from aida.mcp_server import router as mcp_router
from aida.metric_suggestion_api import router as metric_suggestion_router
from aida.models import Organization
from aida.negative_knowledge_api import router as negative_knowledge_router
from aida.notification_api import router as notification_router
from aida.observability import (
    MetricsConfig,
    TracingConfig,
    configure_metrics,
    configure_tracing,
    record_counter,
    traced,
)
from aida.observability_api import router as observability_router
from aida.openlineage_api import router as openlineage_router
from aida.operational_api import router as operational_router
from aida.persona_api import router as persona_router
from aida.playbooks_api import router as playbooks_router
from aida.policy_native_sync_api import router as policy_native_sync_router
from aida.procedure_lineage_api import router as procedure_lineage_router
from aida.procedure_tool_api import router as procedure_tool_router
from aida.product_marketplace_api import router as product_marketplace_router
from aida.quality_api import router as quality_router
from aida.review_queue_api import router as review_queue_router
from aida.runtime_contracts_api import router as runtime_contracts_router
from aida.schemas import HealthResponse
from aida.search_api import router as search_router
from aida.semantic_api import router as semantic_router
from aida.semantic_intelligence_api import router as semantic_intelligence_router
from aida.sql_validation_api import router as sql_validation_router
from aida.stewardship_api import router as stewardship_router
from aida.studio_api import router as studio_router
from aida.table_family_api import router as table_family_router
from aida.token_revocation_api import router as token_revocation_router
from aida.tool_api import router as tool_router
from aida.tool_plans_api import router as tool_plans_router
from aida.unified_lineage_api import router as unified_lineage_router
from aida.view_lineage_api import router as view_lineage_router
from aida.workspace_api import router as workspace_router
from aida.worm_archive import ArchiveConfig, archive_pending_audit_events

settings = get_settings()
configure_logging(settings.log_level)
logger = structlog.get_logger(__name__)

REQUEST_COUNT = Counter(
    "aida_http_requests_total",
    "HTTP requests",
    labelnames=("method", "path", "status"),
)
REQUEST_LATENCY = Histogram(
    "aida_http_request_duration_seconds",
    "HTTP request latency",
    labelnames=("method", "path"),
)


async def _audit_archive_loop(loop_settings: Settings) -> None:
    """OB-3: periodically sweep unarchived `AuditEvent` rows into an
    immutable `AuditArchiveRecord` per organization, via
    `aida.worm_archive.archive_pending_audit_events`.

    Started/cancelled from `lifespan`; runs for the life of the process. A
    failed cycle logs and retries on the next interval instead of crashing
    the task -- archival lagging behind is recoverable, an unhandled task
    exception silently killing the sweep forever is the OB-3 failure mode
    the audit found (an endpoint that reports zeros while looking healthy).
    """
    config = ArchiveConfig(
        retention_days=loop_settings.audit_archive_retention_days,
        storage_backend=loop_settings.audit_archive_storage_backend,
        bucket_name=loop_settings.audit_archive_bucket_name,
        legal_hold_enabled=loop_settings.audit_archive_legal_hold_enabled,
        classification=loop_settings.audit_archive_classification,
    )
    while True:
        await asyncio.sleep(loop_settings.audit_archive_interval_seconds)
        try:
            async with session_factory() as session:
                org_ids = (await session.scalars(select(Organization.id))).all()
                archived_any = False
                for org_id in org_ids:
                    result = await archive_pending_audit_events(
                        session,
                        org_id,
                        config,
                        batch_size=loop_settings.audit_archive_batch_size,
                    )
                    if result is not None:
                        archived_any = True
                if archived_any:
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("audit_archive_cycle_failed")


async def _connect_temporal(loop_settings: Settings) -> Client | None:
    """AU-12: bounded, non-fatal Temporal connect.

    `Client.connect` performs a real RPC handshake (lazy=False, the default)
    with no built-in timeout of its own, so an unreachable or slow Temporal
    server can hang or raise indefinitely. This bounds the attempt to
    `temporal_connect_timeout_seconds` and turns any failure -- timeout,
    connection refused, DNS failure, whatever -- into a logged `None` rather
    than an exception, so callers (`lifespan` at startup,
    `_temporal_reconnect_loop` afterward) never need their own try/except
    around the connect call itself. `None` is the app's "Temporal degraded"
    state: `/health/ready` already reports it as `temporal: DOWN` (see
    `readiness` below) once this returns.
    """
    try:
        return await asyncio.wait_for(
            Client.connect(
                loop_settings.temporal_address,
                namespace=loop_settings.temporal_namespace,
            ),
            timeout=loop_settings.temporal_connect_timeout_seconds,
        )
    except Exception:
        logger.warning(
            "temporal_connect_failed",
            temporal_address=loop_settings.temporal_address,
            temporal_namespace=loop_settings.temporal_namespace,
            timeout_seconds=loop_settings.temporal_connect_timeout_seconds,
            exc_info=True,
        )
        return None


async def _temporal_reconnect_loop(app: FastAPI, loop_settings: Settings) -> None:
    """AU-12: background retry after a failed startup connect.

    Only ever runs when `lifespan` found `app.state.temporal_client` still
    `None` after the initial attempt -- a live outage at process start. Polls
    on `temporal_reconnect_interval_seconds` and, on success, publishes the
    new client onto `app.state.temporal_client` so `/health/ready` (which
    reads that same attribute) flips back to `temporal: UP` on its very next
    call, with no restart needed. Mirrors `_audit_archive_loop`'s shape
    (started/cancelled from `lifespan`, `CancelledError` re-raised so
    shutdown cancellation is never swallowed) for consistency with this
    module's one existing background-task convention.
    """
    while app.state.temporal_client is None:
        await asyncio.sleep(loop_settings.temporal_reconnect_interval_seconds)
        # `_connect_temporal` already converts every non-cancellation failure into
        # a logged `None` -- nothing left for this loop to catch except
        # CancelledError, which it must not swallow, so it is left unhandled here.
        client = await _connect_temporal(loop_settings)
        if client is not None:
            app.state.temporal_client = client
            logger.info(
                "temporal_reconnected",
                temporal_address=loop_settings.temporal_address,
                temporal_namespace=loop_settings.temporal_namespace,
            )


@traced
async def _traced_dispatch(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
    *,
    correlation_id: str,
) -> Response:
    """OB-1: every request is dispatched through this @traced call so a real
    span (and, once metrics are configured, a real OTEL metric) is produced
    from process start -- not only when some future caller opts in.
    """
    return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.temporal_client = None
    temporal_reconnect_task: asyncio.Task[None] | None = None
    if settings.temporal_enabled:
        app.state.temporal_client = await _connect_temporal(settings)
        if app.state.temporal_client is None:
            # AU-12: a Temporal outage at startup no longer aborts process
            # startup -- the app comes up degraded (temporal-dependent routes
            # unavailable, /health/ready reports `temporal: DOWN`) instead of
            # never starting, and this task retries in the background so it
            # recovers on its own once Temporal is reachable again.
            logger.warning(
                "temporal_unavailable_at_startup",
                temporal_address=settings.temporal_address,
                retry_interval_seconds=settings.temporal_reconnect_interval_seconds,
            )
            temporal_reconnect_task = asyncio.create_task(
                _temporal_reconnect_loop(app, settings)
            )
        else:
            logger.info("temporal_connected", temporal_address=settings.temporal_address)
    app.state.temporal_reconnect_task = temporal_reconnect_task

    tracing_active = configure_tracing(
        TracingConfig(
            endpoint=settings.otel_endpoint,
            service_name=settings.service_name,
            insecure=settings.otel_insecure,
            enabled=settings.otel_tracing_enabled,
            exporter=settings.otel_exporter,
        )
    )
    metrics_active = configure_metrics(
        MetricsConfig(
            endpoint=settings.otel_endpoint,
            service_name=settings.service_name,
            insecure=settings.otel_insecure,
            enabled=settings.otel_metrics_enabled,
            exporter=settings.otel_exporter,
            export_interval_millis=settings.otel_metrics_export_interval_millis,
        )
    )
    logger.info(
        "observability_configured",
        tracing=tracing_active,
        metrics=metrics_active,
        exporter=settings.otel_exporter,
    )

    archive_task: asyncio.Task[None] | None = None
    if settings.audit_archive_enabled:
        archive_task = asyncio.create_task(_audit_archive_loop(settings))
    app.state.audit_archive_task = archive_task

    logger.info(
        "service_started",
        service=settings.service_name,
        environment=settings.environment,
        version=__version__,
    )
    yield
    if archive_task is not None:
        archive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await archive_task
    if temporal_reconnect_task is not None:
        temporal_reconnect_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await temporal_reconnect_task
    logger.info("service_stopped", service=settings.service_name)


app = FastAPI(
    title="Bank Data Intelligence Platform API",
    version=__version__,
    description="Governed metadata, semantic intelligence, and analytical control-plane API.",
    lifespan=lifespan,
)
app.include_router(router)
app.include_router(workspace_router)
app.include_router(semantic_router)
app.include_router(tool_router)
app.include_router(operational_router)
app.include_router(intelligence_router)
app.include_router(ai_governance_router)
app.include_router(ai_registry_router)
app.include_router(agent_roster_router)
app.include_router(dbt_router)
app.include_router(composite_key_router)
app.include_router(graph_perspectives_router)
app.include_router(openlineage_router)
app.include_router(bi_router)
app.include_router(semantic_intelligence_router)
app.include_router(sql_validation_router)
app.include_router(quality_router)
app.include_router(ingestion_router)
app.include_router(glossary_router)
app.include_router(stewardship_router)
app.include_router(unified_lineage_router)
app.include_router(lineage_evidence_export_router)
app.include_router(context_product_router)
app.include_router(context_compiler_router)
app.include_router(product_marketplace_router)
app.include_router(marketplace_discovery_router)
app.include_router(search_router)
app.include_router(access_review_router)
app.include_router(ai_decision_lineage_router)
app.include_router(view_lineage_router)
app.include_router(studio_router)
app.include_router(notification_router)
app.include_router(observability_router)
app.include_router(consumption_lineage_router)
app.include_router(runtime_contracts_router)
app.include_router(compliance_router)
app.include_router(negative_knowledge_router)
app.include_router(tool_plans_router)
app.include_router(table_family_router)
app.include_router(token_revocation_router)
app.include_router(detokenization_router)
app.include_router(document_ingestion_router)
app.include_router(delegation_router)
app.include_router(persona_router)
app.include_router(playbooks_router)
app.include_router(asset_description_router)
app.include_router(asset_evidence_router)
app.include_router(metric_suggestion_router)
app.include_router(policy_native_sync_router)
app.include_router(review_queue_router)
# Group I addition (Atlas Wave-2, tracker N3/N12).
app.include_router(procedure_lineage_router)
app.include_router(procedure_tool_router)
app.include_router(
    mcp_router
)  # MCP server: POST /mcp — governed tool & catalog access for AI agents


@app.middleware("http")
async def request_context(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    correlation_id = request.headers.get("X-Correlation-Id") or str(uuid4())
    token = correlation_id_var.set(correlation_id)
    started = perf_counter()
    try:
        response = await _traced_dispatch(request, call_next, correlation_id=correlation_id)
    except Exception:
        logger.exception(
            "unhandled_request_error",
            correlation_id=correlation_id,
            method=request.method,
            path=request.url.path,
        )
        response = JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_SERVER_ERROR",
                    "message": "an unexpected error occurred",
                    "correlation_id": correlation_id,
                }
            },
        )
    finally:
        correlation_id_var.reset(token)
    elapsed = perf_counter() - started
    route = request.scope.get("route")
    path_template = getattr(route, "path", request.url.path)
    REQUEST_COUNT.labels(request.method, path_template, str(response.status_code)).inc()
    REQUEST_LATENCY.labels(request.method, path_template).observe(elapsed)
    # OB-1: OTEL-native counterpart to REQUEST_COUNT above -- a no-op unless
    # configure_metrics succeeded (see lifespan), so this never adds request
    # latency or a hard dependency on the OTLP SDK being installed.
    record_counter(
        "aida_http_requests_total",
        method=request.method,
        path=path_template,
        status=str(response.status_code),
    )
    response.headers["X-Correlation-Id"] = correlation_id
    return response


@app.get("/health/live", response_model=HealthResponse, tags=["health"])
async def liveness() -> HealthResponse:
    return HealthResponse(status="UP", service=settings.service_name, version=__version__)


@app.get("/health/ready", response_model=HealthResponse, tags=["health"])
async def readiness(request: Request, response: Response) -> HealthResponse:
    dependencies: dict[str, str] = {}
    try:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        dependencies["postgresql"] = "UP"
    except Exception:
        dependencies["postgresql"] = "DOWN"
    dependencies["temporal"] = (
        "UP" if not settings.temporal_enabled or request.app.state.temporal_client else "DOWN"
    )
    ready = all(value == "UP" for value in dependencies.values())
    if not ready:
        response.status_code = 503
    return HealthResponse(
        status="UP" if ready else "DOWN",
        service=settings.service_name,
        version=__version__,
        dependencies=dependencies,
    )


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
