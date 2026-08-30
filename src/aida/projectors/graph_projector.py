import asyncio
import hashlib
import json
import signal
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from aiokafka import AIOKafkaConsumer
from neo4j import AsyncDriver, AsyncGraphDatabase
from sqlalchemy import select

from aida.config import get_settings
from aida.db import session_factory
from aida.logging import configure_logging
from aida.models import (
    DataSource,
    DbtProject,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
)
from aida.unified_lineage_api import build_unified_lineage_graph_payload

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


async def load_projection(
    datasource_id: UUID, organization_id: UUID
) -> dict[str, list[dict[str, Any]]]:
    async with session_factory() as session:
        # ADR-0017 SS2 -- every projected node carries its full tenancy path, not
        # just organization_id, so a bounded traversal can be scoped to a domain.
        datasource = await session.get(DataSource, datasource_id)
        tenancy_path = (
            {
                "line_of_business_id": str(datasource.line_of_business_id),
                "data_domain_id": str(datasource.data_domain_id),
                "project_id": str(datasource.project_id),
            }
            if datasource is not None
            else {}
        )
        catalogs = (
            await session.scalars(
                select(MetadataCatalog).where(
                    MetadataCatalog.datasource_id == datasource_id,
                    MetadataCatalog.organization_id == organization_id,
                )
            )
        ).all()
        catalog_ids = [catalog.id for catalog in catalogs]
        schemas = (
            await session.scalars(
                select(MetadataSchema).where(
                    MetadataSchema.catalog_id.in_(catalog_ids),
                    MetadataSchema.organization_id == organization_id,
                )
            )
        ).all()
        schema_ids = [schema.id for schema in schemas]
        tables = (
            await session.scalars(
                select(MetadataTable).where(
                    MetadataTable.schema_id.in_(schema_ids),
                    MetadataTable.organization_id == organization_id,
                )
            )
        ).all()
        table_ids = [table.id for table in tables]
        columns = (
            await session.scalars(
                select(MetadataColumn).where(
                    MetadataColumn.table_id.in_(table_ids),
                    MetadataColumn.organization_id == organization_id,
                )
            )
        ).all()
        constraints = (
            await session.scalars(
                select(MetadataConstraint).where(
                    MetadataConstraint.table_id.in_(table_ids),
                    MetadataConstraint.organization_id == organization_id,
                )
            )
        ).all()

    return {
        "catalogs": [
            {
                "platform_id": str(item.id),
                "organization_id": str(organization_id),
                "datasource_id": str(datasource_id),
                "name": item.name,
                "status": item.status,
                **tenancy_path,
            }
            for item in catalogs
        ],
        "schemas": [
            {
                "platform_id": str(item.id),
                "organization_id": str(organization_id),
                "catalog_id": str(item.catalog_id),
                "name": item.name,
                "status": item.status,
                **tenancy_path,
            }
            for item in schemas
        ],
        "tables": [
            {
                "platform_id": str(item.id),
                "organization_id": str(organization_id),
                "schema_id": str(item.schema_id),
                "name": item.name,
                "object_type": item.object_type,
                "status": item.status,
                **tenancy_path,
            }
            for item in tables
        ],
        "columns": [
            {
                "platform_id": str(item.id),
                "organization_id": str(organization_id),
                "table_id": str(item.table_id),
                "name": item.name,
                "ordinal_position": item.ordinal_position,
                "physical_type": item.physical_type,
                "classification": item.classification,
                "status": item.status,
                **tenancy_path,
            }
            for item in columns
        ],
        "constraints": [
            {
                "platform_id": str(item.id),
                "organization_id": str(organization_id),
                "table_id": str(item.table_id),
                "name": item.name,
                "constraint_type": item.constraint_type,
                "columns": item.columns,
                "referenced_table_id": (
                    str(item.referenced_table_id) if item.referenced_table_id else None
                ),
                "referenced_columns": item.referenced_columns,
                "status": item.status,
                **tenancy_path,
            }
            for item in constraints
        ],
    }


async def project_discovery(driver: AsyncDriver, event: dict[str, Any]) -> None:
    payload = event["payload"]
    datasource_id = UUID(payload["datasource_id"])
    organization_id = UUID(event["organization_id"])
    projection = await load_projection(datasource_id, organization_id)
    async with driver.session() as graph_session:
        await graph_session.run(
            """
            UNWIND $rows AS row
            MERGE (n:Catalog {platform_id: row.platform_id})
            SET n += row
            """,
            rows=projection["catalogs"],
        )
        await graph_session.run(
            """
            UNWIND $rows AS row
            MATCH (parent:Catalog {platform_id: row.catalog_id})
            MERGE (n:Schema {platform_id: row.platform_id})
            SET n += row
            MERGE (parent)-[:HAS_SCHEMA]->(n)
            """,
            rows=projection["schemas"],
        )
        await graph_session.run(
            """
            UNWIND $rows AS row
            MATCH (parent:Schema {platform_id: row.schema_id})
            MERGE (n:Table {platform_id: row.platform_id})
            SET n += row
            MERGE (parent)-[:HAS_TABLE]->(n)
            """,
            rows=projection["tables"],
        )
        await graph_session.run(
            """
            UNWIND $rows AS row
            MATCH (parent:Table {platform_id: row.table_id})
            MERGE (n:Column {platform_id: row.platform_id})
            SET n += row
            MERGE (parent)-[:HAS_COLUMN]->(n)
            """,
            rows=projection["columns"],
        )
        await graph_session.run(
            """
            UNWIND $rows AS row
            MATCH (parent:Table {platform_id: row.table_id})
            MERGE (n:Constraint {platform_id: row.platform_id})
            SET n += row
            MERGE (parent)-[:HAS_CONSTRAINT]->(n)
            """,
            rows=projection["constraints"],
        )
        await graph_session.run(
            """
            UNWIND $rows AS row
            WITH row WHERE row.referenced_table_id IS NOT NULL
            MATCH (n:Constraint {platform_id: row.platform_id})
            MATCH (referenced:Table {platform_id: row.referenced_table_id})
            MERGE (n)-[:REFERENCES]->(referenced)
            """,
            rows=projection["constraints"],
        )


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
        graph = await build_unified_lineage_graph_payload(
            session,
            datasource,
            node_limit=settings.lineage_projection_max_nodes,
            edge_limit=settings.lineage_projection_max_edges,
            suggestion_status="ALL",
            settings=None,
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


async def run_projector() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger(__name__)
    state = ProjectorState()
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
    logger.info("graph_projector_started")
    try:
        async for message in consumer:
            event = json.loads(message.value)
            event_type = event.get("event_type")
            if event_type in {
                "metadata.discovery.completed.v1",
                "metadata.discovery.snapshot.v1",
            }:
                await project_discovery(driver, event)
                logger.info(
                    "metadata_graph_projected",
                    event_id=event["event_id"],
                    datasource_id=event["payload"]["datasource_id"],
                )
            if (
                event_type in UNIFIED_LINEAGE_PROJECTION_EVENT_TYPES
                and await project_unified_lineage(driver, event)
            ):
                logger.info(
                    "unified_lineage_graph_projected",
                    event_id=event.get("event_id"),
                    event_type=event_type,
                )
            await consumer.commit()
            if state.stopping:
                break
    finally:
        await consumer.stop()
        await driver.close()
        logger.info("graph_projector_stopped")


if __name__ == "__main__":
    asyncio.run(run_projector())
