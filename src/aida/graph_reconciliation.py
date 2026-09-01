"""Scheduled Postgres/Neo4j knowledge-graph reconciliation and drift alerting (KG-7).

Module 10's Neo4j projection (`aida.projectors.graph_projector`) is
event-driven: it only writes when it sees one of
`UNIFIED_LINEAGE_PROJECTION_EVENT_TYPES` on the outbox stream. Because
PostgreSQL stays the authoritative store and Neo4j is a derived projection, a
missed or lost event leaves them out of sync -- a row approved/deleted in
Postgres the projector never heard about, or a stale node/edge in Neo4j whose
Postgres source is gone. Nothing previously checked the two stores actually
agree with each other; this module does, on a schedule.

**"Should exist" is never reinvented here.** The Postgres side of the diff
calls the exact same `graph_projector.load_unified_lineage_projection` the
event-driven projector itself calls to build what it writes -- so
reconciliation can never drift from the projector's own selection criteria
(RL-4's `UNIFIED_LINEAGE_PROJECTION_EVENT_TYPES` decides *when* a projection
runs; this module reuses what *that projection computes*, not a parallel
query). The Neo4j side reads the same `organization_id`/`datasource_id`-tagged
`UnifiedLineageNode`/`UNIFIED_LINEAGE` rows `project_unified_lineage`'s own
stale-generation prune already targets.

**Alerting reuses DQ-1's routing engine exactly as GL-6 does**
(`aida.glossary_owner_routing` -- see its module docstring), not a new
notification channel: a drifted datasource becomes the same `Incident` shape
a data-quality incident routes as, matched against the org's real
`NotificationRuleRecord` rows through the unmodified
`notification_routing.route_notification` / `format_itsm_payload`. Unlike
GL-6, this does **not** persist a new escalation-lifecycle model --
`DataQualityIncident.table_id` is a NOT NULL FK to `metadata_table` (most
drifted graph nodes/edges are not one single table -- they can be columns,
dbt resources, BI nodes, or an edge with no natural table subject at all), and
`NotificationEventRecord.incident_id` is a NOT NULL FK to
`data_quality_incident`, so neither existing table can carry a
datasource/graph-level drift finding without a schema change (out of scope
here, same collision-avoidance constraint GL-6 hit and worked around with its
own `UnownedAssetEscalation` table -- a new model this pass deliberately does
not add). Instead, each routed `NotificationEvent` (and, for an ITSM-channel
match, the formatted ITSM payload) is persisted through the existing
`record_audit`/`record_outbox` tables every other scheduled pass in this
codebase already uses -- a real, queryable, non-log-line signal, just not a
stateful PENDING/ROUTED/ESCALATED row. Consequently `should_escalate`/
`escalate` (which need a persisted `sent_at`/`acknowledged_at` to compute
against) are not wired up here; each due sweep re-detects and re-routes
still-open drift at the sweep's own cadence instead. That is a real,
documented gap, not a silent one -- see the KG-7 tracker row.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.db import session_factory
from aida.events import record_audit, record_outbox
from aida.models import DataSource, NotificationRuleRecord
from aida.notification_routing import (
    Incident,
    NotificationRule,
    format_itsm_payload,
    route_notification,
)
from aida.projectors.graph_projector import load_unified_lineage_projection
from aida.security import SecurityContext

logger = structlog.get_logger(__name__)

# The "new incident type/source" this pass routes through DQ-1's engine, and
# the fingerprint namespace for its Incident objects.
GRAPH_DRIFT_SOURCE = "GRAPH_PROJECTION_DRIFT"

DEFAULT_GRAPH_DRIFT_RULE_NAME = "Knowledge graph projection drift (default)"

# A datasource with at least this many total drifted node/edge keys in one
# pass is CRITICAL rather than WARNING. Deliberately a plain count, not a
# doubled base threshold like data_quality._severity's change-percent scale --
# there is no "policy tolerance" for graph drift the way there is for a
# volume/null-rate change; any drift at all is already a WARNING.
DEFAULT_CRITICAL_DRIFT_THRESHOLD = 10

# How many sample projection_keys ride along in an Incident's message/outbox
# payload -- enough to start triage from without turning a large drift event
# into an unbounded payload.
_SAMPLE_KEY_LIMIT = 50


# ---------------------------------------------------------------------------
# Pure diff logic -- no I/O, no live Neo4j/Postgres required to test.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProjectionKeyDrift:
    """Set-difference between what Postgres says should be projected and what
    Neo4j actually holds, for one projection kind (nodes or edges)."""

    missing_in_neo4j: frozenset[str]
    orphaned_in_neo4j: frozenset[str]

    @property
    def has_drift(self) -> bool:
        return bool(self.missing_in_neo4j or self.orphaned_in_neo4j)

    @property
    def count(self) -> int:
        return len(self.missing_in_neo4j) + len(self.orphaned_in_neo4j)


def diff_projection_keys(
    should_exist: Iterable[str], actually_exists: Iterable[str]
) -> ProjectionKeyDrift:
    """Pure set-difference. ``should_exist`` is whatever
    ``graph_projector.load_unified_lineage_projection`` currently computes for
    a datasource (not reinvented here); ``actually_exists`` is the
    projection_keys currently tagged with that datasource in Neo4j.
    """
    should = frozenset(should_exist)
    actual = frozenset(actually_exists)
    return ProjectionKeyDrift(missing_in_neo4j=should - actual, orphaned_in_neo4j=actual - should)


@dataclass(frozen=True, slots=True)
class GraphReconciliationReport:
    """One datasource's reconciliation result: node drift + edge drift."""

    organization_id: UUID
    datasource_id: UUID
    node_drift: ProjectionKeyDrift
    edge_drift: ProjectionKeyDrift
    generated_at: datetime

    @property
    def has_drift(self) -> bool:
        return self.node_drift.has_drift or self.edge_drift.has_drift

    @property
    def total_drift_count(self) -> int:
        return self.node_drift.count + self.edge_drift.count


