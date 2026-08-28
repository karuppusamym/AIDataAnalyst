from dataclasses import dataclass
from typing import Literal
from uuid import UUID

GraphDirection = Literal["BOTH", "REFERENCES", "REFERENCED_BY"]


@dataclass(frozen=True, slots=True)
class GraphLink:
    """A value-free relationship used to expand a bounded metadata neighborhood."""

    edge_id: str
    source_node_id: UUID
    target_node_id: UUID


@dataclass(frozen=True, slots=True)
class FrontierExpansion:
    node_ids: frozenset[UUID]
    node_depths: dict[UUID, int]
    truncated: bool


def expand_frontier(
    *,
    frontier: set[UUID],
    visited: set[UUID],
    links: list[GraphLink],
    direction: GraphDirection,
    depth: int,
    node_limit: int,
) -> FrontierExpansion:
    """Select the next deterministic graph frontier without exceeding the node budget.

    REFERENCES follows the stored source-to-target direction. REFERENCED_BY follows
    target-to-source. BOTH treats the relationship as traversable in either direction.
    Link ordering is stable so repeated requests return the same bounded neighborhood.
    """

    if node_limit < len(visited):
        raise ValueError("node_limit cannot be smaller than the visited node count")

    candidates: set[UUID] = set()
    for link in sorted(links, key=lambda item: item.edge_id):
        if direction in {"BOTH", "REFERENCES"} and link.source_node_id in frontier:
            candidates.add(link.target_node_id)
        if direction in {"BOTH", "REFERENCED_BY"} and link.target_node_id in frontier:
            candidates.add(link.source_node_id)

    unseen = sorted(candidates - visited, key=str)
    remaining = node_limit - len(visited)
    selected = unseen[:remaining]
    return FrontierExpansion(
        node_ids=frozenset(selected),
        node_depths={node_id: depth for node_id in selected},
        truncated=len(unseen) > len(selected),
    )
