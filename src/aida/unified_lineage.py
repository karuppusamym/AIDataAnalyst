"""Pure, value-free graph algorithms for the Unified Lineage Explorer.

Collibra-style lineage tools present one navigable graph spanning declared
foreign keys, human-approved relationship candidates, dbt manifest
dependencies, and OpenLineage ETL runs -- and they compute impact by
*transitive* upstream/downstream traversal, not by counting direct
references. This module provides that traversal as a small, dependency-free
algorithm so it can be unit tested without a database, mirroring the existing
`aida.knowledge_graph` module but generalized to string node ids (a lineage
node may be a `MetadataTable` UUID, or a synthetic id for a dbt resource or
an OpenLineage dataset that has not been matched to a catalog table yet).

Edge direction convention: `source_id` is the *dependent* node and
`target_id` is the node it depends on (upstream). A foreign key on `orders`
referencing `customers` is stored as `source_id="orders"`,
`target_id="customers"`, so "what breaks if customers changes" is a
REFERENCED_BY traversal from customers, and "what does orders depend on" is
a REFERENCES traversal from orders. dbt `depends_on` edges and OpenLineage
input/output edges are normalized to the same convention when the graph is
built (see `aida.unified_lineage_api`), so one traversal routine serves
every lineage source.
"""

from dataclasses import dataclass, field
from typing import Literal

UnifiedDirection = Literal["BOTH", "REFERENCES", "REFERENCED_BY"]


@dataclass(frozen=True, slots=True)
class UnifiedLink:
    """One directed, typed lineage edge in the merged graph."""

    edge_id: str
    source_id: str
    target_id: str
    edge_source: str
    status: str = "ACTIVE"
    confidence: float = 1.0
    source_columns: tuple[str, ...] = ()
    target_columns: tuple[str, ...] = ()
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FrontierExpansion:
    node_ids: frozenset[str]
    node_depths: dict[str, int]
    truncated: bool


def expand_frontier(
    *,
    frontier: set[str],
    visited: set[str],
    links: list[UnifiedLink],
    direction: UnifiedDirection,
    depth: int,
    node_limit: int,
) -> FrontierExpansion:
    """Select the next deterministic graph frontier without exceeding the node budget.

    REFERENCES follows the stored source-to-target direction (what the
    frontier depends on). REFERENCED_BY follows target-to-source (what
    depends on the frontier). BOTH traverses either direction. Link ordering
    is stable so repeated requests return the same bounded neighborhood.
    """

    if node_limit < len(visited):
        raise ValueError("node_limit cannot be smaller than the visited node count")

    candidates: set[str] = set()
    for link in sorted(links, key=lambda item: item.edge_id):
        if direction in {"BOTH", "REFERENCES"} and link.source_id in frontier:
            candidates.add(link.target_id)
        if direction in {"BOTH", "REFERENCED_BY"} and link.target_id in frontier:
            candidates.add(link.source_id)

    unseen = sorted(candidates - visited)
    remaining = node_limit - len(visited)
    selected = unseen[:remaining]
    return FrontierExpansion(
        node_ids=frozenset(selected),
        node_depths={node_id: depth for node_id in selected},
        truncated=len(unseen) > len(selected),
    )


@dataclass(frozen=True, slots=True)
class TraversalResult:
    node_depths: dict[str, int]
    contributing_edge_sources: dict[str, frozenset[str]]
    truncated: bool


def traverse(
    *,
    seed: str,
    links: list[UnifiedLink],
    direction: UnifiedDirection,
    max_depth: int,
    node_limit: int,
) -> TraversalResult:
    """Breadth-first, depth- and node-bounded transitive traversal from one seed node.

    Used for both the unified graph view (BOTH) and impact analysis
    (REFERENCES for upstream, REFERENCED_BY for downstream) so the same
    bounded algorithm backs the whole Unified Lineage Explorer.
    """

    visited: set[str] = {seed}
    frontier: set[str] = {seed}
    node_depths: dict[str, int] = {seed: 0}
    truncated = False

    for current_depth in range(1, max_depth + 1):
        if not frontier or len(visited) >= node_limit:
            if frontier and len(visited) >= node_limit:
                truncated = True
            break
        expansion = expand_frontier(
            frontier=frontier,
            visited=visited,
            links=links,
            direction=direction,
            depth=current_depth,
            node_limit=node_limit,
        )
        if expansion.truncated:
            truncated = True
        frontier = set(expansion.node_ids)
        visited.update(frontier)
        node_depths.update(expansion.node_depths)

    contributing: dict[str, set[str]] = {}
    for link in links:
        if link.source_id in visited and link.target_id in visited:
            contributing.setdefault(link.source_id, set()).add(link.edge_source)
            contributing.setdefault(link.target_id, set()).add(link.edge_source)

    return TraversalResult(
        node_depths=node_depths,
        contributing_edge_sources={key: frozenset(value) for key, value in contributing.items()},
        truncated=truncated,
    )
