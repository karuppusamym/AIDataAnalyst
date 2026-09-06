"""Prometheus instrumentation for the graph projector's rebuild loop.

Invariant held here: every quantity an operator needs to answer "is the
projection keeping up, and for whom is it *not*?" is a real, exported metric
rather than a line in a log nobody aggregates -- and none of them carries an
organization id as a label. F17 in `Docs/review-2026-09-05/REVIEW.md` records
what unbounded metric labels cost; a tenant identifier is exactly that shape, so
the per-tenant detail is emitted as a structlog field (queryable, retained,
bounded by log retention) while the metric surface stays a fixed, small number
of series.

The two gauges are deliberately *last observation* values, not counters: a lag
or backlog figure only means anything as "how far behind is it right now",
and a counter cannot express recovery.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# Every level of the metadata hierarchy the projector writes, in the order it
# must write them (a child MERGE matches its already-written parent). Named
# here rather than inline so the reader (`aida.graph_projection`), the writer
# (`aida.projectors.graph_projector`) and this metric surface cannot disagree
# about what levels exist.
PROJECTION_LEVELS: tuple[str, ...] = (
    "catalogs",
    "schemas",
    "tables",
    "columns",
    "constraints",
)

PROJECTION_ROWS = Counter(
    "aida_graph_projection_rows_total",
    "Metadata rows written into the graph projection, by hierarchy level.",
    labelnames=("level",),
)
PROJECTION_CHUNKS = Counter(
    "aida_graph_projection_chunks_total",
    "Bounded chunks read and written by the graph projector, by hierarchy level.",
    labelnames=("level",),
)
PROJECTION_DELETIONS = Counter(
    "aida_graph_projection_reconciled_deletions_total",
    (
        "Projection nodes deleted because the source row no longer exists "
        "(stale generation), by hierarchy level."
    ),
    labelnames=("level",),
)
PROJECTION_LEVEL_SECONDS = Histogram(
    "aida_graph_projection_level_duration_seconds",
    "Wall-clock time spent reading and writing one hierarchy level of one rebuild.",
    labelnames=("level",),
)
PROJECTION_REBUILD_SECONDS = Histogram(
    "aida_graph_projection_rebuild_duration_seconds",
    "Wall-clock time for one complete datasource projection rebuild.",
)
PROJECTION_LAG_SECONDS = Gauge(
    "aida_graph_projection_lag_seconds",
    (
        "Seconds between the source event's occurred_at and the moment its "
        "projection finished. The freshness of the graph, not a rate."
    ),
)
PROJECTION_OLDEST_BACKLOG_SECONDS = Gauge(
    "aida_graph_projection_oldest_backlog_seconds",
    (
        "Age of the oldest event still waiting in the projector's fair-share "
        "buffer. The signal a tenant-budget policy should be chosen from."
    ),
)
PROJECTION_BACKLOG_EVENTS = Gauge(
    "aida_graph_projection_backlog_events",
    "Events currently buffered in the projector's fair-share scheduler.",
)
PROJECTION_BACKLOG_TENANTS = Gauge(
    "aida_graph_projection_backlog_tenants",
    "Distinct organizations with at least one event in the fair-share buffer.",
)
PROJECTION_TENANT_YIELDS = Counter(
    "aida_graph_projection_tenant_budget_yields_total",
    (
        "Times a tenant reached its per-round event budget and the scheduler "
        "moved on to another tenant with work waiting."
    ),
)

__all__ = [
    "PROJECTION_BACKLOG_EVENTS",
    "PROJECTION_BACKLOG_TENANTS",
    "PROJECTION_CHUNKS",
    "PROJECTION_DELETIONS",
    "PROJECTION_LAG_SECONDS",
    "PROJECTION_LEVELS",
    "PROJECTION_LEVEL_SECONDS",
    "PROJECTION_OLDEST_BACKLOG_SECONDS",
    "PROJECTION_REBUILD_SECONDS",
    "PROJECTION_ROWS",
    "PROJECTION_TENANT_YIELDS",
]
