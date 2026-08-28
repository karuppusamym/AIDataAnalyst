from uuid import UUID

import pytest

from aida.knowledge_graph import GraphLink, expand_frontier
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
