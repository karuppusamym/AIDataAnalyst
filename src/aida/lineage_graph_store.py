from collections.abc import Mapping, Sequence
from typing import Any

import structlog
from neo4j import AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from aida.config import Settings
from aida.models import DataSource
from aida.schemas import UnifiedLineageImpactNodeRead, UnifiedLineageImpactRead

logger = structlog.get_logger(__name__)


def _impact_rows(
    records: Sequence[Mapping[str, Any]],
    *,
    node_limit: int,
) -> tuple[list[UnifiedLineageImpactNodeRead], bool]:
    by_node: dict[str, dict[str, Any]] = {}
    for record in records:
        node_id = str(record["node_id"])
        current = by_node.get(node_id)
        depth = int(record["depth"])
        sources = {str(item) for item in record.get("edge_sources", []) if item}
        if current is None:
            by_node[node_id] = {
                "node_id": node_id,
                "node_kind": str(record["node_kind"]),
                "label": str(record["label"]),
                "qualified_name": str(record["qualified_name"]),
                "depth": depth,
                "sources": sources,
            }
        else:
            current["depth"] = min(int(current["depth"]), depth)
            current["sources"].update(sources)
    truncated = len(by_node) > node_limit
    selected = sorted(by_node.values(), key=lambda item: (int(item["depth"]), str(item["node_id"])))
    return (
        [
            UnifiedLineageImpactNodeRead(
                node_id=item["node_id"],
                node_kind=item["node_kind"],
                label=item["label"],
                qualified_name=item["qualified_name"],
                depth=item["depth"],
                contributing_edge_sources=sorted(item["sources"]),
            )
            for item in selected[:node_limit]
        ],
        truncated,
    )


async def load_projected_lineage_impact(
    settings: Settings,
    datasource: DataSource,
    node_id: str,
    *,
    depth: int,
    node_limit: int,
) -> UnifiedLineageImpactRead | None:
    """Read a bounded projection; return None so PostgreSQL can remain the fallback authority."""
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=0.5,
    )
    prefix = f"{datasource.organization_id}:{datasource.id}:"
    projection_key = f"{prefix}{node_id}"
    try:
        async with driver.session() as graph_session:
            focus_result = await graph_session.run(
                """
                MATCH (focus:UnifiedLineageNode {projection_key: $projection_key})
                RETURN focus.platform_id AS node_id,
                       focus.node_kind AS node_kind,
                       focus.label AS label,
                       focus.qualified_name AS qualified_name
                """,
                projection_key=projection_key,
            )
            focus_record = await focus_result.single()
            if focus_record is None:
                return None
            path_limit = max(node_limit * depth * 4, node_limit)

            async def traverse(direction: str) -> tuple[list[UnifiedLineageImpactNodeRead], bool]:
                pattern = (
                    f"(node)-[:UNIFIED_LINEAGE*1..{depth}]->(focus)"
                    if direction == "UPSTREAM"
                    else f"(focus)-[:UNIFIED_LINEAGE*1..{depth}]->(node)"
                )
                result = await graph_session.run(
                    f"""
                    MATCH (focus:UnifiedLineageNode {{projection_key: $projection_key}})
                    MATCH p={pattern}
                    WHERE all(rel IN relationships(p)
                              WHERE rel.organization_id = $organization_id
                                AND rel.datasource_id = $datasource_id)
                    RETURN node.platform_id AS node_id,
                           node.node_kind AS node_kind,
                           node.label AS label,
                           node.qualified_name AS qualified_name,
                           length(p) AS depth,
                           [rel IN relationships(p) | rel.edge_source] AS edge_sources
                    ORDER BY depth, node_id
                    LIMIT $path_limit
                    """,
                    projection_key=projection_key,
                    organization_id=str(datasource.organization_id),
                    datasource_id=str(datasource.id),
                    path_limit=path_limit,
                )
                records = [record.data() async for record in result]
                rows, truncated = _impact_rows(records, node_limit=node_limit)
                return rows, truncated or len(records) >= path_limit

            upstream, upstream_truncated = await traverse("UPSTREAM")
            downstream, downstream_truncated = await traverse("DOWNSTREAM")
            focus = focus_record.data()
            return UnifiedLineageImpactRead(
                datasource_id=datasource.id,
                focus_node_id=str(focus["node_id"]),
                focus_node_kind=str(focus["node_kind"]),
                focus_label=str(focus["qualified_name"]),
                upstream=upstream,
                downstream=downstream,
                requested_depth=depth,
                node_limit=node_limit,
                upstream_truncated=upstream_truncated,
                downstream_truncated=downstream_truncated,
            )
    except (Neo4jError, ServiceUnavailable, OSError) as exc:
        logger.warning("lineage_projection_read_failed", error=type(exc).__name__)
        return None
    finally:
        await driver.close()