def reconcile_projection(
    *,
    organization_id: UUID,
    datasource_id: UUID,
    should_exist_nodes: Iterable[str],
    actual_nodes: Iterable[str],
    should_exist_edges: Iterable[str],
    actual_edges: Iterable[str],
    now: datetime | None = None,
) -> GraphReconciliationReport:
    """Pure reconciliation: combine node + edge drift into one report."""
    return GraphReconciliationReport(
        organization_id=organization_id,
        datasource_id=datasource_id,
        node_drift=diff_projection_keys(should_exist_nodes, actual_nodes),
        edge_drift=diff_projection_keys(should_exist_edges, actual_edges),
        generated_at=now or datetime.now(UTC),
    )


def drift_severity(
    report: GraphReconciliationReport,
    *,
    critical_threshold: int = DEFAULT_CRITICAL_DRIFT_THRESHOLD,
) -> str:
    """HEALTHY when nothing drifted; otherwise WARNING, or CRITICAL once
    ``total_drift_count`` reaches ``critical_threshold``."""
    if not report.has_drift:
        return "HEALTHY"
    return "CRITICAL" if report.total_drift_count >= critical_threshold else "WARNING"


def graph_reconciliation_due(
    last_run_at: datetime | None, now: datetime, interval_minutes: int
) -> bool:
    """Whether a reconciliation sweep is due for a datasource. ``None``
    (never swept) is always due -- same convention as
    ``workflows.scheduler.owner_routing_due`` /
    ``custom_quality_rules.rule_pack_due``.
    """
    if last_run_at is None:
        return True
    return (now - last_run_at).total_seconds() >= interval_minutes * 60


