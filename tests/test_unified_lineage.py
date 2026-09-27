from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataRoutine, MetadataViewDefinition
from aida.main import app
from aida.mcp_server import _transformation_detail
from aida.models import (
    AiDecisionRecord,
    BiArtifactImport,
    BiConnection,
    BiMetricColumnEdge,
    BiMetricNode,
    BiReportMetricEdge,
    BiReportNode,
    ConsumptionRecord,
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    DataDomain,
    DataQualityIncident,
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
    QueryExecution,
    ViewLineageEdge,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
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


def _apply_column_defaults(obj: Any) -> None:
    """Fill unset columns from their ORM scalar defaults, as a real flush would.

    This store keeps objects exactly as constructed, so a column whose value
    comes from `mapped_column(default=...)` stayed `None` here while it would
    be populated against a real database. P1-05 made that visible: it added
    `review_status` (default "ACTIVE") to the parsed lineage edge tables and a
    graph filter on `review_status == "ACTIVE"`, so every seeded edge silently
    dropped out of the graph and tests asserted on an empty result.
    """
    mapper = sa_inspect(type(obj))
    for column in mapper.columns:
        attr = mapper.get_property_by_column(column).key
        if getattr(obj, attr, None) is not None:
            continue
        default = column.default
        if default is not None and default.is_scalar:
            setattr(obj, attr, default.arg)


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
        _apply_column_defaults(obj)
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
async def test_unified_lineage_impact_surfaces_open_quality_incident(db_session) -> None:
    """DQ-3 "Impact surfacing": a table with an OPEN incident reports
    quality_state="INCIDENT_OPEN" on the impact surface -- not just on its
    Catalog row -- and a table with no incident and no observation history
    reports "UNKNOWN" (never a bare, un-stated PASSING it has no evidence
    for). A non-TABLE node (no `MetadataTable` match) is never looked up
    against `DataQualityIncident` at all, and reports "NOT_APPLICABLE"."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    raw_orders = await _seed_table(db_session, datasource, schema, "raw_orders")
    fct_orders = await _seed_table(db_session, datasource, schema, "fct_orders")

    db_session.add(
        MetadataConstraint(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=fct_orders.id,
            name="fk_fct_orders_raw_orders",
            constraint_type="FOREIGN_KEY",
            columns=["raw_orders_id"],
            referenced_table_id=raw_orders.id,
            referenced_columns=["id"],
            status="ACTIVE",
            fingerprint="fp",
        )
    )
    now = datetime.now(UTC)
    db_session.add(
        DataQualityIncident(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            table_id=fct_orders.id,
            fingerprint=uuid4().hex,
            anomaly_type="VOLUME_CHANGE",
            severity="WARNING",
            status="OPEN",
            summary="Volume dropped below baseline.",
            first_observed_at=now,
            last_observed_at=now,
        )
    )
    await db_session.flush()

    result = await build_unified_lineage_impact_payload(
        db_session, datasource, str(raw_orders.id), depth=5, node_limit=50, settings=None
    )

    downstream_by_id = {row.node_id: row for row in result.downstream}
    assert downstream_by_id[str(fct_orders.id)].quality_state == "INCIDENT_OPEN"

    # A table with no incident and no observation history is UNKNOWN, not a
    # bare, unstated "PASSING" it has no evidence for (ADR-0016 fail-closed).
    upstream_result = await build_unified_lineage_impact_payload(
        db_session, datasource, str(fct_orders.id), depth=5, node_limit=50, settings=None
    )
    upstream_by_id = {row.node_id: row for row in upstream_result.upstream}
    assert upstream_by_id[str(raw_orders.id)].quality_state == "UNKNOWN"


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
async def test_a_routine_edge_folds_into_the_graph_once_a_person_approves_it(
    db_session,
) -> None:
    """The routine-aware procedure table joined ADR-0026's review on
    2026-09-11. Its ACTIVE rows fold into PROCEDURE_DEFINITION edges that name
    the routine behind them; a PROPOSED row -- every edge the lineage agent
    writes, until a person approves it -- stays out of the graph."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    raw_orders = await _seed_table(db_session, datasource, schema, "raw_orders")
    fct_orders = await _seed_table(db_session, datasource, schema, "fct_orders")
    agg_orders = await _seed_table(db_session, datasource, schema, "agg_orders")
    routine = MetadataRoutine(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="load_orders",
        routine_type="PROCEDURE",
        body_sql_redacted="-- redacted body",
        fingerprint="fp",
    )
    db_session.add(routine)
    await db_session.flush()

    def routine_edge(target: MetadataTable, review_status: str) -> DeepProcedureLineageEdge:
        return DeepProcedureLineageEdge(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            routine_id=routine.id,
            statement_ordinal=0,
            source_table="public.raw_orders",
            source_column="id",
            target_table=f"public.{target.name}",
            target_column="order_id",
            source_table_id=raw_orders.id,
            target_table_id=target.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            is_write=True,
            sql_hash="h3",
            review_status=review_status,
            created_by="agent:lineage",
        )

    db_session.add_all(
        [routine_edge(fct_orders, "ACTIVE"), routine_edge(agg_orders, "PROPOSED")]
    )
    await db_session.flush()

    graph = await build_unified_lineage_graph_payload(db_session, datasource, settings=None)

    [procedure_edge] = [
        edge for edge in graph.edges if edge.edge_source == "PROCEDURE_DEFINITION"
    ]
    assert procedure_edge.evidence["routine_ids"] == [str(routine.id)]
    assert graph.counts_by_source["PROCEDURE_DEFINITION"] == 1
    # AT-19, for procedures: one routine establishes the edge, so it carries a
    # reference that resolves to that routine's own body.
    assert procedure_edge.evidence["transformation_reference"] == {
        "tool": "get_transformation_detail",
        "entity_id": str(routine.id),
        "kind": "ROUTINE_BODY",
    }

    detail = await _transformation_detail(db_session, datasource, routine.id)

    assert detail is not None
    assert detail["transformation_source"] == "ROUTINE_BODY"
    assert detail["body_sql_redacted"] == "-- redacted body"
    assert detail["redaction_status"] == procedure_edge.evidence["redaction_status"]
    assert detail["availability"] == procedure_edge.evidence["availability"]


async def _seed_routine(
    db_session: AsyncSession,
    datasource: DataSource,
    schema: MetadataSchema,
    name: str,
    **overrides: Any,
) -> MetadataRoutine:
    values: dict[str, Any] = {
        "organization_id": datasource.organization_id,
        "datasource_id": datasource.id,
        "schema_id": schema.id,
        "name": name,
        "routine_type": "PROCEDURE",
        "body_sql_redacted": f"-- body of {name}",
        "fingerprint": "fp",
    }
    values.update(overrides)
    routine = MetadataRoutine(**values)
    db_session.add(routine)
    await db_session.flush()
    return routine


def _approved_routine_edge(
    datasource: DataSource,
    routine: MetadataRoutine,
    source: MetadataTable,
    target: MetadataTable,
) -> DeepProcedureLineageEdge:
    return DeepProcedureLineageEdge(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        routine_id=routine.id,
        statement_ordinal=0,
        source_table=f"public.{source.name}",
        source_column="id",
        target_table=f"public.{target.name}",
        target_column="order_id",
        source_table_id=source.id,
        target_table_id=target.id,
        transformation_type="DIRECT",
        confidence="FULL",
        dialect="postgres",
        is_write=True,
        sql_hash="h4",
        review_status="ACTIVE",
        created_by="reviewer-1",
    )


@pytest.mark.asyncio
async def test_a_procedure_edge_two_routines_establish_names_both_and_references_neither(
    db_session,
) -> None:
    """No fabrication: with two routines behind one table pair, no single body
    establishes the edge, so it names both and resolves to neither."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    raw_orders = await _seed_table(db_session, datasource, schema, "raw_orders")
    fct_orders = await _seed_table(db_session, datasource, schema, "fct_orders")
    first = await _seed_routine(db_session, datasource, schema, "load_orders")
    second = await _seed_routine(db_session, datasource, schema, "reload_orders")
    db_session.add_all(
        [
            _approved_routine_edge(datasource, first, raw_orders, fct_orders),
            _approved_routine_edge(datasource, second, raw_orders, fct_orders),
        ]
    )
    await db_session.flush()

    graph = await build_unified_lineage_graph_payload(db_session, datasource, settings=None)

    [procedure_edge] = [
        edge for edge in graph.edges if edge.edge_source == "PROCEDURE_DEFINITION"
    ]
    assert procedure_edge.evidence["routine_ids"] == sorted([str(first.id), str(second.id)])
    assert "transformation_reference" not in procedure_edge.evidence


@pytest.mark.asyncio
async def test_a_routine_transformation_detail_withholds_an_unscreened_body_in_its_own_datasource(
    db_session,
) -> None:
    """The body is released under the gate a person's parse applies to it.
    A quarantined body is withheld while its statuses still say why, and
    another datasource cannot read the routine at all."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    other_datasource, _other_schema = await _seed_org_and_datasource(db_session)
    routine = await _seed_routine(
        db_session,
        datasource,
        schema,
        "load_orders",
        screening_status="QUARANTINED",
        screening_reason_codes=["INJECTION_DEFENSE:MULTILINGUAL_INJECTION"],
    )

    detail = await _transformation_detail(db_session, datasource, routine.id)
    elsewhere = await _transformation_detail(db_session, other_datasource, routine.id)

    assert detail is not None
    assert detail["transformation_source"] == "ROUTINE_BODY"
    assert detail["body_sql_redacted"] is None
    assert detail["screening_status"] == "QUARANTINED"
    assert elsewhere is None


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


# ---------------------------------------------------------------------------
# AT-10 investigation: gap-pinning tests for the still-orphaned producers
#
# AT-10 ("One canonical lineage graph -- join the orphaned edge producers")
# re-checked all six producers `Docs/review-2026-08/atlan-context/03-lineage.md`
# named as invisible to the unified graph. View/procedure edges (LN-2) were
# already folded in by LN-7 (2026-08-31), before this row was claimed -- see
# `test_unified_lineage_impact_chains_view_definition_into_foreign_key` above.
#
# BI report/metric edges (LN-4/LN-11) were the first of the remaining four to
# be unblocked: R11-B13 extended both Literals and joined them, so the test
# that pinned that gap has been replaced by the positive tests in the
# R11-B13 section at the end of this file. The header below is kept for the
# three that are still out -- AI decision edges (LN-3/AU-5), context-product
# consumption edges (CX-4), and gateway executed-query column lineage
# (`query_gateway.extract_column_lineage`). The first two were blocked when
# this was written; consumption has since been re-examined by R11-B13 and is
# now excluded on its merits rather than on the Literal constraint -- the
# test keeps its name (the claim it pins is unchanged) and its docstring
# carries the new reasoning.
#
#   * `UnifiedLineageEdgeSource` and `UnifiedLineageNodeKind` (both in
#     `schemas.py`) are closed `Literal`s -- pydantic rejects any value
#     outside the declared set at construction time, proven directly:
#     `UnifiedLineageEdgeRead(edge_source="GATEWAY_QUERY_LINEAGE", ...)`
#     raises a `ValidationError`, it does not silently pass through.
#   * Every one of these four producers needs a *new* member on one or both
#     of those Literals to be represented honestly. Reusing an existing tag
#     (e.g. tagging an AI decision as `SUGGESTED_RELATIONSHIP`) would
#     misrepresent provenance -- the opposite of what this row exists to fix
#     -- so it was declined rather than done.
#   * BI, AI-decision, and consumption edges additionally need a brand-new
#     `UnifiedLineageNodeKind` on their non-table side: `BiReportNode`/
#     `BiMetricNode` are not `MetadataTable` rows (already tracked
#     separately as LN-11 for exactly this reason -- see LN-7's row);
#     `AiDecisionRecord.source_node` is always a synthetic AI-component tag
#     ("governed_retriever", "governed_planner", ...), never a catalog
#     entity, and `AiDecisionRecord` itself carries no `datasource_id`;
#     `ContextProduct`/`ContextProductVersion` are `project_id`-scoped, not
#     `datasource_id`- or `MetadataTable`-scoped at all, and
#     `ContextProductConsumptionEdge`'s other side is a bare `principal_id`
#     string (a human or agent identity), not an asset.
#   * Gateway executed-query lineage (`QueryExecution.column_lineage`) is
#     table-resolvable on the source side, but its "target" is an ephemeral
#     query result, not a catalog table -- and the platform's own decision
#     record (`Docs/60-delivery/03-tracker.md` AT-12) already routes
#     query-log-derived lineage through the *candidate* review queue
#     (`RelationshipCandidate`, AT-12, still TODO) rather than asserting it
#     directly, which is a different, not-yet-built mechanism this row does
#     not build.
#
# This pass's hard rule forbids editing `schemas.py`, so all four stay out
# of scope, honestly, rather than joined by reusing an inaccurate existing
# tag. These tests pin the current, real gap with real persisted rows (not
# just a design assertion) so a future session that *is* allowed to extend
# the Literals has a concrete regression test to flip green, and so a silent
# accidental "fix" that only half-works cannot go unnoticed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at10_ai_decision_edges_do_not_appear_in_the_unified_graph_or_impact(
    db_session,
) -> None:
    """A real `AiDecisionRecord` (`RETRIEVAL_SELECTED`, the same shape
    `agent_orchestrator.py` writes via AU-5) naming a real table as its
    `target_node` still contributes nothing to the unified graph or to that
    table's impact traversal -- `source_node` is always a synthetic
    AI-component tag ("governed_retriever"), never a catalog entity, so
    there is no second real node for an edge to connect it to, and
    `AiDecisionRecord` itself carries no `datasource_id` to scope a query by.
    Blocked on a new `UnifiedLineageNodeKind` for the AI-component side, out
    of this row's `schemas.py`-free scope."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    orders = await _seed_table(db_session, datasource, schema, "orders")
    run_id = uuid4()

    db_session.add(
        AiDecisionRecord(
            id=uuid4(),
            organization_id=datasource.organization_id,
            run_id=run_id,
            decision_type="RETRIEVAL_SELECTED",
            source_node="governed_retriever",
            target_node=f"table:{orders.id}",
            reason="fusion_rank_1",
            evidence={"score": 0.92},
        )
    )
    await db_session.flush()

    graph_result = await build_unified_lineage_graph_payload(
        db_session, datasource, node_limit=50, edge_limit=50, settings=None
    )
    assert graph_result.counts_by_source.get("AI_DECISION", 0) == 0
    assert not any("AI_DECISION" in edge.edge_source for edge in graph_result.edges)
    assert not any(node.id == "governed_retriever" for node in graph_result.nodes)

    impact_result = await build_unified_lineage_impact_payload(
        db_session, datasource, str(orders.id), depth=5, node_limit=50, settings=None
    )
    assert impact_result.upstream == []
    assert impact_result.downstream == []


@pytest.mark.asyncio
async def test_at10_consumption_edges_do_not_appear_in_the_unified_graph(db_session) -> None:
    """A real `ContextProductConsumptionEdge` (the immutable per-read record
    CX-4 emits) contributes nothing to the unified graph -- and as of R11-B13
    that is a decision, not a blocker. AT-10 recorded it as blocked on the
    closed `UnifiedLineageNodeKind` Literal; R11-B13 was authorized to extend
    that Literal, extended it for BI, and still declined consumption, because
    a consumption row records *who read* an asset rather than what derives
    from what. `collect_bi_lineage`'s docstring carries the four reasons; the
    two visible in this fixture are that `principal_id` is a consumer identity
    rather than an asset, and that `ContextProductVersion` is `project_id`-
    scoped with no `datasource_id` or `MetadataTable` link at all even though
    its `table_ids` JSON column names real tables -- so there is no join that
    could tenant-frame it the way every other provider here is framed.

    This is pinned rather than merely documented so the exclusion stays a
    decision: folding consumption in later would have to flip this test
    deliberately."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    orders = await _seed_table(db_session, datasource, schema, "orders")

    product = ContextProduct(
        id=uuid4(),
        organization_id=datasource.organization_id,
        project_id=datasource.project_id,
        product_key="revenue-context",
        created_by="test-harness",
    )
    db_session.add(product)
    await db_session.flush()
    version = ContextProductVersion(
        id=uuid4(),
        organization_id=datasource.organization_id,
        product_id=product.id,
        version=1,
        status="PUBLISHED",
        name="Revenue context",
        description="Revenue context product",
        purpose="Answer revenue questions",
        owner_principal="steward@example.com",
        table_ids=[str(orders.id)],
        allowed_consumer_roles=["Analyst"],
        fingerprint="fp-cp-1",
        created_by="test-harness",
    )
    db_session.add(version)
    await db_session.flush()
    db_session.add(
        ContextProductConsumptionEdge(
            id=uuid4(),
            organization_id=datasource.organization_id,
            context_product_version_id=version.id,
            principal_id="agent-run-123",
            principal_type="AGENT",
            channel="MCP",
            correlation_id="corr-1",
            product_fingerprint="fp-cp-1",
            policy_decision="ALLOWED",
        )
    )
    # The other consumption table, and the harder one: `ConsumptionRecord`
    # names the table directly -- but as free text in `resource_id`, with no
    # FK, so resolving it would be a name match across datasources of exactly
    # the kind `_register_definition_edges` refuses to make.
    db_session.add(
        ConsumptionRecord(
            id=uuid4(),
            organization_id=datasource.organization_id,
            consumer_id="analyst@example.com",
            consumer_type="USER",
            resource_type="metadata_table",
            resource_id=str(orders.id),
            channel="MCP",
            correlation_id="corr-2",
            policy_decision="ALLOWED",
        )
    )
    await db_session.flush()

    result = await build_unified_lineage_graph_payload(
        db_session, datasource, node_limit=50, edge_limit=50, settings=None
    )

    assert result.counts_by_source.get("CONSUMPTION", 0) == 0
    assert not any("CONSUMPTION" in edge.edge_source for edge in result.edges)
    assert not any(node.id == "agent-run-123" for node in result.nodes)
    assert not any(node.id == "analyst@example.com" for node in result.nodes)
    assert not any(node.id == str(version.id) for node in result.nodes)
    # `orders` is a real, resolved node -- the exclusion is of the consumption
    # edges specifically, not an artefact of an empty fixture.
    assert str(orders.id) in {node.id for node in result.nodes}
    assert result.edges == []


