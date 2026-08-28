import asyncio
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
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
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
    )
    async with driver.session() as graph_session:
        for statement in statements:
            await graph_session.run(statement)


async def load_projection(
    datasource_id: UUID, organization_id: UUID
) -> dict[str, list[dict[str, Any]]]:
    async with session_factory() as session:
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
            if event.get("event_type") in {
                "metadata.discovery.completed.v1",
                "metadata.discovery.snapshot.v1",
            }:
                await project_discovery(driver, event)
                logger.info(
                    "metadata_graph_projected",
                    event_id=event["event_id"],
                    datasource_id=event["payload"]["datasource_id"],
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
