"""R11-GQL01: lineage over GraphQL is REST's traversal and REST's graph, paged.

`lineageImpact` and `lineageGraph` call the builders the unified-lineage routes call
(`aida.unified_lineage_service`), with the routes' bounds and settings, so these tests put
the two side by side on one real SQLite estate: every page of a GraphQL read, concatenated,
is the REST answer -- the same nodes in the same order, the same depths, edge kinds and
quality states, the same truncation. What differs is deliberate and pinned here: GraphQL
pages by cursor inside its object budget. Both ask the datasource's workspace gate, as
`DataSource.tables` does, before they name a single table (REST since R11-D28).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from strawberry.types.graphql import OperationType

from aida.graphql_limits import DocumentRefused, admit_document
from aida.graphql_reads import ReadRefused, get_lineage_impact, list_lineage_impact_nodes
from aida.graphql_reads import open_read_scope as _open_read_scope
from aida.graphql_schema import metadata_schema
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    RelationshipCandidate,
    SourceBinding,
    ViewLineageEdge,
    Workspace,
)
from aida.relationship_validation import RECORDED_VALIDATION_KEY
from aida.security_types import SecurityContext
from aida.unified_lineage_api import get_unified_lineage_graph, get_unified_lineage_impact
from aida.unified_lineage_service import (
    UNIFIED_LINEAGE_READER_ROLES,
    build_unified_lineage_impact_payload,
)
from aida.workspace_access import ENFORCE
from atlas.platform.config import Settings
from atlas.platform.db import Base
from tests.support.app_surface import reaches_call

SETTINGS = Settings(_env_file=None)
CLOSED_TABLE = "zzq_secret_ledger"
DIMENSIONS = 5

IMPACT_NODE = "nodeId nodeKind label qualifiedName depth contributingEdgeSources qualityState"
IMPACT = """
query Impact($ds: ID!, $node: String!, $depth: Int!, $limit: Int!, $first: Int!, $after: String) {
  lineageImpact(datasourceId: $ds, nodeId: $node, depth: $depth, nodeLimit: $limit) {
    datasourceId focusNodeId focusNodeKind focusLabel requestedDepth nodeLimit
    upstreamTruncated downstreamTruncated
    %s(first: $first, after: $after) {
      totalCount pageInfo { hasNextPage endCursor } nodes { %s }
    }
  }
}
"""
GRAPH = """
query Graph(
  $ds: ID!, $status: LineageSuggestionStatus!, $pending: Boolean!, $first: Int!, $after: String
) {
  lineageGraph(datasourceId: $ds, suggestionStatus: $status, includePendingEdges: $pending) {
    datasourceId countsBySource returnedNodeCount returnedEdgeCount nodeLimit edgeLimit
    truncated truncationReasons
    %s(first: $first, after: $after) { totalCount pageInfo { hasNextPage endCursor } %s }
  }
}
"""
GRAPH_NODES = (
    "nodes { id nodeKind label qualifiedName matchedTableId resolved "
    "inboundEdgeCount outboundEdgeCount }"
)
GRAPH_EDGES = (
    "nodes { id edgeSource sourceNodeId targetNodeId sourceLabel targetLabel status "
    "confidence sourceColumns targetColumns evidence }"
)


@dataclass
class Estate:
    db: AsyncSession
    org: Organization
    open_ds: DataSource
    closed_ds: DataSource
    foreign_ds: DataSource
    tables: dict[str, MetadataTable]
    closed_table: MetadataTable


async def _datasource(
    db: AsyncSession, org: Organization, name: str
) -> tuple[DataSource, MetadataSchema]:
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name=f"LOB {name}", code=f"L{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name=f"Domain {name}",
        code=f"D{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name=f"Project {name}",
        slug=f"p-{uuid4().hex[:8]}",
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
    db.add_all([lob, domain, project, datasource, catalog])
    await db.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    db.add(schema)
    await db.flush()
    return datasource, schema


async def _table(
    db: AsyncSession, datasource: DataSource, schema: MetadataSchema, name: str
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
    db.add(table)
    await db.flush()
    return table


def _foreign_key(child: MetadataTable, parent: MetadataTable) -> MetadataConstraint:
    return MetadataConstraint(
        organization_id=child.organization_id,
        datasource_id=child.datasource_id,
        table_id=child.id,
        name=f"fk_{child.name}_{parent.name}",
        constraint_type="FOREIGN_KEY",
        columns=[f"{parent.name}_id"],
        referenced_table_id=parent.id,
        referenced_columns=["id"],
        status="ACTIVE",
        fingerprint="fp",
    )


def _suggestion(
    tables: dict[str, MetadataTable],
    keys: dict[str, MetadataColumn],
    source: str,
    target: str,
    status: str,
) -> RelationshipCandidate:
    return RelationshipCandidate(
        organization_id=tables[source].organization_id,
        datasource_id=tables[source].datasource_id,
        target_datasource_id=tables[target].datasource_id,
        source_table_id=tables[source].id,
        source_column_id=keys[source].id,
        target_table_id=tables[target].id,
        target_column_id=keys[target].id,
        detection_rule="EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1",
        confidence=0.9,
        evidence={
            "signals": [],
            RECORDED_VALIDATION_KEY: {"fingerprint": "recorded", "outcome": "CORROBORATED"},
        },
        created_by="steward@bank.example",
        status=status,
    )


@pytest_asyncio.fixture
async def estate() -> AsyncIterator[Estate]:
    """raw_orders --VIEW_DEFINITION--> vw_orders <--FK-- fct_orders, and five dimensions
    keyed on raw_orders: seven nodes downstream of raw_orders across two edge kinds and two
    hops, enough to page. Beside it, a datasource in an enforcing workspace the caller is
    not a member of, and another tenant's."""
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        other = Organization(id=uuid4(), name="Other", slug=f"other-{uuid4().hex[:8]}")
        db.add_all([org, other])
        await db.flush()

        open_ds, schema = await _datasource(db, org, "warehouse")
        tables = {
            name: await _table(db, open_ds, schema, name)
            for name in (
                "raw_orders",
                "vw_orders",
                "fct_orders",
                *(f"dim_{index}" for index in range(DIMENSIONS)),
            )
        }
        db.add(
            ViewLineageEdge(
                organization_id=org.id,
                datasource_id=open_ds.id,
                source_table="bank.public.raw_orders",
                source_column="id",
                target_table="bank.public.vw_orders",
                target_column="order_id",
                source_table_id=tables["raw_orders"].id,
                target_table_id=tables["vw_orders"].id,
                transformation_type="DIRECT",
                confidence="FULL",
                dialect="postgres",
                sql_hash="h1",
            )
        )
        db.add(_foreign_key(tables["fct_orders"], tables["vw_orders"]))
        for index in range(DIMENSIONS):
            db.add(_foreign_key(tables[f"dim_{index}"], tables["raw_orders"]))
        # Two reviewed suggestions between dimensions, one approved and one pending, so
        # the review filter has something to filter -- and the approved one carries the
        # validation its approval recorded, which no graph edge may serve (R11-FP06).
        keys = {
            name: MetadataColumn(
                id=uuid4(),
                organization_id=org.id,
                table_id=tables[name].id,
                name=f"{name}_key",
                ordinal_position=1,
                physical_type="varchar",
                nullable=False,
                fingerprint="fp",
            )
            for name in ("dim_1", "dim_2", "dim_3", "dim_4")
        }
        db.add_all(keys.values())
        await db.flush()
        db.add_all(
            [
                _suggestion(tables, keys, "dim_3", "dim_4", "APPROVED"),
                _suggestion(tables, keys, "dim_1", "dim_2", "PENDING"),
            ]
        )

        closed_ds, closed_schema = await _datasource(db, org, "restricted")
        closed_table = await _table(db, closed_ds, closed_schema, CLOSED_TABLE)
        closed_child = await _table(db, closed_ds, closed_schema, f"{CLOSED_TABLE}_child")
        db.add(_foreign_key(closed_child, closed_table))
        workspace = Workspace(
            organization_id=org.id,
            name="Restricted",
            slug=f"r-{uuid4().hex[:6]}",
            purpose="restricted",
            authorization_mode=ENFORCE,
        )
        db.add(workspace)
        await db.flush()
        db.add(
            SourceBinding(
                organization_id=org.id,
                workspace_id=workspace.id,
                datasource_id=closed_ds.id,
                purpose="restricted",
                status="ACTIVE",
                requested_by="seed",
            )
        )

        foreign_ds, foreign_schema = await _datasource(db, other, "theirs")
        await _table(db, foreign_ds, foreign_schema, "their_orders")
        await db.commit()
        yield Estate(
            db=db,
            org=org,
            open_ds=open_ds,
            closed_ds=closed_ds,
            foreign_ds=foreign_ds,
            tables=tables,
            closed_table=closed_table,
        )
    await engine.dispose()


