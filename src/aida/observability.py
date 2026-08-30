"""OpenTelemetry observability integration (OB-1).

Configures OTLP trace and metrics exporters, provides a @traced decorator
for automatic span creation, and integrates with existing Prometheus
metrics.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


# Lightweight span-like record for environments where the OpenTelemetry SDK
# is not installed.  The configure_* helpers are intentionally no-ops that
# guard against import errors so the rest of the control-plane can import
# this module unconditionally and the @traced decorator stays usable as a
# pure-Python timing wrapper.

_tracer_configured = False
_meter_configured = False


@dataclass(frozen=True, slots=True)
class TracingConfig:
    """OTLP tracing exporter configuration."""

    endpoint: str = "http://localhost:4317"
    service_name: str = "aida-control-plane"
    insecure: bool = True
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    """OTLP metrics exporter configuration."""

    endpoint: str = "http://localhost:4317"
    service_name: str = "aida-control-plane"
    # Mirrors `TracingConfig`. Its absence was not cosmetic: `configure_metrics` reads
    # `config.insecure`, so enabling metrics raised AttributeError before this existed.
    insecure: bool = True
    export_interval_millis: int = 60_000
    enabled: bool = False


def configure_tracing(config: TracingConfig) -> bool:
    """Set up OTLP trace exporter.

    Returns True if successfully configured, False otherwise. Silently
    degrades when the OpenTelemetry SDK is not installed.
    """
    global _tracer_configured
    if not config.enabled:
        logger.info("otlp_tracing_disabled")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": config.service_name})
        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(
            endpoint=config.endpoint, insecure=config.insecure
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer_configured = True
        logger.info(
            "otlp_tracing_configured",
            endpoint=config.endpoint,
            service=config.service_name,
        )
        return True
    except ImportError:
        logger.warning("opentelemetry_sdk_not_installed", feature="tracing")
        return False


def configure_metrics(config: MetricsConfig) -> bool:
    """Set up OTLP metrics exporter.

    Returns True if successfully configured, False otherwise.
    """
    global _meter_configured
    if not config.enabled:
        logger.info("otlp_metrics_disabled")
        return False

    try:
        from opentelemetry import metrics
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({"service.name": config.service_name})
        exporter = OTLPMetricExporter(
            endpoint=config.endpoint, insecure=config.insecure
        )
        reader = PeriodicExportingMetricReader(
            exporter, export_interval_millis=config.export_interval_millis
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(provider)
        _meter_configured = True
        logger.info(
            "otlp_metrics_configured",
            endpoint=config.endpoint,
            service=config.service_name,
        )
        return True
    except ImportError:
        logger.warning("opentelemetry_sdk_not_installed", feature="metrics")
        return False


def traced[F: Callable[..., Any]](func: F) -> F:
    """Decorator for automatic span creation.

    When OpenTelemetry is configured, creates a span with organization_id,
    principal_id, and correlation_id attributes. Falls back to structured
    logging when the SDK is unavailable.
    """

    @functools.wraps(func)
    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
        span_name = f"{func.__module__}.{func.__qualname__}"
        start = time.perf_counter()

        if _tracer_configured:
            # Only tracer *acquisition* is guarded. The wrapped call sits in the `else`
            # branch on purpose: it used to be inside the `try`, so a function that raised
            # had its exception swallowed by `except Exception: pass` and was then called
            # a second time by the fallback below -- a silent duplicate side effect, and
            # the caller saw only the second failure. Tracing must never change how many
            # times the thing it observes runs.
            try:
                from opentelemetry import trace

                tracer = trace.get_tracer(__name__)
            except Exception:  # pragma: no cover - SDK absent or misconfigured
                logger.warning("tracing_unavailable", span_name=span_name)
            else:
                with tracer.start_as_current_span(span_name) as span:
                    _set_span_attributes(span, kwargs)
                    result = await func(*args, **kwargs)
                    elapsed = time.perf_counter() - start
                    span.set_attribute("duration_ms", round(elapsed * 1000, 2))
                    return result

        result = await func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        logger.debug(
            "traced_call",
            span_name=span_name,
            duration_ms=round(elapsed * 1000, 2),
        )
        return result

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        span_name = f"{func.__module__}.{func.__qualname__}"
        start = time.perf_counter()

        if _tracer_configured:
            # Only tracer *acquisition* is guarded. The wrapped call sits in the `else`
            # branch on purpose: it used to be inside the `try`, so a function that raised
            # had its exception swallowed by `except Exception: pass` and was then called
            # a second time by the fallback below -- a silent duplicate side effect, and
            # the caller saw only the second failure. Tracing must never change how many
            # times the thing it observes runs.
            try:
                from opentelemetry import trace

                tracer = trace.get_tracer(__name__)
            except Exception:  # pragma: no cover - SDK absent or misconfigured
                logger.warning("tracing_unavailable", span_name=span_name)
            else:
                with tracer.start_as_current_span(span_name) as span:
                    _set_span_attributes(span, kwargs)
                    result = func(*args, **kwargs)
                    elapsed = time.perf_counter() - start
                    span.set_attribute("duration_ms", round(elapsed * 1000, 2))
                    return result

        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        logger.debug(
            "traced_call",
            span_name=span_name,
            duration_ms=round(elapsed * 1000, 2),
        )
        return result

    import asyncio

    if asyncio.iscoroutinefunction(func):
        return async_wrapper  # type: ignore[return-value]
    return sync_wrapper  # type: ignore[return-value]


def _set_span_attributes(span: Any, kwargs: dict[str, Any]) -> None:
    """Set standard span attributes from keyword arguments."""
    for attr in ("organization_id", "principal_id", "correlation_id"):
        if attr in kwargs and kwargs[attr] is not None:
            span.set_attribute(attr, str(kwargs[attr]))
