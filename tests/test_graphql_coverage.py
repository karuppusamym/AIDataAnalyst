"""R11-GQL01: coverage over GraphQL is REST's coverage, decided and refused as REST decides it.

Three reads, each the answer of one REST route, compared with that route through the real
application against one in-memory database:

* `contextProductCoverage(versionId)` -- the compiled product's `context.coverage` section and
  the `generated_from.source_freshness` beside it, as `GET /v1/context-product-versions/{id}/
  compile` resolves them. The same entries, keys and order for every reader class; the same
  refusal for every reason the route refuses (role, tenant, lifecycle, consumer role, purpose,
  quality, envelope, unresolved references); recorded as a read, once per request.
* `routineParseCoverage` / `triggerParseCoverage` -- `GET /v1/datasources/{id}/procedures/
  {routine_id}/parse-coverage` and its trigger twin: the same measurement, the same "not
  measured" answer, the same refusals including the datasource's workspace gate (R11-D30).

Plus: the admission check prices every coverage section before anything runs, no resolver
reaches a source, and no routine body or view definition reaches a response.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import count
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

import aida.models  # noqa: F401 - registers every mapped table on Base.metadata
from aida.change_signal_models import MetadataChangeSignal
from aida.envelope_models import MetadataRoutine, MetadataTrigger, MetadataViewDefinition
from aida.graphql_reads import CONTEXT_COMPILER_ROLES, COVERAGE_CONSUMPTION_CHANNEL
from aida.main import app
from aida.models import (
    AgentContract,
    AnalysisRun,
    AssetTermLink,
    AuditEvent,
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    DataSource,
    GlossaryTerm,
    GlossaryTermVersion,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SourceBinding,
    ViewLineageEdge,
    Workspace,
)
from aida.procedure_lineage_models import (
    DeepProcedureLineageEdge,
    RoutineParseCoverage,
    TriggerParseCoverage,
)
from aida.unified_lineage_service import UNIFIED_LINEAGE_READER_ROLES
from aida.workspace_access import ENFORCE
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session
from tests.support.app_surface import reaches_call
from tests.test_graphql_api import (
    _camel,
    _datasource,
    _hierarchy,
    _route_roles,
    _same_value,
    _table,
)

# Strings that cannot occur naturally: a routine body and a view definition. Coverage says
# whether a definition would be released and digests the stored text; it never carries it.
BODY_SENTINEL = "ZZQ-COVERAGE-BODY-41c7"
VIEW_SENTINEL = "ZZQ-COVERAGE-VIEW-8d02"
_PUBLISHED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
_MOVED_AT = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)

# `AuditEvent.id` is a BIGINT PostgreSQL fills from a sequence; SQLite does not.
_audit_ids = count(50_000_000)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(_mapper: object, _connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_ids)


# --- estate ----------------------------------------------------------------------------


@dataclass
class Estate:
    engine: AsyncEngine
    db: AsyncSession
    statements: list[str]
    org: Organization
    other_org: Organization
    project: Project
    warehouse: DataSource
    closed: DataSource
    foreign_ds: DataSource
    versions: dict[str, ContextProductVersion] = field(default_factory=dict)
    routines: dict[str, MetadataRoutine] = field(default_factory=dict)
    triggers: dict[str, MetadataTrigger] = field(default_factory=dict)


async def _product(
    db: AsyncSession,
    project: Project,
    key: str,
    *,
    table_ids: list[UUID],
    status: str = "PUBLISHED",
    version: int = 1,
    routine_ids: list[UUID] | None = None,
    glossary_term_version_ids: list[UUID] | None = None,
    ontology_version_ids: list[UUID] | None = None,
    consumer_roles: list[str] | None = None,
    policy_summary: dict[str, Any] | None = None,
    quality_requirements: dict[str, Any] | None = None,
    lifecycle_status: str = "ACTIVE",
) -> ContextProductVersion:
    product = await db.scalar(
        select(ContextProduct).where(
            ContextProduct.project_id == project.id, ContextProduct.product_key == key
        )
    )
    if product is None:
        product = ContextProduct(
            organization_id=project.organization_id,
            project_id=project.id,
            product_key=key,
            lifecycle_status=lifecycle_status,
            created_by="steward-maker",
        )
        db.add(product)
        await db.flush()
    row = ContextProductVersion(
        organization_id=project.organization_id,
        product_id=product.id,
        version=version,
        status=status,
        name=f"{key} v{version}",
        description="What an agent needs to answer order questions.",
        purpose="Answer order questions.",
        owner_principal="steward-maker",
        table_ids=[str(value) for value in table_ids],
        routine_ids=[str(value) for value in routine_ids or []],
        glossary_term_version_ids=[str(value) for value in glossary_term_version_ids or []],
        ontology_version_ids=[str(value) for value in ontology_version_ids or []],
        allowed_consumer_roles=consumer_roles or ["Analyst"],
        policy_summary=policy_summary or {"source_values": "GATEWAY_ONLY"},
        quality_requirements=quality_requirements or {},
        fingerprint=uuid4().hex + uuid4().hex,
        created_by="steward-maker",
        approved_by="steward-checker" if status != "DRAFT" else None,
        approved_at=_PUBLISHED_AT if status != "DRAFT" else None,
        published_at=_PUBLISHED_AT if status != "DRAFT" else None,
    )
    db.add(row)
    await db.flush()
    return row


def _routine(
    datasource: DataSource,
    schema: MetadataSchema,
    name: str,
    routine_type: str,
    *,
    routine_id: UUID | None = None,
) -> MetadataRoutine:
    return MetadataRoutine(
        id=routine_id or uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        signature="(integer)",
        routine_type=routine_type,
        body_sql_redacted=f"BEGIN INSERT INTO customer SELECT '{BODY_SENTINEL}'; END",
        fingerprint=f"fp-{name}",
    )


def _edge(
    routine: MetadataRoutine,
    source: MetadataTable,
    target: MetadataTable,
    *,
    review_status: str = "ACTIVE",
) -> DeepProcedureLineageEdge:
    return DeepProcedureLineageEdge(
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        statement_ordinal=1,
        source_table=source.name,
        source_column="id",
        target_table=target.name,
        target_column="id",
        source_table_id=source.id,
        target_table_id=target.id,
        is_write=True,
        transformation_type="INSERT_SELECT",
        confidence="HIGH",
        dialect="postgres",
        sql_hash="h" * 64,
        review_status=review_status,
    )


def _measured(routine: MetadataRoutine) -> RoutineParseCoverage:
    return RoutineParseCoverage(
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        parse_completed=False,
        is_read_only=False,
        statement_count=4,
        unparsed_statement_count=1,
        unparsed_reason_codes="DYNAMIC_SQL,UNSUPPORTED_CONSTRUCT",
        dialect="postgres",
        confidence="MEDIUM",
        sql_hash="s" * 64,
        source_mapping_granularity="STATEMENT_RANGE",
        parsed_at=datetime(2026, 9, 12, 8, 30, tzinfo=UTC),
        measured_by="lineage-agent",
    )


def _trigger(
    datasource: DataSource, schema: MetadataSchema, name: str, function: MetadataRoutine | None
) -> MetadataTrigger:
    return MetadataTrigger(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        table_name="orders",
        action_routine=function.name if function is not None else None,
        availability="UNAVAILABLE",
        body_sql_redacted=None,
        unavailable_reason="PostgreSQL keeps no trigger body; see action_routine",
        fingerprint=f"fp-{name}",
    )


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
        org, project = await _hierarchy(db, "BankA")
        other_org, other_project = await _hierarchy(db, "BankB")
        warehouse, sales = await _datasource(db, project, "warehouse")
        lake, raw = await _datasource(db, project, "lake")
        closed, closed_schema = await _datasource(db, project, "closed")
        foreign_ds, foreign_schema = await _datasource(db, other_project, "foreign")

        orders = await _table(db, warehouse, sales, "orders", columns=2)
        customer = await _table(db, warehouse, sales, "customer", columns=2)
        v_orders = await _table(db, warehouse, sales, "v_orders", columns=1, object_type="VIEW")
        mv_customer = await _table(
            db, warehouse, sales, "mv_customer", columns=1, object_type="MATERIALIZED_VIEW"
        )
        landing = await _table(db, lake, raw, "landing", columns=1)
        outside = await _table(db, warehouse, sales, "outside", columns=1)
        foreign_table = await _table(db, foreign_ds, foreign_schema, "f_orders", columns=1)
        db.add_all(
            [
                MetadataViewDefinition(
                    organization_id=org.id,
                    datasource_id=warehouse.id,
                    table_id=v_orders.id,
                    definition_sql_redacted=f"SELECT id FROM orders WHERE note = '{VIEW_SENTINEL}'",  # noqa: S608, E501
                    fingerprint="fp-v-orders",
                ),
                ViewLineageEdge(
                    organization_id=org.id,
                    datasource_id=warehouse.id,
                    source_table="orders",
                    source_column="id",
                    target_table="v_orders",
                    target_column="id",
                    source_table_id=orders.id,
                    target_table_id=v_orders.id,
                    transformation_type="SELECT",
                    confidence="HIGH",
                    dialect="postgres",
                    sql_hash="v" * 64,
                ),
            ]
        )

        # Ids that sort opposite to the names, so an order by anything but REST's (the id)
        # shows up as a different page sequence.
        rebuild = _routine(
            warehouse, sales, "rebuild", "PROCEDURE",
            routine_id=UUID("f0000000-0000-4000-8000-000000000001"),
        )
        refresh = _routine(
            warehouse, sales, "refresh", "FUNCTION",
            routine_id=UUID("10000000-0000-4000-8000-000000000001"),
        )
        hidden = _routine(closed, closed_schema, "hidden", "PROCEDURE")
        foreign_routine = _routine(foreign_ds, foreign_schema, "f_rebuild", "PROCEDURE")
        db.add_all([rebuild, refresh, hidden, foreign_routine])
        await db.flush()
        db.add_all(
            [
                _edge(rebuild, orders, customer),
                # Reads a table the product does not cover: it must not be named or counted.
                _edge(rebuild, outside, customer),
                _edge(refresh, orders, mv_customer, review_status="PROPOSED"),
                _measured(rebuild),
                _measured(hidden),
                _measured(foreign_routine),
            ]
        )
        on_orders = _trigger(warehouse, sales, "trg_orders", refresh)
        unmeasured_trigger = _trigger(warehouse, sales, "trg_customer", None)
        db.add_all([on_orders, unmeasured_trigger])
        await db.flush()
        db.add(
            TriggerParseCoverage(
                organization_id=org.id,
                datasource_id=warehouse.id,
                trigger_id=on_orders.id,
                routine_id=refresh.id,
                parse_completed=True,
                is_read_only=False,
                statement_count=2,
                unparsed_statement_count=0,
                unparsed_reason_codes="",
                dialect="postgres",
                confidence="HIGH",
                sql_hash="t" * 64,
                parsed_at=datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
            )
        )

        # Freshness: a full scan of the warehouse, an incremental one of the lake.
        db.add_all(
            [
                AnalysisRun(
                    organization_id=org.id, datasource_id=warehouse.id, status="COMPLETED",
                    mode="FULL",
                ),
                AnalysisRun(
                    organization_id=org.id, datasource_id=lake.id, status="COMPLETED",
                    mode="INCREMENTAL",
                ),
            ]
        )
        term = GlossaryTerm(organization_id=org.id, term_key="order")
        db.add(term)
        await db.flush()
        term_version = GlossaryTermVersion(
            organization_id=org.id,
            term_id=term.id,
            version=1,
            status="APPROVED",
            display_name="Order",
            definition="A customer's request to buy.",
            created_by="steward-maker",
        )
        db.add_all(
            [
                term_version,
                AssetTermLink(
                    organization_id=org.id, table_id=orders.id, term_id=term.id, linked_by="s"
                ),
            ]
        )
        await db.flush()
        # A covered view's definition moved after publication: the product is stale.
        db.add(
            MetadataChangeSignal(
                organization_id=org.id,
                datasource_id=warehouse.id,
                subject_kind="VIEW",
                subject_id=v_orders.id,
                signal_type="DEFINITION_CHANGED",
                change_class="STRUCTURAL",
                detected_at=_MOVED_AT,
            )
        )

        covered = [orders.id, customer.id, v_orders.id, mv_customer.id, landing.id]
        built = Estate(
            engine=engine,
            db=db,
            statements=statements,
            org=org,
            other_org=other_org,
            project=project,
            warehouse=warehouse,
            closed=closed,
            foreign_ds=foreign_ds,
        )
        built.routines = {
            "rebuild": rebuild,
            "refresh": refresh,
            "hidden": hidden,
            "foreign": foreign_routine,
        }
        built.triggers = {"measured": on_orders, "unmeasured": unmeasured_trigger}
        full = {
            "table_ids": covered,
            "routine_ids": [rebuild.id, refresh.id],
            "glossary_term_version_ids": [term_version.id],
        }
        built.versions = {
            "published": await _product(db, project, "orders-context", **full),
            "draft": await _product(
                db, project, "orders-context", status="DRAFT", version=2, **full
            ),
            "steward-only": await _product(
                db, project, "steward-context", table_ids=[orders.id],
                consumer_roles=["DataSteward"],
            ),
            "purpose-bound": await _product(
                db, project, "fraud-context", table_ids=[orders.id],
                policy_summary={"allowed_purposes": ["fraud review"]},
            ),
            "quality-gated": await _product(
                db, project, "scored-context", table_ids=[orders.id],
                quality_requirements={"minimum_score": 90},
            ),
            "dangling": await _product(
                db, project, "dangling-context", table_ids=[orders.id], routine_ids=[uuid4()]
            ),
            # R11-GQL01: the other two pins the shared resolution refuses when they go stale.
            "dangling-table": await _product(
                db, project, "ghost-table-context", table_ids=[orders.id, uuid4()]
            ),
            "dangling-ontology": await _product(
                db, project, "ghost-ontology-context", table_ids=[orders.id],
                ontology_version_ids=[uuid4()],
            ),
            "retired": await _product(
                db, project, "retired-context", table_ids=[orders.id], status="SUPERSEDED"
            ),
            "inactive-product": await _product(
                db, project, "archived-context", table_ids=[orders.id],
                lifecycle_status="RETIRED",
            ),
            "foreign": await _product(
                db, other_project, "orders-context", table_ids=[foreign_table.id]
            ),
        }
        # The closed datasource sits in an enforcing workspace nobody here belongs to.
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
                datasource_id=closed.id,
                purpose="restricted",
                status="ACTIVE",
                requested_by="seed",
            )
        )
        # An agent whose envelope names another product only.
        db.add(
            AgentContract(
                id=uuid4(),
                organization_id=org.id,
                ai_asset_version_id=uuid4(),
                agent_principal_id="agent:order-bot",
                capability_envelope={
                    "tool_slugs": [],
                    "context_product_ids": ["steward-context"],
                    "write_lanes": [],
                },
                autonomy_tier="T1",
                supervisor_persona="STEWARD",
                kill_scope="AGENT",
                sampling_rate=0.05,
                created_by="agent-owner",
            )
        )
        await db.commit()
        event.listen(engine.sync_engine, "before_cursor_execute", _count)
        yield built
    await engine.dispose()


@pytest_asyncio.fixture
async def http(estate: Estate) -> AsyncIterator[httpx.AsyncClient]:
    previous = dict(app.dependency_overrides)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield estate.db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://coverage.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _headers(
    org: Organization,
    roles: str,
    *,
    principal: str = "coverage-reader",
    principal_type: str = "USER",
) -> dict[str, str]:
    return {
        "X-Principal-Id": principal,
        "X-Principal-Type": principal_type,
        "X-Roles": roles,
        "X-Organization-Id": str(org.id),
    }


async def _gql(
    client: httpx.AsyncClient,
    query: str,
    headers: dict[str, str],
    operation: str,
    **variables: Any,
) -> httpx.Response:
    return await client.post(
        "/graphql",
        json={"query": query, "operationName": operation, "variables": variables},
        headers=headers,
    )


def _codes(body: dict[str, Any]) -> list[tuple[str, str | None]]:
    return [
        (error["extensions"]["code"], error["extensions"].get("reason"))
        for error in body.get("errors", [])
    ]


# --- wiring --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "roles"),
    [
        ("/v1/context-product-versions/{version_id}/compile", CONTEXT_COMPILER_ROLES),
        (
            "/v1/datasources/{datasource_id}/procedures/{routine_id}/parse-coverage",
            UNIFIED_LINEAGE_READER_ROLES,
        ),
        (
            "/v1/datasources/{datasource_id}/triggers/{trigger_id}/parse-coverage",
            UNIFIED_LINEAGE_READER_ROLES,
        ),
    ],
)
def test_each_coverage_field_requires_the_roles_its_rest_route_declares(
    path: str, roles: tuple[str, ...]
) -> None:
    """Read back from the closures FastAPI wired into the live routes: a route that narrows or
    widens its roles fails here until GraphQL follows."""
    assert _route_roles("GET", path) == tuple(sorted(roles))


_COVERAGE_RESOLVERS = [
    "Query.context_product_coverage",
    "Query.routine_parse_coverage",
    "Query.trigger_parse_coverage",
    "ContextProductCoverage.routines",
    "ContextProductCoverage.views",
    "ContextProductCoverage.meaning",
    "ContextProductCoverage.changed_since_published",
    "ContextProductCoverage.source_freshness",
]
_READ_SERVICE_CALLS = frozenset(
    {
        "get_context_product_coverage",
        "list_coverage_items",
        "get_routine_parse_coverage",
        "get_trigger_parse_coverage",
    }
)
_SOURCE_EXECUTION_CALLS = frozenset(
    {"QueryExecutionGateway", "open_execution_session", "execute_read_query", "estimate_read_query"}
)


def test_the_surface_matrix_derives_each_coverage_fields_controls() -> None:
    """The matrix row of each coverage resolver is derived from its call graph: the roles are
    the role set its read service asks (not a label), parse coverage reaches the workspace gate,
    and `contextProductCoverage` writes -- deliberately: an audit event on every read and a
    consumption edge for a PUBLISHED version, as the compile route records a compilation."""
    from scripts.generate_surface_control_matrix import _graphql_rows

    rows = {row.surface: row for row in _graphql_rows()}
    for resolver in _COVERAGE_RESOLVERS:
        owner, method = resolver.split(".")
        row = rows[f"`GRAPHQL {owner}.{_camel(method)}`"]
        parse = "ParseCoverage" in resolver or "parse_coverage" in resolver
        expected = UNIFIED_LINEAGE_READER_ROLES if parse else CONTEXT_COMPILER_ROLES
        assert row.roles == ", ".join(sorted(expected)), resolver
        assert row.tenant_check == "yes", resolver
        if parse:
            assert (row.workspace_check, row.side_effects, row.writes_audit) == (
                "yes",
                "read",
                "no",
            ), resolver
        else:
            assert (row.side_effects, row.writes_audit) == ("writes", "yes"), resolver


@pytest.mark.parametrize("resolver", _COVERAGE_RESOLVERS)
def test_no_coverage_resolver_reaches_source_execution(resolver: str) -> None:
    assert reaches_call("aida.graphql_schema", resolver, _READ_SERVICE_CALLS), resolver
    assert not reaches_call("aida.graphql_schema", resolver, _SOURCE_EXECUTION_CALLS)


# --- contextProductCoverage: the compiled coverage, for every reader ----------------------

_ROUTINE_FIELDS = (
    "id qualifiedName routineType signature status definitionAvailable lineage fullyParsed "
    "readsTableIds writesTableIds definitionDigest description descriptionState"
)
_VIEW_FIELDS = "tableId objectType status definitionAvailable truncated lineage definitionDigest"
_MEANING_FIELDS = "kind versionId key version status current tableIds routineIds"
_CHANGE_FIELDS = "subjectKind subjectId change changeClass"
_FRESHNESS_FIELDS = "datasourceId tableIds lastScanCompletedAt lastFullScanCompletedAt"
_SECTIONS = {
    "routines": ("routines", _ROUTINE_FIELDS),
    "views": ("views", _VIEW_FIELDS),
    "meaning": ("meaning", _MEANING_FIELDS),
    "changedSincePublished": ("changed_since_published", _CHANGE_FIELDS),
    "sourceFreshness": ("source_freshness", _FRESHNESS_FIELDS),
}


def _coverage_query(first: int = 20) -> str:
    sections = " ".join(
        f"{name}(first: {first}) {{ totalCount pageInfo {{ hasNextPage endCursor }} "
        f"nodes {{ {fields} }} }}"
        for name, (_, fields) in _SECTIONS.items()
    )
    return (
        "query Coverage($id: ID!) { contextProductCoverage(versionId: $id) { "
        f"versionId productKey version status {sections} }} }}"
    )


async def _rest_coverage(
    client: httpx.AsyncClient, version_id: UUID, headers: dict[str, str]
) -> httpx.Response:
    return await client.get(
        f"/v1/context-product-versions/{version_id}/compile",
        params={"target": "REST"},
        headers=headers,
    )


def _rest_sections(response: httpx.Response) -> dict[str, list[dict[str, Any]]]:
    """The compiled REST artifact's coverage, plus the freshness beside it. A section the
    compiler leaves out because it has nothing to say is an empty list."""
    body = response.json()
    coverage = json.loads(body["content"])["context"].get("coverage", {})
    return {
        "routines": coverage.get("routines", []),
        "views": coverage.get("views", []),
        "meaning": coverage.get("meaning", []),
        "changed_since_published": coverage.get("changed_since_published", []),
        "source_freshness": body["generated_from"]["source_freshness"],
    }


def _assert_same_entries(rest: list[dict[str, Any]], graphql: list[dict[str, Any]]) -> None:
    """Every key REST gives an entry, under its camelCase name, with the same value -- and no
    key GraphQL has that REST does not."""
    assert len(rest) == len(graphql)
    for rest_entry, graphql_entry in zip(rest, graphql, strict=True):
        assert {_camel(key) for key in rest_entry} == set(graphql_entry)
        for key, value in rest_entry.items():
            assert _same_value(value, graphql_entry[_camel(key)]), (key, value)


@pytest.mark.parametrize(
    "roles",
    ["Analyst", "DataSteward", "MetadataAdmin", "PlatformAdmin"],
)
async def test_coverage_is_the_compiled_coverage_for_every_reader(
    http: httpx.AsyncClient, estate: Estate, roles: str
) -> None:
    version = estate.versions["published"]
    headers = _headers(estate.org, roles)

    rest = await _rest_coverage(http, version.id, headers)
    graphql = await _gql(http, _coverage_query(), headers, "Coverage", id=str(version.id))

    assert rest.status_code == 200, rest.text
    body = graphql.json()
    assert "errors" not in body, body
    read = body["data"]["contextProductCoverage"]
    assert (read["versionId"], read["productKey"], read["version"], read["status"]) == (
        str(version.id),
        "orders-context",
        1,
        "PUBLISHED",
    )
    expected = _rest_sections(rest)
    for name, (section, _) in _SECTIONS.items():
        page = read[name]
        assert page["pageInfo"]["hasNextPage"] is False
        assert page["totalCount"] == len(expected[section])
        _assert_same_entries(expected[section], page["nodes"])
    # Parity over something: every section has entries, so none of the above is vacuous.
    assert {section: len(entries) for section, entries in expected.items()} == {
        "routines": 2,
        "views": 2,
        "meaning": 1,
        "changed_since_published": 1,
        "source_freshness": 2,
    }


async def test_coverage_names_nothing_outside_the_product_and_carries_no_definition(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    graphql = await _gql(
        http,
        _coverage_query(),
        _headers(estate.org, "Analyst"),
        "Coverage",
        id=str(estate.versions["published"].id),
    )
    text = graphql.text
    body = graphql.json()["data"]["contextProductCoverage"]
    rebuild = next(
        node
        for node in body["routines"]["nodes"]
        if node["id"] == str(estate.routines["rebuild"].id)
    )
    outside = await estate.db.scalar(select(MetadataTable).where(MetadataTable.name == "outside"))
    assert outside is not None
    assert str(outside.id) not in text, "a table outside the product was named"
    assert rebuild["readsTableIds"] and rebuild["lineage"] == "ACTIVE"
    assert rebuild["definitionDigest"], "a digest of the stored value-free body, not the body"
    for sentinel in (BODY_SENTINEL, VIEW_SENTINEL):
        assert sentinel not in text


async def test_coverage_pages_by_cursor_in_rests_order(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    version = estate.versions["published"]
    headers = _headers(estate.org, "DataSteward")
    expected = _rest_sections(await _rest_coverage(http, version.id, headers))
    query = (
        "query Page($id: ID!, $after: String) { contextProductCoverage(versionId: $id) { "
        "routines(first: 1, after: $after) { totalCount pageInfo { hasNextPage endCursor } "
        f"nodes {{ {_ROUTINE_FIELDS} }} }} "
        "sourceFreshness(first: 1, after: $after) { totalCount pageInfo { hasNextPage endCursor } "
        f"nodes {{ {_FRESHNESS_FIELDS} }} }} }} }}"
    )
    for name, section in (("routines", "routines"), ("sourceFreshness", "source_freshness")):
        walked: list[dict[str, Any]] = []
        totals: list[int | None] = []
        after: str | None = None
        while True:
            body = (
                await _gql(http, query, headers, "Page", id=str(version.id), after=after)
            ).json()
            page = body["data"]["contextProductCoverage"][name]
            walked += page["nodes"]
            totals.append(page["totalCount"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        _assert_same_entries(expected[section], walked)
        assert len(walked) == 2
        assert totals == [2, None], "totals are first-page only, as every connection gives them"


async def test_a_lifecycle_reader_reads_a_drafts_coverage_as_the_compile_route_does(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    version = estate.versions["draft"]
    headers = _headers(estate.org, "DataSteward")
    rest = await _rest_coverage(http, version.id, headers)
    body = (await _gql(http, _coverage_query(), headers, "Coverage", id=str(version.id))).json()
    assert rest.status_code == 200
    read = body["data"]["contextProductCoverage"]
    assert read["status"] == "DRAFT"
    expected = _rest_sections(rest)
    # Never published: no baseline to be stale against, as REST says.
    assert expected["changed_since_published"] == []
    for name, (section, _) in _SECTIONS.items():
        _assert_same_entries(expected[section], read[name]["nodes"])


# --- contextProductCoverage: the same refusals ----------------------------------------

_REFUSALS: list[tuple[str, str, str, dict[str, str], int, tuple[str, str | None]]] = [
    # (case, version, roles, extra headers, REST status, GraphQL code and reason)
    ("role-not-a-compiler", "published", "Viewer", {}, 403, ("FORBIDDEN", "ROLE_REQUIRED")),
    ("consumer-reads-a-draft", "draft", "Analyst", {}, 404, ("NOT_FOUND", None)),
    ("not-a-consumer-role", "steward-only", "Analyst", {}, 404, ("NOT_FOUND", None)),
    ("purpose-required", "purpose-bound", "Analyst", {}, 404, ("NOT_FOUND", None)),
    ("quality-evidence-missing", "quality-gated", "Analyst", {}, 404, ("NOT_FOUND", None)),
    ("retired", "retired", "Analyst", {}, 404, ("NOT_FOUND", None)),
    ("inactive-product", "inactive-product", "DataSteward", {}, 404, ("NOT_FOUND", None)),
    ("unknown-version", "unknown", "Analyst", {}, 404, ("NOT_FOUND", None)),
    ("another-tenant", "foreign", "Analyst", {}, 403, ("FORBIDDEN", "CROSS_ORGANIZATION")),
    (
        "unresolved-routine",
        "dangling",
        "DataSteward",
        {},
        409,
        ("CONFLICT", "CONTEXT_PRODUCT_REFERENCES_UNRESOLVED"),
    ),
    (
        "unresolved-table",
        "dangling-table",
        "DataSteward",
        {},
        409,
        ("CONFLICT", "CONTEXT_PRODUCT_REFERENCES_UNRESOLVED"),
    ),
    (
        "unresolved-ontology",
        "dangling-ontology",
        "DataSteward",
        {},
        409,
        ("CONFLICT", "CONTEXT_PRODUCT_REFERENCES_UNRESOLVED"),
    ),
    (
        "outside-the-agents-envelope",
        "published",
        "Analyst",
        {"X-Principal-Id": "agent:order-bot", "X-Principal-Type": "AGENT"},
        404,
        ("NOT_FOUND", None),
    ),
]


@pytest.mark.parametrize(
    ("version_key", "roles", "extra", "rest_status", "graphql_code"),
    [case[1:] for case in _REFUSALS],
    ids=[case[0] for case in _REFUSALS],
)
async def test_coverage_is_refused_where_the_compile_route_refuses(
    http: httpx.AsyncClient,
    estate: Estate,
    version_key: str,
    roles: str,
    extra: dict[str, str],
    rest_status: int,
    graphql_code: tuple[str, str | None],
) -> None:
    version_id = estate.versions[version_key].id if version_key in estate.versions else uuid4()
    headers = {**_headers(estate.org, roles), **extra}

    rest = await _rest_coverage(http, version_id, headers)
    body = (await _gql(http, _coverage_query(), headers, "Coverage", id=str(version_id))).json()

    assert rest.status_code == rest_status, rest.text
    assert body["data"]["contextProductCoverage"] is None
    assert _codes(body) == [graphql_code]


async def test_purpose_and_quality_do_not_hold_back_a_lifecycle_reader(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """The compile route's own lifecycle readers -- not the version read's -- skip the
    consumer gates: a MetadataAdmin reads what an Analyst is refused."""
    headers = _headers(estate.org, "MetadataAdmin")
    for key in ("purpose-bound", "quality-gated", "steward-only"):
        version_id = estate.versions[key].id
        rest = await _rest_coverage(http, version_id, headers)
        body = (
            await _gql(http, _coverage_query(), headers, "Coverage", id=str(version_id))
        ).json()
        assert rest.status_code == 200, (key, rest.text)
        assert "errors" not in body, (key, body)


# --- contextProductCoverage: recorded as a read, once per request -----------------------


async def test_a_consumers_coverage_read_is_recorded_once_per_request(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    version = estate.versions["published"]
    query = (
        "query Twice($id: ID!) { a: contextProductCoverage(versionId: $id) { productKey } "
        "b: contextProductCoverage(versionId: $id) { routines { totalCount } } }"
    )
    body = (
        await _gql(http, query, _headers(estate.org, "Analyst"), "Twice", id=str(version.id))
    ).json()
    assert "errors" not in body, body

    edges = (
        await estate.db.scalars(
            select(ContextProductConsumptionEdge).where(
                ContextProductConsumptionEdge.context_product_version_id == version.id
            )
        )
    ).all()
    assert [(edge.channel, edge.principal_id) for edge in edges] == [
        (COVERAGE_CONSUMPTION_CHANNEL, "coverage-reader")
    ]
    audits = (
        await estate.db.scalars(
            select(AuditEvent).where(AuditEvent.action == "context_product.coverage_read")
        )
    ).all()
    assert len(audits) == 1
    assert audits[0].resource_id == str(version.id)


async def test_a_drafts_coverage_read_is_audited_but_is_no_consumption(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """As the compile route: a consumption edge only for a PUBLISHED version."""
    version = estate.versions["draft"]
    body = (
        await _gql(
            http, _coverage_query(), _headers(estate.org, "DataSteward"), "Coverage",
            id=str(version.id),
        )
    ).json()
    assert "errors" not in body, body
    assert (await estate.db.scalars(select(ContextProductConsumptionEdge))).all() == []
    audits = (
        await estate.db.scalars(
            select(AuditEvent).where(AuditEvent.action == "context_product.coverage_read")
        )
    ).all()
    assert len(audits) == 1


# --- contextProductCoverage: priced before it runs --------------------------------------


async def test_admission_prices_every_coverage_section_at_its_page_size(
    http: httpx.AsyncClient, estate: Estate
) -> None:
    """Each section is a connection, so the estimate multiplies its `first`: the coverage
    object, five connections, five pageInfos and five pages. One page of 100 too many is
    refused before a single statement."""
    headers = _headers(estate.org, "Analyst")
    version_id = str(estate.versions["published"].id)
    admitted = (await _gql(http, _coverage_query(20), headers, "Coverage", id=version_id)).json()
    assert admitted["extensions"]["cost"]["estimatedNodes"] == 1 + 5 + 5 + 5 * 20

    estate.statements.clear()
    refused = await _gql(http, _coverage_query(100), headers, "Coverage", id=version_id)
    assert refused.status_code == 400
    assert _codes(refused.json()) == [("NODE_BUDGET_EXCEEDED", None)]
    assert estate.statements == []


# --- parse coverage ------------------------------------------------------------------

_ROUTINE_COVERAGE = (
    "query Routine($ds: ID!, $id: ID!) { routineParseCoverage(datasourceId: $ds, routineId: $id) "
    "{ routineId state parseCompleted isReadOnly statementCount unparsedStatementCount "
    "unparsedReasonCodes dialect confidence sourceMappingGranularity parsedAt "
    "memberAttribution memberFallbackReason } }"
)
_TRIGGER_COVERAGE = (
    "query Trigger($ds: ID!, $id: ID!) { triggerParseCoverage(datasourceId: $ds, triggerId: $id) "
    "{ triggerId routineId state parseCompleted isReadOnly statementCount "
    "unparsedStatementCount unparsedReasonCodes dialect confidence sourceMappingGranularity "
    "parsedAt } }"
)


async def _both(
    client: httpx.AsyncClient,
    kind: str,
    datasource_id: UUID,
    object_id: UUID,
    headers: dict[str, str],
) -> tuple[httpx.Response, dict[str, Any]]:
    segment, query, operation = (
        ("procedures", _ROUTINE_COVERAGE, "Routine")
        if kind == "routine"
        else ("triggers", _TRIGGER_COVERAGE, "Trigger")
    )
    rest = await client.get(
        f"/v1/datasources/{datasource_id}/{segment}/{object_id}/parse-coverage", headers=headers
    )
    graphql = await _gql(
        client, query, headers, operation, ds=str(datasource_id), id=str(object_id)
    )
    return rest, graphql.json()


@pytest.mark.parametrize("kind", ["routine", "trigger"])
async def test_a_measured_body_reports_the_rest_measurement(
    http: httpx.AsyncClient, estate: Estate, kind: str
) -> None:
    subject = estate.routines["rebuild"] if kind == "routine" else estate.triggers["measured"]
    rest, body = await _both(
        http, kind, estate.warehouse.id, subject.id, _headers(estate.org, "Viewer")
    )
    assert rest.status_code == 200, rest.text
    assert "errors" not in body, body
    node = body["data"][f"{kind}ParseCoverage"]
    rest_item = rest.json()
    assert {_camel(key) for key in rest_item} == set(node)
    for key, value in rest_item.items():
        assert _same_value(value, node[_camel(key)]), (key, value, node[_camel(key)])


def _parse_refusals(
    estate: Estate,
) -> list[tuple[str, str, UUID, UUID, str, int, tuple[str, str | None]]]:
    warehouse, closed = estate.warehouse.id, estate.closed.id
    routines, triggers = estate.routines, estate.triggers
    return [
        # (case, kind, datasource, object, roles, REST status, GraphQL code)
        (
            "not-measured",
            "routine",
            warehouse,
            routines["refresh"].id,
            "Analyst",
            404,
            ("NOT_FOUND", "COVERAGE_NOT_MEASURED"),
        ),
        (
            "trigger-not-measured",
            "trigger",
            warehouse,
            triggers["unmeasured"].id,
            "Analyst",
            404,
            ("NOT_FOUND", "COVERAGE_NOT_MEASURED"),
        ),
        (
            "routine-of-another-datasource",
            "routine",
            warehouse,
            routines["hidden"].id,
            "Analyst",
            404,
            ("NOT_FOUND", None),
        ),
        ("unknown-routine", "routine", warehouse, uuid4(), "Analyst", 404, ("NOT_FOUND", None)),
        (
            "unknown-trigger",
            "trigger",
            warehouse,
            uuid4(),
            "Analyst",
            404,
            ("NOT_FOUND", None),
        ),
        (
            "unknown-datasource",
            "routine",
            uuid4(),
            routines["rebuild"].id,
            "Analyst",
            404,
            ("NOT_FOUND", None),
        ),
        (
            "another-tenants-datasource",
            "routine",
            estate.foreign_ds.id,
            routines["foreign"].id,
            "Analyst",
            403,
            ("FORBIDDEN", "CROSS_ORGANIZATION"),
        ),
        (
            "closed-workspace",
            "routine",
            closed,
            routines["hidden"].id,
            "Analyst",
            403,
            ("FORBIDDEN", "NO_WORKSPACE_MEMBERSHIP"),
        ),
        (
            "closed-workspace-hides-existence",
            "routine",
            closed,
            uuid4(),
            "Analyst",
            403,
            ("FORBIDDEN", "NO_WORKSPACE_MEMBERSHIP"),
        ),
        (
            "role-not-a-lineage-reader",
            "routine",
            warehouse,
            routines["rebuild"].id,
            "Reviewer",
            403,
            ("FORBIDDEN", "ROLE_REQUIRED"),
        ),
    ]


_PARSE_CASES = [
    "not-measured",
    "trigger-not-measured",
    "routine-of-another-datasource",
    "unknown-routine",
    "unknown-trigger",
    "unknown-datasource",
    "another-tenants-datasource",
    "closed-workspace",
    "closed-workspace-hides-existence",
    "role-not-a-lineage-reader",
]


@pytest.mark.parametrize("case", _PARSE_CASES)
async def test_parse_coverage_is_refused_where_rest_refuses(
    http: httpx.AsyncClient, estate: Estate, case: str
) -> None:
    cases = _parse_refusals(estate)
    assert [entry[0] for entry in cases] == _PARSE_CASES, "a case was added or dropped"
    (_, kind, datasource_id, object_id, roles, rest_status, graphql_code) = next(
        entry for entry in cases if entry[0] == case
    )
    rest, body = await _both(http, kind, datasource_id, object_id, _headers(estate.org, roles))
    assert rest.status_code == rest_status, rest.text
    assert body["data"][f"{kind}ParseCoverage"] is None
    assert _codes(body) == [graphql_code]
    if graphql_code[1] == "NO_WORKSPACE_MEMBERSHIP":
        # The gate's own reason code, the same on both surfaces (R11-D30).
        assert rest.json()["detail"] == "NO_WORKSPACE_MEMBERSHIP"
