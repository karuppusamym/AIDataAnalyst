from collections.abc import Callable
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


def _candidate_targets(
    frontier: set[UUID], links: list[GraphLink], direction: GraphDirection
) -> set[UUID]:
    """The set of nodes one hop away from `frontier`, before dedup/budget/policy.

    Shared by `expand_frontier` and `expand_cross_source_frontier` so the two
    never drift on what "one hop away" means -- only what happens to a
    candidate once it is found differs between them.
    """

    candidates: set[UUID] = set()
    for link in sorted(links, key=lambda item: item.edge_id):
        if direction in {"BOTH", "REFERENCES"} and link.source_node_id in frontier:
            candidates.add(link.target_node_id)
        if direction in {"BOTH", "REFERENCED_BY"} and link.target_node_id in frontier:
            candidates.add(link.source_node_id)
    return candidates


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

    candidates = _candidate_targets(frontier, links, direction)
    unseen = sorted(candidates - visited, key=str)
    remaining = node_limit - len(visited)
    selected = unseen[:remaining]
    return FrontierExpansion(
        node_ids=frozenset(selected),
        node_depths={node_id: depth for node_id in selected},
        truncated=len(unseen) > len(selected),
    )


def expand_cross_source_frontier(
    *,
    frontier: set[UUID],
    visited: set[UUID],
    links: list[GraphLink],
    direction: GraphDirection,
    depth: int,
    node_limit: int,
    node_datasource_id: dict[UUID, UUID],
    is_datasource_authorized: Callable[[UUID], bool],
) -> FrontierExpansion:
    """`expand_frontier`, plus a per-node datasource authorization check (KG-2).

    A `GraphLink` may connect nodes that live in two different datasources (a
    `RelationshipCandidate` whose `datasource_id` and `target_datasource_id`
    differ -- see `aida.models.RelationshipCandidate`). `node_datasource_id`
    names the owning datasource for every node this call already knows about;
    a candidate node missing from it is treated as already-authorized (the
    caller is expected to omit only nodes it separately established access to,
    e.g. the traversal seed, which the route layer checks before traversal
    starts).

    A candidate whose datasource is not authorized is dropped *before* the
    node_limit budget is applied -- same as if the edge leading to it did not
    exist: it never displaces an authorized node for a budget slot, it is
    never counted in `node_ids`, and it never sets `truncated`. This is what
    makes the denial indistinguishable from absence (no "exists but denied"
    signal survives into the response) -- the caller-visible behavior for an
    org that has no such relationship at all and one where the relationship
    exists but the caller cannot see the far datasource is identical.
    """

    if node_limit < len(visited):
        raise ValueError("node_limit cannot be smaller than the visited node count")

    candidates = _candidate_targets(frontier, links, direction) - visited
    authorized = {
        node_id
        for node_id in candidates
        if node_id not in node_datasource_id
        or is_datasource_authorized(node_datasource_id[node_id])
    }
    unseen = sorted(authorized, key=str)
    remaining = node_limit - len(visited)
    selected = unseen[:remaining]
    return FrontierExpansion(
        node_ids=frozenset(selected),
        node_depths={node_id: depth for node_id in selected},
        truncated=len(unseen) > len(selected),
    )