def _caller(organization_id: UUID, *roles: str) -> SecurityContext:
    return SecurityContext(
        principal_id="analyst@bank.example",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset(roles or ("Analyst",)),
    )


def _scope(estate: Estate, context: SecurityContext) -> Any:
    assert context.organization_id is not None
    return _open_read_scope(
        session=estate.db,
        context=context,
        settings=SETTINGS,
        organization_id=context.organization_id,
    )


async def _gql(
    estate: Estate, context: SecurityContext, query: str, name: str, **variables: Any
) -> Any:
    return await metadata_schema.execute(
        query,
        variable_values=variables,
        context_value=_scope(estate, context),
        operation_name=name,
        allowed_operation_types=(OperationType.QUERY,),
    )


def _codes(result: Any) -> list[tuple[str, str | None]]:
    return [
        (error.original_error.code, error.original_error.reason) for error in result.errors or ()
    ]


async def _walk_impact(
    estate: Estate,
    context: SecurityContext,
    focus: str,
    direction: str,
    *,
    depth: int = 5,
    limit: int = 200,
    first: int = 2,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Every page of one direction, in order; the header of the first; how many pages."""
    rows: list[dict[str, Any]] = []
    header: dict[str, Any] = {}
    after: str | None = None
    pages = 0
    while True:
        result = await _gql(
            estate,
            context,
            IMPACT % (direction, IMPACT_NODE),
            "Impact",
            ds=str(estate.open_ds.id),
            node=focus,
            depth=depth,
            limit=limit,
            first=first,
            after=after,
        )
        assert result.errors is None, result.errors
        impact = result.data["lineageImpact"]
        page = impact.pop(direction)
        header = header or impact
        if pages == 0:
            assert page["totalCount"] is not None
        rows += page["nodes"]
        pages += 1
        if not page["pageInfo"]["hasNextPage"]:
            return header, rows, pages
        after = page["pageInfo"]["endCursor"]


def _impact_rows(rest_rows: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "nodeId": row.node_id,
            "nodeKind": row.node_kind,
            "label": row.label,
            "qualifiedName": row.qualified_name,
            "depth": row.depth,
            "contributingEdgeSources": list(row.contributing_edge_sources),
            "qualityState": row.quality_state,
        }
        for row in rest_rows
    ]


async def _rest_impact(
    estate: Estate, context: SecurityContext, datasource_id: UUID, focus: str, **bounds: int
) -> Any:
    return await get_unified_lineage_impact(
        datasource_id,
        focus,
        depth=bounds.get("depth", 5),
        node_limit=bounds.get("node_limit", 200),
        context=context,
        session=estate.db,
        settings=SETTINGS,
    )


# --- parity ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("focus", "direction", "expected"),
    [
        ("raw_orders", "downstream", DIMENSIONS + 2),
        ("fct_orders", "upstream", 2),
        ("raw_orders", "upstream", 0),
    ],
)
@pytest.mark.parametrize("roles", [("Analyst",), ("DataSteward",), ("Auditor",)])
async def test_every_page_of_an_impact_read_is_rests_traversal_in_its_order(
    estate: Estate, focus: str, direction: str, expected: int, roles: tuple[str, ...]
) -> None:
    context = _caller(estate.org.id, *roles)
    focus_id = str(estate.tables[focus].id)

    header, rows, pages = await _walk_impact(estate, context, focus_id, direction)
    rest = await _rest_impact(estate, context, estate.open_ds.id, focus_id)

    assert rows == _impact_rows(getattr(rest, direction))
    assert len(rows) == expected
    assert pages == max(1, -(-expected // 2)), "two to a page, resumed by cursor"
    assert header == {
        "datasourceId": str(rest.datasource_id),
        "focusNodeId": rest.focus_node_id,
        "focusNodeKind": rest.focus_node_kind,
        "focusLabel": rest.focus_label,
        "requestedDepth": rest.requested_depth,
        "nodeLimit": rest.node_limit,
        "upstreamTruncated": rest.upstream_truncated,
        "downstreamTruncated": rest.downstream_truncated,
    }


@pytest.mark.parametrize(
    ("depth", "limit", "truncated"),
    [(1, 200, False), (5, 5, True)],
    ids=["one-hop", "node-bound"],
)
async def test_the_bounds_cut_the_traversal_where_rest_cuts_it(
    estate: Estate, depth: int, limit: int, truncated: bool
) -> None:
    context = _caller(estate.org.id)
    focus_id = str(estate.tables["raw_orders"].id)

    header, rows, _ = await _walk_impact(
        estate, context, focus_id, "downstream", depth=depth, limit=limit
    )
    rest = await _rest_impact(
        estate, context, estate.open_ds.id, focus_id, depth=depth, node_limit=limit
    )

    assert rows == _impact_rows(rest.downstream)
    assert header["downstreamTruncated"] is rest.downstream_truncated is truncated
    assert str(estate.tables["fct_orders"].id) not in {row["nodeId"] for row in rows}


async def _walk_graph(
    estate: Estate, context: SecurityContext, part: str, selection: str, **arguments: Any
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    header: dict[str, Any] = {}
    after: str | None = None
    while True:
        result = await _gql(
            estate,
            context,
            GRAPH % (part, selection),
            "Graph",
            ds=str(estate.open_ds.id),
            first=3,
            after=after,
            **arguments,
        )
        assert result.errors is None, result.errors
        graph = result.data["lineageGraph"]
        page = graph.pop(part)
        header = header or graph
        rows += page["nodes"]
        if not page["pageInfo"]["hasNextPage"]:
            return header, rows
        after = page["pageInfo"]["endCursor"]


@pytest.mark.parametrize(
    ("status", "pending", "suggestions"),
    [("APPROVED", False, 1), ("ALL", False, 2), ("PENDING", False, 1), ("APPROVED", True, 1)],
)
async def test_the_graph_is_rests_graph_node_for_node_and_edge_for_edge(
    estate: Estate, status: str, pending: bool, suggestions: int
) -> None:
    context = _caller(estate.org.id)
    arguments = {"status": status, "pending": pending}

    header, nodes = await _walk_graph(estate, context, "nodes", GRAPH_NODES, **arguments)
    _, edges = await _walk_graph(estate, context, "edges", GRAPH_EDGES, **arguments)
    rest = await get_unified_lineage_graph(
        estate.open_ds.id,
        node_limit=300,
        edge_limit=1_500,
        suggestion_status=status,  # type: ignore[arg-type]
        include_pending_edges=pending,
        context=context,
        session=estate.db,
        settings=SETTINGS,
    )
    served = rest.model_dump(mode="json")

    rest_nodes = sorted(served["nodes"], key=lambda node: (node["qualified_name"], node["id"]))
    assert nodes == [
        {
            "id": node["id"],
            "nodeKind": node["node_kind"],
            "label": node["label"],
            "qualifiedName": node["qualified_name"],
            "matchedTableId": node["matched_table_id"],
            "resolved": node["resolved"],
            "inboundEdgeCount": node["inbound_edge_count"],
            "outboundEdgeCount": node["outbound_edge_count"],
        }
        for node in rest_nodes
    ]
    rest_edges = sorted(
        served["edges"],
        key=lambda edge: (edge["id"], edge["source_node_id"], edge["target_node_id"]),
    )
    assert edges == [
        {
            "id": edge["id"],
            "edgeSource": edge["edge_source"],
            "sourceNodeId": edge["source_node_id"],
            "targetNodeId": edge["target_node_id"],
            "sourceLabel": edge["source_label"],
            "targetLabel": edge["target_label"],
            "status": edge["status"],
            "confidence": edge["confidence"],
            "sourceColumns": edge["source_columns"],
            "targetColumns": edge["target_columns"],
            "evidence": edge["evidence"],
        }
        for edge in rest_edges
    ]
    assert len(nodes) == DIMENSIONS + 3
    assert len(edges) == DIMENSIONS + 2 + suggestions, "the review filter chose these"
    assert all(
        RECORDED_VALIDATION_KEY not in edge["evidence"]
        for edge in edges
        if edge["edgeSource"] == "SUGGESTED_RELATIONSHIP"
    )
    assert header == {
        "datasourceId": served["datasource_id"],
        "countsBySource": served["counts_by_source"],
        "returnedNodeCount": served["returned_node_count"],
        "returnedEdgeCount": served["returned_edge_count"],
        "nodeLimit": served["node_limit"],
        "edgeLimit": served["edge_limit"],
        "truncated": served["truncated"],
        "truncationReasons": served["truncation_reasons"],
    }


# --- refusals ----------------------------------------------------------------------


async def test_a_role_the_lineage_routes_do_not_admit_is_refused_before_anything_is_read(
    estate: Estate,
) -> None:
    """Operations may read a datasource here -- it is one of that route's roles -- but not
    its lineage, exactly as the lineage routes' `require_roles` refuses it."""
    assert "Operations" not in UNIFIED_LINEAGE_READER_ROLES
    context = _caller(estate.org.id, "Operations")

    result = await _gql(
        estate,
        context,
        IMPACT % ("downstream", "nodeId"),
        "Impact",
        ds=str(uuid4()),
        node="anything",
        depth=5,
        limit=200,
        first=5,
    )

    assert result.data["lineageImpact"] is None
    assert _codes(result) == [("FORBIDDEN", "ROLE_REQUIRED")]


@pytest.mark.parametrize(
    ("case", "status", "expected"),
    [
        ("unknown-datasource", 404, ("NOT_FOUND", None)),
        ("another-tenants-datasource", 403, ("FORBIDDEN", "CROSS_ORGANIZATION")),
        ("unknown-node", 404, ("NOT_FOUND", None)),
    ],
)
async def test_where_rest_answers_403_or_404_graphql_refuses_with_the_same_meaning(
    estate: Estate, case: str, status: int, expected: tuple[str, str | None]
) -> None:
    context = _caller(estate.org.id)
    datasource_id, focus = {
        "unknown-datasource": (uuid4(), str(estate.tables["raw_orders"].id)),
        "another-tenants-datasource": (estate.foreign_ds.id, str(uuid4())),
        "unknown-node": (estate.open_ds.id, str(uuid4())),
    }[case]

    with pytest.raises(HTTPException) as rest:
        await _rest_impact(estate, context, datasource_id, focus)
    result = await _gql(
        estate,
        context,
        IMPACT % ("downstream", "nodeId"),
        "Impact",
        ds=str(datasource_id),
        node=focus,
        depth=5,
        limit=200,
        first=5,
    )

    assert rest.value.status_code == status
    assert result.data["lineageImpact"] is None
    assert _codes(result) == [expected]


@pytest.mark.parametrize(
    ("document", "variables", "reason"),
    [
        (IMPACT % ("downstream", "nodeId"), {"depth": 9, "limit": 200}, "DEPTH_OUT_OF_RANGE"),
        (IMPACT % ("downstream", "nodeId"), {"depth": 0, "limit": 200}, "DEPTH_OUT_OF_RANGE"),
        (IMPACT % ("downstream", "nodeId"), {"depth": 5, "limit": 4}, "NODE_LIMIT_OUT_OF_RANGE"),
        (
            IMPACT % ("downstream", "nodeId"),
            {"depth": 5, "limit": 2_001},
            "NODE_LIMIT_OUT_OF_RANGE",
        ),
    ],
)
async def test_an_out_of_range_bound_is_refused_as_rest_refuses_it(
    estate: Estate, document: str, variables: dict[str, int], reason: str
) -> None:
    result = await _gql(
        estate,
        _caller(estate.org.id),
        document,
        "Impact",
        ds=str(estate.open_ds.id),
        node=str(estate.tables["raw_orders"].id),
        first=5,
        **variables,
    )

    assert _codes(result) == [("INVALID_ARGUMENT", reason)]


async def test_a_graph_edge_limit_past_rests_is_refused(estate: Estate) -> None:
    result = await _gql(
        estate,
        _caller(estate.org.id),
        "query G($ds: ID!) { lineageGraph(datasourceId: $ds, edgeLimit: 10001) { truncated } }",
        "G",
        ds=str(estate.open_ds.id),
    )

    assert _codes(result) == [("INVALID_ARGUMENT", "EDGE_LIMIT_OUT_OF_RANGE")]


async def test_a_cursor_this_field_did_not_issue_is_refused(estate: Estate) -> None:
    result = await _gql(
        estate,
        _caller(estate.org.id),
        IMPACT % ("downstream", "nodeId"),
        "Impact",
        ds=str(estate.open_ds.id),
        node=str(estate.tables["raw_orders"].id),
        depth=5,
        limit=200,
        first=2,
        after="not-a-cursor",
    )

    assert result.data["lineageImpact"]["downstream"] is None
    assert _codes(result) == [("INVALID_CURSOR", None)]


# --- the workspace gate: stricter than REST, and never a name ----------------------------


@pytest.mark.parametrize("field", ["impact", "graph"])
async def test_a_datasource_the_callers_workspace_refuses_names_none_of_its_tables(
    estate: Estate, field: str
) -> None:
    """`DataSource.tables` refuses this datasource (NO_WORKSPACE_MEMBERSHIP), so its
    lineage must not become the way to read its table names."""
    context = _caller(estate.org.id)
    if field == "impact":
        result = await _gql(
            estate,
            context,
            IMPACT % ("downstream", IMPACT_NODE),
            "Impact",
            ds=str(estate.closed_ds.id),
            node=str(estate.closed_table.id),
            depth=5,
            limit=200,
            first=5,
        )
    else:
        result = await _gql(
            estate,
            context,
            GRAPH % ("nodes", GRAPH_NODES),
            "Graph",
            ds=str(estate.closed_ds.id),
            status="ALL",
            pending=True,
            first=5,
        )
    tables = await _gql(
        estate,
        context,
        "query T($ds: ID!) { tables(datasourceId: $ds) { nodes { name } } }",
        "T",
        ds=str(estate.closed_ds.id),
    )

    assert _codes(result) == _codes(tables) == [("FORBIDDEN", "NO_WORKSPACE_MEMBERSHIP")]
    assert CLOSED_TABLE not in repr(result.data) + repr([str(e) for e in result.errors or ()])


async def test_a_page_decides_its_datasource_again_rather_than_trust_its_parent(
    estate: Estate,
) -> None:
    """A child page is handed a read its parent built. If that read is of a datasource this
    caller may not read -- a parent under a different decision, a cached object -- the page
    refuses it rather than page it."""
    # Built by the service itself, which decides nothing about the caller -- the way a read
    # reaches a page without this caller's decision.
    impact = await build_unified_lineage_impact_payload(
        estate.db, estate.closed_ds, str(estate.closed_table.id), settings=SETTINGS
    )
    assert impact.downstream, "the closed datasource has lineage to withhold"

    with pytest.raises(ReadRefused) as refused:
        await list_lineage_impact_nodes(
            _scope(estate, _caller(estate.org.id)), impact, upstream=False, first=5, after=None
        )

    assert (refused.value.code, refused.value.reason) == ("FORBIDDEN", "NO_WORKSPACE_MEMBERSHIP")


async def test_the_read_service_refuses_the_same_way_the_field_does(estate: Estate) -> None:
    with pytest.raises(ReadRefused) as refused:
        await get_lineage_impact(
            _scope(estate, _caller(estate.org.id)),
            estate.closed_ds.id,
            str(estate.closed_table.id),
            depth=5,
            node_limit=200,
        )

    assert refused.value.code == "FORBIDDEN"


# --- the document budget counts lineage pages ----------------------------------------


def _impact_aliases(count: int, first: int) -> str:
    fields = " ".join(
        f'i{index}: lineageImpact(datasourceId: "{uuid4()}", nodeId: "n") '
        f"{{ upstream(first: {first}) {{ nodes {{ nodeId }} }} "
        f"downstream(first: {first}) {{ nodes {{ nodeId }} }} }}"
        for index in range(count)
    )
    return f"query Many {{ {fields} }}"


def test_both_directions_of_an_impact_read_are_charged_at_their_page_size() -> None:
    """Two full pages per impact read is 203 objects; three reads pass the 500-object
    budget and are refused before anything runs. Two are admitted."""
    schema = metadata_schema._schema

    _, cost = admit_document(
        query=_impact_aliases(2, 100), operation_name="Many", variables=None, schema=schema
    )
    with pytest.raises(DocumentRefused) as refused:
        admit_document(
            query=_impact_aliases(3, 100), operation_name="Many", variables=None, schema=schema
        )

    assert cost.estimated_nodes == 2 * (1 + (1 + 100) + (1 + 100))
    assert refused.value.code == "NODE_BUDGET_EXCEEDED"


def test_a_lineage_page_past_the_page_limit_is_refused_at_admission() -> None:
    with pytest.raises(DocumentRefused) as refused:
        admit_document(
            query=_impact_aliases(1, 101),
            operation_name="Many",
            variables=None,
            schema=metadata_schema._schema,
        )

    assert refused.value.code == "PAGE_SIZE_EXCEEDED"


# --- the surface ----------------------------------------------------------------------

_LINEAGE_RESOLVERS = (
    "Query.lineage_impact",
    "Query.lineage_graph",
    "LineageImpact.upstream",
    "LineageImpact.downstream",
    "LineageGraph.nodes",
    "LineageGraph.edges",
)
_SOURCE_EXECUTION_CALLS = frozenset(
    {"QueryExecutionGateway", "open_execution_session", "execute_read_query", "estimate_read_query"}
)


@pytest.mark.parametrize("resolver", _LINEAGE_RESOLVERS)
def test_no_lineage_resolver_reaches_source_execution(resolver: str) -> None:
    """Lineage is Atlas' own catalog: no resolver's call graph reaches the gateway or a
    connector's execution session -- and each reaches the lineage decision."""
    assert reaches_call("aida.graphql_schema", resolver, frozenset({"_lineage_datasource"}))
    assert not reaches_call("aida.graphql_schema", resolver, _SOURCE_EXECUTION_CALLS)
