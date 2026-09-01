"""SM-5 exit-condition tests: multi-table tool blueprints, deterministically
rendered.

Two halves, mirroring `tests/test_aida_tool_sdk.py`'s split:

* Pure `build_multi_table_blueprint` tests -- no database. Prove (a) the same
  declared relationship data always renders byte-identical SQL and an
  identically-ordered parameter list regardless of the order tables were
  requested in, and (b) a table pair with no declared/approved relationship
  between them is refused outright rather than guessed.
* One real (in-memory sqlite) database integration test against the actual
  draft-creation endpoint (`aida.tool_api.create_multi_table_tool_blueprint`),
  seeding a real `MetadataConstraint` foreign key and a real approved
  `RelationshipCandidate`, proving the generated draft is created exactly the
  way a hand-authored `create_tool_version` draft would be -- ``DRAFT``
  status, never approved/published by this path.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
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
    Organization,
    Project,
    RelationshipCandidate,
)
from aida.multi_table_blueprint import (
    BlueprintColumn,
    BlueprintJoinEdge,
    BlueprintTable,
    MultiTableBlueprintError,
    UnjoinableTablesError,
    build_multi_table_blueprint,
    resolve_blueprint_tables_and_edges,
)
from aida.sql_guard import SqlGuard
from aida.tool_api import MultiTableToolBlueprintRequest, create_multi_table_tool_blueprint
from aida.tool_rendering import render_tool_sql, template_placeholders
from tests.support.doubles import security_context

# ---------------------------------------------------------------------------
# Pure builder: determinism and the refusal-to-guess contract. No database.
# ---------------------------------------------------------------------------


def _customers_and_orders() -> tuple[BlueprintTable, BlueprintTable, BlueprintJoinEdge]:
    customers_id = uuid4()
    orders_id = uuid4()
    customers = BlueprintTable(
        table_id=customers_id,
        qualified_name="retail.customers",
        columns=(
            BlueprintColumn("customer_id", "INTEGER", 1),
            BlueprintColumn("name", "VARCHAR(100)", 2),
        ),
    )
    orders = BlueprintTable(
        table_id=orders_id,
        qualified_name="retail.orders",
        columns=(
            BlueprintColumn("order_id", "INTEGER", 1),
            BlueprintColumn("customer_id", "INTEGER", 2),
            BlueprintColumn("total_amount", "NUMERIC(10,2)", 3),
        ),
    )
    edge = BlueprintJoinEdge(
        kind="DECLARED_FOREIGN_KEY",
        left_table_id=orders_id,
        left_columns=("customer_id",),
        right_table_id=customers_id,
        right_columns=("customer_id",),
        source_id="fk-orders-customers",
    )
    return customers, orders, edge


def test_same_input_twice_renders_byte_identical_sql() -> None:
    customers, orders, edge = _customers_and_orders()

    first = build_multi_table_blueprint([customers, orders], [edge], dialect="postgres")
    second = build_multi_table_blueprint([customers, orders], [edge], dialect="postgres")

    assert first.sql_template == second.sql_template
    assert first.parameters == second.parameters
    assert first.table_order == second.table_order


def test_determinism_is_independent_of_caller_supplied_table_and_edge_order() -> None:
    customers, orders, edge = _customers_and_orders()

    forward = build_multi_table_blueprint([customers, orders], [edge], dialect="postgres")
    # Reverse the table list; the edge list has only one element so order
    # there is trivially unaffected, but the anchor/alias assignment must
    # still land the same way because it is picked by canonical
    # (qualified_name, table_id) sort, never by list position.
    backward = build_multi_table_blueprint([orders, customers], [edge], dialect="postgres")

    assert forward.sql_template == backward.sql_template
    assert forward.parameters == backward.parameters
    assert forward.table_order == backward.table_order


def test_determinism_holds_for_a_three_table_chain_regardless_of_order() -> None:
    a_id, b_id, c_id = uuid4(), uuid4(), uuid4()
    table_a = BlueprintTable(a_id, "retail.a", (BlueprintColumn("id", "INTEGER", 1),))
    table_b = BlueprintTable(
        b_id,
        "retail.b",
        (BlueprintColumn("id", "INTEGER", 1), BlueprintColumn("a_id", "INTEGER", 2)),
    )
    table_c = BlueprintTable(
        c_id,
        "retail.c",
        (BlueprintColumn("id", "INTEGER", 1), BlueprintColumn("b_id", "INTEGER", 2)),
    )
    edge_ab = BlueprintJoinEdge("DECLARED_FOREIGN_KEY", b_id, ("a_id",), a_id, ("id",), "fk-ab")
    edge_bc = BlueprintJoinEdge("DECLARED_FOREIGN_KEY", c_id, ("b_id",), b_id, ("id",), "fk-bc")

    orderings = [
        ([table_a, table_b, table_c], [edge_ab, edge_bc]),
        ([table_c, table_a, table_b], [edge_bc, edge_ab]),
        ([table_b, table_c, table_a], [edge_ab, edge_bc]),
    ]
    rendered = [
        build_multi_table_blueprint(tables, edges, dialect="snowflake")
        for tables, edges in orderings
    ]
    assert len({blueprint.sql_template for blueprint in rendered}) == 1
    assert len({blueprint.table_order for blueprint in rendered}) == 1
    parameter_name_sequences = {
        tuple(parameter.name for parameter in blueprint.parameters) for blueprint in rendered
    }
    assert len(parameter_name_sequences) == 1


def test_declared_foreign_key_wins_over_approved_candidate_deterministically() -> None:
    # Two edges connect the same pair: a declared FK and an (unrelated)
    # approved candidate on different columns. The FK must always win, no
    # matter which order the edges are supplied in.
    customers, orders, fk_edge = _customers_and_orders()
    candidate_edge = BlueprintJoinEdge(
        kind="APPROVED_RELATIONSHIP_CANDIDATE",
        left_table_id=orders.table_id,
        left_columns=("total_amount",),
        right_table_id=customers.table_id,
        right_columns=("name",),
        source_id="candidate-noise",
    )

    forward = build_multi_table_blueprint(
        [customers, orders], [fk_edge, candidate_edge], dialect="postgres"
    )
    backward = build_multi_table_blueprint(
        [customers, orders], [candidate_edge, fk_edge], dialect="postgres"
    )

    assert forward.sql_template == backward.sql_template
    assert "customer_id" in forward.sql_template
    assert forward.join_steps[0].kind == "DECLARED_FOREIGN_KEY"


def test_unjoinable_table_is_rejected_not_guessed() -> None:
    customers, orders, edge = _customers_and_orders()
    lonely = BlueprintTable(
        table_id=uuid4(),
        qualified_name="retail.warehouses",
        columns=(BlueprintColumn("warehouse_id", "INTEGER", 1),),
    )

    with pytest.raises(UnjoinableTablesError) as excinfo:
        build_multi_table_blueprint([customers, orders, lonely], [edge], dialect="postgres")

    assert excinfo.value.unreachable_tables == ("retail.warehouses",)
    assert "retail.warehouses" in str(excinfo.value)


def test_no_edges_at_all_is_rejected_not_guessed() -> None:
    customers, orders, _edge = _customers_and_orders()

    with pytest.raises(UnjoinableTablesError):
        build_multi_table_blueprint([customers, orders], [], dialect="postgres")


def test_fewer_than_two_tables_is_a_structural_error() -> None:
    customers, _orders, _edge = _customers_and_orders()

    with pytest.raises(MultiTableBlueprintError, match="at least two"):
        build_multi_table_blueprint([customers], [], dialect="postgres")


def test_duplicate_table_ids_are_a_structural_error() -> None:
    customers, _orders, _edge = _customers_and_orders()
    duplicate = BlueprintTable(
        table_id=customers.table_id, qualified_name="retail.customers", columns=customers.columns
    )

    with pytest.raises(MultiTableBlueprintError, match="duplicate"):
        build_multi_table_blueprint([customers, duplicate], [], dialect="postgres")


def test_rendered_sql_passes_the_real_sql_guard_and_renders_at_execution_time() -> None:
    customers, orders, edge = _customers_and_orders()
    blueprint = build_multi_table_blueprint([customers, orders], [edge], dialect="postgres")

    # The declared placeholders match the generated parameter schema exactly
    # -- the same invariant `create_tool_version` enforces for a hand-written
    # template.
    placeholders = template_placeholders(blueprint.sql_template, dialect="postgres")
    assert placeholders == {parameter.name for parameter in blueprint.parameters}

    guard = SqlGuard(default_row_limit=1000, hard_row_limit=10_000)
    validation = guard.validate(blueprint.sql_template, dialect="postgres")
    assert validation.valid, validation.violations
    assert set(validation.referenced_tables) == {"retail.customers", "retail.orders"}

    # No filter supplied -> the optional predicate must not exclude rows.
    unfiltered = render_tool_sql(
        blueprint.sql_template,
        dialect="postgres",
        definitions=list(blueprint.parameters),
        values={},
    )
    assert "IS NULL" in unfiltered.sql

    # A filter value renders as a real equality predicate.
    filter_name = blueprint.parameters[0].name
    filtered = render_tool_sql(
        blueprint.sql_template,
        dialect="postgres",
        definitions=list(blueprint.parameters),
        values={filter_name: 42},
    )
    assert "42" in filtered.sql


def test_composite_foreign_key_joins_on_every_column_pair() -> None:
    left_id, right_id = uuid4(), uuid4()
    left = BlueprintTable(
        left_id,
        "retail.left_table",
        (BlueprintColumn("k1", "INTEGER", 1), BlueprintColumn("k2", "INTEGER", 2)),
    )
    right = BlueprintTable(
        right_id,
        "retail.right_table",
        (BlueprintColumn("k1", "INTEGER", 1), BlueprintColumn("k2", "INTEGER", 2)),
    )
    edge = BlueprintJoinEdge(
        "DECLARED_FOREIGN_KEY", right_id, ("k1", "k2"), left_id, ("k1", "k2"), "fk-composite"
    )

    blueprint = build_multi_table_blueprint([left, right], [edge], dialect="postgres")

    guard = SqlGuard(default_row_limit=1000, hard_row_limit=10_000)
    validation = guard.validate(blueprint.sql_template, dialect="postgres")
    assert validation.valid, validation.violations
    assert {parameter.name for parameter in blueprint.parameters} == {"t2_k1", "t2_k2"}


# ---------------------------------------------------------------------------
# Real-database integration: the generated blueprint through the actual
# draft-creation endpoint. Mirrors the harness in `tests/test_aida_tool_sdk.py`.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class _Scenario:
    """One organization/project/datasource with two real, related tables --
    `retail.customers` (a declared FK target) and `retail.orders` (its FK
    child) -- plus a third, unrelated table with no declared or approved
    relationship to either."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> "_Scenario":
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Commerce",
            code="COMMERCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Retail Platform",
            slug="retail-platform",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="retail-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://retail-warehouse",
        )
        db.add(self.datasource)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="warehouse",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="retail",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        self.customers_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="customers",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-customers",
        )
        db.add(self.customers_table)
        self.orders_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="orders",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-orders",
        )
        db.add(self.orders_table)
        self.warehouses_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="warehouses",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp-warehouses",
        )
        db.add(self.warehouses_table)
        await db.flush()

        self.customer_id_column = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.customers_table.id,
            name="customer_id",
            ordinal_position=1,
            physical_type="INTEGER",
            nullable=False,
            fingerprint="fp-customer-id",
        )
        db.add(self.customer_id_column)
        db.add(
            MetadataColumn(
                organization_id=self.organization.id,
                table_id=self.customers_table.id,
                name="name",
                ordinal_position=2,
                physical_type="VARCHAR(100)",
                nullable=False,
                fingerprint="fp-customer-name",
            )
        )
        db.add(
            MetadataColumn(
                organization_id=self.organization.id,
                table_id=self.orders_table.id,
                name="order_id",
                ordinal_position=1,
                physical_type="INTEGER",
                nullable=False,
                fingerprint="fp-order-id",
            )
        )
        self.order_customer_id_column = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.orders_table.id,
            name="customer_id",
            ordinal_position=2,
            physical_type="INTEGER",
            nullable=False,
            fingerprint="fp-order-customer-id",
        )
        db.add(self.order_customer_id_column)
        db.add(
            MetadataColumn(
                organization_id=self.organization.id,
                table_id=self.warehouses_table.id,
                name="warehouse_id",
                ordinal_position=1,
                physical_type="INTEGER",
                nullable=False,
                fingerprint="fp-warehouse-id",
            )
        )
        await db.flush()

        db.add(
            MetadataConstraint(
                organization_id=self.organization.id,
                datasource_id=self.datasource.id,
                table_id=self.orders_table.id,
                name="fk_orders_customer_id",
                constraint_type="FOREIGN_KEY",
                columns=["customer_id"],
                referenced_table_id=self.customers_table.id,
                referenced_columns=["customer_id"],
                status="ACTIVE",
                fingerprint="fp-fk-orders-customers",
            )
        )
        await db.flush()
        return self

    def maker(self) -> object:
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"ToolDeveloper"})
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def test_endpoint_creates_a_draft_from_a_declared_foreign_key(
    scenario: _Scenario,
) -> None:
    request = MultiTableToolBlueprintRequest(
        slug="customer_orders_lookup",
        name="Customer orders lookup",
        description="Auto-generated multi-table join over customers and orders.",
        datasource_id=scenario.datasource.id,
        table_ids=[scenario.customers_table.id, scenario.orders_table.id],
        allowed_roles=["Analyst"],
    )

    created = await create_multi_table_tool_blueprint(
        scenario.project.id,
        request,
        context=scenario.maker(),
        session=scenario.db,
        settings=Settings(),
    )

    assert created.status == "DRAFT"
    assert created.slug == "customer_orders_lookup"
    assert set(created.referenced_tables) == {"retail.customers", "retail.orders"}
    assert "JOIN" in created.sql_template.upper()
    assert len(created.parameters) == 1
    # Never approved/published by this generative path either.
    assert created.approved_by is None
    assert created.approved_at is None