def incident_for_drift(
    report: GraphReconciliationReport,
    *,
    critical_threshold: int = DEFAULT_CRITICAL_DRIFT_THRESHOLD,
) -> Incident | None:
    """Build the same ``Incident`` shape a data-quality incident routes as
    (mirrors ``glossary_owner_routing._incident_for``'s reuse of DQ-1's
    engine for a non-quality-service domain). ``None`` when there is nothing
    to alert on.
    """
    if not report.has_drift:
        return None
    severity = drift_severity(report, critical_threshold=critical_threshold)
    sample = sorted(
        report.node_drift.missing_in_neo4j
        | report.node_drift.orphaned_in_neo4j
        | report.edge_drift.missing_in_neo4j
        | report.edge_drift.orphaned_in_neo4j
    )[:20]
    message = (
        f"Knowledge graph projection drift detected for datasource "
        f"{report.datasource_id}: {len(report.node_drift.missing_in_neo4j)} node(s) "
        f"missing in Neo4j, {len(report.node_drift.orphaned_in_neo4j)} orphaned "
        f"node(s) with no Postgres source, {len(report.edge_drift.missing_in_neo4j)} "
        f"edge(s) missing in Neo4j, {len(report.edge_drift.orphaned_in_neo4j)} "
        f"orphaned edge(s) with no Postgres source "
        f"(sample keys: {', '.join(sample) if sample else 'none'})."
    )
    return Incident(
        incident_id=f"graph-drift-{report.datasource_id}-{int(report.generated_at.timestamp())}",
        fingerprint=f"{GRAPH_DRIFT_SOURCE}:{report.organization_id}:{report.datasource_id}",
        severity=severity,
        source_id=str(report.datasource_id),
        domain=None,
        owner=None,
        message=message,
    )


def _as_engine_rules(rules: list[NotificationRuleRecord]) -> list[NotificationRule]:
    """Adapt persisted org notification rules into the pure engine's rule
    shape -- same adapter shape as
    ``glossary_owner_routing._as_engine_rules``, duplicated rather than
    imported since it is a trivial, module-private mapping each reuse site
    already owns independently."""
    return [
        NotificationRule(
            rule_id=str(rule.id),
            organization_id=str(rule.organization_id),
            conditions=rule.conditions,
            channel=rule.channel,
            recipients=list(rule.recipients),
            escalation_after_minutes=rule.escalation_after_minutes,
            enabled=rule.enabled,
        )
        for rule in rules
    ]


# ---------------------------------------------------------------------------
# I/O -- Postgres selection criteria + Neo4j actuals.
# ---------------------------------------------------------------------------


async def load_should_exist_projection_keys(
    datasource_id: UUID, organization_id: UUID
) -> tuple[frozenset[str], frozenset[str]]:
    """The current would-be-projected node/edge projection_keys for one
    datasource, using the exact same
    ``graph_projector.load_unified_lineage_projection`` the event-driven
    projector calls -- so this can never invent its own selection criteria.
    """
    projection = await load_unified_lineage_projection(datasource_id, organization_id)
    node_keys = frozenset(row["projection_key"] for row in projection["nodes"])
    edge_keys = frozenset(row["projection_key"] for row in projection["edges"])
    return node_keys, edge_keys


async def load_actual_projection_keys(
    driver: AsyncDriver, organization_id: UUID, datasource_id: UUID
) -> tuple[frozenset[str], frozenset[str]]:
    """What Neo4j actually holds tagged to this datasource -- the same
    ``organization_id``/``datasource_id`` tag filter
    ``project_unified_lineage``'s own stale-generation prune already uses.
    """
    async with driver.session() as graph_session:
        node_result = await graph_session.run(
            """
            MATCH (node:UnifiedLineageNode)
            WHERE node.organization_id = $organization_id
              AND node.datasource_id = $datasource_id
            RETURN node.projection_key AS projection_key
            """,
            organization_id=str(organization_id),
            datasource_id=str(datasource_id),
        )
        node_keys = frozenset([str(record["projection_key"]) async for record in node_result])
        edge_result = await graph_session.run(
            """
            MATCH ()-[edge:UNIFIED_LINEAGE]->()
            WHERE edge.organization_id = $organization_id
              AND edge.datasource_id = $datasource_id
            RETURN edge.projection_key AS projection_key
            """,
            organization_id=str(organization_id),
            datasource_id=str(datasource_id),
        )
        edge_keys = frozenset([str(record["projection_key"]) async for record in edge_result])
    return node_keys, edge_keys