@pytest.mark.asyncio
async def test_at10_gateway_query_lineage_does_not_appear_in_the_unified_graph(
    db_session,
) -> None:
    """A real `QueryExecution` row carrying real `extract_column_lineage`
    output (`orders.amount -> net_amount`) over two real catalog tables
    still contributes nothing to the unified graph -- `column_lineage` is a
    JSON column with no edge table and no `UnifiedLineageEdgeSource` member
    of its own, and the platform's own decision record (AT-12, still TODO)
    already routes query-log-derived lineage through the candidate review
    queue rather than asserting it directly here."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    orders = await _seed_table(db_session, datasource, schema, "orders")
    revenue_agg = await _seed_table(db_session, datasource, schema, "revenue_agg")

    db_session.add(
        QueryExecution(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            principal_id="analyst-1",
            status="COMPLETED",
            dialect="postgres",
            sql_hash="sql-hash-1",
            referenced_tables=["bank.public.orders"],
            referenced_columns=["orders.amount"],
            column_lineage=[
                {
                    "output_column": "net_amount",
                    "lineage_type": "DERIVED",
                    "source_columns": [{"table": "orders", "column": "amount"}],
                    "transformations": ["SUM"],
                }
            ],
        )
    )
    await db_session.flush()

    result = await build_unified_lineage_graph_payload(
        db_session, datasource, node_limit=50, edge_limit=50, settings=None
    )

    assert result.counts_by_source.get("GATEWAY_QUERY_LINEAGE", 0) == 0
    assert not any("GATEWAY" in edge.edge_source for edge in result.edges)
    # Both tables are real, resolved nodes in the graph on their own merits
    # (the catalog scan always includes every ACTIVE table) -- proving the
    # absence above is a genuine gap in edges, not an artifact of the tables
    # themselves being invisible.
    node_ids = {node.id for node in result.nodes}
    assert str(orders.id) in node_ids
    assert str(revenue_agg.id) in node_ids


# ---------------------------------------------------------------------------
# R11-B13: the graph runs past the warehouse edge into BI, and stops there.
#
# "Which reports does this column feed?" is the question a bank asks before it
# changes a column. `bi_lineage.py` has imported and stored the report ->
# metric -> column chain since LN-4, but nothing joined it to the unified
# graph, so the graph could not answer. `collect_bi_lineage` joins it, folded
# to the table grain every other provider in that module already uses.
#
# These tests are written to fail if any of the three properties the row is
# accepted on regresses: reports are reachable *with provenance*, the
# traversal stays *bounded*, and it never crosses an organization. The two
# bound tests and the two tenancy tests each assert on a concretely missing
# node or edge, not merely on a truncation flag, so a bound that silently
# stopped truncating could not pass them.
# ---------------------------------------------------------------------------


async def _seed_bi_connection(
    session: AsyncSession,
    datasource: DataSource,
    *,
    connection_key: str = "site-1",
    status: str = "ACTIVE",
) -> BiConnection:
    connection = BiConnection(
        id=uuid4(),
        organization_id=datasource.organization_id,
        project_id=datasource.project_id,
        datasource_id=datasource.id,
        bi_tool="TABLEAU",
        connection_key=connection_key,
        display_name="Tableau Site",
        status=status,
        created_by="test-harness",
    )
    session.add(connection)
    await session.flush()
    return connection


async def _seed_bi_import(
    session: AsyncSession,
    connection: BiConnection,
    *,
    fingerprint: str = "fp-1",
    status: str = "IMPORTED",
    created_at: datetime | None = None,
) -> BiArtifactImport:
    artifact_import = BiArtifactImport(
        id=uuid4(),
        organization_id=connection.organization_id,
        connection_id=connection.id,
        artifact_fingerprint=fingerprint,
        bi_tool=connection.bi_tool,
        status=status,
        report_count=0,
        metric_count=0,
        report_metric_edge_count=0,
        metric_column_edge_count=0,
        matched_column_count=0,
        unmatched_column_count=0,
        imported_by="test-harness",
        **({"created_at": created_at} if created_at is not None else {}),
    )
    session.add(artifact_import)
    await session.flush()
    return artifact_import


async def _seed_bi_report(
    session: AsyncSession,
    artifact_import: BiArtifactImport,
    *,
    name: str,
    report_type: str,
    parent: BiReportNode | None = None,
    project_name: str | None = "Finance",
) -> BiReportNode:
    report = BiReportNode(
        id=uuid4(),
        organization_id=artifact_import.organization_id,
        artifact_import_id=artifact_import.id,
        parent_report_id=None if parent is None else parent.id,
        external_id=f"ext-{uuid4().hex[:10]}",
        name=name,
        report_type=report_type,
        project_name=project_name,
    )
    session.add(report)
    await session.flush()
    return report


async def _seed_bi_metric(
    session: AsyncSession,
    artifact_import: BiArtifactImport,
    *,
    name: str,
    reports: list[BiReportNode],
    matched_table_id: UUID | None,
    source_table_name: str,
    source_column_name: str,
) -> BiMetricNode:
    """One field, used by `reports`, deriving from one source column."""
    metric = BiMetricNode(
        id=uuid4(),
        organization_id=artifact_import.organization_id,
        artifact_import_id=artifact_import.id,
        external_id=f"fld-{uuid4().hex[:10]}",
        name=name,
        field_type="CalculatedField",
    )
    session.add(metric)
    await session.flush()
    for report in reports:
        session.add(
            BiReportMetricEdge(
                id=uuid4(),
                organization_id=artifact_import.organization_id,
                artifact_import_id=artifact_import.id,
                report_id=report.id,
                metric_id=metric.id,
            )
        )
    session.add(
        BiMetricColumnEdge(
            id=uuid4(),
            organization_id=artifact_import.organization_id,
            artifact_import_id=artifact_import.id,
            metric_id=metric.id,
            source_table_name=source_table_name,
            source_column_name=source_column_name,
            matched_table_id=matched_table_id,
        )
    )
    await session.flush()
    return metric


def test_r11b13_the_builders_edge_sources_match_the_published_contract() -> None:
    """`EDGE_SOURCES` seeds `counts_by_source`, and `UnifiedLineageEdgeSource`
    is what pydantic validates every edge against. A member added to one and
    not the other is either an edge the API rejects at serialization time or a
    count key no caller can receive, so the two are pinned equal."""
    from typing import get_args

    from aida.schemas import UnifiedLineageEdgeSource, UnifiedLineageNodeKind
    from aida.unified_lineage_builder import (
        BI_NODE_KIND_BY_REPORT_TYPE,
        DBT_NODE_KIND_BY_RESOURCE_TYPE,
        EDGE_SOURCES,
    )

    assert set(EDGE_SOURCES) == set(get_args(UnifiedLineageEdgeSource))
    assert "BI_LINEAGE" in EDGE_SOURCES
    declared_kinds = set(get_args(UnifiedLineageNodeKind))
    assert set(BI_NODE_KIND_BY_REPORT_TYPE.values()) <= declared_kinds
    assert set(DBT_NODE_KIND_BY_RESOURCE_TYPE.values()) <= declared_kinds


def test_r11b13_every_report_type_the_parsers_emit_has_a_node_kind() -> None:
    """The mapping is only useful if it covers what `bi_lineage.py` actually
    produces; a parser emitting a `report_type` with no node kind would have
    its reports silently skipped."""
    from aida.bi_lineage import _TABLEAU_REPORT_TYPES
    from aida.unified_lineage_builder import BI_NODE_KIND_BY_REPORT_TYPE

    # Tableau: the workbook itself plus each field container's own type.
    assert {"WORKBOOK", *_TABLEAU_REPORT_TYPES.values()} <= set(BI_NODE_KIND_BY_REPORT_TYPE)
    # Power BI: `_parse_power_bi` emits REPORT for a report and PAGE per page.
    assert {"REPORT", "PAGE"} <= set(BI_NODE_KIND_BY_REPORT_TYPE)


@pytest.mark.asyncio
async def test_r11b13_a_bi_report_reaches_the_graph_with_its_provenance(db_session) -> None:
    """The row's headline: a Tableau sheet that reads `orders.amount` becomes a
    real graph node with a real edge to `orders`, carrying enough provenance to
    say which tool, which connection, which import and which metric produced
    it -- and its containing workbook comes with it."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    orders = await _seed_table(db_session, datasource, schema, "orders")

    connection = await _seed_bi_connection(db_session, datasource)
    artifact_import = await _seed_bi_import(db_session, connection)
    workbook = await _seed_bi_report(
        db_session, artifact_import, name="Revenue", report_type="WORKBOOK"
    )
    sheet = await _seed_bi_report(
        db_session, artifact_import, name="Net Revenue", report_type="SHEET", parent=workbook
    )
    await _seed_bi_metric(
        db_session,
        artifact_import,
        name="Net Revenue",
        reports=[sheet],
        matched_table_id=orders.id,
        source_table_name="orders",
        source_column_name="amount",
    )

    result = await build_unified_lineage_graph_payload(
        db_session, datasource, node_limit=50, edge_limit=50, settings=None
    )

    nodes_by_id = {node.id: node for node in result.nodes}
    sheet_node = nodes_by_id[f"bi:{sheet.id}"]
    assert sheet_node.node_kind == "BI_SHEET"
    assert sheet_node.label == "Net Revenue"
    assert sheet_node.qualified_name == "TABLEAU.Finance.Net Revenue"
    # A workbook is not a catalog table, so it resolves to nothing -- the same
    # posture an unmatched dbt resource or OpenLineage dataset gets.
    assert sheet_node.resolved is False
    assert sheet_node.matched_table_id is None
    assert nodes_by_id[f"bi:{workbook.id}"].node_kind == "BI_WORKBOOK"

    edges_by_id = {edge.id: edge for edge in result.edges}
    reads = edges_by_id[f"bi:{sheet.id}:{orders.id}"]
    assert reads.edge_source == "BI_LINEAGE"
    # source-depends-on-target: the report depends on the table, so a
    # REFERENCED_BY walk from the table finds the report.
    assert reads.source_node_id == f"bi:{sheet.id}"
    assert reads.target_node_id == str(orders.id)
    assert reads.confidence == 1.0
    # The metric did not become a node; it rides on the edge beside the
    # catalog column it derives from.
    assert reads.source_columns == ["Net Revenue"]
    assert reads.target_columns == ["amount"]
    assert reads.evidence["source"] == "BI_LINEAGE"
    assert reads.evidence["relation"] == "REPORT_READS_TABLE"
    assert reads.evidence["bi_tool"] == "TABLEAU"
    assert reads.evidence["report_type"] == "SHEET"
    assert reads.evidence["bi_connection_id"] == str(connection.id)
    assert reads.evidence["bi_artifact_import_id"] == str(artifact_import.id)
    assert reads.evidence["bi_report_id"] == str(sheet.id)
    assert reads.evidence["metric_count"] == 1

    contains = edges_by_id[f"bi:{workbook.id}:{sheet.id}"]
    assert contains.edge_source == "BI_LINEAGE"
    assert contains.evidence["relation"] == "REPORT_CONTAINS_REPORT"
    assert contains.source_node_id == f"bi:{workbook.id}"
    assert contains.target_node_id == f"bi:{sheet.id}"

    assert result.counts_by_source["BI_LINEAGE"] == 2
    assert result.truncation_reasons == []