async def test_endpoint_rejects_an_unjoinable_table_pair(scenario: _Scenario) -> None:
    request = MultiTableToolBlueprintRequest(
        slug="customer_warehouse_lookup",
        name="Customer warehouse lookup",
        description="No declared relationship connects these two tables.",
        datasource_id=scenario.datasource.id,
        table_ids=[scenario.customers_table.id, scenario.warehouses_table.id],
        allowed_roles=["Analyst"],
    )

    with pytest.raises(HTTPException) as excinfo:
        await create_multi_table_tool_blueprint(
            scenario.project.id,
            request,
            context=scenario.maker(),
            session=scenario.db,
            settings=Settings(),
        )
    assert excinfo.value.status_code == 422
    assert "retail.warehouses" in str(excinfo.value.detail)


async def test_endpoint_via_approved_relationship_candidate(scenario: _Scenario) -> None:
    # No FK between customers and warehouses -- but a reviewer-approved
    # RelationshipCandidate makes the join legitimate without a database FK.
    db = scenario.db
    warehouse_column = (
        await db.scalars(
            select(MetadataColumn).where(
                MetadataColumn.table_id == scenario.warehouses_table.id,
                MetadataColumn.name == "warehouse_id",
            )
        )
    ).one()
    db.add(
        RelationshipCandidate(
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            target_datasource_id=scenario.datasource.id,
            source_table_id=scenario.orders_table.id,
            source_column_id=scenario.order_customer_id_column.id,
            target_table_id=scenario.warehouses_table.id,
            target_column_id=warehouse_column.id,
            detection_rule="NAME_MATCH",
            confidence=0.9,
            status="APPROVED",
            created_by="analyst",
            reviewed_by="steward",
        )
    )
    await db.flush()

    request = MultiTableToolBlueprintRequest(
        slug="orders_warehouse_lookup",
        name="Orders warehouse lookup",
        description="Auto-generated join via an approved relationship candidate.",
        datasource_id=scenario.datasource.id,
        table_ids=[scenario.orders_table.id, scenario.warehouses_table.id],
        allowed_roles=["Analyst"],
    )

    created = await create_multi_table_tool_blueprint(
        scenario.project.id,
        request,
        context=scenario.maker(),
        session=scenario.db,
        settings=Settings(),
    )

    assert created.status == "DRAFT"
    assert set(created.referenced_tables) == {"retail.orders", "retail.warehouses"}


async def test_resolver_rejects_a_table_id_outside_the_datasource(scenario: _Scenario) -> None:
    other_table_id = uuid4()
    with pytest.raises(MultiTableBlueprintError, match="unknown or inactive"):
        await resolve_blueprint_tables_and_edges(
            scenario.db,
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            table_ids=[scenario.customers_table.id, other_table_id],
        )
