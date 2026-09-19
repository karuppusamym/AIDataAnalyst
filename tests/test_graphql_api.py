"""R11-GQL01: the metadata GraphQL facade (design section 13A, acceptance GQL-A/B/C).

Everything here drives the real application -- `POST /graphql` and the REST routes it
answers for -- through the ASGI transport against one in-memory database, with a
statement counter on the engine. Three claims are proven:

* **GQL-A, parity.** For the same identity and scope, GraphQL returns what REST returns:
  the same objects, in the same order, with the same page boundaries, cursors and totals,
  and the same refusal for the same reason. That is the evidence that GraphQL is an
  interface over the existing reads and not a second catalog.
* **GQL-B, no cross-boundary exposure.** Direct ids, aliases, nested edges, aggregates
  and the request's loader cache cannot reach another tenant's objects or a datasource
  the caller's workspace does not admit -- and no refusal names what it refused.
* **GQL-C, refusal before work.** Cyclic, deep, wide, aliased, oversized and batched
  documents are refused with a stable code before a single statement reaches the
  database.

Plus the non-negotiables: no field or error carries a source value (INV-6), and nothing
a resolver does reaches a connector or the query gateway.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool
from structlog.testing import capture_logs

import aida.models  # noqa: F401 - registers every mapped table on Base.metadata
from aida import graphql_api, graphql_reads
from aida.context_product_reads import CONTEXT_PRODUCT_READERS
from aida.governed_execution import RECEIPT_OVERSIGHT_ROLES, TOOL_EXECUTION_ROLES
from aida.graphql_limits import (
    DEFAULT_LIMITS,
    EXECUTION_ERROR_CODES,
    REFUSAL_CODES,
    GraphQLLimits,
)
from aida.graphql_reads import (
    CATALOG_READ_ROLES,
    DATASOURCE_READ_ROLES,
    GRAPHQL_ENDPOINT_ROLES,
    ReadRefused,
    list_organization_tables,
    open_read_scope,
)
from aida.main import app
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    ColumnDocumentation,
    ColumnDocumentationVersion,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataPartition,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SourceBinding,
    Workspace,
)
from aida.security_types import SecurityContext
from aida.workspace_access import ENFORCE
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session
from tests.support.app_surface import iter_api_routes, reaches_call, require_roles_gate

REPO_ROOT = Path(__file__).resolve().parents[1]

# Strings that cannot occur naturally. A hit anywhere in a response or a log is a leak.
SENTINEL_DEFAULT = "ZZQ-SENTINEL-DEFAULT-7c1e"
SENTINEL_PARTITION = "ZZQ-SENTINEL-PARTITION-3a9f"
SENTINEL_CREDENTIAL = "ZZQ_SENTINEL_CREDENTIAL_55d2"
CLOSED_NAME = "ZZQ-CLOSED-NAME"
FOREIGN_NAME = "ZZQ-FOREIGN-NAME"
_VALUE_SENTINELS = (SENTINEL_DEFAULT, SENTINEL_PARTITION, SENTINEL_CREDENTIAL)
_NAME_SENTINELS = (CLOSED_NAME, FOREIGN_NAME)
_NOW = datetime(2026, 9, 17, 9, 30, tzinfo=UTC)


# --- estate -----------------------------------------------------------------


@dataclass
class Estate:
    engine: AsyncEngine
    db: AsyncSession
    statements: list[str]
    org: Organization
    other_org: Organization
    open_ds: DataSource
    closed_ds: DataSource
    datasources: list[DataSource]
    open_tables: list[MetadataTable]
    documented: MetadataTable
    closed_tables: list[MetadataTable]
    foreign_table: MetadataTable
    documented_column: MetadataColumn
    extra_tables: list[MetadataTable] = field(default_factory=list)


async def _hierarchy(db: AsyncSession, name: str) -> tuple[Organization, Project]:
    org = Organization(id=uuid4(), name=name, slug=f"{name.lower()}-{uuid4().hex[:6]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"R{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Customers",
        code=f"C{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:6]}",
    )
    db.add_all([org, lob, domain, project])
    await db.flush()
    return org, project


async def _datasource(
    db: AsyncSession, project: Project, name: str, *, credential: str = "env://TEST_DSN"
) -> tuple[DataSource, MetadataSchema]:
    datasource = DataSource(
        id=uuid4(),
        organization_id=project.organization_id,
        line_of_business_id=project.line_of_business_id,
        data_domain_id=project.data_domain_id,
        project_id=project.id,
        name=name,
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference=credential,
        status="ACTIVE",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=project.organization_id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    db.add_all([datasource, catalog])
    await db.flush()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=project.organization_id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp",
    )
    db.add(schema)
    await db.flush()
    return datasource, schema


async def _table(
    db: AsyncSession,
    datasource: DataSource,
    schema: MetadataSchema,
    name: str,
    *,
    columns: int,
    object_type: str = "BASE_TABLE",
    status: str = "ACTIVE",
    description: str | None = None,
) -> MetadataTable:
    table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type=object_type,
        status=status,
        fingerprint=f"fp-{name}",
        source_description=description,
    )
    db.add(table)
    await db.flush()
    db.add_all(
        [
            MetadataColumn(
                id=uuid4(),
                organization_id=datasource.organization_id,
                table_id=table.id,
                name=f"c{index:02d}",
                ordinal_position=index + 1,
                physical_type="varchar",
                nullable=index % 2 == 0,
                fingerprint="fp",
            )
            for index in range(columns)
        ]
    )
    await db.flush()
    return table


async def _constraint(
    db: AsyncSession, table: MetadataTable, name: str, kind: str, referenced: UUID | None
) -> None:
    db.add(
        MetadataConstraint(
            id=uuid4(),
            organization_id=table.organization_id,
            datasource_id=table.datasource_id,
            table_id=table.id,
            name=name,
            constraint_type=kind,
            columns=["c00"],
            referenced_table_id=referenced,
            referenced_columns=["c00"] if referenced else [],
            fingerprint="fp",
        )
    )
    await db.flush()


@pytest_asyncio.fixture
async def estate() -> AsyncIterator[Estate]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    statements: list[str] = []

    def _count(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as db:
        db.info["maker"] = maker
        org, project = await _hierarchy(db, "BankA")
        other_org, other_project = await _hierarchy(db, "BankB")
        open_ds, open_schema = await _datasource(
            db, project, "alpha-open", credential=f"env://{SENTINEL_CREDENTIAL}"
        )
        closed_ds, closed_schema = await _datasource(db, project, "beta-closed")
        extra = [await _datasource(db, project, name) for name in ("delta", "epsilon", "gamma")]
        foreign_ds, foreign_schema = await _datasource(db, other_project, "foreign")

        open_tables = [
            await _table(db, open_ds, open_schema, f"t_{index:03d}", columns=4)
            for index in range(1, 25)
        ]
        documented = await _table(
            db, open_ds, open_schema, "t_000", columns=12, description="customer master"
        )
        open_tables.insert(0, documented)
        open_tables.append(
            await _table(db, open_ds, open_schema, "v_customer", columns=3, object_type="VIEW")
        )
        open_tables.append(
            await _table(db, open_ds, open_schema, "t_old", columns=2, status="DEPRECATED")
        )
        closed_tables = [
            await _table(db, closed_ds, closed_schema, f"{CLOSED_NAME}-{index}", columns=3)
            for index in range(3)
        ]
        extra_tables = [
            await _table(db, extra[0][0], extra[0][1], f"x_{index}", columns=2)
            for index in range(4)
        ]
        foreign_table = await _table(db, foreign_ds, foreign_schema, f"{FOREIGN_NAME}-0", columns=2)

        # Every edge a constraint can draw: within the datasource, into a datasource the
        # caller's workspace does not admit, into another tenant, and into nothing at all.
        await _constraint(db, documented, "pk_t000", "PRIMARY_KEY", None)
        await _constraint(db, documented, "fk_open", "FOREIGN_KEY", open_tables[1].id)
        await _constraint(db, documented, "fk_closed", "FOREIGN_KEY", closed_tables[0].id)
        await _constraint(db, documented, "fk_foreign", "FOREIGN_KEY", foreign_table.id)
        await _constraint(db, documented, "fk_dangling", "FOREIGN_KEY", uuid4())

        # Value-bearing catalog content that no read may return (INV-6).
        columns = {
            column.name: column
            for column in (
                await db.scalars(
                    select(MetadataColumn).where(MetadataColumn.table_id == documented.id)
                )
            ).all()
        }
        columns["c00"].default_expression = f"'{SENTINEL_DEFAULT}'"
        db.add(
            MetadataPartition(
                id=uuid4(),
                organization_id=org.id,
                datasource_id=open_ds.id,
                table_id=documented.id,
                name="p_2026",
                partition_type="RANGE",
                key_columns=["c00"],
                high_value=SENTINEL_PARTITION,
                fingerprint="fp",
            )
        )

        # Approved documentation: a superseded and a current table readme, and one
        # approved column description.
        documentation = AssetDocumentation(organization_id=org.id, table_id=documented.id)
        db.add(documentation)
        await db.flush()
        for version, status in ((1, "SUPERSEDED"), (2, "APPROVED")):
            db.add(
                AssetDocumentationVersion(
                    organization_id=org.id,
                    documentation_id=documentation.id,
                    version=version,
                    status=status,
                    readme=f"Customer master, version {version}.",
                    created_by="steward-maker",
                    approved_by="steward-checker",
                    approved_at=_NOW,
                )
            )
        column_doc = ColumnDocumentation(
            organization_id=org.id, table_id=documented.id, column_id=columns["c01"].id
        )
        db.add(column_doc)
        await db.flush()
        db.add(
            ColumnDocumentationVersion(
                organization_id=org.id,
                documentation_id=column_doc.id,
                version=1,
                status="APPROVED",
                description="The customer's legal name.",
                created_by="steward-maker",
                approved_by="steward-checker",
                approved_at=_NOW,
            )
        )

        # The closed datasource sits in an enforcing workspace nobody here belongs to:
        # the gate refuses it with NO_WORKSPACE_MEMBERSHIP. The open one resolves to no
        # workspace and proceeds under the default SHADOW posture -- the same decisions
        # REST gets for the same datasources.
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
        await db.commit()

        event.listen(engine.sync_engine, "before_cursor_execute", _count)
        yield Estate(
            engine=engine,
            db=db,
            statements=statements,
            org=org,
            other_org=other_org,
            open_ds=open_ds,
            closed_ds=closed_ds,
            datasources=[open_ds, closed_ds, *(pair[0] for pair in extra)],
            open_tables=open_tables,
            documented=documented,
            closed_tables=closed_tables,
            foreign_table=foreign_table,
            documented_column=columns["c01"],
            extra_tables=extra_tables,
        )
    await engine.dispose()


@pytest_asyncio.fixture
async def http(estate: Estate) -> AsyncIterator[httpx.AsyncClient]:
    """The real application on the estate's session. Overrides are restored as found."""
    previous = dict(app.dependency_overrides)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield estate.db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gql.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _headers(
    org: Organization, roles: str = "Viewer", principal: str = "gql-reader"
) -> dict[str, str]:
    return {"X-Principal-Id": principal, "X-Roles": roles, "X-Organization-Id": str(org.id)}