@pytest.mark.asyncio
async def test_r11b13_impact_from_a_column_reaches_the_reports_it_feeds(db_session) -> None:
    """The acceptance criterion stated as the traversal that answers it: a
    downstream impact walk from `orders` reaches the sheet at depth 1 and the
    workbook containing it at depth 2, both attributing BI_LINEAGE."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    orders = await _seed_table(db_session, datasource, schema, "orders")

    connection = await _seed_bi_connection(db_session, datasource)
    artifact_import = await _seed_bi_import(db_session, connection)
    workbook = await _seed_bi_report(
        db_session, artifact_import, name="Revenue", report_type="WORKBOOK"
    )
    sheet = await _seed_bi_report(
        db_session, artifact_import, name="Net Revenue", report_type="SHEET", parent=workbook
    )
    await _seed_bi_metric(
        db_session,
        artifact_import,
        name="Net Revenue",
        reports=[sheet],
        matched_table_id=orders.id,
        source_table_name="orders",
        source_column_name="amount",
    )

    impact = await build_unified_lineage_impact_payload(
        db_session, datasource, str(orders.id), depth=5, node_limit=50, settings=None
    )

    downstream = {node.node_id: node for node in impact.downstream}
    assert downstream[f"bi:{sheet.id}"].depth == 1
    assert downstream[f"bi:{sheet.id}"].node_kind == "BI_SHEET"
    assert "BI_LINEAGE" in downstream[f"bi:{sheet.id}"].contributing_edge_sources
    assert downstream[f"bi:{workbook.id}"].depth == 2
    assert downstream[f"bi:{workbook.id}"].node_kind == "BI_WORKBOOK"
    # A report is not upstream of the table it reads.
    assert impact.upstream == []


@pytest.mark.asyncio
async def test_r11b13_a_bi_edge_matched_to_another_orgs_table_is_unreachable(db_session) -> None:
    """Tenancy, the adversarial case. `BiMetricColumnEdge.matched_table_id` is a
    bare FK to `metadata_table` with nothing tying it to the connection's own
    datasource, so a row in *this* organization naming *another* one's table is
    representable in the database. It must not be traversable: neither the
    foreign table nor the report that names it may enter this graph."""
    datasource_a, schema_a = await _seed_org_and_datasource(db_session, name="ds-a")
    datasource_b, schema_b = await _seed_org_and_datasource(db_session, name="ds-b")
    await _seed_table(db_session, datasource_a, schema_a, "orders")
    secret_ledger = await _seed_table(db_session, datasource_b, schema_b, "secret_ledger")
    assert datasource_a.organization_id != datasource_b.organization_id

    connection = await _seed_bi_connection(db_session, datasource_a)
    artifact_import = await _seed_bi_import(db_session, connection)
    leaky_sheet = await _seed_bi_report(
        db_session, artifact_import, name="Leaky Sheet", report_type="SHEET"
    )
    await _seed_bi_metric(
        db_session,
        artifact_import,
        name="Foreign Balance",
        reports=[leaky_sheet],
        matched_table_id=secret_ledger.id,
        source_table_name="secret_ledger",
        source_column_name="balance",
    )

    result = await build_unified_lineage_graph_payload(
        db_session, datasource_a, node_limit=50, edge_limit=50, settings=None
    )

    node_ids = {node.id for node in result.nodes}
    assert str(secret_ledger.id) not in node_ids
    assert f"bi:{leaky_sheet.id}" not in node_ids
    assert not any(node.id.startswith("bi:") for node in result.nodes)
    assert result.counts_by_source["BI_LINEAGE"] == 0
    assert not any(edge.edge_source == "BI_LINEAGE" for edge in result.edges)
    assert not any("secret_ledger" in node.qualified_name for node in result.nodes)


@pytest.mark.asyncio
async def test_r11b13_another_orgs_bi_connection_never_enters_this_graph(db_session) -> None:
    """Tenancy, the ordinary case, with a positive control so the assertion
    cannot pass vacuously: the very same BI fixture *is* reachable from the
    datasource that owns it, and is absent from the other organization's."""
    datasource_a, _schema_a = await _seed_org_and_datasource(db_session, name="ds-a")
    datasource_b, schema_b = await _seed_org_and_datasource(db_session, name="ds-b")
    ledger = await _seed_table(db_session, datasource_b, schema_b, "ledger")

    connection = await _seed_bi_connection(db_session, datasource_b)
    artifact_import = await _seed_bi_import(db_session, connection)
    sheet = await _seed_bi_report(
        db_session, artifact_import, name="B Sheet", report_type="SHEET"
    )
    await _seed_bi_metric(
        db_session,
        artifact_import,
        name="Balance",
        reports=[sheet],
        matched_table_id=ledger.id,
        source_table_name="ledger",
        source_column_name="balance",
    )

    owner_graph = await build_unified_lineage_graph_payload(
        db_session, datasource_b, node_limit=50, edge_limit=50, settings=None
    )
    assert f"bi:{sheet.id}" in {node.id for node in owner_graph.nodes}
    assert owner_graph.counts_by_source["BI_LINEAGE"] == 1

    foreign_graph = await build_unified_lineage_graph_payload(
        db_session, datasource_a, node_limit=50, edge_limit=50, settings=None
    )
    assert not any(node.id.startswith("bi:") for node in foreign_graph.nodes)
    assert foreign_graph.counts_by_source["BI_LINEAGE"] == 0


