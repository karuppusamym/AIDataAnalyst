from uuid import UUID

import pytest

from aida.knowledge_graph import GraphLink, expand_cross_source_frontier, expand_frontier
from aida.main import app
from aida.schemas import GraphNodeRead, KnowledgeGraphRead


def uid(value: int) -> UUID:
    return UUID(int=value)


def test_frontier_expansion_respects_relationship_direction() -> None:
    links = [
        GraphLink("edge-a", uid(1), uid(2)),
        GraphLink("edge-b", uid(3), uid(1)),
    ]

    references = expand_frontier(
        frontier={uid(1)},
        visited={uid(1)},
        links=links,
        direction="REFERENCES",
        depth=1,
        node_limit=10,
    )
    referenced_by = expand_frontier(
        frontier={uid(1)},
        visited={uid(1)},
        links=links,
        direction="REFERENCED_BY",
        depth=1,
        node_limit=10,
    )

    assert references.node_ids == frozenset({uid(2)})
    assert referenced_by.node_ids == frozenset({uid(3)})
    assert references.node_depths == {uid(2): 1}


def test_frontier_expansion_is_bounded_and_deterministic() -> None:
    links = [GraphLink(f"edge-{value}", uid(1), uid(value)) for value in range(2, 8)]

    result = expand_frontier(
        frontier={uid(1)},
        visited={uid(1)},
        links=list(reversed(links)),
        direction="BOTH",
        depth=1,
        node_limit=3,
    )

    assert result.node_ids == frozenset({uid(2), uid(3)})
    assert result.truncated is True


def test_frontier_rejects_an_invalid_budget() -> None:
    with pytest.raises(ValueError, match="node_limit"):
        expand_frontier(
            frontier={uid(1)},
            visited={uid(1), uid(2)},
            links=[],
            direction="BOTH",
            depth=1,
            node_limit=1,
        )


# ---------------------------------------------------------------------------
# KG-2 -- cross-source traversal. `expand_cross_source_frontier` is the pure,
# DB-free core of "follow a RelationshipCandidate edge across a datasource
# boundary, bounded like same-source traversal, and never surface a node whose
# datasource the caller is not authorized for." Datasource A = uid(100),
# datasource B = uid(200) throughout; node ids below 100 stand in for
# MetadataTable ids (as in the tests above), never reused as datasource ids.
# ---------------------------------------------------------------------------

DATASOURCE_A = uid(100)
DATASOURCE_B = uid(200)


def test_cross_source_frontier_follows_an_edge_into_an_authorized_datasource() -> None:
    # uid(1)/uid(2) live in A, uid(3) only exists via a RelationshipCandidate
    # whose target_datasource_id is B.
    links = [GraphLink("candidate:cross-1", uid(2), uid(3))]
    node_datasource_id = {uid(1): DATASOURCE_A, uid(2): DATASOURCE_A, uid(3): DATASOURCE_B}

    expansion = expand_cross_source_frontier(
        frontier={uid(2)},
        visited={uid(1), uid(2)},
        links=links,
        direction="REFERENCES",
        depth=1,
        node_limit=10,
        node_datasource_id=node_datasource_id,
        is_datasource_authorized=lambda ds_id: ds_id in {DATASOURCE_A, DATASOURCE_B},
    )

    assert expansion.node_ids == frozenset({uid(3)})
    assert expansion.truncated is False


def test_cross_source_frontier_matches_expand_frontier_when_every_node_is_authorized() -> None:
    # Same shape as test_frontier_expansion_is_bounded_and_deterministic above --
    # with every candidate authorized, the two functions must agree exactly, so
    # adding cross-source awareness never changes single-source behavior.
    links = [GraphLink(f"edge-{value}", uid(1), uid(value)) for value in range(2, 8)]
    node_datasource_id = {uid(value): DATASOURCE_A for value in range(1, 8)}

    plain = expand_frontier(
        frontier={uid(1)},
        visited={uid(1)},
        links=list(reversed(links)),
        direction="BOTH",
        depth=1,
        node_limit=3,
    )
    cross_source = expand_cross_source_frontier(
        frontier={uid(1)},
        visited={uid(1)},
        links=list(reversed(links)),
        direction="BOTH",
        depth=1,
        node_limit=3,
        node_datasource_id=node_datasource_id,
        is_datasource_authorized=lambda _ds_id: True,
    )

    assert cross_source.node_ids == plain.node_ids == frozenset({uid(2), uid(3)})
    assert cross_source.truncated is plain.truncated is True


def test_cross_source_frontier_rejects_an_invalid_budget() -> None:
    with pytest.raises(ValueError, match="node_limit"):
        expand_cross_source_frontier(
            frontier={uid(1)},
            visited={uid(1), uid(2)},
            links=[],
            direction="BOTH",
            depth=1,
            node_limit=1,
            node_datasource_id={},
            is_datasource_authorized=lambda _ds_id: True,
        )


