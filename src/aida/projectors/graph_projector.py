"""The Kafka-driven projector that keeps Neo4j in step with PostgreSQL.

Three invariants this module is responsible for holding:

* **A rebuild's memory is bounded by a chunk, not by the estate.** Reading is
  delegated to `aida.graph_projection.iter_projection_chunks`; nothing here
  ever holds a whole datasource's metadata at once.
* **A rebuild is a replacement, not an accumulation.** Every node written in a
  rebuild is stamped with that rebuild's `generation`; anything still carrying
  an older generation for the same tenant and datasource is deleted afterwards.
  Before this, a row deleted at the source stayed in the graph forever, because
  MERGE only ever adds.
* **No single tenant owns the sweep.** Consumed events are buffered through
  `TenantFairQueue`, which round-robins between organizations, and the age of
  the oldest waiting event is exported so a budget policy can be chosen from
  measurements instead of guesses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from aiokafka import AIOKafkaConsumer
from neo4j import AsyncDriver, AsyncGraphDatabase
from neo4j import AsyncSession as AsyncNeo4jSession

from aida.config import get_settings
from aida.db import session_factory
from aida.graph_projection import (
    BacklogSnapshot,
    ProjectionChunk,
    TenantBudget,
    TenantFairQueue,
    event_lag_seconds,
    iter_projection_chunks,
    resolve_chunk_size,
)
from aida.logging import configure_logging
from aida.models import DataSource, DbtProject
from aida.projection_metrics import (
    PROJECTION_BACKLOG_EVENTS,
    PROJECTION_BACKLOG_TENANTS,
    PROJECTION_CHUNKS,
    PROJECTION_DELETIONS,
    PROJECTION_LAG_SECONDS,
    PROJECTION_LEVEL_SECONDS,
    PROJECTION_OLDEST_BACKLOG_SECONDS,
    PROJECTION_REBUILD_SECONDS,
    PROJECTION_ROWS,
    PROJECTION_TENANT_YIELDS,
)
from aida.unified_lineage_api import build_unified_lineage_graph_payload

logger = structlog.get_logger(__name__)

# Event types that trigger `project_unified_lineage` in `run_projector` below.
# Named (rather than an inline set literal) so it can be asserted against
# directly -- e.g. by `intelligence_api._relationship_candidate_decision_event_type`
# regression tests -- rather than only by reading `run_projector`'s source.
# RL-4: `relationship_candidate.approved.v1` / `.rejected.v1` were already
# listed here before the corresponding `record_outbox()` call in
# `intelligence_api.decide_relationship_candidate` emitted a different,
# single consolidated event type (`relationship_candidate.decided.v1`) --
# the two never matched, so an approved/rejected candidate never actually
# reached this projector. Keep these two names in lockstep with
# `intelligence_api._relationship_candidate_decision_event_type`.
UNIFIED_LINEAGE_PROJECTION_EVENT_TYPES = frozenset(
    {
        "metadata.discovery.completed.v1",
        "metadata.discovery.snapshot.v1",
        "dbt_artifact.imported.v1",
        "openlineage.run_event.ingested.v1",
        "relationship_candidate.approved.v1",
        "relationship_candidate.rejected.v1",
    }
)


@dataclass(slots=True)
class ProjectorState:
    stopping: bool = False


async def ensure_graph_constraints(driver: AsyncDriver) -> None:
    statements = (
        "CREATE CONSTRAINT catalog_platform_id IF NOT EXISTS "
        "FOR (n:Catalog) REQUIRE n.platform_id IS UNIQUE",
        "CREATE CONSTRAINT schema_platform_id IF NOT EXISTS "
        "FOR (n:Schema) REQUIRE n.platform_id IS UNIQUE",
        "CREATE CONSTRAINT table_platform_id IF NOT EXISTS "
        "FOR (n:Table) REQUIRE n.platform_id IS UNIQUE",
        "CREATE CONSTRAINT column_platform_id IF NOT EXISTS "
        "FOR (n:Column) REQUIRE n.platform_id IS UNIQUE",
        "CREATE CONSTRAINT constraint_platform_id IF NOT EXISTS "
        "FOR (n:Constraint) REQUIRE n.platform_id IS UNIQUE",
        "CREATE CONSTRAINT unified_lineage_projection_key IF NOT EXISTS "
        "FOR (n:UnifiedLineageNode) REQUIRE n.projection_key IS UNIQUE",
        # ADR-0017 SS2 -- tenancy-path indexes. A domain-scoped traversal filters
        # by data_domain_id before it walks edges, rather than walking first and
        # checking after; these are ordinary (non-unique) indexes since every
        # tagged label shares the same organization/domain/project property names.
        "CREATE INDEX table_data_domain_id IF NOT EXISTS "
        "FOR (n:Table) ON (n.data_domain_id)",
        "CREATE INDEX table_project_id IF NOT EXISTS "
        "FOR (n:Table) ON (n.project_id)",
        "CREATE INDEX unified_lineage_node_data_domain_id IF NOT EXISTS "
        "FOR (n:UnifiedLineageNode) ON (n.data_domain_id)",
        "CREATE INDEX unified_lineage_node_project_id IF NOT EXISTS "
        "FOR (n:UnifiedLineageNode) ON (n.project_id)",
    )
    async with driver.session() as graph_session:
        for statement in statements:
            await graph_session.run(statement)


def _tenancy_path(datasource: DataSource | None) -> dict[str, str]:
    """ADR-0017 SS2 -- every projected node carries its full tenancy path, not
    just `organization_id`, so a bounded traversal can be scoped to a domain
    before it walks edges rather than filtering after."""
    if datasource is None:
        return {}
    return {
        "line_of_business_id": str(datasource.line_of_business_id),
        "data_domain_id": str(datasource.data_domain_id),
        "project_id": str(datasource.project_id),
    }


def rebuild_generation(event: Mapping[str, Any]) -> str:
    """The stamp that separates "written by this rebuild" from "left over".

    Derived only from the event envelope, so it is a pure function of
    authoritative input: replaying the same event produces the same generation
    and therefore the same projection (INV-1's rebuild property, asserted by
    `tests/test_inv1_single_authoritative_store.py::test_projection_rebuild`),
    while a *later* discovery event -- necessarily a different `event_id` --
    produces a different one and so retires whatever the previous rebuild left
    behind.
    """
    seed = json.dumps(
        {
            "event_id": str(event.get("event_id") or ""),
            "occurred_at": str(event.get("occurred_at") or ""),
            "organization_id": str(event.get("organization_id") or ""),
            "datasource_id": str((event.get("payload") or {}).get("datasource_id") or ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


# Neo4j label per hierarchy level, and the MERGE statements that write it.
# Written as a table rather than as five inline blocks so the write path, the
# deletion-reconciliation path and the metric labels are all driven by one
# declaration and cannot fall out of step.
_LEVEL_LABELS: dict[str, str] = {
    "catalogs": "Catalog",
    "schemas": "Schema",
    "tables": "Table",
    "columns": "Column",
    "constraints": "Constraint",
}

# Children before parents: DETACH DELETE on a Table would silently take its
# columns' HAS_COLUMN edges with it, so the columns are reconciled on their own
# terms first and the resulting count means what it says.
_DELETION_ORDER: tuple[str, ...] = ("columns", "constraints", "tables", "schemas", "catalogs")


@dataclass(frozen=True, slots=True)
class ProjectionRebuildReport:
    """What one rebuild actually did.

    Returned rather than only logged so a measurement harness or a test can
    assert on progress and on reconciliation without parsing log lines.
    """

    generation: str
    rows_by_level: dict[str, int]
    chunks_by_level: dict[str, int]
    deleted_by_level: dict[str, int]
    duration_seconds: float
    lag_seconds: float | None

    @property
    def rows(self) -> int:
        return sum(self.rows_by_level.values())

    @property
    def deleted(self) -> int:
        return sum(self.deleted_by_level.values())


async def _write_chunk(
    graph_session: AsyncNeo4jSession, chunk: ProjectionChunk, *, generation: str
) -> None:
    """Write one bounded chunk of one level.

    Every statement is written inline at its own `run()` call rather than
    looked up from a table of Cypher strings. That is deliberate:
    `tests/test_inv1_single_authoritative_store.py` proves the dual-write
    prohibition and the MERGE-not-CREATE idempotence rule by extracting Cypher
    at the graph-call site, and a statement hidden behind a dict lookup is a
    statement that invariant scan can no longer see.

    `SET n += row, n.generation = $generation` is what makes a rebuild a
    replacement: the stamp says which rebuild last wrote this node, so
    `_reconcile_deleted_nodes` can retire whatever this one did not touch.
    """
    rows = chunk.rows
    if chunk.level == "catalogs":
        await graph_session.run(
            """
            UNWIND $rows AS row
            MERGE (n:Catalog {platform_id: row.platform_id})
            SET n += row, n.generation = $generation
            """,
            rows=rows,
            generation=generation,
        )
    elif chunk.level == "schemas":
        await graph_session.run(
            """
            UNWIND $rows AS row
            MATCH (parent:Catalog {platform_id: row.catalog_id})
            MERGE (n:Schema {platform_id: row.platform_id})
            SET n += row, n.generation = $generation
            MERGE (parent)-[:HAS_SCHEMA]->(n)
            """,
            rows=rows,
            generation=generation,
        )
    elif chunk.level == "tables":
        await graph_session.run(
            """
            UNWIND $rows AS row
            MATCH (parent:Schema {platform_id: row.schema_id})
            MERGE (n:Table {platform_id: row.platform_id})
            SET n += row, n.generation = $generation
            MERGE (parent)-[:HAS_TABLE]->(n)
            """,
            rows=rows,
            generation=generation,
        )
    elif chunk.level == "columns":
        await graph_session.run(
            """
            UNWIND $rows AS row
            MATCH (parent:Table {platform_id: row.table_id})
            MERGE (n:Column {platform_id: row.platform_id})
            SET n += row, n.generation = $generation
            MERGE (parent)-[:HAS_COLUMN]->(n)
            """,
            rows=rows,
            generation=generation,
        )
    elif chunk.level == "constraints":
        await graph_session.run(
            """
            UNWIND $rows AS row
            MATCH (parent:Table {platform_id: row.table_id})
            MERGE (n:Constraint {platform_id: row.platform_id})
            SET n += row, n.generation = $generation
            MERGE (parent)-[:HAS_CONSTRAINT]->(n)
            """,
            rows=rows,
            generation=generation,
        )
        await graph_session.run(
            """
            UNWIND $rows AS row
            WITH row WHERE row.referenced_table_id IS NOT NULL
            MATCH (n:Constraint {platform_id: row.platform_id})
            MATCH (referenced:Table {platform_id: row.referenced_table_id})
            MERGE (n)-[:REFERENCES]->(referenced)
            """,
            rows=rows,
            generation=generation,
        )
    else:  # pragma: no cover -- PROJECTION_LEVELS is closed; a new level is a code change
        raise ValueError(f"unknown projection level: {chunk.level}")


async def _reconcile_deleted_nodes(
    graph_session: AsyncNeo4jSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    generation: str,
    batch_size: int,
) -> dict[str, int]:
    """Retire every node of this tenant/datasource the current rebuild did not write.

    `n.generation IS NULL` is the one-time migration clause: nodes written
    before generations existed carry none, and a rebuild that rewrites every
    live row leaves exactly the retired ones holding a null. Without it those
    nodes would outlive their source rows forever -- the "additive world"
    assumption the 2026-09-05 review flagged.

    Batched by `batch_size` for the same reason the read side is chunked: one
    unbounded `DETACH DELETE` over a retired million-column source is a single
    transaction the graph has to hold entirely in memory.
    """
    deleted: dict[str, int] = {}
    for level in _DELETION_ORDER:
        label = _LEVEL_LABELS[level]
        total = 0
        while True:
            result = await graph_session.run(
                f"""
                MATCH (n:{label})
                WHERE n.organization_id = $organization_id
                  AND n.datasource_id = $datasource_id
                  AND (n.generation IS NULL OR n.generation <> $generation)
                WITH n LIMIT $batch_size
                DETACH DELETE n
                RETURN count(*) AS deleted
                """,
                organization_id=str(organization_id),
                datasource_id=str(datasource_id),
                generation=generation,
                batch_size=batch_size,
            )
            removed = 0
            async for record in result:
                removed = int(record["deleted"])
            if removed <= 0:
                break
            total += removed
            if removed < batch_size:
                break
        if total:
            PROJECTION_DELETIONS.labels(level).inc(total)
        deleted[level] = total
    return deleted

async def project_discovery(
    driver: AsyncDriver,
    event: dict[str, Any],
    *,
    chunk_size: int | None = None,
) -> ProjectionRebuildReport:
    """Rebuild one datasource's metadata projection with bounded memory.

    Two properties this holds that the previous whole-estate `load_projection`
    did not: peak memory is a function of `chunk_size` rather than of the
    estate, and a source row that has gone away is removed from the graph
    instead of surviving indefinitely because MERGE only ever adds.
    """
    payload = event["payload"]
    datasource_id = UUID(payload["datasource_id"])
    organization_id = UUID(event["organization_id"])
    generation = rebuild_generation(event)
    size = chunk_size if chunk_size is not None else resolve_chunk_size()
    started = time.perf_counter()
    rows_by_level: dict[str, int] = {}
    chunks_by_level: dict[str, int] = {}

    async with session_factory() as session:
        datasource = await session.get(DataSource, datasource_id)
        tenancy_path = _tenancy_path(datasource)
        async with driver.session() as graph_session:
            current_level: str | None = None
            level_started = time.perf_counter()
            async for chunk in iter_projection_chunks(
                session,
                datasource_id,
                organization_id,
                tenancy_path=tenancy_path,
                chunk_size=size,
            ):
                if chunk.level != current_level:
                    if current_level is not None:
                        PROJECTION_LEVEL_SECONDS.labels(current_level).observe(
                            time.perf_counter() - level_started
                        )
                    current_level = chunk.level
                    level_started = time.perf_counter()
                await _write_chunk(graph_session, chunk, generation=generation)
                # Release the read snapshot between chunks. A large-source
                # rebuild now spans every Neo4j write as well as every read, and
                # one PostgreSQL transaction held open for that whole span
                # blocks vacuum on the metadata tables for the duration. Nothing
                # is lost: under READ COMMITTED each statement already takes its
                # own snapshot, so keyset pagination was never
                # snapshot-consistent, and a row that appears or disappears
                # mid-rebuild is reconciled by the next generation anyway.
                await session.rollback()
                rows_by_level[chunk.level] = rows_by_level.get(chunk.level, 0) + len(chunk.rows)
                chunks_by_level[chunk.level] = chunks_by_level.get(chunk.level, 0) + 1
                PROJECTION_ROWS.labels(chunk.level).inc(len(chunk.rows))
                PROJECTION_CHUNKS.labels(chunk.level).inc()
            if current_level is not None:
                PROJECTION_LEVEL_SECONDS.labels(current_level).observe(
                    time.perf_counter() - level_started
                )
            deleted_by_level = await _reconcile_deleted_nodes(
                graph_session,
                organization_id=organization_id,
                datasource_id=datasource_id,
                generation=generation,
                batch_size=size,
            )

    duration = time.perf_counter() - started
    lag = event_lag_seconds(event)
    PROJECTION_REBUILD_SECONDS.observe(duration)
    if lag is not None:
        PROJECTION_LAG_SECONDS.set(lag)
    report = ProjectionRebuildReport(
        generation=generation,
        rows_by_level=rows_by_level,
        chunks_by_level=chunks_by_level,
        deleted_by_level=deleted_by_level,
        duration_seconds=duration,
        lag_seconds=lag,
    )
    logger.info(
        "metadata_graph_rebuilt",
        datasource_id=str(datasource_id),
        organization_id=str(organization_id),
        generation=generation,
        rows=report.rows,
        chunks=sum(chunks_by_level.values()),
        reconciled_deletions=report.deleted,
        duration_seconds=round(duration, 4),
        lag_seconds=None if lag is None else round(lag, 3),
    )
    return report

async def _event_datasource_id(event: dict[str, Any]) -> UUID | None:
    payload = event.get("payload") or {}
    raw_datasource_id = payload.get("datasource_id")
    if raw_datasource_id:
        try:
            return UUID(str(raw_datasource_id))
        except ValueError:
            return None
    raw_dbt_project_id = payload.get("dbt_project_id")
    if not raw_dbt_project_id:
        return None
    try:
        dbt_project_id = UUID(str(raw_dbt_project_id))
    except ValueError:
        return None
    async with session_factory() as session:
        dbt_project = await session.get(DbtProject, dbt_project_id)
        return dbt_project.datasource_id if dbt_project is not None else None


async def load_unified_lineage_projection(
    datasource_id: UUID,
    organization_id: UUID,
) -> dict[str, list[dict[str, Any]]]:
    settings = get_settings()
    async with session_factory() as session:
        datasource = await session.get(DataSource, datasource_id)
        if datasource is None or datasource.organization_id != organization_id:
            return {"nodes": [], "edges": []}
        # P1-05 / ADR-0026: the Neo4j projection is the shared, tenant-
        # facing graph. PROPOSED parsed lineage edges (view/procedure/dbt/
        # OpenLineage under `require_review` mode) are NOT part of it --
        # they belong to the review queue only until a human accepts
        # them. `include_pending_edges=False` (the default) filters
        # every one of the five edge tables inside
        # `build_unified_lineage_graph_payload`, so this projection
        # inherits the filter without a change to the MERGE below.
        graph = await build_unified_lineage_graph_payload(
            session,
            datasource,
            node_limit=settings.lineage_projection_max_nodes,
            edge_limit=settings.lineage_projection_max_edges,
            suggestion_status="ALL",
            settings=None,
            include_pending_edges=False,
        )
    # ADR-0017 SS2 -- same tenancy-path tagging as load_projection, so a domain-
    # scoped unified-lineage traversal can filter before it walks edges.
    tenancy_path = {
        "line_of_business_id": str(datasource.line_of_business_id),
        "data_domain_id": str(datasource.data_domain_id),
        "project_id": str(datasource.project_id),
    }
    prefix = f"{organization_id}:{datasource_id}:"
    nodes = [
        {
            "projection_key": f"{prefix}{node.id}",
            "platform_id": node.id,
            "organization_id": str(organization_id),
            "datasource_id": str(datasource_id),
            "node_kind": node.node_kind,
            "label": node.label,
            "qualified_name": node.qualified_name,
            "matched_table_id": str(node.matched_table_id) if node.matched_table_id else None,
            "resolved": node.resolved,
            **tenancy_path,
        }
        for node in graph.nodes
    ]
    edges = [
        {
            "projection_key": f"{prefix}{edge.id}",
            "source_projection_key": f"{prefix}{edge.source_node_id}",
            "target_projection_key": f"{prefix}{edge.target_node_id}",
            "organization_id": str(organization_id),
            "datasource_id": str(datasource_id),
            "edge_source": edge.edge_source,
            "status": edge.status,
            "confidence": edge.confidence,
            "source_columns": edge.source_columns,
            "target_columns": edge.target_columns,
            "evidence": json.dumps(edge.evidence, sort_keys=True, separators=(",", ":")),
            **tenancy_path,
        }
        for edge in graph.edges
    ]
    return {"nodes": nodes, "edges": edges}


async def project_unified_lineage(driver: AsyncDriver, event: dict[str, Any]) -> bool:
    datasource_id = await _event_datasource_id(event)
    raw_organization_id = event.get("organization_id")
    if datasource_id is None or not raw_organization_id:
        return False
    try:
        organization_id = UUID(str(raw_organization_id))
    except ValueError:
        return False
    projection = await load_unified_lineage_projection(datasource_id, organization_id)
    generation_source = json.dumps(
        {
            "event_id": event.get("event_id"),
            "nodes": [row["projection_key"] for row in projection["nodes"]],
            "edges": [row["projection_key"] for row in projection["edges"]],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    generation = hashlib.sha256(generation_source.encode("utf-8")).hexdigest()
    async with driver.session() as graph_session:
        await graph_session.run(
            """
            UNWIND $rows AS row
            MERGE (n:UnifiedLineageNode {projection_key: row.projection_key})
            SET n += row, n.generation = $generation
            """,
            rows=projection["nodes"],
            generation=generation,
        )
        await graph_session.run(
            """
            UNWIND $rows AS row
            MATCH (source:UnifiedLineageNode {projection_key: row.source_projection_key})
            MATCH (target:UnifiedLineageNode {projection_key: row.target_projection_key})
            MERGE (source)-[edge:UNIFIED_LINEAGE {projection_key: row.projection_key}]->(target)
            SET edge += row, edge.generation = $generation
            """,
            rows=projection["edges"],
            generation=generation,
        )
        await graph_session.run(
            """
            MATCH ()-[edge:UNIFIED_LINEAGE]->()
            WHERE edge.organization_id = $organization_id
              AND edge.datasource_id = $datasource_id
              AND edge.generation <> $generation
            DELETE edge
            """,
            organization_id=str(organization_id),
            datasource_id=str(datasource_id),
            generation=generation,
        )
        await graph_session.run(
            """
            MATCH (node:UnifiedLineageNode)
            WHERE node.organization_id = $organization_id
              AND node.datasource_id = $datasource_id
              AND node.generation <> $generation
            DETACH DELETE node
            """,
            organization_id=str(organization_id),
            datasource_id=str(datasource_id),
            generation=generation,
        )
    return True


async def project_event(driver: AsyncDriver, event: dict[str, Any]) -> None:
    """Apply one consumed event to the graph.

    Split out of `run_projector`'s loop so the fair-share scheduler has
    something to call per event, and so a test can drive the projection of a
    single event without a Kafka consumer.
    """
    event_type = event.get("event_type")
    if event_type in {"metadata.discovery.completed.v1", "metadata.discovery.snapshot.v1"}:
        report = await project_discovery(driver, event)
        logger.info(
            "metadata_graph_projected",
            event_id=event.get("event_id"),
            datasource_id=event["payload"]["datasource_id"],
            rows=report.rows,
            reconciled_deletions=report.deleted,
        )
    if event_type in UNIFIED_LINEAGE_PROJECTION_EVENT_TYPES and await project_unified_lineage(
        driver, event
    ):
        logger.info(
            "unified_lineage_graph_projected",
            event_id=event.get("event_id"),
            event_type=event_type,
        )


def publish_backlog(queue: TenantFairQueue) -> BacklogSnapshot:
    """Export the fair-share buffer's current depth and oldest age.

    The review asks for tenant budgets *and* for the evidence to choose them
    from; this is the second half. `oldest_organization_id` goes to the log,
    never to a metric label -- an organization id is exactly the unbounded
    label cardinality F17 flagged.
    """
    snapshot = queue.backlog()
    PROJECTION_BACKLOG_EVENTS.set(snapshot.events)
    PROJECTION_BACKLOG_TENANTS.set(snapshot.tenants)
    PROJECTION_OLDEST_BACKLOG_SECONDS.set(snapshot.oldest_age_seconds)
    return snapshot


async def drain_fairly(driver: AsyncDriver, queue: TenantFairQueue) -> int:
    """Project everything currently buffered, round-robin across tenants.

    Returns the number of events projected. The buffer is drained *completely*
    before the caller commits its offsets: fairness reorders work within a
    fetched batch, it never lets an offset advance past an event that has not
    been projected.
    """
    projected = 0
    yields_before = queue.yields
    while (event := queue.take()) is not None:
        await project_event(driver, event)
        projected += 1
        publish_backlog(queue)
    yielded = queue.yields - yields_before
    if yielded:
        PROJECTION_TENANT_YIELDS.inc(yielded)
        logger.info("graph_projector_tenant_budget_yielded", yields=yielded, projected=projected)
    return projected


async def run_projector() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    state = ProjectorState()
    budget = TenantBudget.from_env()
    queue = TenantFairQueue(budget=budget)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, setattr, state, "stopping", True)

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    await driver.verify_connectivity()
    await ensure_graph_constraints(driver)
    consumer = AIOKafkaConsumer(
        "aida.platform.events.v1",
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id="aida-graph-projector-v1",
        client_id="aida-graph-projector",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    await consumer.start()
    logger.info(
        "graph_projector_started",
        tenant_events_per_round=budget.events_per_round,
        max_buffered_events=budget.max_buffered_events,
        chunk_rows=resolve_chunk_size(),
    )
    try:
        while not state.stopping:
            # `getmany` rather than `async for message in consumer`: fairness
            # can only mean anything across more than one event, and a batch is
            # the only place several tenants' events are visible at once. The
            # commit below still happens strictly after every event in the
            # batch has been projected, so at-least-once delivery is unchanged.
            batches = await consumer.getmany(
                timeout_ms=1_000, max_records=budget.max_buffered_events
            )
            if not batches:
                publish_backlog(queue)
                continue
            for records in batches.values():
                for message in records:
                    event = json.loads(message.value)
                    if not queue.offer(event):
                        # Backpressure rather than loss: the buffer is full, so
                        # drain what is in it and then take this event. Offsets
                        # are still committed only after the whole batch has
                        # been projected, so an at-least-once redelivery -- not
                        # a dropped event -- is the worst a crash here costs.
                        await drain_fairly(driver, queue)
                        queue.offer(event)
            publish_backlog(queue)
            await drain_fairly(driver, queue)
            await consumer.commit()
    finally:
        await consumer.stop()
        await driver.close()
        logger.info("graph_projector_stopped")


if __name__ == "__main__":
    asyncio.run(run_projector())
