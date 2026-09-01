from collections import defaultdict
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataViewDefinition
from aida.main import app
from aida.mcp_server import _transformation_detail
from aida.models import (
    DataDomain,
    DataSource,
    DbtArtifactImport,
    DbtLineageEdge,
    DbtProject,
    DbtResource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    ProcedureLineageEdge,
    Project,
    ViewLineageEdge,
)
from aida.schemas import UnifiedLineageGraphRead, UnifiedLineageImpactRead
from aida.security_types import SecurityContext
from aida.unified_lineage import UnifiedLink, expand_frontier, traverse
from aida.unified_lineage_api import (
    build_unified_lineage_graph_payload,
    build_unified_lineage_impact_payload,
    get_unified_lineage_impact,
)


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


# ---------------------------------------------------------------------------
# LN-7: transitive, cross-kind, bounded, policy-filtered impact traversal
# against a real (in-memory SQLite) database -- exercises `_build_unified_graph`
# end to end rather than the pure-algorithm layer above, specifically for the
# view/procedure lineage edges (LN-2) newly folded into the unified graph.
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as session:
        yield session
    await engine.dispose()


async def _seed_org_and_datasource(
    session: AsyncSession, *, name: str = "primary"
) -> tuple[DataSource, MetadataSchema]:
    org = Organization(id=uuid4(), name=f"org-{uuid4().hex[:8]}", slug=f"org-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=name,
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    return datasource, schema


async def _seed_table(
    session: AsyncSession, datasource: DataSource, schema: MetadataSchema, name: str
) -> MetadataTable:
    table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type="BASE_TABLE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return table


@pytest.mark.asyncio
async def test_unified_lineage_impact_chains_view_definition_into_foreign_key(db_session) -> None:
    """LN-7 regression: a 2-hop chain across two different edge kinds --
    raw_orders --VIEW_DEFINITION--> vw_orders --FOREIGN_KEY--> fct_orders --
    must surface fct_orders as transitive downstream impact of raw_orders,
    correctly attributed to depth 2 and the FOREIGN_KEY edge, even though
    reaching it required crossing out of the VIEW_DEFINITION edge kind that
    connects raw_orders to vw_orders. Neither edge kind alone reaches
    fct_orders from raw_orders."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    raw_orders = await _seed_table(db_session, datasource, schema, "raw_orders")
    vw_orders = await _seed_table(db_session, datasource, schema, "vw_orders")
    fct_orders = await _seed_table(db_session, datasource, schema, "fct_orders")

    db_session.add(
        ViewLineageEdge(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            source_table="bank.public.raw_orders",
            source_column="id",
            target_table="bank.public.vw_orders",
            target_column="order_id",
            source_table_id=raw_orders.id,
            target_table_id=vw_orders.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h1",
        )
    )
    db_session.add(
        MetadataConstraint(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=fct_orders.id,
            name="fk_fct_orders_vw_orders",
            constraint_type="FOREIGN_KEY",
            columns=["vw_orders_id"],
            referenced_table_id=vw_orders.id,
            referenced_columns=["order_id"],
            status="ACTIVE",
            fingerprint="fp",
        )
    )
    await db_session.flush()

    result = await build_unified_lineage_impact_payload(
        db_session, datasource, str(raw_orders.id), depth=5, node_limit=50, settings=None
    )

    downstream_by_id = {row.node_id: row for row in result.downstream}
    assert str(vw_orders.id) in downstream_by_id
    assert downstream_by_id[str(vw_orders.id)].depth == 1
    # vw_orders sits between both edges in the reachable subgraph, so it
    # carries both contributing kinds -- same convention as the mixed-source
    # pure-algorithm test above.
    assert downstream_by_id[str(vw_orders.id)].contributing_edge_sources == [
        "FOREIGN_KEY",
        "VIEW_DEFINITION",
    ]

    assert str(fct_orders.id) in downstream_by_id
    assert downstream_by_id[str(fct_orders.id)].depth == 2
    assert downstream_by_id[str(fct_orders.id)].contributing_edge_sources == ["FOREIGN_KEY"]
    assert not result.downstream_truncated


@pytest.mark.asyncio
async def test_unified_lineage_impact_node_bound_stops_before_the_second_hop(db_session) -> None:
    """Same chain as above, but with `node_limit` too small to reach the
    second hop: the FOREIGN_KEY-only-reachable fct_orders must not appear,
    and the result must self-report as truncated rather than silently
    returning a partial, unmarked answer."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    raw_orders = await _seed_table(db_session, datasource, schema, "raw_orders")
    vw_orders = await _seed_table(db_session, datasource, schema, "vw_orders")
    fct_orders = await _seed_table(db_session, datasource, schema, "fct_orders")

    db_session.add(
        ViewLineageEdge(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            source_table="bank.public.raw_orders",
            source_column="id",
            target_table="bank.public.vw_orders",
            target_column="order_id",
            source_table_id=raw_orders.id,
            target_table_id=vw_orders.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h1",
        )
    )
    db_session.add(
        MetadataConstraint(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=fct_orders.id,
            name="fk_fct_orders_vw_orders",
            constraint_type="FOREIGN_KEY",
            columns=["vw_orders_id"],
            referenced_table_id=vw_orders.id,
            referenced_columns=["order_id"],
            status="ACTIVE",
            fingerprint="fp",
        )
    )
    await db_session.flush()

    # node_limit=2 admits only the seed (raw_orders) and one more node --
    # the graph itself has 3 tables plus the seed already counted, so the
    # bound is deliberately smaller than the reachable set.
    result = await build_unified_lineage_impact_payload(
        db_session, datasource, str(raw_orders.id), depth=5, node_limit=2, settings=None
    )

    downstream_ids = {row.node_id for row in result.downstream}
    assert str(vw_orders.id) in downstream_ids
    assert str(fct_orders.id) not in downstream_ids
    assert result.downstream_truncated is True


@pytest.mark.asyncio
async def test_unified_lineage_never_leaks_a_table_outside_the_datasources_own_scope(
    db_session,
) -> None:
    """Policy containment: a ViewLineageEdge stored under datasource A whose
    matched `target_table_id` happens to reference a table belonging to a
    completely different datasource (a mismatched parser match, or another
    tenant's table) must never surface as a node in datasource A's unified
    graph -- `_build_unified_graph` only ever admits edges whose endpoints
    are already among the requesting datasource's own tables, regardless of
    what the row's own `datasource_id` column says."""
    datasource_a, schema_a = await _seed_org_and_datasource(db_session, name="ds-a")
    datasource_b, schema_b = await _seed_org_and_datasource(db_session, name="ds-b")
    raw_orders = await _seed_table(db_session, datasource_a, schema_a, "raw_orders")
    foreign_table = await _seed_table(db_session, datasource_b, schema_b, "secret_table")

    db_session.add(
        ViewLineageEdge(
            organization_id=datasource_a.organization_id,
            datasource_id=datasource_a.id,
            source_table="bank.public.raw_orders",
            source_column="id",
            target_table="other.public.secret_table",
            target_column="id",
            source_table_id=raw_orders.id,
            target_table_id=foreign_table.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h2",
        )
    )
    await db_session.flush()

    graph = await build_unified_lineage_graph_payload(db_session, datasource_a, settings=None)

    node_ids = {node.id for node in graph.nodes}
    assert str(foreign_table.id) not in node_ids
    assert graph.counts_by_source["VIEW_DEFINITION"] == 0


@pytest.mark.asyncio
async def test_unified_lineage_impact_route_denies_a_caller_from_another_organization(
    db_session,
) -> None:
    """End-to-end policy check on the HTTP surface itself (complementing the
    codebase-wide INV-5 structural sweep): a caller authenticated to a
    different organization than the datasource is denied before the graph
    is ever built, so a mis-scoped request cannot leak any impact rows."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    raw_orders = await _seed_table(db_session, datasource, schema, "raw_orders")

    foreign_context = SecurityContext(
        principal_id="tester",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"Viewer"}),
    )

    with pytest.raises(HTTPException) as denied:
        await get_unified_lineage_impact(
            datasource_id=datasource.id,
            node_id=str(raw_orders.id),
            depth=5,
            node_limit=200,
            context=foreign_context,
            session=db_session,
            settings=Settings(_env_file=None),
        )

    assert denied.value.status_code == 403


# ---------------------------------------------------------------------------
# AT-19 -- transformation code rendered on the lineage edge. A VIEW_DEFINITION
# edge's evidence carries a resolvable `transformation_reference` plus
# `redaction_status`, so a caller answers "why do you say so" and "is it
# redacted" straight from the graph, without a blind extra round trip to
# discover whether one exists. FOREIGN_KEY and PROCEDURE_DEFINITION edges must
# never carry a fabricated one -- an FK is not transformation-code-backed at
# all, and a ProcedureLineageEdge carries no stable identity back to the
# specific MetadataRoutine row it was parsed from (see
# `mcp_server.py::_view_definition_transformation_detail`'s docstring).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_view_definition_edge_carries_a_resolvable_transformation_reference(
    db_session,
) -> None:
    datasource, schema = await _seed_org_and_datasource(db_session)
    raw_orders = await _seed_table(db_session, datasource, schema, "raw_orders")
    vw_orders = await _seed_table(db_session, datasource, schema, "vw_orders")
    fct_orders = await _seed_table(db_session, datasource, schema, "fct_orders")

    db_session.add(
        ViewLineageEdge(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            source_table="bank.public.raw_orders",
            source_column="id",
            target_table="bank.public.vw_orders",
            target_column="order_id",
            source_table_id=raw_orders.id,
            target_table_id=vw_orders.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h1",
        )
    )
    db_session.add(
        MetadataViewDefinition(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=vw_orders.id,
            definition_sql_redacted=(
                "CREATE VIEW vw_orders AS SELECT id AS order_id FROM raw_orders"
            ),
            definition_fingerprint="def-fp-1",
            redaction_status="PARSED",
            screening_status="CLEAN",
            availability="AVAILABLE",
            fingerprint="fp",
        )
    )
    db_session.add(
        MetadataConstraint(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=fct_orders.id,
            name="fk_fct_orders_vw_orders",
            constraint_type="FOREIGN_KEY",
            columns=["vw_orders_id"],
            referenced_table_id=vw_orders.id,
            referenced_columns=["order_id"],
            status="ACTIVE",
            fingerprint="fp",
        )
    )
    db_session.add(
        ProcedureLineageEdge(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            source_table="bank.public.raw_orders",
            source_column="id",
            target_table="bank.public.fct_orders",
            target_column="order_id",
            source_table_id=raw_orders.id,
            target_table_id=fct_orders.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h2",
        )
    )
    await db_session.flush()

    graph = await build_unified_lineage_graph_payload(db_session, datasource, settings=None)
    edges_by_source = {edge.edge_source: edge for edge in graph.edges}

    view_edge = edges_by_source["VIEW_DEFINITION"]
    assert view_edge.evidence["transformation_reference"] == {
        "tool": "get_transformation_detail",
        "entity_id": str(vw_orders.id),
        "kind": "VIEW_DEFINITION",
    }
    assert view_edge.evidence["redaction_status"] == "PARSED"
    assert view_edge.evidence["availability"] == "AVAILABLE"

    fk_edge = edges_by_source["FOREIGN_KEY"]
    assert "transformation_reference" not in fk_edge.evidence
    assert "redaction_status" not in fk_edge.evidence

    procedure_edge = edges_by_source["PROCEDURE_DEFINITION"]
    assert "transformation_reference" not in procedure_edge.evidence
    assert "redaction_status" not in procedure_edge.evidence


@pytest.mark.asyncio
async def test_view_definition_edge_omits_the_reference_when_no_definition_is_ingested_yet(
    db_session,
) -> None:
    """No fabrication: a view target table with no `MetadataViewDefinition`
    row (definition not yet discovered/ingested for that table) gets a
    VIEW_DEFINITION edge with no `transformation_reference` at all, rather
    than a reference that would 404 against `get_transformation_detail`."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    raw_orders = await _seed_table(db_session, datasource, schema, "raw_orders")
    vw_orders = await _seed_table(db_session, datasource, schema, "vw_orders")

    db_session.add(
        ViewLineageEdge(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            source_table="bank.public.raw_orders",
            source_column="id",
            target_table="bank.public.vw_orders",
            target_column="order_id",
            source_table_id=raw_orders.id,
            target_table_id=vw_orders.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h1",
        )
    )
    await db_session.flush()

    graph = await build_unified_lineage_graph_payload(db_session, datasource, settings=None)
    edges_by_source = {edge.edge_source: edge for edge in graph.edges}

    view_edge = edges_by_source["VIEW_DEFINITION"]
    assert "transformation_reference" not in view_edge.evidence
    assert "redaction_status" not in view_edge.evidence


@pytest.mark.asyncio
async def test_view_definition_transformation_reference_round_trips_to_the_real_fragment(
    db_session,
) -> None:
    """The edge's `transformation_reference.entity_id` must resolve, through
    `get_transformation_detail` (`mcp_server._transformation_detail`), to the
    *same* redacted SQL text and redaction status the edge's own evidence
    reports -- one fact, not two representations that could drift apart."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    raw_orders = await _seed_table(db_session, datasource, schema, "raw_orders")
    vw_orders = await _seed_table(db_session, datasource, schema, "vw_orders")

    definition_sql = "CREATE VIEW vw_orders AS SELECT id AS order_id FROM raw_orders"
    db_session.add(
        ViewLineageEdge(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            source_table="bank.public.raw_orders",
            source_column="id",
            target_table="bank.public.vw_orders",
            target_column="order_id",
            source_table_id=raw_orders.id,
            target_table_id=vw_orders.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            sql_hash="h1",
        )
    )
    db_session.add(
        MetadataViewDefinition(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=vw_orders.id,
            definition_sql_redacted=definition_sql,
            definition_fingerprint="def-fp-1",
            redaction_status="PARSED",
            screening_status="CLEAN",
            availability="AVAILABLE",
            fingerprint="fp",
        )
    )
    await db_session.flush()

    graph = await build_unified_lineage_graph_payload(db_session, datasource, settings=None)
    view_edge = next(edge for edge in graph.edges if edge.edge_source == "VIEW_DEFINITION")
    reference = view_edge.evidence["transformation_reference"]
    assert reference["tool"] == "get_transformation_detail"

    detail = await _transformation_detail(db_session, datasource, UUID(reference["entity_id"]))

    assert detail is not None
    assert detail["transformation_source"] == "VIEW_DEFINITION"
    assert detail["definition_sql_redacted"] == definition_sql
    assert detail["redaction_status"] == view_edge.evidence["redaction_status"]
    assert detail["availability"] == view_edge.evidence["availability"]


@pytest.mark.asyncio
async def test_view_definition_transformation_detail_withholds_quarantined_text(
    db_session,
) -> None:
    """`get_transformation_detail` honours `MetadataViewDefinition`'s stored
    `screening_status` (computed once at ingestion, per `ingest_screening.py`)
    the same way it already does for a dbt resource's live-screened
    `description` -- quarantined text never reaches the calling LLM's
    context, but the redaction/screening status themselves still do, so a
    caller can tell *that* code exists and *why* it is withheld."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    vw_orders = await _seed_table(db_session, datasource, schema, "vw_orders")
    db_session.add(
        MetadataViewDefinition(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=vw_orders.id,
            definition_sql_redacted="CREATE VIEW vw_orders AS SELECT 1",
            definition_fingerprint="def-fp-2",
            redaction_status="PARSED",
            screening_status="QUARANTINED",
            screening_reason_codes=["INJECTION_DEFENSE:MULTILINGUAL_INJECTION"],
            availability="AVAILABLE",
            fingerprint="fp",
        )
    )
    await db_session.flush()

    detail = await _transformation_detail(db_session, datasource, vw_orders.id)

    assert detail is not None
    assert detail["definition_sql_redacted"] is None
    assert detail["screening_status"] == "QUARANTINED"
    assert detail["redaction_status"] == "PARSED"