async def ensure_default_graph_drift_notification_rule(
    session: AsyncSession, organization_id: UUID
) -> NotificationRuleRecord:
    """Return the organization's catch-all graph-drift routing rule, creating
    it if missing -- same lazy-default idiom as
    ``workflows.scheduler.ensure_default_unowned_backlog_notification_rule``
    and for the same reason (no migration-time row to seed a rule *for* until
    an organization has a datasource worth reconciling). Kept as its own
    separate rule rather than folding onto GL-6's default so an operator can
    tune or disable graph-drift alerting independently of unowned-asset
    routing. ``conditions={}`` / ``recipients=[]`` for the same reasons
    documented on that function: an empty-conditions rule matches every
    incident it is asked to match (graph-drift incidents carry no
    ``domain``/``owner``), and an empty-recipient EMAIL rule is inert for
    actual delivery but unblocks routing from sitting matched-to-nothing.
    """
    existing = await session.scalar(
        select(NotificationRuleRecord).where(
            NotificationRuleRecord.organization_id == organization_id,
            NotificationRuleRecord.name == DEFAULT_GRAPH_DRIFT_RULE_NAME,
        )
    )
    if existing is not None:
        return existing
    rule = NotificationRuleRecord(
        organization_id=organization_id,
        name=DEFAULT_GRAPH_DRIFT_RULE_NAME,
        conditions={},
        channel="EMAIL",
        recipients=[],
        escalation_after_minutes=None,
        enabled=True,
        created_by="fleet-scheduler",
    )
    session.add(rule)
    await session.flush()
    return rule


async def reconcile_and_alert_datasource(
    session: AsyncSession,
    driver: AsyncDriver,
    *,
    datasource_id: UUID,
    organization_id: UUID,
    now: datetime,
    critical_threshold: int = DEFAULT_CRITICAL_DRIFT_THRESHOLD,
) -> GraphReconciliationReport:
    """One datasource's reconciliation pass: diff Postgres's current
    selection against Neo4j's actual projection and, if drift is found, route
    it through DQ-1's notification engine and persist the outcome via
    ``record_audit``/``record_outbox`` (see module docstring for why not a
    new incident table).
    """
    should_nodes, should_edges = await load_should_exist_projection_keys(
        datasource_id, organization_id
    )
    actual_nodes, actual_edges = await load_actual_projection_keys(
        driver, organization_id, datasource_id
    )
    report = reconcile_projection(
        organization_id=organization_id,
        datasource_id=datasource_id,
        should_exist_nodes=should_nodes,
        actual_nodes=actual_nodes,
        should_exist_edges=should_edges,
        actual_edges=actual_edges,
        now=now,
    )
    incident = incident_for_drift(report, critical_threshold=critical_threshold)
    if incident is None:
        return report

    await ensure_default_graph_drift_notification_rule(session, organization_id)
    notification_rules = list(
        await session.scalars(
            select(NotificationRuleRecord).where(
                NotificationRuleRecord.organization_id == organization_id,
                NotificationRuleRecord.enabled.is_(True),
            )
        )
    )
    events = route_notification(incident, _as_engine_rules(notification_rules))

    worker_context = SecurityContext(
        principal_id="fleet-scheduler",
        principal_type="WORKER",
        organization_id=organization_id,
        roles=frozenset({"SchedulerWorker"}),
    )
    record_audit(
        session,
        worker_context,
        action="knowledge_graph.reconciliation.drift_detected",
        resource_type="datasource",
        resource_id=str(datasource_id),
        outcome="SUCCESS",
        correlation_id=incident.fingerprint,
        details={
            "severity": incident.severity,
            "missing_nodes": len(report.node_drift.missing_in_neo4j),
            "orphaned_nodes": len(report.node_drift.orphaned_in_neo4j),
            "missing_edges": len(report.edge_drift.missing_in_neo4j),
            "orphaned_edges": len(report.edge_drift.orphaned_in_neo4j),
            "notification_events_routed": len(events),
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="datasource",
        aggregate_id=str(datasource_id),
        event_type="knowledge_graph.drift_detected.v1",
        payload={
            "datasource_id": str(datasource_id),
            "source": GRAPH_DRIFT_SOURCE,
            "severity": incident.severity,
            "fingerprint": incident.fingerprint,
            "missing_nodes": sorted(report.node_drift.missing_in_neo4j)[:_SAMPLE_KEY_LIMIT],
            "orphaned_nodes": sorted(report.node_drift.orphaned_in_neo4j)[:_SAMPLE_KEY_LIMIT],
            "missing_edges": sorted(report.edge_drift.missing_in_neo4j)[:_SAMPLE_KEY_LIMIT],
            "orphaned_edges": sorted(report.edge_drift.orphaned_in_neo4j)[:_SAMPLE_KEY_LIMIT],
        },
    )
    for event in events:
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="datasource",
            aggregate_id=str(datasource_id),
            event_type="knowledge_graph.drift_alert_routed.v1",
            payload={
                "datasource_id": str(datasource_id),
                "notification_id": event.notification_id,
                "rule_id": event.rule_id,
                "channel": event.channel,
                "recipients": event.recipients,
                "severity": event.severity,
                "dedup_key": event.dedup_key,
            },
        )
        if event.channel == "ITSM":
            record_outbox(
                session,
                organization_id=organization_id,
                aggregate_type="datasource",
                aggregate_id=str(datasource_id),
                event_type="knowledge_graph.drift_itsm_payload.v1",
                payload=format_itsm_payload(incident),
            )
    logger.warning(
        "graph_reconciliation_drift_detected",
        datasource_id=str(datasource_id),
        organization_id=str(organization_id),
        severity=incident.severity,
        missing_nodes=len(report.node_drift.missing_in_neo4j),
        orphaned_nodes=len(report.node_drift.orphaned_in_neo4j),
        missing_edges=len(report.edge_drift.missing_in_neo4j),
        orphaned_edges=len(report.edge_drift.orphaned_in_neo4j),
        notification_events_routed=len(events),
    )
    return report