@pytest.mark.asyncio
async def test_r11b13_the_node_bound_actually_drops_a_bi_report(db_session) -> None:
    """The bound truncates rather than merely reporting: with one table and a
    node budget of three, two of three sheets are admitted, the third is named
    by neither a node nor a dangling edge, and which two survive is decided by
    the same deterministic ordering `collect_tables` uses."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    orders = await _seed_table(db_session, datasource, schema, "orders")

    connection = await _seed_bi_connection(db_session, datasource)
    artifact_import = await _seed_bi_import(db_session, connection)
    sheets = []
    for suffix in ("a", "b", "c"):
        sheet = await _seed_bi_report(
            db_session, artifact_import, name=f"sheet_{suffix}", report_type="SHEET"
        )
        await _seed_bi_metric(
            db_session,
            artifact_import,
            name=f"metric_{suffix}",
            reports=[sheet],
            matched_table_id=orders.id,
            source_table_name="orders",
            source_column_name="amount",
        )
        sheets.append(sheet)

    result = await build_unified_lineage_graph_payload(
        db_session, datasource, node_limit=3, edge_limit=50, settings=None
    )

    node_ids = {node.id for node in result.nodes}
    assert len(result.nodes) == 3  # orders + two of the three sheets
    assert node_ids == {str(orders.id), f"bi:{sheets[0].id}", f"bi:{sheets[1].id}"}
    assert f"bi:{sheets[2].id}" not in node_ids
    assert "NODE_LIMIT" in result.truncation_reasons
    assert result.truncated is True
    # The dropped node takes its edge with it rather than leaving a dangler.
    assert result.counts_by_source["BI_LINEAGE"] == 2
    assert f"bi:{sheets[2].id}:{orders.id}" not in {edge.id for edge in result.edges}


@pytest.mark.asyncio
async def test_r11b13_the_edge_bound_actually_drops_a_bi_edge(db_session) -> None:
    """The edge budget truncates the same way: three sheets sharing one metric
    want three edges, an `edge_limit` of two admits two, and the third edge is
    genuinely absent from the response."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    orders = await _seed_table(db_session, datasource, schema, "orders")

    connection = await _seed_bi_connection(db_session, datasource)
    artifact_import = await _seed_bi_import(db_session, connection)
    sheets = [
        await _seed_bi_report(
            db_session, artifact_import, name=f"sheet_{suffix}", report_type="SHEET"
        )
        for suffix in ("a", "b", "c")
    ]
    await _seed_bi_metric(
        db_session,
        artifact_import,
        name="Shared Metric",
        reports=sheets,
        matched_table_id=orders.id,
        source_table_name="orders",
        source_column_name="amount",
    )

    result = await build_unified_lineage_graph_payload(
        db_session, datasource, node_limit=50, edge_limit=2, settings=None
    )

    edge_ids = {edge.id for edge in result.edges}
    assert result.returned_edge_count == 2
    assert edge_ids == {
        f"bi:{sheets[0].id}:{orders.id}",
        f"bi:{sheets[1].id}:{orders.id}",
    }
    assert f"bi:{sheets[2].id}:{orders.id}" not in edge_ids
    assert result.counts_by_source["BI_LINEAGE"] == 2
    assert "EDGE_LIMIT" in result.truncation_reasons
    assert result.truncated is True


