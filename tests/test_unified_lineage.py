from collections import defaultdict
from typing import Any
from uuid import UUID, uuid4

import pytest

from aida.main import app
from aida.models import DataSource, DbtArtifactImport, DbtLineageEdge, DbtProject, DbtResource
from aida.schemas import UnifiedLineageGraphRead, UnifiedLineageImpactRead
from aida.unified_lineage import UnifiedLink, expand_frontier, traverse
from aida.unified_lineage_api import build_unified_lineage_graph_payload


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


# ---------------------------------------------------------------------------
# LN-5 regression: column-level dbt edges must not duplicate the unified
# graph's table/resource-level dbt dependency links.
# ---------------------------------------------------------------------------


def _equality_filters(whereclause: Any) -> list[tuple[str, str, Any]]:
    """Recursively pull (table_name, column_name, value) equalities out of a
    whereclause. Trimmed copy of the helper in
    `tests/test_dbt_run_results_integration.py` -- see that module for the
    full rationale behind this style of AsyncSession double."""
    if whereclause is None:
        return []
    clauses = getattr(whereclause, "clauses", None)
    if clauses is not None:
        filters: list[tuple[str, str, Any]] = []
        for clause in clauses:
            filters.extend(_equality_filters(clause))
        return filters
    left = getattr(whereclause, "left", None)
    right = getattr(whereclause, "right", None)
    table = getattr(left, "table", None)
    col_name = getattr(left, "key", None) or getattr(left, "name", None)
    if left is None or right is None or table is None or col_name is None:
        return []
    value = getattr(right, "value", right)
    return [(table.name, col_name, value)]


class _FakeScalarsResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None


class _FakeExecResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeUnifiedLineageSession:
    """Minimal in-memory AsyncSession double covering only the read-only
    queries `_build_unified_graph` issues. No FK constraints, suggested
    relationship candidates, catalog tables, or OpenLineage edges are seeded
    in this test, so every multi-entity join (the MetadataTable/Schema/Catalog
    join, and the OpenLineageTableEdge/RunEvent join) is guaranteed empty and
    is answered as such without reimplementing either join.
    """

    def __init__(self) -> None:
        self._store: dict[type, dict[Any, Any]] = defaultdict(dict)

    def seed(self, obj: Any) -> Any:
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        self._store[type(obj)][obj.id] = obj
        return obj

    def _rows_for(self, stmt: Any) -> list[Any]:
        model = stmt.column_descriptions[0]["type"]
        filters = _equality_filters(stmt.whereclause)
        candidates = list(self._store.get(model, {}).values())
        table_name = model.__table__.name
        for filter_table, col, value in filters:
            if filter_table != table_name:
                continue
            candidates = [obj for obj in candidates if getattr(obj, col) == value]
        return candidates

    async def scalars(self, stmt: Any) -> _FakeScalarsResult:
        return _FakeScalarsResult(self._rows_for(stmt))

    async def execute(self, stmt: Any) -> _FakeExecResult:
        entities = [d["type"] for d in stmt.column_descriptions]
        if len(entities) == 1:
            return _FakeExecResult([(row,) for row in self._rows_for(stmt)])
        return _FakeExecResult([])


@pytest.mark.asyncio
async def test_unified_graph_ignores_column_level_dbt_edges() -> None:
    """Column-level (LN-5) `COLUMN_DEPENDS_ON` rows must not render as extra
    parallel links in the unified graph alongside the table-level
    `DEPENDS_ON` edge between the same two dbt resources."""
    session = _FakeUnifiedLineageSession()
    organization_id = uuid4()

    datasource = session.seed(
        DataSource(
            organization_id=organization_id,
            line_of_business_id=uuid4(),
            project_id=uuid4(),
            name="bank-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PROD",
            credential_reference="secret://bank-warehouse",
        )
    )
    dbt_project = session.seed(
        DbtProject(
            organization_id=organization_id,
            project_id=uuid4(),
            datasource_id=datasource.id,
            project_key="bank-dbt",
            display_name="Bank dbt project",
            target_name="prod",
            status="ACTIVE",
            created_by="dbt-bot@bank.internal",
        )
    )
    artifact = session.seed(
        DbtArtifactImport(
            organization_id=organization_id,
            dbt_project_id=dbt_project.id,
            status="IMPORTED",
        )
    )
    upstream = session.seed(
        DbtResource(
            organization_id=organization_id,
            artifact_import_id=artifact.id,
            unique_id="model.bank.stg_orders",
            resource_type="MODEL",
            name="stg_orders",
            relation_name="analytics.staging.stg_orders",
        )
    )
    downstream = session.seed(
        DbtResource(
            organization_id=organization_id,
            artifact_import_id=artifact.id,
            unique_id="model.bank.fct_orders",
            resource_type="MODEL",
            name="fct_orders",
            relation_name="analytics.marts.fct_orders",
        )
    )
    session.seed(
        DbtLineageEdge(
            organization_id=organization_id,
            artifact_import_id=artifact.id,
            source_resource_id=upstream.id,
            target_resource_id=downstream.id,
            edge_type="DEPENDS_ON",
            source_column="",
            target_column="",
        )
    )
    # Two column-level edges between the exact same resource pair -- without
    # the `edge_type == "DEPENDS_ON"` filter these would render as two more
    # parallel links on top of the one above.
    for source_col, target_col in (("id", "order_id"), ("amount", "order_amount")):
        session.seed(
            DbtLineageEdge(
                organization_id=organization_id,
                artifact_import_id=artifact.id,
                source_resource_id=upstream.id,
                target_resource_id=downstream.id,
                edge_type="COLUMN_DEPENDS_ON",
                source_column=source_col,
                target_column=target_col,
                transformation_type="DIRECT",
                confidence="FULL",
            )
        )

    graph = await build_unified_lineage_graph_payload(session, datasource, settings=None)  # type: ignore[arg-type]

    assert graph.counts_by_source["DBT_DEPENDENCY"] == 1
    dbt_edges = [edge for edge in graph.edges if edge.edge_source == "DBT_DEPENDENCY"]
    assert len(dbt_edges) == 1
    assert dbt_edges[0].source_node_id == f"dbt:{upstream.id}"
    assert dbt_edges[0].target_node_id == f"dbt:{downstream.id}"
