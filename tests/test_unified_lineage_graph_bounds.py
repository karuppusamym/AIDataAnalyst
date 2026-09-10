"""Characterization suite for `_build_unified_graph`'s bounds and filters.

R02 names `unified_lineage_api._build_unified_graph` as a refactoring hotspot.
`tests/test_unified_lineage.py` already pins what each *edge kind* contributes;
what it does not pin is the pair of rules that are easiest to break when the
per-kind collection is split into providers:

* **bounds** -- `node_limit` and `edge_limit` are hard caps on the returned
  graph, and every path that hits one must contribute the corresponding
  `NODE_LIMIT` / `EDGE_LIMIT` truncation reason exactly once (the reasons are
  a sorted set, not a log);
* **filters** -- `suggestion_status` selects which `RelationshipCandidate`
  rows render, and `include_pending_edges` selects whether unreviewed
  (`review_status != "ACTIVE"`) parser/dbt/OpenLineage rows render at all.
  These are two independent switches, and neither may leak into the other.

Also pinned here: `counts_by_source` always carries all six keys, a link is
only ever admitted when both endpoints are registered nodes, and a datasource
scopes the graph absolutely.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    OpenLineageRunEvent,
    OpenLineageTableEdge,
    Organization,
    Project,
    RelationshipCandidate,
    ViewLineageEdge,
)
from aida.unified_lineage_api import _build_unified_graph

pytestmark = pytest.mark.asyncio

ALL_EDGE_SOURCES = {
    "FOREIGN_KEY",
    "SUGGESTED_RELATIONSHIP",
    "DBT_DEPENDENCY",
    "OPENLINEAGE_ETL",
    "VIEW_DEFINITION",
    "PROCEDURE_DEFINITION",
}


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_datasource(session: AsyncSession) -> tuple[DataSource, MetadataSchema]:
    org = Organization(id=uuid4(), name=f"org-{uuid4().hex[:8]}", slug=f"org-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Core",
        code=f"COR{uuid4().hex[:6]}",
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
        name=f"ds-{uuid4().hex[:6]}",
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


async def _table(
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


async def _column(
    session: AsyncSession, table: MetadataTable, name: str, ordinal: int = 1
) -> MetadataColumn:
    column = MetadataColumn(
        id=uuid4(),
        organization_id=table.organization_id,
        table_id=table.id,
        name=name,
        ordinal_position=ordinal,
        physical_type="text",
        nullable=True,
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return column


async def _foreign_key(
    session: AsyncSession, source: MetadataTable, target: MetadataTable, name: str
) -> MetadataConstraint:
    constraint = MetadataConstraint(
        id=uuid4(),
        organization_id=source.organization_id,
        datasource_id=source.datasource_id,
        table_id=source.id,
        name=name,
        constraint_type="FOREIGN_KEY",
        columns=["ref_id"],
        referenced_table_id=target.id,
        referenced_columns=["id"],
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(constraint)
    await session.flush()
    return constraint


async def _build(session: AsyncSession, datasource: DataSource, **overrides):
    kwargs = dict(
        node_limit=300,
        edge_limit=1_500,
        suggestion_status="ALL",
        include_pending_edges=False,
    )
    kwargs.update(overrides)
    return await _build_unified_graph(session, datasource, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


async def test_empty_datasource_yields_an_empty_graph_with_every_count_key(db_session) -> None:
    datasource, _ = await _seed_datasource(db_session)

    graph = await _build(db_session, datasource)

    assert graph.nodes == {}
    assert graph.links == []
    assert graph.truncation_reasons == []
    assert set(graph.counts_by_source) == ALL_EDGE_SOURCES
    assert all(count == 0 for count in graph.counts_by_source.values())


async def test_table_nodes_carry_the_catalog_qualified_name_and_resolve(db_session) -> None:
    datasource, schema = await _seed_datasource(db_session)
    orders = await _table(db_session, datasource, schema, "orders")

    graph = await _build(db_session, datasource)

    node = graph.nodes[str(orders.id)]
    assert node.node_kind == "TABLE"
    assert node.label == "orders"
    assert node.qualified_name == "bank.public.orders"
    assert node.matched_table_id == orders.id
    assert node.resolved is True


async def test_another_datasources_tables_never_enter_the_graph(db_session) -> None:
    datasource, schema = await _seed_datasource(db_session)
    other, other_schema = await _seed_datasource(db_session)
    await _table(db_session, datasource, schema, "mine")
    await _table(db_session, other, other_schema, "theirs")

    graph = await _build(db_session, datasource)

    assert [info.label for info in graph.nodes.values()] == ["mine"]


# ---------------------------------------------------------------------------
# Bounds -- the invariant with the most scattered implementation
# ---------------------------------------------------------------------------


async def test_node_limit_caps_the_graph_and_reports_the_reason_once(db_session) -> None:
    datasource, schema = await _seed_datasource(db_session)
    for index in range(6):
        await _table(db_session, datasource, schema, f"t{index}")

    graph = await _build(db_session, datasource, node_limit=3)

    assert len(graph.nodes) == 3
    # Truncation reasons are a sorted set: hitting the same bound repeatedly
    # still names it once.
    assert graph.truncation_reasons == ["NODE_LIMIT"]


async def test_node_limit_selects_the_first_tables_in_catalog_order(db_session) -> None:
    datasource, schema = await _seed_datasource(db_session)
    for name in ("delta", "alpha", "charlie", "bravo"):
        await _table(db_session, datasource, schema, name)

    graph = await _build(db_session, datasource, node_limit=2)

    # Tables are ordered by (catalog, schema, table) name before the cap, so
    # the bound is deterministic rather than insertion-ordered.
    assert sorted(info.label for info in graph.nodes.values()) == ["alpha", "bravo"]


async def test_edge_limit_caps_the_links_and_reports_the_reason(db_session) -> None:
    datasource, schema = await _seed_datasource(db_session)
    hub = await _table(db_session, datasource, schema, "hub")
    for index in range(5):
        spoke = await _table(db_session, datasource, schema, f"spoke{index}")
        await _foreign_key(db_session, hub, spoke, f"fk_hub_{index}")

    graph = await _build(db_session, datasource, edge_limit=2)

    assert len(graph.links) == 2
    assert graph.truncation_reasons == ["EDGE_LIMIT"]
    assert graph.counts_by_source["FOREIGN_KEY"] == 2


async def test_a_graph_within_its_bounds_reports_no_truncation(db_session) -> None:
    datasource, schema = await _seed_datasource(db_session)
    orders = await _table(db_session, datasource, schema, "orders")
    customers = await _table(db_session, datasource, schema, "customers")
    await _foreign_key(db_session, orders, customers, "fk_orders_customers")

    graph = await _build(db_session, datasource, node_limit=10, edge_limit=10)

    assert graph.truncation_reasons == []
    assert len(graph.nodes) == 2
    assert len(graph.links) == 1


async def test_both_bounds_can_be_reported_together_and_stay_sorted(db_session) -> None:
    datasource, schema = await _seed_datasource(db_session)
    hub = await _table(db_session, datasource, schema, "aaa_hub")
    for index in range(6):
        spoke = await _table(db_session, datasource, schema, f"spoke{index}")
        await _foreign_key(db_session, hub, spoke, f"fk_hub_{index}")

    graph = await _build(db_session, datasource, node_limit=4, edge_limit=1)

    assert graph.truncation_reasons == ["EDGE_LIMIT", "NODE_LIMIT"]


async def test_a_link_whose_endpoint_was_dropped_by_the_node_bound_is_dropped_too(
    db_session,
) -> None:
    """A foreign key survives only when both of its tables made it into the
    node budget. Without that rule the response would carry an edge pointing at
    a node the caller cannot see."""
    datasource, schema = await _seed_datasource(db_session)
    # Names chosen so catalog ordering admits `aaa_source` and excludes `zzz_target`.
    source = await _table(db_session, datasource, schema, "aaa_source")
    target = await _table(db_session, datasource, schema, "zzz_target")
    await _foreign_key(db_session, source, target, "fk_source_target")

    graph = await _build(db_session, datasource, node_limit=1)

    assert set(graph.nodes) == {str(source.id)}
    assert graph.links == []
    assert graph.counts_by_source["FOREIGN_KEY"] == 0


# ---------------------------------------------------------------------------
# suggestion_status
# ---------------------------------------------------------------------------


async def _seed_candidates(db_session) -> tuple[DataSource, dict[str, RelationshipCandidate]]:
    datasource, schema = await _seed_datasource(db_session)
    orders = await _table(db_session, datasource, schema, "orders")
    candidates: dict[str, RelationshipCandidate] = {}
    for index, status in enumerate(("PENDING", "APPROVED", "REJECTED")):
        target = await _table(db_session, datasource, schema, f"target_{status.lower()}")
        source_column = await _column(db_session, orders, f"{status.lower()}_id", index + 1)
        target_column = await _column(db_session, target, "id")
        candidate = RelationshipCandidate(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            target_datasource_id=datasource.id,
            source_table_id=orders.id,
            source_column_id=source_column.id,
            target_table_id=target.id,
            target_column_id=target_column.id,
            detection_rule="NAME_MATCH",
            confidence=0.5 + index / 10,
            evidence={"rule": "NAME_MATCH"},
            status=status,
            created_by="inference",
        )
        db_session.add(candidate)
        candidates[status] = candidate
    await db_session.flush()
    return datasource, candidates


async def test_suggestion_status_all_admits_every_candidate_status(db_session) -> None:
    datasource, candidates = await _seed_candidates(db_session)

    graph = await _build(db_session, datasource, suggestion_status="ALL")

    assert {link.edge_id for link in graph.links} == {
        f"candidate:{candidate.id}" for candidate in candidates.values()
    }
    assert graph.counts_by_source["SUGGESTED_RELATIONSHIP"] == 3


@pytest.mark.parametrize("status", ["PENDING", "APPROVED", "REJECTED"])
async def test_suggestion_status_admits_only_that_status(db_session, status: str) -> None:
    datasource, candidates = await _seed_candidates(db_session)

    graph = await _build(db_session, datasource, suggestion_status=status)

    assert [link.edge_id for link in graph.links] == [f"candidate:{candidates[status].id}"]
    assert graph.links[0].status == status
    assert graph.counts_by_source["SUGGESTED_RELATIONSHIP"] == 1


async def test_candidate_edges_carry_their_column_names_and_evidence(db_session) -> None:
    datasource, candidates = await _seed_candidates(db_session)

    graph = await _build(db_session, datasource, suggestion_status="APPROVED")

    link = graph.links[0]
    assert link.source_columns == ("approved_id",)
    assert link.target_columns == ("id",)
    assert link.evidence == {"rule": "NAME_MATCH"}
    assert link.confidence == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# include_pending_edges (P1-05)
# ---------------------------------------------------------------------------


async def _seed_view_edges(db_session) -> tuple[DataSource, ViewLineageEdge, ViewLineageEdge]:
    datasource, schema = await _seed_datasource(db_session)
    base = await _table(db_session, datasource, schema, "raw_orders")
    reviewed_view = await _table(db_session, datasource, schema, "vw_reviewed")
    proposed_view = await _table(db_session, datasource, schema, "vw_proposed")
    reviewed = ViewLineageEdge(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        source_table="bank.public.raw_orders",
        source_column="id",
        target_table="bank.public.vw_reviewed",
        target_column="order_id",
        source_table_id=base.id,
        target_table_id=reviewed_view.id,
        transformation_type="DIRECT",
        confidence="FULL",
        dialect="postgres",
        sql_hash="h-reviewed",
        review_status="ACTIVE",
    )
    proposed = ViewLineageEdge(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        source_table="bank.public.raw_orders",
        source_column="id",
        target_table="bank.public.vw_proposed",
        target_column="order_id",
        source_table_id=base.id,
        target_table_id=proposed_view.id,
        transformation_type="DIRECT",
        confidence="PARTIAL",
        dialect="postgres",
        sql_hash="h-proposed",
        review_status="PROPOSED",
    )
    db_session.add_all([reviewed, proposed])
    await db_session.flush()
    return datasource, reviewed, proposed


async def test_unreviewed_parser_edges_are_excluded_by_default(db_session) -> None:
    datasource, reviewed, _ = await _seed_view_edges(db_session)

    graph = await _build(db_session, datasource, include_pending_edges=False)

    assert [link.edge_id for link in graph.links] == [f"view_definition:{reviewed.id}"]
    assert graph.counts_by_source["VIEW_DEFINITION"] == 1


async def test_unreviewed_parser_edges_appear_only_on_explicit_opt_in(db_session) -> None:
    datasource, reviewed, proposed = await _seed_view_edges(db_session)

    graph = await _build(db_session, datasource, include_pending_edges=True)

    assert {link.edge_id for link in graph.links} == {
        f"view_definition:{reviewed.id}",
        f"view_definition:{proposed.id}",
    }
    assert graph.counts_by_source["VIEW_DEFINITION"] == 2


async def test_definition_edge_confidence_maps_the_parser_vocabulary_onto_a_scale(
    db_session,
) -> None:
    datasource, reviewed, proposed = await _seed_view_edges(db_session)

    graph = await _build(db_session, datasource, include_pending_edges=True)

    by_id = {link.edge_id: link for link in graph.links}
    assert by_id[f"view_definition:{reviewed.id}"].confidence == 1.0
    assert by_id[f"view_definition:{proposed.id}"].confidence == 0.6
    # The dependent (view) is the source; the base table is what it depends on.
    assert by_id[f"view_definition:{reviewed.id}"].source_id == str(reviewed.target_table_id)
    assert by_id[f"view_definition:{reviewed.id}"].target_id == str(reviewed.source_table_id)


async def test_suggestion_status_and_include_pending_edges_are_independent(db_session) -> None:
    """A PENDING relationship candidate is not an unreviewed parser edge, and
    vice versa -- the two switches must not be wired to each other."""
    datasource, candidates = await _seed_candidates(db_session)

    opted_in = await _build(
        db_session, datasource, suggestion_status="PENDING", include_pending_edges=True
    )
    opted_out = await _build(
        db_session, datasource, suggestion_status="PENDING", include_pending_edges=False
    )

    assert [link.edge_id for link in opted_in.links] == [f"candidate:{candidates['PENDING'].id}"]
    assert [link.edge_id for link in opted_out.links] == [f"candidate:{candidates['PENDING'].id}"]


# ---------------------------------------------------------------------------
# OpenLineage: the one edge kind that can register nodes of its own
# ---------------------------------------------------------------------------


async def test_openlineage_edges_register_unresolved_dataset_nodes(db_session) -> None:
    datasource, schema = await _seed_datasource(db_session)
    known = await _table(db_session, datasource, schema, "orders")
    run_event = OpenLineageRunEvent(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        event_fingerprint=uuid4().hex,
        event_type="COMPLETE",
        event_time=datetime.now(UTC),
        producer="https://example.test/openlineage",
        job_namespace="etl",
        job_name="load_orders",
        run_id=uuid4().hex,
        input_dataset_count=1,
        output_dataset_count=1,
        table_edge_count=1,
        column_edge_count=0,
        unresolved_dataset_count=1,
        imported_by="importer@example.com",
    )
    db_session.add(run_event)
    await db_session.flush()
    edge = OpenLineageTableEdge(
        id=uuid4(),
        organization_id=datasource.organization_id,
        run_event_id=run_event.id,
        input_dataset_namespace="s3://lake",
        input_dataset_name="raw_orders",
        output_dataset_namespace="bank",
        output_dataset_name="orders",
        input_table_id=None,
        output_table_id=known.id,
        edge_kind="ETL",
        review_status="ACTIVE",
    )
    db_session.add(edge)
    await db_session.flush()

    graph = await _build(db_session, datasource)

    unresolved = graph.nodes["openlineage:s3://lake:raw_orders"]
    assert unresolved.node_kind == "UNRESOLVED_DATASET"
    assert unresolved.resolved is False
    assert unresolved.matched_table_id is None
    assert [link.edge_id for link in graph.links] == [f"openlineage:{edge.id}"]
    # Output depends on input, matching every other kind's convention.
    assert graph.links[0].source_id == str(known.id)
    assert graph.links[0].target_id == "openlineage:s3://lake:raw_orders"
    assert graph.counts_by_source["OPENLINEAGE_ETL"] == 1


async def test_openlineage_node_registration_respects_the_node_bound(db_session) -> None:
    """An unresolved dataset node that cannot be registered drops its edge too
    -- the node bound is not a suggestion one edge kind may exceed."""
    datasource, schema = await _seed_datasource(db_session)
    known = await _table(db_session, datasource, schema, "orders")
    run_event = OpenLineageRunEvent(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        event_fingerprint=uuid4().hex,
        event_type="COMPLETE",
        event_time=datetime.now(UTC),
        producer="https://example.test/openlineage",
        job_namespace="etl",
        job_name="load_orders",
        run_id=uuid4().hex,
        input_dataset_count=1,
        output_dataset_count=1,
        table_edge_count=1,
        column_edge_count=0,
        unresolved_dataset_count=1,
        imported_by="importer@example.com",
    )
    db_session.add(run_event)
    await db_session.flush()
    db_session.add(
        OpenLineageTableEdge(
            id=uuid4(),
            organization_id=datasource.organization_id,
            run_event_id=run_event.id,
            input_dataset_namespace="s3://lake",
            input_dataset_name="raw_orders",
            output_dataset_namespace="bank",
            output_dataset_name="orders",
            input_table_id=None,
            output_table_id=known.id,
            edge_kind="ETL",
            review_status="ACTIVE",
        )
    )
    await db_session.flush()

    graph = await _build(db_session, datasource, node_limit=1)

    assert set(graph.nodes) == {str(known.id)}
    assert graph.links == []
    assert "NODE_LIMIT" in graph.truncation_reasons
