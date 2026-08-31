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
from typing import Any, Literal

import structlog

logger = structlog.get_logger(__name__)


# Lightweight span-like record for environments where the OpenTelemetry SDK
# is not installed.  The configure_* helpers are intentionally no-ops that
# guard against import errors so the rest of the control-plane can import
# this module unconditionally and the @traced decorator stays usable as a
# pure-Python timing wrapper.

_tracer_configured = False
_meter_configured = False
_counters: dict[str, Any] = {}


@dataclass(frozen=True, slots=True)
class TracingConfig:
    """Tracing exporter configuration.

    `exporter="console"` (the default) uses `ConsoleSpanExporter`, which
    ships inside `opentelemetry-sdk` -- no collector, no extra package,
    genuinely active the moment `configure_tracing` runs. `exporter="otlp"`
    ships real spans to `endpoint` via the OTLP gRPC exporter, an optional
    package guarded by the same ImportError fallback as everything else here.
    """

    endpoint: str = "http://localhost:4317"
    service_name: str = "aida-control-plane"
    insecure: bool = True
    enabled: bool = False
    exporter: Literal["console", "otlp"] = "console"


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    """Metrics exporter configuration. See `TracingConfig.exporter`."""

    endpoint: str = "http://localhost:4317"
    service_name: str = "aida-control-plane"
    # Mirrors `TracingConfig`. Its absence was not cosmetic: `configure_metrics` reads
    # `config.insecure`, so enabling metrics raised AttributeError before this existed.
    insecure: bool = True
    export_interval_millis: int = 60_000
    enabled: bool = False
    exporter: Literal["console", "otlp"] = "console"


def configure_tracing(config: TracingConfig) -> bool:
    """Set up the trace exporter (console by default, OTLP when configured).

    Returns True if successfully configured, False otherwise. Silently
    degrades when the required OpenTelemetry package is not installed --
    the base SDK (console) is always available; the OTLP exporter is an
    optional package.
    """
    global _tracer_configured
    if not config.enabled:
        logger.info("otlp_tracing_disabled")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        resource = Resource.create({"service.name": config.service_name})
        provider = TracerProvider(resource=resource)

        if config.exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            otlp_exporter = OTLPSpanExporter(
                endpoint=config.endpoint, insecure=config.insecure
            )
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        else:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)
        _tracer_configured = True
        logger.info(
            "otlp_tracing_configured",
            exporter=config.exporter,
            endpoint=config.endpoint if config.exporter == "otlp" else "console",
            service=config.service_name,
        )
        return True
    except ImportError:
        logger.warning("opentelemetry_sdk_not_installed", feature="tracing")
        return False


def configure_metrics(config: MetricsConfig) -> bool:
    """Set up the metrics exporter (console by default, OTLP when configured).

    Returns True if successfully configured, False otherwise.
    """
    global _meter_configured
    if not config.enabled:
        logger.info("otlp_metrics_disabled")
        return False

    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create({"service.name": config.service_name})

        if config.exporter == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
                OTLPMetricExporter,
            )

            metric_exporter: Any = OTLPMetricExporter(
                endpoint=config.endpoint, insecure=config.insecure
            )
        else:
            from opentelemetry.sdk.metrics.export import ConsoleMetricExporter

            metric_exporter = ConsoleMetricExporter()

        reader = PeriodicExportingMetricReader(
            metric_exporter, export_interval_millis=config.export_interval_millis
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(provider)
        _meter_configured = True
        logger.info(
            "otlp_metrics_configured",
            exporter=config.exporter,
            service=config.service_name,
        )
        return True
    except ImportError:
        logger.warning("opentelemetry_sdk_not_installed", feature="metrics")
        return False


def record_counter(name: str, value: int = 1, **attributes: str) -> None:
    """Increment a named OTEL counter metric.

    No-op unless `configure_metrics` has successfully configured a meter
    provider -- mirrors `@traced`'s graceful degradation when OpenTelemetry
    is unavailable or metrics were never enabled, so callers (e.g. the
    request middleware) never need to check `_meter_configured` themselves.
    """
    if not _meter_configured:
        return
    try:
        from opentelemetry import metrics

        counter = _counters.get(name)
        if counter is None:
            counter = metrics.get_meter(__name__).create_counter(name)
            _counters[name] = counter
        counter.add(value, attributes=attributes or None)
    except Exception:  # pragma: no cover - defensive: metrics must never break a request
        logger.warning("otlp_metric_record_failed", metric=name)


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
