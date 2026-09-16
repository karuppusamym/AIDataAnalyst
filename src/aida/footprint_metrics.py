"""R11-FP17: the footprint's queue depth as metrics an operator can alert on.

`aida.footprint_gaps` answers "what does Atlas not know about this source, and who closes it"
for a person looking at a screen. Nobody watches a screen at 03:00, and the failure this exports
against is the quiet one: a pass an operator never turned on, or one that has fallen behind, so
the backlog grows while every request still succeeds. A gap register nobody reads is a log line;
a gauge is something an alert can be written against.

**No tenant in a label.** F17 in the 2026-09-05 review records what unbounded metric labels cost,
and an organization or datasource id is exactly that shape. The only label here is `kind`, whose
values are the fixed set in `GAP_DEFINITIONS`; the per-organization detail goes to structlog,
where retention bounds it. That also means these gauges disclose nothing a tenant boundary
protects: a fleet total by kind names no source and no object.

**Last observation, not a counter.** A backlog means something as "how far behind is it now", and
a counter cannot express recovery. Every kind is exported on every pass, zero included, because a
series that disappears when the queue drains is indistinguishable from a scrape that failed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

import structlog
from prometheus_client import Gauge
from sqlalchemy import select

from aida.config import Settings
from aida.footprint_gaps import GAP_DEFINITIONS, footprint_gaps
from aida.models import Organization
from aida.security import SecurityContext

_log = structlog.get_logger(__name__)

FOOTPRINT_GAPS = Gauge(
    "aida_footprint_gaps",
    "Recorded footprint gaps across the fleet, by kind, as of the last refresh.",
    labelnames=("kind",),
)
FOOTPRINT_OLDEST_PENDING_SIGNAL_SECONDS = Gauge(
    "aida_footprint_oldest_pending_change_signal_seconds",
    (
        "How long the oldest unprocessed source-change signal has waited anywhere in the fleet. "
        "Zero when nothing is pending, which is the healthy state rather than a missing reading."
    ),
)
FOOTPRINT_METRICS_ORGANIZATIONS = Gauge(
    "aida_footprint_metrics_organizations",
    "Organizations the last refresh read, so a partial sweep is visible as one.",
)

#: The pass's own principal. `Operations` is the role the read model's routes already require,
#: so the sweep sees exactly what an operator reading the register would see -- not more.
_PRINCIPAL: Final = "system:footprint-metrics"

_last_run_at: datetime | None = None


def _system_context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id=_PRINCIPAL,
        principal_type="SERVICE",
        organization_id=organization_id,
        roles=frozenset({"Operations"}),
    )


def publish_footprint_metrics(totals: dict[str, int], oldest_minutes: int | None) -> None:
    """Set every gauge from one sweep's totals, including the kinds that are at zero."""
    for kind in GAP_DEFINITIONS:
        FOOTPRINT_GAPS.labels(kind=kind).set(totals.get(kind, 0))
    FOOTPRINT_OLDEST_PENDING_SIGNAL_SECONDS.set((oldest_minutes or 0) * 60)


async def run_footprint_metrics_pass(
    settings: Settings, *, now: datetime | None = None
) -> int | None:
    """Refresh the fleet's footprint gauges, at most once per interval.

    Returns `None` when the pass was skipped (disabled or not yet due) and the number of
    organizations read when it ran -- the same shape as the vector-index and roll-up passes,
    whose cadence idiom this follows rather than inventing a second one.

    One organization's failure is logged and skipped rather than aborting the sweep: a gauge
    that is a little stale for one tenant is better than no reading for the fleet, and
    `aida_footprint_metrics_organizations` makes a partial sweep visible instead of silent.
    """
    from aida.db import session_factory

    global _last_run_at
    if not settings.footprint_metrics_enabled:
        return None
    effective_now = now or datetime.now(UTC)
    interval = timedelta(seconds=settings.footprint_metrics_interval_seconds)
    if _last_run_at is not None and (effective_now - _last_run_at) < interval:
        return None
    _last_run_at = effective_now

    async with session_factory() as session:
        organization_ids = list(
            (
                await session.scalars(
                    select(Organization.id)
                    .where(Organization.status == "ACTIVE")
                    .order_by(Organization.created_at)
                )
            ).all()
        )

    totals: dict[str, int] = {}
    oldest_minutes: int | None = None
    read = 0
    for organization_id in organization_ids:
        async with session_factory() as session:
            try:
                result = await footprint_gaps(
                    session,
                    context=_system_context(organization_id),
                    settings=settings,
                    organization_id=organization_id,
                    now=effective_now,
                )
            except Exception:
                _log.exception(
                    "footprint_metrics_organization_failed",
                    organization_id=str(organization_id),
                )
                continue
        read += 1
        for kind, count in result.totals.items():
            totals[kind] = totals.get(kind, 0) + count
        for source in result.datasources:
            waited = source.oldest_pending_signal_minutes
            if waited is not None and (oldest_minutes is None or waited > oldest_minutes):
                oldest_minutes = waited
        # The per-tenant figure an operator needs when a gauge moves, where retention bounds it.
        _log.info(
            "footprint_metrics_organization",
            organization_id=str(organization_id),
            totals=dict(sorted(result.totals.items())),
            datasources=len(result.datasources),
        )

    publish_footprint_metrics(totals, oldest_minutes)
    FOOTPRINT_METRICS_ORGANIZATIONS.set(read)
    return read
