"""
Graph Expansion from Seed Hits
================================

Given seed nodes from lexical/vector search, expand via knowledge graph
edges with bounded hop traversal.  Each expanded result carries an
evidence trail showing the expansion path.

Architecture
------------
- ``GraphNode``    : node in the knowledge graph.
- ``GraphEdge``    : directed edge between two nodes.
- ``GraphHit``     : expansion result with path evidence.
- ``expand_graph`` : bounded BFS expansion from seed nodes.

Policy filtering is enforced: expansion never crosses organization
boundaries, and respects the ``allowed_org_id`` parameter.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class GraphNode:
    """A node in the knowledge graph."""

    node_id: str
    node_type: str
    display_name: str
    organization_id: UUID
    datasource_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEdge:
    """A directed edge in the knowledge graph."""

    source_id: str
    target_id: str
    edge_type: str
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphHit:
    """A graph-expansion result with path evidence."""

    object_type: str
    object_id: str
    display_name: str
    depth: int
    proximity_score: float
    expansion_path: list[str]
    edge_types: list[str]
    datasource_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Graph structure for expansion
# ---------------------------------------------------------------------------


class KnowledgeGraph:
    """In-memory knowledge graph for BFS expansion.

    In production the graph is loaded from the MetadataConstraint and
    RelationshipCandidate tables; this class provides the traversal
    logic independent of the storage backend.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._adjacency: dict[str, list[GraphEdge]] = {}

    def add_node(self, node: GraphNode) -> None:
        self._nodes[node.node_id] = node
        if node.node_id not in self._adjacency:
            self._adjacency[node.node_id] = []

    def add_edge(self, edge: GraphEdge) -> None:
        self._adjacency.setdefault(edge.source_id, []).append(edge)
        # Also add reverse direction for undirected traversal
        reverse = GraphEdge(
            source_id=edge.target_id,
            target_id=edge.source_id,
            edge_type=edge.edge_type,
            confidence=edge.confidence,
            metadata=edge.metadata,
        )
        self._adjacency.setdefault(edge.target_id, []).append(reverse)

    def get_node(self, node_id: str) -> GraphNode | None:
        return self._nodes.get(node_id)

    def get_edges(self, node_id: str) -> list[GraphEdge]:
        return self._adjacency.get(node_id, [])

    @property
    def node_count(self) -> int:
        return len(self._nodes)


# ---------------------------------------------------------------------------
# Bounded BFS expansion
# ---------------------------------------------------------------------------


def expand_graph(
    graph: KnowledgeGraph,
    seed_ids: list[str],
    *,
    allowed_org_id: UUID,
    max_hops: int = 2,
    max_results: int = 50,
) -> list[GraphHit]:
    """Expand from seed nodes via BFS up to ``max_hops`` hops.

    Policy filtering:
    - Only nodes belonging to ``allowed_org_id`` are traversed.
    - Seed nodes that do not belong to the org are silently skipped.

    Each result includes the full expansion path from the originating
    seed node.

    Parameters
    ----------
    graph          The knowledge graph to traverse.
    seed_ids       Starting node IDs (from lexical/vector search).
    allowed_org_id Organization boundary -- expansion never crosses.
    max_hops       Maximum BFS depth (default 2).
    max_results    Cap on returned hits (default 50).
    """
    visited: set[str] = set()
    hits: list[GraphHit] = []

    # Queue entries: (node_id, depth, path, edge_types)
    queue: deque[tuple[str, int, list[str], list[str]]] = deque()

    for seed_id in seed_ids:
        node = graph.get_node(seed_id)
        if node is None:
            continue
        # Policy filter: only expand from nodes in the allowed org
        if node.organization_id != allowed_org_id:
            continue
        visited.add(seed_id)
        queue.append((seed_id, 0, [seed_id], []))

    while queue and len(hits) < max_results:
        current_id, depth, path, edge_types = queue.popleft()
        current_node = graph.get_node(current_id)
        if current_node is None:
            continue

        # Add non-seed nodes as expansion hits
        if depth > 0:
            # Proximity score decays with distance
            proximity = round(1.0 / (1.0 + depth), 4)
            hits.append(
                GraphHit(
                    object_type=current_node.node_type,
                    object_id=current_id,
                    display_name=current_node.display_name,
                    depth=depth,
                    proximity_score=proximity,
                    expansion_path=list(path),
                    edge_types=list(edge_types),
                    datasource_id=current_node.datasource_id,
                    metadata=current_node.metadata,
                )
            )

        # Expand neighbours if within hop limit
        if depth < max_hops:
            for edge in graph.get_edges(current_id):
                target_id = edge.target_id
                if target_id in visited:
                    continue
                target_node = graph.get_node(target_id)
                if target_node is None:
                    continue
                # Policy filter: never cross org boundary
                if target_node.organization_id != allowed_org_id:
                    continue
                visited.add(target_id)
                queue.append((
                    target_id,
                    depth + 1,
                    path + [target_id],
                    edge_types + [edge.edge_type],
                ))

    # Sort by proximity descending
    hits.sort(key=lambda h: h.proximity_score, reverse=True)
    return hits[:max_results]
