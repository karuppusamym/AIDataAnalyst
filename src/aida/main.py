from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text
from temporalio.client import Client

from aida import __version__
from aida.abac_api import router as abac_router
from aida.ai_decision_lineage_api import router as ai_decision_lineage_router
from aida.ai_governance_api import router as ai_governance_router
from aida.ai_registry_api import router as ai_registry_router
from aida.api import router
from aida.asset_description_api import router as asset_description_router
from aida.bi_api import router as bi_router
from aida.compliance_api import router as compliance_router
from aida.composite_key_api import router as composite_key_router
from aida.config import get_settings
from aida.consumption_lineage_api import router as consumption_lineage_router
from aida.context import correlation_id_var
from aida.context_compiler_api import router as context_compiler_router
from aida.context_product_api import router as context_product_router
from aida.db import session_factory
from aida.dbt_api import router as dbt_router
from aida.glossary_api import router as glossary_router
from aida.graph_perspectives_api import router as graph_perspectives_router
from aida.ingestion_api import router as ingestion_router
from aida.intelligence_api import router as intelligence_router
from aida.logging import configure_logging
from aida.mcp_server import router as mcp_router
from aida.negative_knowledge_api import router as negative_knowledge_router
from aida.notification_api import router as notification_router
from aida.observability_api import router as observability_router
from aida.openlineage_api import router as openlineage_router
from aida.operational_api import router as operational_router
from aida.product_marketplace_api import router as product_marketplace_router
from aida.quality_api import router as quality_router
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.temporal_client = None
    if settings.temporal_enabled:
        app.state.temporal_client = await Client.connect(
            settings.temporal_address,
            namespace=settings.temporal_namespace,
        )
    logger.info(
        "service_started",
        service=settings.service_name,
        environment=settings.environment,
        version=__version__,
    )
    yield
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
app.include_router(context_product_router)
app.include_router(context_compiler_router)
app.include_router(product_marketplace_router)
app.include_router(search_router)
app.include_router(abac_router)
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
app.include_router(asset_description_router)
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
        response = await call_next(request)
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