@pytest.mark.asyncio
async def test_r11b13_only_the_latest_import_of_a_connection_is_folded_in(db_session) -> None:
    """A BI site is re-scanned on a schedule. Folding every snapshot in would
    multiply each report by its import count, so only the newest IMPORTED
    artifact per connection contributes -- the rule `collect_dbt_dependencies`
    already applies to dbt."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    orders = await _seed_table(db_session, datasource, schema, "orders")

    connection = await _seed_bi_connection(db_session, datasource)
    older = await _seed_bi_import(
        db_session,
        connection,
        fingerprint="fp-old",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = await _seed_bi_import(
        db_session,
        connection,
        fingerprint="fp-new",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    stale_sheet = await _seed_bi_report(
        db_session, older, name="Stale Sheet", report_type="SHEET"
    )
    await _seed_bi_metric(
        db_session,
        older,
        name="Stale Metric",
        reports=[stale_sheet],
        matched_table_id=orders.id,
        source_table_name="orders",
        source_column_name="amount",
    )
    current_sheet = await _seed_bi_report(
        db_session, newer, name="Current Sheet", report_type="SHEET"
    )
    await _seed_bi_metric(
        db_session,
        newer,
        name="Current Metric",
        reports=[current_sheet],
        matched_table_id=orders.id,
        source_table_name="orders",
        source_column_name="amount",
    )

    result = await build_unified_lineage_graph_payload(
        db_session, datasource, node_limit=50, edge_limit=50, settings=None
    )

    node_ids = {node.id for node in result.nodes}
    assert f"bi:{current_sheet.id}" in node_ids
    assert f"bi:{stale_sheet.id}" not in node_ids
    assert result.counts_by_source["BI_LINEAGE"] == 1


@pytest.mark.asyncio
async def test_r11b13_a_report_type_with_no_node_kind_is_skipped_not_guessed(db_session) -> None:
    """An unrecognised `report_type` -- a Looker parser landing later, a Tableau
    object type not parsed today -- gets no node rather than an invented kind
    the API's closed `UnifiedLineageNodeKind` would reject at serialization."""
    datasource, schema = await _seed_org_and_datasource(db_session)
    orders = await _seed_table(db_session, datasource, schema, "orders")

    connection = await _seed_bi_connection(db_session, datasource)
    artifact_import = await _seed_bi_import(db_session, connection)
    known = await _seed_bi_report(
        db_session, artifact_import, name="Known Sheet", report_type="SHEET"
    )
    unknown = await _seed_bi_report(
        db_session, artifact_import, name="Some Story", report_type="STORY"
    )
    await _seed_bi_metric(
        db_session,
        artifact_import,
        name="Shared Metric",
        reports=[known, unknown],
        matched_table_id=orders.id,
        source_table_name="orders",
        source_column_name="amount",
    )

    result = await build_unified_lineage_graph_payload(
        db_session, datasource, node_limit=50, edge_limit=50, settings=None
    )

    node_ids = {node.id for node in result.nodes}
    assert f"bi:{known.id}" in node_ids
    assert f"bi:{unknown.id}" not in node_ids
    assert result.counts_by_source["BI_LINEAGE"] == 1