async def run_graph_reconciliation_pass(
    settings: Settings,
    *,
    now: datetime | None = None,
    last_run_at: dict[UUID, datetime],
    datasource_ids: Sequence[tuple[UUID, UUID]] | None = None,
) -> int:
    """Sweep every datasource due for a KG-7 reconciliation pass (own cadence,
    ``settings.graph_reconciliation_interval_minutes``), returning how many
    were actually swept.

    Same in-process-memory due-tracking + per-item fault isolation as
    ``workflows.scheduler.run_owner_routing_pass`` /
    ``custom_quality_rules.run_due_rule_packs``: there is no per-datasource
    "next-due-at" column to persist to without a new model/migration column
    (out of scope, same collision-avoidance constraint those two note), and a
    reconciliation pass is read-only against both stores and safe to repeat,
    so a scheduler restart costs at most one redundant sweep per datasource.
    One datasource's failure (Neo4j unreachable, a bad rule) is logged and
    skipped, never aborting the rest of the sweep.

    ``datasource_ids`` (pairs of ``(datasource_id, organization_id)``)
    overrides the DB-derived candidate list; production call sites leave it
    unset. It exists so tests can drive the due-check/fault-isolation
    behavior without a live Postgres/Neo4j, matching this codebase's
    fake-session/pure-function test convention.
    """
    effective_now = now or datetime.now(UTC)
    interval_minutes = settings.graph_reconciliation_interval_minutes

    if datasource_ids is None:
        async with session_factory() as session:
            rows = (
                await session.execute(select(DataSource.id, DataSource.organization_id))
            ).all()
        candidates: list[tuple[UUID, UUID]] = [(row[0], row[1]) for row in rows]
    else:
        candidates = list(datasource_ids)

    due = [
        (ds_id, org_id)
        for ds_id, org_id in candidates
        if graph_reconciliation_due(last_run_at.get(ds_id), effective_now, interval_minutes)
    ]
    if not due:
        return 0

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=5.0,
    )
    swept = 0
    try:
        for datasource_id, organization_id in due:
            try:
                async with session_factory() as session:
                    await reconcile_and_alert_datasource(
                        session,
                        driver,
                        datasource_id=datasource_id,
                        organization_id=organization_id,
                        now=effective_now,
                    )
                    await session.commit()
            except (Neo4jError, ServiceUnavailable, OSError):
                logger.exception(
                    "graph_reconciliation_neo4j_unavailable", datasource_id=str(datasource_id)
                )
                continue
            except Exception:
                logger.exception(
                    "graph_reconciliation_pass_failed", datasource_id=str(datasource_id)
                )
                continue
            last_run_at[datasource_id] = effective_now
            swept += 1
    finally:
        await driver.close()
    return swept
