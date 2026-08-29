from uuid import UUID

from aida.main import app
from aida.schemas import UnifiedLineageGraphRead, UnifiedLineageImpactRead
from aida.unified_lineage import UnifiedLink, expand_frontier, traverse


def uid(value: int) -> str:
    return str(UUID(int=value))


def test_unified_frontier_expansion_respects_relationship_direction() -> None:
    links = [
        UnifiedLink("edge-a", uid(1), uid(2), "FOREIGN_KEY"),
        UnifiedLink("edge-b", uid(3), uid(1), "DBT_DEPENDENCY"),
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


def test_unified_frontier_expansion_is_bounded_and_deterministic() -> None:
    links = [UnifiedLink(f"edge-{v}", uid(1), uid(v), "FOREIGN_KEY") for v in range(2, 8)]

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


def test_traverse_finds_transitive_downstream_impact_across_mixed_edge_sources() -> None:
    # raw_orders <- (dbt depends_on) stg_orders <- (fk) fct_orders
    # i.e. fct_orders references stg_orders which depends_on raw_orders.
    links = [
        UnifiedLink("dbt-1", source_id=uid(2), target_id=uid(1), edge_source="DBT_DEPENDENCY"),
        UnifiedLink("fk-1", source_id=uid(3), target_id=uid(2), edge_source="FOREIGN_KEY"),
    ]

    downstream = traverse(
        seed=uid(1), links=links, direction="REFERENCED_BY", max_depth=5, node_limit=50
    )

    assert downstream.node_depths[uid(2)] == 1
    assert downstream.node_depths[uid(3)] == 2
    assert downstream.contributing_edge_sources[uid(2)] == frozenset(
        {"DBT_DEPENDENCY", "FOREIGN_KEY"}
    )  # uid(2) sits between both edges in the reachable subgraph
    assert not downstream.truncated


def test_traverse_upstream_is_bounded_by_depth() -> None:
    links = [
        UnifiedLink(f"e{i}", source_id=uid(i), target_id=uid(i + 1), edge_source="FOREIGN_KEY")
        for i in range(1, 6)
    ]

    upstream = traverse(
        seed=uid(1), links=links, direction="REFERENCES", max_depth=2, node_limit=50
    )

    assert set(upstream.node_depths) == {uid(1), uid(2), uid(3)}


def test_frontier_rejects_an_invalid_budget() -> None:
    import pytest

    with pytest.raises(ValueError, match="node_limit"):
        expand_frontier(
            frontier={uid(1)},
            visited={uid(1), uid(2)},
            links=[],
            direction="BOTH",
            depth=1,
            node_limit=1,
        )


def test_unified_lineage_contracts_are_published() -> None:
    paths = app.openapi()["paths"]

    assert "/v1/datasources/{datasource_id}/unified-lineage/graph" in paths
    assert "/v1/datasources/{datasource_id}/unified-lineage/impact/{node_id}" in paths


def test_unified_lineage_graph_contract_exposes_bounds_without_source_values() -> None:
    from aida.schemas import UnifiedLineageEdgeRead, UnifiedLineageNodeRead

    node = UnifiedLineageNodeRead(
        id=uid(1),
        node_kind="TABLE",
        label="customers",
        qualified_name="bank.public.customers",
        resolved=True,
        inbound_edge_count=1,
        outbound_edge_count=0,
    )
    edge = UnifiedLineageEdgeRead(
        id="fk:1",
        edge_source="FOREIGN_KEY",
        source_node_id=uid(2),
        target_node_id=uid(1),
        source_label="bank.public.orders",
        target_label="bank.public.customers",
        status="DECLARED",
        confidence=1.0,
    )
    graph = UnifiedLineageGraphRead(
        datasource_id=UUID(int=9),
        nodes=[node],
        edges=[edge],
        counts_by_source={
            "FOREIGN_KEY": 1,
            "SUGGESTED_RELATIONSHIP": 0,
            "DBT_DEPENDENCY": 0,
            "OPENLINEAGE_ETL": 0,
        },
        returned_node_count=1,
        returned_edge_count=1,
        node_limit=300,
        edge_limit=1500,
        truncated=False,
    )

    payload = graph.model_dump(mode="json")
    assert payload["nodes"][0]["id"] == uid(1)
    assert "values" not in payload["nodes"][0]
    assert payload["counts_by_source"]["FOREIGN_KEY"] == 1


def test_unified_lineage_impact_contract_carries_transitive_depth() -> None:
    from aida.schemas import UnifiedLineageImpactNodeRead

    impact = UnifiedLineageImpactRead(
        datasource_id=UUID(int=9),
        focus_node_id=uid(1),
        focus_node_kind="TABLE",
        focus_label="bank.public.customers",
        upstream=[],
        downstream=[
            UnifiedLineageImpactNodeRead(
                node_id=uid(2),
                node_kind="DBT_MODEL",
                label="stg_orders",
                qualified_name="analytics.stg_orders",
                depth=2,
                contributing_edge_sources=["DBT_DEPENDENCY", "FOREIGN_KEY"],
            )
        ],
        requested_depth=5,
        node_limit=200,
        upstream_truncated=False,
        downstream_truncated=False,
    )

    payload = impact.model_dump(mode="json")
    assert payload["downstream"][0]["depth"] == 2
    assert payload["downstream"][0]["contributing_edge_sources"] == [
        "DBT_DEPENDENCY",
        "FOREIGN_KEY",
    ]