async def _gql(
    client: httpx.AsyncClient,
    query: str,
    headers: dict[str, str],
    *,
    variables: dict[str, Any] | None = None,
    operation: str | None = "Q",
) -> httpx.Response:
    payload: dict[str, Any] = {"query": query}
    if operation is not None:
        payload["operationName"] = operation
    if variables is not None:
        payload["variables"] = variables
    return await client.post("/graphql", json=payload, headers=headers)


def _codes(body: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [
        (error["extensions"]["code"], error["extensions"].get("reason"))
        for error in body.get("errors", [])
    ]


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _same_value(rest_value: Any, graphql_value: Any) -> bool:
    if isinstance(rest_value, str) and isinstance(graphql_value, str):
        try:
            return datetime.fromisoformat(rest_value) == datetime.fromisoformat(graphql_value)
        except ValueError:
            return rest_value == graphql_value
    return bool(rest_value == graphql_value)


def _assert_same_objects(
    rest_items: list[dict[str, Any]], graphql_items: list[dict[str, Any]]
) -> None:
    """REST's snake_case object and GraphQL's camelCase one agree on every field GraphQL has."""
    assert len(rest_items) == len(graphql_items)
    for rest_item, graphql_item in zip(rest_items, graphql_items, strict=True):
        for key, rest_value in rest_item.items():
            name = _camel(key)
            if name in graphql_item:
                assert _same_value(rest_value, graphql_item[name]), (
                    key,
                    rest_value,
                    graphql_item[name],
                )


# --- wiring ------------------------------------------------------------------


def _route_roles(method: str, path: str) -> tuple[str, ...]:
    for route in iter_api_routes():
        if route.path == path and method in route.methods:
            gate = require_roles_gate(route)
            assert gate is not None, f"{method} {path} carries no require_roles gate"
            return tuple(sorted(gate[1]))
    raise AssertionError(f"{method} {path} is not mounted")


@pytest.mark.parametrize(
    ("method", "path", "roles"),
    [
        ("GET", "/v1/datasources/{datasource_id}", DATASOURCE_READ_ROLES),
        ("GET", "/v1/organizations/{organization_id}/datasources", DATASOURCE_READ_ROLES),
        ("GET", "/v1/datasources/{datasource_id}/tables", CATALOG_READ_ROLES),
        ("GET", "/v1/tables/{table_id}/columns", CATALOG_READ_ROLES),
        ("GET", "/v1/tables/{table_id}/constraints", CATALOG_READ_ROLES),
        ("GET", "/v1/tables/{table_id}/description", CATALOG_READ_ROLES),
        ("GET", "/v1/tables/{table_id}/column-documentation", CATALOG_READ_ROLES),
        ("GET", "/v1/organizations/{organization_id}/catalog/rows", CATALOG_READ_ROLES),
        ("GET", "/v1/projects/{project_id}/context-products", CONTEXT_PRODUCT_READERS),
        ("GET", "/v1/context-product-versions/{version_id}", CONTEXT_PRODUCT_READERS),
        ("POST", "/graphql", graphql_api.GRAPHQL_ROUTE_ROLES),
    ],
)
def test_each_field_requires_the_roles_its_rest_route_declares(
    method: str, path: str, roles: tuple[str, ...]
) -> None:
    """The role sets are read back from the closures FastAPI wired into the live routes, so
    a REST route that narrows or widens its roles fails here until GraphQL follows."""
    assert _route_roles(method, path) == tuple(sorted(roles))
    assert set(GRAPHQL_ENDPOINT_ROLES) == (
        set(DATASOURCE_READ_ROLES) | set(CATALOG_READ_ROLES) | set(CONTEXT_PRODUCT_READERS)
    )
    # R11-GQL02: the route also admits whoever may execute a governed tool or read an
    # execution receipt -- and nobody else. Each field still enforces its own set.
    assert set(graphql_api.GRAPHQL_ROUTE_ROLES) == (
        set(GRAPHQL_ENDPOINT_ROLES) | set(TOOL_EXECUTION_ROLES) | set(RECEIPT_OVERSIGHT_ROLES)
    )


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


_GRAPHQL_MODULES = ("graphql_api", "graphql_limits", "graphql_reads", "graphql_schema")


def test_no_graphql_module_imports_a_router_the_gateway_or_a_connector() -> None:
    """Resolvers reuse domain services: never a router (no HTTP loopback, no handler
    imports), never the query gateway, never a connector."""
    offenders = []
    for module in _GRAPHQL_MODULES:
        for imported in _module_imports(REPO_ROOT / "src" / "aida" / f"{module}.py"):
            is_router = (imported.endswith("_api") and imported != "aida.graphql_api") or (
                imported == "aida.api"
                or imported.endswith(".router")
                or imported == "aida.mcp_server"
            )
            if (
                is_router
                or imported.startswith("aida.connectors")
                or imported
                in {"aida.query_gateway", "aida.sql_guard", "aida.procedure_tool_runtime"}
            ):
                offenders.append(f"aida.{module} -> {imported}")
    assert offenders == []


# Every name on the way to a source: the gateway itself, the one module allowed to open
# a connector's execution session (INV-2), and the executor protocol's two methods.
_SOURCE_EXECUTION_CALLS = frozenset(
    {
        "QueryExecutionGateway",
        "open_execution_session",
        "execute_read_query",
        "estimate_read_query",
    }
)
# What every resolver must reach instead: the shared read service.
_READ_SERVICE_CALLS = frozenset(
    {
        "get_datasource",
        "get_table",
        "list_datasources",
        "list_tables",
        "list_organization_tables",
        "list_datasource_tables",
        "list_columns",
        "list_constraints",
        "table_description",
        "column_business_description",
        "referenced_table",
    }
)
_RESOLVERS = [
    ("aida.graphql_schema", f"{owner}.{name}")
    for owner, names in {
        "Query": ("datasource", "datasources", "table", "tables"),
        "DataSource": ("tables",),
        "Table": ("datasource", "columns", "constraints", "description"),
        "Column": ("business_description",),
        "Constraint": ("referenced_table",),
    }.items()
    for name in names
]


@pytest.mark.parametrize(("module", "resolver"), _RESOLVERS)
def test_no_resolver_reaches_source_execution(module: str, resolver: str) -> None:
    """A GraphQL query must not trigger source SQL. Statically: no resolver's call graph
    reaches the gateway or a connector's execution session."""
    assert reaches_call(module, resolver, _READ_SERVICE_CALLS), (
        f"{resolver} no longer calls into aida.graphql_reads; the scan below would be vacuous"
    )
    assert not reaches_call(module, resolver, _SOURCE_EXECUTION_CALLS)


def test_the_source_execution_scan_can_see_a_source_call() -> None:
    """The negative scan above is worth what this is: the gateway's own `execute` does
    reach the executor, so a resolver that called it would be reported."""
    assert reaches_call(
        "aida.query_gateway", "QueryExecutionGateway.execute", _SOURCE_EXECUTION_CALLS
    )


def test_the_surface_matrix_maps_every_graphql_field_to_its_controls() -> None:
    """The route's own handler dispatches resolvers dynamically, so the surface-control
    matrix carries a derived row per resolver; each shows the decision its REST twin makes."""
    from scripts.generate_surface_control_matrix import collect_rows

    rows = {row.surface: row for row in collect_rows() if row.family == "GRAPHQL"}
    assert "`POST /graphql`" in rows
    ungated = {"Query.datasource", "Query.datasources", "Table.datasource"}
    for module, resolver in _RESOLVERS:
        owner, method = resolver.split(".")
        row = rows[f"`GRAPHQL {owner}.{_camel(method)}`"]
        assert row.handler == f"{module}.{resolver}"
        assert (row.tenant_check, row.side_effects, row.writes_audit) == ("yes", "read", "no")
        assert row.workspace_check == ("no" if resolver in ungated else "yes"), resolver
        expected = DATASOURCE_READ_ROLES if resolver in ungated else CATALOG_READ_ROLES
        assert row.roles == ", ".join(sorted(expected)), resolver


async def test_a_full_query_never_opens_a_source_connection(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behaviourally: every way to a source is made to explode, and the widest query the
    schema allows still answers."""
    import aida.connectors.execution_access as execution_access
    import aida.query_gateway as query_gateway
    from aida.connectors.registry import connector_registry

    def _explode(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a GraphQL read reached a source")

    monkeypatch.setattr(execution_access, "open_execution_session", _explode)
    monkeypatch.setattr(query_gateway.QueryExecutionGateway, "execute", _explode)
    monkeypatch.setattr(connector_registry, "create", _explode)
    response = await _gql(http, _everything(estate), _headers(estate.org))
    assert response.status_code == 200
    assert response.json()["data"]["table"]["name"] == "t_000"


# --- GQL-A: REST and GraphQL agree -------------------------------------------

_TABLE_FIELDS = "id datasourceId schemaId name objectType status fingerprint"
_DATASOURCE_FIELDS = (
    "id organizationId projectId lineOfBusinessId dataDomainId name connectorType dialect "
    "environment networkZone status maxConcurrency createdAt updatedAt"
)


async def test_a_datasource_by_id_is_the_rest_datasource(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    headers = _headers(estate.org)
    rest = await http.get(f"/v1/datasources/{estate.open_ds.id}", headers=headers)
    graphql = await _gql(
        http,
        f"query Q($id: ID!) {{ datasource(id: $id) {{ {_DATASOURCE_FIELDS} }} }}",
        headers,
        variables={"id": str(estate.open_ds.id)},
    )
    assert rest.status_code == 200
    node = graphql.json()["data"]["datasource"]
    _assert_same_objects([rest.json()], [node])
    assert set(node) == set(_DATASOURCE_FIELDS.split())
    assert "credentialReference" not in json.dumps(graphql.json())


async def test_the_datasource_listing_is_the_rest_listing(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    headers = _headers(estate.org)
    rest = (
        await http.get(f"/v1/organizations/{estate.org.id}/datasources?limit=100", headers=headers)
    ).json()
    walked: list[dict[str, Any]] = []
    after: str | None = None
    totals: list[int | None] = []
    while True:
        body = (
            await _gql(
                http,
                "query Q($after: String) { datasources(first: 2, after: $after) { totalCount "
                f"pageInfo {{ hasNextPage endCursor }} nodes {{ {_DATASOURCE_FIELDS} }} }} }}",
                headers,
                variables={"after": after},
            )
        ).json()["data"]["datasources"]
        walked += body["nodes"]
        totals.append(body["totalCount"])
        if not body["pageInfo"]["hasNextPage"]:
            break
        after = body["pageInfo"]["endCursor"]
    _assert_same_objects(rest["items"], walked)
    assert [item["name"] for item in walked] == sorted(item["name"] for item in walked)
    assert totals[0] == rest["total"] == 5
    assert all(total is None for total in totals[1:]), "totals are first-page only, as REST"


async def _rest_walk(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], limit: int
) -> tuple[list[list[dict[str, Any]]], list[str | None], int | None]:
    pages: list[list[dict[str, Any]]] = []
    cursors: list[str | None] = []
    total: int | None = None
    cursor: str | None = None
    separator = "&" if "?" in url else "?"
    while True:
        query = f"{url}{separator}limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        response = await client.get(query, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        if total is None:
            total = body["total"]
        if body["items"]:
            pages.append(body["items"])
            cursors.append(body["next_cursor"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    return pages, cursors, total


async def _graphql_walk(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    query: str,
    path: tuple[str, ...],
    variables: dict[str, Any],
) -> tuple[list[list[dict[str, Any]]], list[str | None], int | None]:
    pages: list[list[dict[str, Any]]] = []
    cursors: list[str | None] = []
    total: int | None = None
    after: str | None = None
    while True:
        response = await _gql(client, query, headers, variables={**variables, "after": after})
        body = response.json()
        assert "errors" not in body, body
        connection = body["data"]
        for key in path:
            connection = connection[key]
        if total is None:
            total = connection["totalCount"]
        pages.append(connection["nodes"])
        cursors.append(connection["pageInfo"]["endCursor"])
        if not connection["pageInfo"]["hasNextPage"]:
            break
        after = connection["pageInfo"]["endCursor"]
    return pages, cursors, total


def _assert_same_walk(
    rest: tuple[list[list[dict[str, Any]]], list[str | None], int | None],
    graphql: tuple[list[list[dict[str, Any]]], list[str | None], int | None],
) -> None:
    rest_pages, rest_cursors, rest_total = rest
    graphql_pages, graphql_cursors, graphql_total = graphql
    assert rest_total == graphql_total
    assert len(rest_pages) == len(graphql_pages)
    for rest_page, graphql_page in zip(rest_pages, graphql_pages, strict=True):
        _assert_same_objects(rest_page, graphql_page)
    # Page for page, the cursor that continues the walk is the same opaque string.
    for rest_cursor, graphql_cursor in zip(rest_cursors, graphql_cursors, strict=True):
        if rest_cursor is not None:
            assert rest_cursor == graphql_cursor


@pytest.mark.parametrize(
    ("rest_filter", "graphql_filter"),
    [
        ("", {}),
        ("q=t_01", {"q": "t_01"}),
        ("object_type=VIEW", {"objectType": "VIEW"}),
        ("status=ALL", {"status": "ALL"}),
        ("q=CUSTOMER", {"q": "CUSTOMER"}),
    ],
)
async def test_a_datasource_table_walk_is_the_rest_walk(
    http: httpx.AsyncClient, estate: Estate, rest_filter: str, graphql_filter: dict[str, str]
) -> None:
    """Same tables, same order, same page boundaries, same cursors, same total -- for the
    filters REST supports, including a search that matches a source description."""
    headers = _headers(estate.org)
    url = f"/v1/datasources/{estate.open_ds.id}/tables" + (f"?{rest_filter}" if rest_filter else "")
    rest = await _rest_walk(http, url, headers, limit=7)
    arguments = ", ".join(f"{name}: ${name}" for name in graphql_filter)
    declarations = "".join(f", ${name}: String!" for name in graphql_filter)
    query = (
        f"query Q($id: ID!, $after: String{declarations}) {{ datasource(id: $id) {{ "
        f"tables(first: 7, after: $after{', ' + arguments if arguments else ''}) {{ totalCount "
        f"pageInfo {{ hasNextPage endCursor }} nodes {{ {_TABLE_FIELDS} }} }} }} }}"
    )
    graphql = await _graphql_walk(
        http,
        headers,
        query,
        ("datasource", "tables"),
        {"id": str(estate.open_ds.id), **graphql_filter},
    )
    _assert_same_walk(rest, graphql)
    assert sum(len(page) for page in graphql[0]) > 0


async def test_the_top_level_tables_field_with_a_datasource_is_the_same_walk(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    headers = _headers(estate.org)
    rest = await _rest_walk(http, f"/v1/datasources/{estate.open_ds.id}/tables", headers, limit=10)
    graphql = await _graphql_walk(
        http,
        headers,
        "query Q($id: ID!, $after: String) { tables(datasourceId: $id, first: 10, after: $after) "
        f"{{ totalCount pageInfo {{ hasNextPage endCursor }} nodes {{ {_TABLE_FIELDS} }} }} }}",
        ("tables",),
        {"id": str(estate.open_ds.id)},
    )
    _assert_same_walk(rest, graphql)


@pytest.mark.parametrize(
    ("child", "fields"),
    [
        (
            "columns",
            "id name ordinalPosition physicalType nullable classification "
            "classificationSource status sourceDescription",
        ),
        (
            "constraints",
            "id tableId name constraintType columns referencedTableId referencedColumns status",
        ),
    ],
)
async def test_a_table_child_walk_is_the_rest_walk(
    http: httpx.AsyncClient, estate: Estate, child: str, fields: str
) -> None:
    headers = _headers(estate.org)
    rest = await _rest_walk(http, f"/v1/tables/{estate.documented.id}/{child}", headers, limit=5)
    graphql = await _graphql_walk(
        http,
        headers,
        f"query Q($id: ID!, $after: String) {{ table(id: $id) {{ {child}(first: 5, after: $after) "
        f"{{ totalCount pageInfo {{ hasNextPage endCursor }} nodes {{ {fields} }} }} }} }}",
        ("table", child),
        {"id": str(estate.documented.id)},
    )
    _assert_same_walk(rest, graphql)


async def test_batched_nested_columns_are_each_tables_rest_first_page(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """Twenty tables' first column pages come from one batched statement, and each one is
    exactly the page `GET /v1/tables/{id}/columns?limit=3` returns for that table."""
    headers = _headers(estate.org)
    estate.statements.clear()
    body = (
        await _gql(
            http,
            "query Q($id: ID!) { datasource(id: $id) { tables(first: 20) { nodes { id "
            "columns(first: 3) { totalCount pageInfo { hasNextPage endCursor } "
            "nodes { id name ordinalPosition } } } } } }",
            headers,
            variables={"id": str(estate.open_ds.id)},
        )
    ).json()
    column_statements = [s for s in estate.statements if "FROM metadata_column" in s]
    tables = body["data"]["datasource"]["tables"]["nodes"]
    assert len(tables) == 20
    for table in tables:
        rest = (await http.get(f"/v1/tables/{table['id']}/columns?limit=3", headers=headers)).json()
        _assert_same_objects(rest["items"], table["columns"]["nodes"])
        assert table["columns"]["totalCount"] == rest["total"]
        if rest["next_cursor"] is not None:
            assert table["columns"]["pageInfo"]["endCursor"] == rest["next_cursor"]
    # One windowed page statement and one grouped count, not two per table.
    assert len(column_statements) == 2, column_statements


async def test_descriptions_are_the_rest_descriptions(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    headers = _headers(estate.org)
    table_id = estate.documented.id
    rest_table = (await http.get(f"/v1/tables/{table_id}/description", headers=headers)).json()
    rest_columns = (
        await http.get(f"/v1/tables/{table_id}/column-documentation?limit=100", headers=headers)
    ).json()["items"]
    body = (
        await _gql(
            http,
            "query Q($id: ID!) { table(id: $id) { description { tableId name sourceDescription "
            "readme readmeVersion approvedBy approvedAt withdrawnReadme } "
            "columns(first: 20) { nodes { id businessDescription { description version "
            "approvedBy approvedAt } } } } }",
            headers,
            variables={"id": str(table_id)},
        )
    ).json()["data"]["table"]
    _assert_same_objects([rest_table], [body["description"]])
    assert body["description"]["readme"] == "Customer master, version 2."
    by_id = {column["id"]: column["businessDescription"] for column in body["columns"]["nodes"]}
    for column in rest_columns:
        described = by_id[column["column_id"]]
        if column["business_description"] is None:
            assert described is None
        else:
            assert described["description"] == column["business_description"]
            assert described["version"] == column["description_version"]
            assert described["approvedBy"] == column["description_approved_by"]
    assert by_id[str(estate.documented_column.id)] is not None


async def test_the_organization_listing_is_the_catalog_rows(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """`GET /v1/organizations/{id}/catalog/rows` and the organization-wide `tables` field
    walk the same readable tables in the same order. The closed datasource is dropped by
    both; GraphQL drops it before paging (so its pages are full) and before counting."""
    headers = _headers(estate.org)
    rest_pages, _, _ = await _rest_walk(
        http, f"/v1/organizations/{estate.org.id}/catalog/rows", headers, limit=9
    )
    rest_ids = [item["id"] for page in rest_pages for item in page]
    graphql_pages, _, graphql_total = await _graphql_walk(
        http,
        headers,
        "query Q($after: String) { tables(first: 9, after: $after) { totalCount "
        "pageInfo { hasNextPage endCursor } nodes { id datasourceId } } }",
        ("tables",),
        {},
    )
    graphql_ids = [node["id"] for page in graphql_pages for node in page]
    assert graphql_ids == rest_ids
    assert graphql_total == len(graphql_ids)
    assert str(estate.closed_tables[0].id) not in graphql_ids


@pytest.mark.parametrize(
    ("case", "rest_status", "code", "reason"),
    [
        ("closed-datasource-tables", 403, "FORBIDDEN", "NO_WORKSPACE_MEMBERSHIP"),
        ("foreign-tenant-table", 403, "FORBIDDEN", "CROSS_ORGANIZATION"),
        ("missing-table", 404, "NOT_FOUND", None),
        ("data-admin-tables", 403, "FORBIDDEN", "ROLE_REQUIRED"),
        # REST's role gate runs before the handler looks anything up, so a missing id
        # is still a 403 for a role the route does not admit -- not a 404.
        ("data-admin-tables-of-a-missing-datasource", 403, "FORBIDDEN", "ROLE_REQUIRED"),
    ],
)
async def test_the_same_identity_is_refused_the_same_way(
    http: httpx.AsyncClient,
    estate: Estate,
    case: str,
    rest_status: int,
    code: str,
    reason: str | None,
) -> None:
    """Refusal parity: where REST answers 403 or 404, GraphQL answers FORBIDDEN or
    NOT_FOUND on that field, carrying the reason code REST put in its 403 when it has one."""
    headers = _headers(estate.org)
    if case == "closed-datasource-tables":
        rest_url = f"/v1/datasources/{estate.closed_ds.id}/tables"
        query = "query Q($id: ID!) { datasource(id: $id) { name tables { totalCount } } }"
        variables = {"id": str(estate.closed_ds.id)}
    elif case == "foreign-tenant-table":
        rest_url = f"/v1/tables/{estate.foreign_table.id}/columns"
        query = "query Q($id: ID!) { table(id: $id) { name } }"
        variables = {"id": str(estate.foreign_table.id)}
    elif case == "missing-table":
        missing = uuid4()
        rest_url = f"/v1/tables/{missing}/columns"
        query = "query Q($id: ID!) { table(id: $id) { name } }"
        variables = {"id": str(missing)}
    elif case == "data-admin-tables-of-a-missing-datasource":
        missing = uuid4()
        headers = _headers(estate.org, roles="DataAdmin")
        rest_url = f"/v1/datasources/{missing}/tables"
        query = "query Q($id: ID!) { tables(datasourceId: $id) { totalCount } }"
        variables = {"id": str(missing)}
    else:
        headers = _headers(estate.org, roles="DataAdmin")
        rest_url = f"/v1/datasources/{estate.open_ds.id}/tables"
        query = "query Q($id: ID!) { datasource(id: $id) { name tables { totalCount } } }"
        variables = {"id": str(estate.open_ds.id)}
    rest = await http.get(rest_url, headers=headers)
    assert rest.status_code == rest_status
    if reason == "NO_WORKSPACE_MEMBERSHIP":
        assert rest.json()["detail"] == reason
    body = (await _gql(http, query, headers, variables=variables)).json()
    assert _codes(body) == [(code, reason)]


async def test_a_data_admin_reads_a_datasource_but_not_its_tables_exactly_as_over_rest(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    headers = _headers(estate.org, roles="DataAdmin")
    assert (
        await http.get(f"/v1/datasources/{estate.open_ds.id}", headers=headers)
    ).status_code == 200
    body = (
        await _gql(
            http,
            "query Q($id: ID!) { datasource(id: $id) { name tables { totalCount } } }",
            headers,
            variables={"id": str(estate.open_ds.id)},
        )
    ).json()
    assert body["data"]["datasource"] == {"name": "alpha-open", "tables": None}


# --- GQL-B: nothing crosses a boundary ---------------------------------------


def _assert_unnamed(body: Any) -> None:
    text = json.dumps(body)
    for sentinel in _NAME_SENTINELS:
        assert sentinel not in text, f"a refused object's name ({sentinel}) reached the caller"


async def test_a_direct_id_from_another_tenant_is_refused_and_unnamed(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    body = (
        await _gql(
            http,
            "query Q($t: ID!, $d: ID!) { table(id: $t) { name columns { totalCount } } "
            "datasource(id: $d) { name } }",
            _headers(estate.org),
            variables={
                "t": str(estate.foreign_table.id),
                "d": str(estate.foreign_table.datasource_id),
            },
        )
    ).json()
    assert body["data"] == {"table": None, "datasource": None}
    assert sorted(_codes(body)) == [("FORBIDDEN", "CROSS_ORGANIZATION")] * 2
    _assert_unnamed(body)


async def test_an_alias_cannot_refetch_a_forbidden_node_under_a_new_name(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """The classic vector: the same field, the same forbidden id, several names. Every
    alias is decided on its own -- and a loader cache hit for the second alias is decided
    again, not waved through."""
    closed, foreign = estate.closed_tables[0].id, estate.foreign_table.id
    body = (
        await _gql(
            http,
            "query Q($ok: ID!, $closed: ID!, $foreign: ID!) { "
            "ok: table(id: $ok) { name } "
            "evil: table(id: $closed) { name } again: table(id: $closed) { id } "
            "other: table(id: $foreign) { name } "
            "nested: table(id: $ok) { same: datasource { name } "
            "columns(first: 1) { totalCount } } }",
            _headers(estate.org),
            variables={
                "ok": str(estate.documented.id),
                "closed": str(closed),
                "foreign": str(foreign),
            },
        )
    ).json()
    data = body["data"]
    assert data["ok"] == {"name": "t_000"}
    assert data["evil"] is None and data["again"] is None and data["other"] is None
    assert data["nested"]["same"] == {"name": "alpha-open"}
    assert sorted(_codes(body)) == sorted(
        [("FORBIDDEN", "NO_WORKSPACE_MEMBERSHIP")] * 2 + [("FORBIDDEN", "CROSS_ORGANIZATION")]
    )
    _assert_unnamed(body)


async def test_a_nested_edge_is_authorized_on_its_own(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """A foreign key is an edge into another object. Reaching the edge through a readable
    table grants nothing: the referenced table is decided like a direct id."""
    body = (
        await _gql(
            http,
            "query Q($id: ID!) { table(id: $id) { constraints(first: 10) { nodes { name "
            "referencedTableId referencedTable { name } } } } }",
            _headers(estate.org),
            variables={"id": str(estate.documented.id)},
        )
    ).json()
    edges = {node["name"]: node for node in body["data"]["table"]["constraints"]["nodes"]}
    assert edges["fk_open"]["referencedTable"] == {"name": "t_001"}
    assert edges["fk_closed"]["referencedTable"] is None
    assert edges["fk_foreign"]["referencedTable"] is None
    assert edges["fk_dangling"]["referencedTable"] is None
    assert edges["pk_t000"]["referencedTable"] is None
    # The id is what REST already returns for the constraint; the object is what is refused.
    assert edges["fk_closed"]["referencedTableId"] == str(estate.closed_tables[0].id)
    assert sorted(_codes(body)) == [
        ("FORBIDDEN", "CROSS_ORGANIZATION"),
        ("FORBIDDEN", "NO_WORKSPACE_MEMBERSHIP"),
    ]
    _assert_unnamed(body)


async def test_an_aggregate_over_a_forbidden_datasource_carries_no_count(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    body = (
        await _gql(
            http,
            "query Q($id: ID!) { datasource(id: $id) { name tables { totalCount "
            "pageInfo { hasNextPage } } } }",
            _headers(estate.org),
            variables={"id": str(estate.closed_ds.id)},
        )
    ).json()
    assert body["data"]["datasource"] == {"name": "beta-closed", "tables": None}
    assert "totalCount" not in json.dumps(body["data"])


async def test_organization_wide_counts_and_pages_exclude_forbidden_datasources_first(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """Forbidden nodes are filtered before counts and pagination: the total counts only
    readable tables, and every page is full until the last one."""
    readable_active = [
        t for t in (*estate.open_tables, *estate.extra_tables) if t.status == "ACTIVE"
    ]
    pages, _, total = await _graphql_walk(
        http,
        _headers(estate.org),
        "query Q($after: String) { tables(first: 8, after: $after) { totalCount "
        "pageInfo { hasNextPage endCursor } nodes { id name datasourceId } } }",
        ("tables",),
        {},
    )
    assert total == len(readable_active)
    assert all(len(page) == 8 for page in pages[:-1]), "a forbidden row shortened a page"
    seen = [node for page in pages for node in page]
    assert sorted(node["id"] for node in seen) == sorted(str(t.id) for t in readable_active)
    assert str(estate.closed_ds.id) not in {node["datasourceId"] for node in seen}
    _assert_unnamed(pages)


async def test_the_loader_cache_is_never_shared_between_callers(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """A table one caller loaded is not served to the next caller from a cache: each
    request has its own scope, and the second caller is decided as themselves."""
    query = "query Q($id: ID!) { table(id: $id) { name } }"
    variables = {"id": str(estate.foreign_table.id)}
    first = (await _gql(http, query, _headers(estate.other_org), variables=variables)).json()
    assert first["data"]["table"]["name"].startswith(FOREIGN_NAME)
    second = (await _gql(http, query, _headers(estate.org), variables=variables)).json()
    assert second["data"]["table"] is None
    assert _codes(second) == [("FORBIDDEN", "CROSS_ORGANIZATION")]


async def test_every_request_scope_has_its_own_loaders(estate: Estate) -> None:
    context = SecurityContext(
        principal_id="p",
        principal_type="USER",
        organization_id=estate.org.id,
        roles=frozenset({"Viewer"}),
    )
    settings = Settings(_env_file=None)
    one = open_read_scope(
        session=estate.db, context=context, settings=settings, organization_id=estate.org.id
    )
    two = open_read_scope(
        session=estate.db, context=context, settings=settings, organization_id=estate.org.id
    )
    for name in (
        "datasources",
        "tables",
        "column_pages",
        "constraint_pages",
        "approved_documentation",
    ):
        assert getattr(one, name) is not getattr(two, name)
    assert one.decisions is not two.decisions and one.lock is not two.lock


async def test_an_organization_listing_over_too_many_datasources_is_refused(estate: Estate) -> None:
    context = SecurityContext(
        principal_id="p",
        principal_type="USER",
        organization_id=estate.org.id,
        roles=frozenset({"Viewer"}),
    )
    scope = open_read_scope(
        session=estate.db,
        context=context,
        settings=Settings(_env_file=None),
        organization_id=estate.org.id,
        limits=GraphQLLimits(max_scope_datasources=1),
    )
    with pytest.raises(ReadRefused) as refused:
        await list_organization_tables(
            scope, first=5, after=None, q=None, object_type=None, status="ACTIVE"
        )
    assert (refused.value.code, refused.value.reason) == ("SCOPE_TOO_BROAD", "NAME_A_DATASOURCE")


# --- GQL-C: refused before any work ------------------------------------------


def _refused_documents(
    estate: Estate,
) -> list[tuple[str, dict[str, Any] | list[Any] | bytes, str, int]]:
    ds = str(estate.open_ds.id)
    many_aliases = " ".join(f'a{i}: datasource(id: "{ds}") {{ name }}' for i in range(51))
    ten_aliases = " ".join(f"n{i}: name" for i in range(10))
    spread_six = " ".join("...F" for _ in range(6))
    doubling = (
        "".join(f"fragment F{i} on DataSource {{ ...F{i + 1} ...F{i + 1} }} " for i in range(20))
        + "fragment F20 on DataSource { name }"
    )
    return [
        (
            "depth-7",
            {
                "operationName": "Q",
                "query": f'query Q {{ datasource(id: "{ds}") {{ tables(first: 1) {{ nodes {{ '
                "constraints(first: 1) { nodes { referencedTable { name } } } } } } }",
            },
            "DEPTH_LIMIT_EXCEEDED",
            400,
        ),
        (
            "aliases-51",
            {"operationName": "Q", "query": f"query Q {{ {many_aliases} }}"},
            "ALIAS_LIMIT_EXCEEDED",
            400,
        ),
        (
            "alias-repetition-through-a-fragment",
            {
                "operationName": "Q",
                "query": f'query Q {{ datasource(id: "{ds}") {{ {spread_six} }} }} '
                f"fragment F on DataSource {{ {ten_aliases} }}",
            },
            "ALIAS_LIMIT_EXCEEDED",
            400,
        ),
        (
            "page-101",
            {"operationName": "Q", "query": "query Q { datasources(first: 101) { totalCount } }"},
            "PAGE_SIZE_EXCEEDED",
            400,
        ),
        (
            "page-through-a-variable",
            {
                "operationName": "Q",
                "query": "query Q($n: Int!) { datasources(first: $n) { totalCount } }",
                "variables": {"n": 5000},
            },
            "PAGE_SIZE_EXCEEDED",
            400,
        ),
        (
            "nested-fan-out",
            {
                "operationName": "Q",
                "query": "query Q { tables(first: 100) { nodes { "
                "columns(first: 100) { nodes { name } } } } }",
            },
            "NODE_BUDGET_EXCEEDED",
            400,
        ),
        (
            "fan-out-by-alias-repetition",
            {
                "operationName": "Q",
                "query": "query Q { "
                + " ".join(f"p{i}: tables(first: 100) {{ nodes {{ id }} }}" for i in range(5))
                + " }",
            },
            "NODE_BUDGET_EXCEEDED",
            400,
        ),
        (
            "fragment-cycle",
            {
                "operationName": "Q",
                "query": f'query Q {{ datasource(id: "{ds}") {{ ...A }} }} '
                "fragment A on DataSource { name ...B } fragment B on DataSource { id ...A }",
            },
            "FRAGMENT_CYCLE",
            400,
        ),
        (
            "exponential-fragments",
            {
                "operationName": "Q",
                "query": f'query Q {{ datasource(id: "{ds}") {{ ...F0 }} }} {doubling}',
            },
            "DOCUMENT_TOO_COMPLEX",
            400,
        ),
        (
            "token-ceiling",
            {"operationName": "Q", "query": "query Q { " + "__typename " * 2100 + "}"},
            "DOCUMENT_TOO_LARGE",
            400,
        ),
        (
            "request-bytes",
            b'{"operationName":"Q","query":"' + b" " * 40_000 + b'query Q { __typename }"}',
            "REQUEST_TOO_LARGE",
            413,
        ),
        (
            "http-batch",
            [{"operationName": "Q", "query": "query Q { __typename }"}],
            "BATCHING_NOT_SUPPORTED",
            400,
        ),
        ("no-operation-name", {"query": "query Q { __typename }"}, "OPERATION_NAME_REQUIRED", 400),
        (
            "two-operations",
            {"operationName": "A", "query": "query A { __typename } query B { __typename }"},
            "MULTIPLE_OPERATIONS",
            400,
        ),
        (
            "wrong-operation-name",
            {"operationName": "Z", "query": "query Q { __typename }"},
            "OPERATION_NOT_FOUND",
            400,
        ),
        (
            "mutation",
            {"operationName": "M", "query": "mutation M { __typename }"},
            "EXECUTION_ROOT_INVALID",
            400,
        ),
        (
            "subscription",
            {"operationName": "S", "query": "subscription S { __typename }"},
            "OPERATION_NOT_SUPPORTED",
            400,
        ),
        (
            "introspection",
            {"operationName": "Q", "query": "query Q { __schema { types { name } } }"},
            "INTROSPECTION_DISABLED",
            400,
        ),
        (
            "argument-length",
            {
                "operationName": "Q",
                "query": 'query Q { datasources(q: "%s") { totalCount } }' % ("x" * 600),
            },
            "ARGUMENT_TOO_LONG",
            400,
        ),
        (
            "variable-length",
            {
                "operationName": "Q",
                "query": "query Q($q: String) { datasources(q: $q) { totalCount } }",
                "variables": {"q": "y" * 600},
            },
            "ARGUMENT_TOO_LONG",
            400,
        ),
        (
            "unknown-field",
            {"operationName": "Q", "query": "query Q { secrets { name } }"},
            "VALIDATION_FAILED",
            400,
        ),
        (
            "syntax",
            {"operationName": "Q", "query": "query Q { datasource(id: "},
            "DOCUMENT_INVALID",
            400,
        ),
        (
            "unknown-body-key",
            {"operationName": "Q", "query": "query Q { __typename }", "queryId": "stored-1"},
            "REQUEST_INVALID",
            400,
        ),
    ]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            b'{"query":"query Q { __typename }","operationName":"Q","extensions":'
            + b"[" * 2000
            + b"0"
            + b"]" * 2000
            + b"}",
            "REQUEST_INVALID",
        ),
        (
            json.dumps(
                {
                    "query": "query Q { " + "a { " * 400 + "__typename" + " }" * 401,
                    "operationName": "Q",
                }
            ).encode(),
            "DOCUMENT_TOO_COMPLEX",
        ),
        (
            b'{"query":"query Q { __typename }","operationName":"\\ud800"}',
            "REQUEST_INVALID",
        ),
    ],
    ids=["nested-json", "nested-graphql", "invalid-unicode-operation"],
)
async def test_parser_failures_are_refused_before_database_work(
    http: httpx.AsyncClient, estate: Estate, body: bytes, code: str
) -> None:
    assert len(body) < DEFAULT_LIMITS.max_request_bytes
    estate.statements.clear()
    response = await http.post(
        "/graphql",
        content=body,
        headers={**_headers(estate.org), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["errors"][0]["extensions"]["code"] == code
    assert not estate.statements


def test_json_parser_stack_exhaustion_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Interpreter stack limits vary; exercise the decoder's failure deterministically.
    def exhausted_decoder(body: bytes) -> None:
        raise RecursionError

    monkeypatch.setattr(graphql_api.json, "loads", exhausted_decoder)
    with pytest.raises(graphql_api.DocumentRefused) as refused:
        graphql_api._parse_request(b"{}")
    assert refused.value.code == "REQUEST_INVALID"


def _refused_ids() -> list[str]:
    return [
        "depth-7",
        "aliases-51",
        "alias-repetition-through-a-fragment",
        "page-101",
        "page-through-a-variable",
        "nested-fan-out",
        "fan-out-by-alias-repetition",
        "fragment-cycle",
        "exponential-fragments",
        "token-ceiling",
        "request-bytes",
        "http-batch",
        "no-operation-name",
        "two-operations",
        "wrong-operation-name",
        "mutation",
        "subscription",
        "introspection",
        "argument-length",
        "variable-length",
        "unknown-field",
        "syntax",
        "unknown-body-key",
    ]


@pytest.mark.parametrize("case", _refused_ids())
async def test_a_refused_document_runs_no_statement(
    http: httpx.AsyncClient, estate: Estate, case: str
) -> None:
    """GQL-C: every one of these is refused with its stable code, and the database never
    sees a statement for it -- not the datasource lookup, not the gate, nothing."""
    (payload, code, status) = next(
        (payload, code, status)
        for name, payload, code, status in _refused_documents(estate)
        if name == case
    )
    estate.statements.clear()
    headers = _headers(estate.org)
    if isinstance(payload, bytes):
        response = await http.post(
            "/graphql", content=payload, headers={**headers, "content-type": "application/json"}
        )
    else:
        response = await http.post("/graphql", json=payload, headers=headers)
    assert response.status_code == status, response.text
    body = response.json()
    assert [error["extensions"]["code"] for error in body["errors"]] == [code]
    assert "data" not in body
    assert estate.statements == [], f"{case} reached the database: {estate.statements[:3]}"
    assert code in REFUSAL_CODES


async def test_a_document_exactly_at_every_limit_is_served(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """The limits are not off by one, and strawberry's own backstop limiters are no
    tighter than the admission check: depth 6, 50 aliases and a page of 100 all pass."""
    ds = str(estate.open_ds.id)
    # 48 here, plus `deep` and `page` below: exactly 50.
    aliases = " ".join(f'a{i}: datasource(id: "{ds}") {{ name }}' for i in range(48))
    query = (
        f'query Q {{ {aliases} deep: datasource(id: "{ds}") {{ tables(first: 5) {{ nodes {{ '
        "columns(first: 2) { nodes { name } } } } } "
        "page: datasources(first: 100) { totalCount } }"
    )
    response = await _gql(http, query, _headers(estate.org))
    body = response.json()
    assert response.status_code == 200, body
    assert "errors" not in body, body
    cost = body["extensions"]["cost"]
    assert cost["depth"] == DEFAULT_LIMITS.max_depth
    assert cost["aliases"] == DEFAULT_LIMITS.max_aliases
    assert cost["returnedObjects"] <= cost["estimatedNodes"] <= DEFAULT_LIMITS.max_nodes


async def test_the_estimate_bounds_what_is_returned(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    body = (await _gql(http, _everything(estate), _headers(estate.org))).json()
    cost = body["extensions"]["cost"]
    assert 0 < cost["returnedObjects"] <= cost["estimatedNodes"] <= DEFAULT_LIMITS.max_nodes


async def test_the_deadline_bounds_execution(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _slow(*_args: Any, **_kwargs: Any) -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(graphql_reads, "gate", _slow)
    monkeypatch.setattr(graphql_api, "DEFAULT_LIMITS", GraphQLLimits(deadline_seconds=0.05))
    body = (
        await _gql(
            http,
            "query Q($id: ID!) { table(id: $id) { name } }",
            _headers(estate.org),
            variables={"id": str(estate.documented.id)},
        )
    ).json()
    assert body["data"] is None
    assert _codes(body) == [("DEADLINE_EXCEEDED", None)]


async def test_an_oversized_response_is_withheld(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(graphql_api, "DEFAULT_LIMITS", GraphQLLimits(max_response_bytes=300))
    body = (
        await _gql(
            http,
            "query Q($id: ID!) { datasource(id: $id) { tables(first: 50) { nodes { name } } } }",
            _headers(estate.org),
            variables={"id": str(estate.open_ds.id)},
        )
    ).json()
    assert body["data"] is None
    assert _codes(body) == [("RESPONSE_TOO_LARGE", None)]


class _NoLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> None:
        return None


@dataclass
class _InFlight:
    """Session calls in flight, per task. `AsyncSession.scalars` calls `self.execute`, so
    one task nests; only *distinct tasks* inside the session at once is concurrency."""

    depth: dict[asyncio.Task[Any], int] = field(default_factory=dict)
    peak: int = 0


def _probe_concurrency(db: AsyncSession) -> _InFlight:
    """Wrap the session's statement methods to record how many tasks are inside it at
    once. Each wrapper yields to the loop first, so unguarded siblings do overlap."""
    probe = _InFlight()
    for name in ("execute", "scalars", "scalar", "get"):
        original = getattr(db, name)

        async def _wrapped(*args: Any, __original: Any = original, **kwargs: Any) -> Any:
            task = asyncio.current_task()
            assert task is not None
            probe.depth[task] = probe.depth.get(task, 0) + 1
            probe.peak = max(probe.peak, len(probe.depth))
            try:
                await asyncio.sleep(0)
                return await __original(*args, **kwargs)
            finally:
                probe.depth[task] -= 1
                if probe.depth[task] == 0:
                    del probe.depth[task]

        setattr(db, name, _wrapped)
    return probe


_CONCURRENT_QUERY = (
    "query Q($a: ID!, $b: ID!, $c: ID!) { a: table(id: $a) { name description { readme } } "
    "b: table(id: $b) { name } c: datasource(id: $c) { tables(first: 3) { nodes { name } } } "
    "d: datasources { totalCount } }"
)


async def test_one_request_never_runs_two_statements_on_its_session_at_once(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """graphql-core resolves siblings concurrently; an AsyncSession serves one statement
    at a time (asyncpg raises otherwise). The scope's lock serializes them."""
    probe = _probe_concurrency(estate.db)
    body = (
        await _gql(
            http,
            _CONCURRENT_QUERY,
            _headers(estate.org),
            variables={
                "a": str(estate.documented.id),
                "b": str(estate.open_tables[1].id),
                "c": str(estate.open_ds.id),
            },
        )
    ).json()
    assert "errors" not in body, body
    assert probe.peak == 1


async def test_the_concurrency_probe_would_notice_an_unguarded_session(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test above is worth what this one is: with the lock removed, the same query
    does overlap statements, so a peak of 1 above is the lock's doing."""
    original = graphql_api.open_read_scope

    def _unguarded(**kwargs: Any) -> Any:
        scope = original(**kwargs)
        scope.lock = _NoLock()  # type: ignore[assignment]
        return scope

    monkeypatch.setattr(graphql_api, "open_read_scope", _unguarded)
    probe = _probe_concurrency(estate.db)
    await _gql(
        http,
        _CONCURRENT_QUERY,
        _headers(estate.org),
        variables={
            "a": str(estate.documented.id),
            "b": str(estate.open_tables[1].id),
            "c": str(estate.open_ds.id),
        },
    )
    assert probe.peak > 1


# --- INV-6: no values, no bodies, no names in any field or error --------------


def _everything(estate: Estate) -> str:
    """Every field of every type, at every depth the limits allow, plus refused branches."""
    return (
        "query Q { "
        f'datasource(id: "{estate.open_ds.id}") {{ {_DATASOURCE_FIELDS} '
        "tables(first: 30) { totalCount pageInfo { hasNextPage endCursor } "
        f"nodes {{ {_TABLE_FIELDS} }} }} }} "
        "datasources(first: 10) { totalCount nodes { id name } } "
        "tables(first: 50) { totalCount nodes { id name } } "
        f'table(id: "{estate.documented.id}") {{ {_TABLE_FIELDS} datasource {{ id name }} '
        "description { tableId name sourceDescription readme readmeVersion approvedBy approvedAt "
        "withdrawnReadme } "
        "columns(first: 20) { totalCount pageInfo { hasNextPage endCursor } nodes { id name "
        "ordinalPosition physicalType nullable classification classificationSource status "
        "sourceDescription businessDescription { description version approvedBy approvedAt } } } "
        "constraints(first: 10) { totalCount nodes { id tableId name constraintType columns "
        "referencedTableId referencedColumns status referencedTable { id name } } } } "
        f'closed: datasource(id: "{estate.closed_ds.id}") {{ name '
        "tables { totalCount nodes { name } } } "
        f'foreign: table(id: "{estate.foreign_table.id}") {{ name }} '
        "}"
    )


_VALUE_BEARING_FIELD_NAMES = frozenset(
    {
        "defaultExpression",
        "highValue",
        "definition",
        "definitionText",
        "body",
        "sql",
        "sampleValues",
        "samples",
        "rows",
        "minValue",
        "maxValue",
        "topValues",
        "histogram",
        "credentialReference",
        "capabilities",
    }
)


def test_the_schema_exposes_no_value_bearing_field() -> None:
    from graphql import GraphQLObjectType

    from aida.graphql_schema import metadata_schema

    exposed = {
        f"{name}.{field_name}"
        for name, named in metadata_schema._schema.type_map.items()
        if isinstance(named, GraphQLObjectType) and not name.startswith("__")
        for field_name in named.fields
        if field_name in _VALUE_BEARING_FIELD_NAMES
    }
    # R11-GQL02: the one value-bearing field is the execution mutation's result rows,
    # and no query can reach its type -- a receipt is what a query returns.
    assert exposed == {"GovernedExecutionResult.rows"}
    reachable_from_query = _types_reachable(metadata_schema._schema.query_type)
    assert "GovernedExecutionResult" not in reachable_from_query
    mutation = metadata_schema._schema.mutation_type
    assert mutation is not None
    assert set(mutation.fields) == {"executeGovernedTool"}
    assert metadata_schema._schema.subscription_type is None


def _types_reachable(root: Any) -> set[str]:
    from graphql import GraphQLObjectType, get_named_type

    seen: set[str] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        if not isinstance(current, GraphQLObjectType) or current.name in seen:
            continue
        seen.add(current.name)
        pending.extend(get_named_type(field.type) for field in current.fields.values())
    return seen


async def test_no_source_value_body_or_forbidden_name_reaches_a_response_or_a_log(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    with capture_logs() as logs:
        response = await _gql(http, _everything(estate), _headers(estate.org))
    body = response.json()
    assert response.status_code == 200
    assert body["data"]["table"]["name"] == "t_000", "the probe must actually read the table"
    assert body["data"]["closed"]["tables"] is None and body["data"]["foreign"] is None
    text = response.text
    logged = json.dumps(logs, default=str)
    for sentinel in (*_VALUE_SENTINELS, *_NAME_SENTINELS):
        assert sentinel not in text, f"{sentinel} reached the response"
        assert sentinel not in logged, f"{sentinel} reached a log line"
    for error in body["errors"]:
        assert error["message"] == error["extensions"]["code"]


async def test_an_internal_failure_is_masked_in_the_response_and_the_log(
    http: httpx.AsyncClient, estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What a failing statement's exception text looks like: SQL with a bound value in it.
    leaked_statement = "SELECT holder FROM account WHERE owner = 'ZZQ-SENTINEL-INTERNAL-91b'"

    async def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(leaked_statement)

    monkeypatch.setattr(graphql_reads, "latest_withdrawn_table_version", _boom)
    monkeypatch.setattr(graphql_reads, "_latest_approved_documentation", _boom)
    with capture_logs() as logs:
        body = (
            await _gql(
                http,
                "query Q($id: ID!) { table(id: $id) { name description { readme } } }",
                _headers(estate.org),
                variables={"id": str(estate.documented.id)},
            )
        ).json()
    assert body["data"]["table"] == {"name": "t_000", "description": None}
    assert _codes(body) == [("INTERNAL_ERROR", None)]
    assert body["extensions"]["correlationId"]
    assert "ZZQ-SENTINEL-INTERNAL" not in json.dumps(body)
    assert "ZZQ-SENTINEL-INTERNAL" not in json.dumps(logs, default=str)
    assert any(entry.get("code") == "INTERNAL_ERROR" for entry in logs)


def test_every_code_a_resolver_raises_is_published() -> None:
    """A field-level code that is not in the published registry is a contract nobody
    documented. Read from the source so a new `ReadRefused("...")` cannot slip past."""
    raised: set[str] = set()
    for module in _GRAPHQL_MODULES:
        tree = ast.parse((REPO_ROOT / "src" / "aida" / f"{module}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ReadRefused"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                raised.add(str(node.args[0].value))
    assert raised, "the scan found no ReadRefused codes; it is broken"
    assert raised <= set(EXECUTION_ERROR_CODES)


# --- the published schema ------------------------------------------------------


def _generator() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "generate_graphql_schema", REPO_ROOT / "scripts" / "generate_graphql_schema.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_published_schema_and_reference_page_are_current() -> None:
    """The same check `scripts/generate_graphql_schema.py --check` performs. If this fails,
    run the script and commit its output; do not edit the files."""
    generator = _generator()
    assert generator.SDL_PATH.read_text(encoding="utf-8") == generator.render_sdl()
    assert generator.MD_PATH.read_text(encoding="utf-8") == generator.render_markdown()


def test_a_breaking_change_needs_a_new_schema_version() -> None:
    generator = _generator()
    old = "# Schema version: 1\ntype Query { table(id: ID!): String\n  tables: Int }\n"
    removed_field = "# Schema version: 1\ntype Query { table(id: ID!): String }\n"
    bumped = "# Schema version: 2\ntype Query { table(id: ID!): String }\n"
    added_field = (
        "# Schema version: 1\ntype Query { table(id: ID!): String\n  tables: Int\n  more: Int }\n"
    )
    assert generator.compatibility_problems(old, removed_field), "a removed field is breaking"
    assert generator.compatibility_problems(old, bumped) == []
    assert generator.compatibility_problems(old, added_field) == []


def test_the_committed_schema_is_the_served_schema() -> None:
    """The artifact is a contract only if it is what the endpoint serves."""
    from graphql import build_schema, find_breaking_changes, find_dangerous_changes

    from aida.graphql_schema import metadata_schema

    committed = build_schema(
        (REPO_ROOT / "Docs" / "90-reference" / "graphql-schema.graphql").read_text(encoding="utf-8")
    )
    assert find_breaking_changes(committed, metadata_schema._schema) == []
    assert find_dangerous_changes(committed, metadata_schema._schema) == []