def test_leak_cross_source_frontier_denied_datasource_node_never_appears() -> None:
    """A caller authorized for A but not B must never see uid(3) (B), even though
    a real RelationshipCandidate edge connects it to uid(2) (A) in the frontier.

    Mirrors the EE.10 leak-test shape: the denied case and an "if this edge simply
    didn't exist" case must be indistinguishable to the caller. Proven two ways,
    like EE.10 -- (1) the denied-B run and a run where the cross-source link is
    absent entirely produce byte-identical `FrontierExpansion` results; and (2) the
    denied node never appears in `node_ids`/`node_depths` and never causes
    `truncated` to flip, which is what "excluded exactly as if the edge did not
    exist" requires (a truncation reason would itself be a distinguishable signal
    that something was withheld).
    """

    links_with_cross_source_edge = [GraphLink("candidate:cross-1", uid(2), uid(3))]
    node_datasource_id = {uid(1): DATASOURCE_A, uid(2): DATASOURCE_A, uid(3): DATASOURCE_B}

    denied = expand_cross_source_frontier(
        frontier={uid(2)},
        visited={uid(1), uid(2)},
        links=links_with_cross_source_edge,
        direction="REFERENCES",
        depth=1,
        node_limit=10,
        node_datasource_id=node_datasource_id,
        is_datasource_authorized=lambda ds_id: ds_id == DATASOURCE_A,  # B is NOT authorized
    )

    no_such_edge = expand_cross_source_frontier(
        frontier={uid(2)},
        visited={uid(1), uid(2)},
        links=[],  # stands in for "the relationship candidate never existed"
        direction="REFERENCES",
        depth=1,
        node_limit=10,
        node_datasource_id=node_datasource_id,
        is_datasource_authorized=lambda ds_id: ds_id == DATASOURCE_A,
    )

    assert denied == no_such_edge
    assert denied.node_ids == frozenset()
    assert denied.node_depths == {}
    assert denied.truncated is False
    assert uid(3) not in denied.node_ids


def test_leak_cross_source_frontier_denied_node_does_not_consume_node_budget() -> None:
    """A denied cross-source candidate must not spend a node_limit slot that an
    authorized node in the same frontier expansion is entitled to -- otherwise a
    denial would be observable indirectly, as an authorized node going missing.
    """

    links = [
        GraphLink("candidate:cross-1", uid(2), uid(3)),  # uid(3) in denied B
        GraphLink("candidate:same-source", uid(2), uid(4)),  # uid(4) in authorized A
    ]
    node_datasource_id = {
        uid(1): DATASOURCE_A,
        uid(2): DATASOURCE_A,
        uid(3): DATASOURCE_B,
        uid(4): DATASOURCE_A,
    }

    expansion = expand_cross_source_frontier(
        frontier={uid(2)},
        visited={uid(1), uid(2)},
        links=links,
        direction="REFERENCES",
        depth=1,
        node_limit=3,  # 2 already visited + exactly one more slot for the winner
        node_datasource_id=node_datasource_id,
        is_datasource_authorized=lambda ds_id: ds_id == DATASOURCE_A,
    )

    assert expansion.node_ids == frozenset({uid(4)})
    assert expansion.truncated is False


def test_graph_v2_contracts_are_published() -> None:
    paths = app.openapi()["paths"]

    assert "/v1/datasources/{datasource_id}/knowledge-graph/search" in paths
    assert "/v1/datasources/{datasource_id}/knowledge-graph/neighborhood" in paths


def test_graph_contract_exposes_bounds_without_source_values() -> None:
    node = GraphNodeRead(
        id=uid(1),
        node_type="TABLE",
        label="customers",
        qualified_name="bank.public.customers",
        object_type="TABLE",
        status="ACTIVE",
        column_count=12,
        sensitive_column_count=4,
        depth=0,
        inbound_edge_count=2,
        outbound_edge_count=1,
    )
    graph = KnowledgeGraphRead(
        datasource_id=uid(9),
        nodes=[node],
        edges=[],
        total_tables=10_000,
        total_declared_edges=7_500,
        total_suggested_edges=800,
        pending_suggestions=120,
        truncated=True,
        focus_node_id=node.id,
        requested_depth=2,
        returned_node_count=1,
        returned_edge_count=0,
        node_limit=100,
        edge_limit=500,
        truncation_reasons=["NODE_LIMIT"],
    )

    payload = graph.model_dump(mode="json")
    assert payload["focus_node_id"] == str(node.id)
    assert payload["truncation_reasons"] == ["NODE_LIMIT"]
    assert "values" not in payload["nodes"][0]
