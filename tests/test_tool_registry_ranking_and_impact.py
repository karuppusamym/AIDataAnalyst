"""TL-4 / TL-7: usage-weighted tool ranking and deprecation impact preview.

Both are proven end to end against a real in-memory database, mirroring
`tests/test_quality_runtime_coupling.py`'s harness -- ORM-seeded rows, real
endpoint functions called directly, real SQL joins and traversal, not a
hand-simulated approximation.

  * TL-4: `tool_api.py::list_tools` and `mcp_server.py::_handle_tools_list`
    (the CX-5 role-eligible catalog an MCP client is offered) both rank two
    otherwise-identical PUBLISHED tools by real `ToolExecution` history --
    the tool with completed executions ranks above the one with none, and
    the response surfaces the count that drove the ordering
    (`GovernedToolVersionRead.usage_count` / `_atlas_meta.usage_count`).

  * TL-7: `tool_api.py::get_tool_deprecation_impact` (and
    `submit_tool_deprecation`, which records the same evidence on the real
    DEPRECATE governance-review submission) computes the blast radius of
    deprecating a tool version by reusing LN-7's own bounded transitive
    traversal (`unified_lineage_api.build_unified_lineage_impact_payload`)
    seeded from the version's declared `referenced_tables`, plus sibling
    tools and context products that share the same reachable tables. A tool
    with a real downstream dependent (a declared foreign key, one of LN-7's
    edge kinds) shows it in the preview; a tool with no dependents at all
    shows an empty, zero-count blast radius -- not a truncated or
    default-populated one.
"""

from collections.abc import AsyncIterator
from itertools import count
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on Base.metadata
from aida.config import Settings
from aida.db import Base
from aida.mcp_server import _handle_tools_list
from aida.models import (
    AuditEvent,
    ContextProduct,
    ContextProductVersion,
    DataDomain,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    ToolExecution,
)
from aida.tool_api import get_tool_deprecation_impact, list_tools, submit_tool_deprecation
from tests.support.doubles import security_context

# `AuditEvent.id` is a `BigInteger` autoincrement PK relying in production on
# Postgres's own sequence; sqlite only auto-populates a bare `INTEGER PRIMARY
# KEY`. Same workaround as `test_quality_runtime_coupling.py`.
_audit_event_ids = count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


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
    """One organization, one datasource, three tables (`customers`,
    `orders` -- FK'd to `customers` -- and an isolated `no_deps` table with
    no edges at all), seeded directly through the ORM."""

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
            name="Finance",
            code="FINANCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug="core-banking",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://core-warehouse",
        )
        db.add(self.datasource)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="bank",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="finance",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()
        self.schema = schema

        self.customers_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="customers",
            object_type="TABLE",
            fingerprint="fp-customers",
        )
        self.orders_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="orders",
            object_type="TABLE",
            fingerprint="fp-orders",
        )
        self.isolated_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="no_deps",
            object_type="TABLE",
            fingerprint="fp-no-deps",
        )
        db.add_all([self.customers_table, self.orders_table, self.isolated_table])
        await db.flush()

        # orders -> customers: a real declared foreign key, one of LN-7's
        # edge kinds. Traversal convention: source_id is the dependent
        # (orders), target_id is what it depends on (customers) -- so
        # "what's downstream of customers" (REFERENCED_BY) finds orders.
        constraint = MetadataConstraint(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            table_id=self.orders_table.id,
            name="fk_orders_customer_id",
            constraint_type="FOREIGN_KEY",
            columns=["customer_id"],
            referenced_table_id=self.customers_table.id,
            referenced_columns=["id"],
            fingerprint="fp-fk-orders-customers",
        )
        db.add(constraint)
        await db.flush()
        return self

    async def tool_version(
        self, *, slug: str, table: str, allowed_roles: list[str] | None = None
    ) -> GovernedToolVersion:
        db = self.db
        tool = GovernedTool(
            organization_id=self.organization.id,
            project_id=self.project.id,
            slug=slug,
        )
        db.add(tool)
        await db.flush()
        version = GovernedToolVersion(
            organization_id=self.organization.id,
            tool_id=tool.id,
            version=1,
            status="PUBLISHED",
            name=slug.replace("-", " ").title(),
            description=f"Reads {table}.",
            datasource_id=self.datasource.id,
            sql_template=f"SELECT 1 FROM {table}",  # noqa: S608 -- test fixture, not user input
            referenced_tables=[table],
            parameter_schema=[],
            allowed_roles=allowed_roles or ["Analyst"],
            fingerprint=f"fp-{slug}",
            created_by="tool-maker",
        )
        db.add(version)
        await db.flush()
        return version

    async def execution(self, version: GovernedToolVersion, *, status: str = "COMPLETED") -> None:
        self.db.add(
            ToolExecution(
                organization_id=self.organization.id,
                tool_version_id=version.id,
                principal_id=f"analyst-{uuid4().hex[:8]}",
                parameter_fingerprint=f"fp-{uuid4().hex[:8]}",
                status=status,
            )
        )
        await self.db.flush()

    async def context_product(
        self,
        *,
        key: str,
        eligible_tool_version_ids: list[str] | None = None,
        table_ids: list[str] | None = None,
    ) -> ContextProductVersion:
        db = self.db
        product = ContextProduct(
            organization_id=self.organization.id,
            project_id=self.project.id,
            product_key=key,
            created_by="product-maker",
        )
        db.add(product)
        await db.flush()
        version = ContextProductVersion(
            organization_id=self.organization.id,
            product_id=product.id,
            version=1,
            status="PUBLISHED",
            name=key.replace("-", " ").title(),
            description="Test context product.",
            purpose="Testing TL-7 blast radius.",
            owner_type="INDIVIDUAL",
            owner_principal="product-maker",
            table_ids=table_ids or [],
            eligible_tool_version_ids=eligible_tool_version_ids or [],
            allowed_consumer_roles=["Analyst"],
            fingerprint=f"fp-{key}",
            created_by="product-maker",
        )
        db.add(version)
        await db.flush()
        return version

    def analyst(self) -> object:
        return security_context(organization_id=self.organization.id, roles=frozenset({"Analyst"}))

    def platform_admin(self) -> object:
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"PlatformAdmin"})
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


# ---------------------------------------------------------------------------
# TL-4: usage-weighted ranking actually reorders the real listing endpoints
# ---------------------------------------------------------------------------


async def test_list_tools_ranks_a_used_tool_above_an_identical_unused_one(
    scenario: _Scenario,
) -> None:
    popular = await scenario.tool_version(slug="popular-lookup", table="finance.customers")
    await scenario.tool_version(slug="quiet-lookup", table="finance.customers")
    for _ in range(3):
        await scenario.execution(popular)

    page = await list_tools(
        scenario.project.id,
        tool_status="PUBLISHED",
        limit=50,
        offset=0,
        context=scenario.analyst(),
        session=scenario.db,
    )

    slugs_in_order = [item.slug for item in page.items]
    assert slugs_in_order.index("popular-lookup") < slugs_in_order.index("quiet-lookup")
    by_slug = {item.slug: item for item in page.items}
    assert by_slug["popular-lookup"].usage_count == 3
    assert by_slug["quiet-lookup"].usage_count == 0


async def test_list_tools_ignores_non_completed_executions(scenario: _Scenario) -> None:
    version = await scenario.tool_version(slug="flaky-lookup", table="finance.customers")
    await scenario.execution(version, status="FAILED")
    await scenario.execution(version, status="REJECTED")

    page = await list_tools(
        scenario.project.id,
        tool_status="PUBLISHED",
        limit=50,
        offset=0,
        context=scenario.analyst(),
        session=scenario.db,
    )

    item = next(item for item in page.items if item.slug == "flaky-lookup")
    assert item.usage_count == 0


async def test_mcp_tools_list_ranks_the_role_eligible_catalog_by_usage(
    scenario: _Scenario,
) -> None:
    popular = await scenario.tool_version(slug="popular-mcp-lookup", table="finance.customers")
    await scenario.tool_version(slug="quiet-mcp-lookup", table="finance.customers")
    for _ in range(5):
        await scenario.execution(popular)

    result = await _handle_tools_list(scenario.db, scenario.analyst())
    governed = [t for t in result["tools"] if t["_atlas_meta"].get("tool_id")]
    names_in_order = [t["name"] for t in governed]

    assert names_in_order.index("atlas__popular-mcp-lookup") < names_in_order.index(
        "atlas__quiet-mcp-lookup"
    )
    by_name = {t["name"]: t for t in governed}
    assert by_name["atlas__popular-mcp-lookup"]["_atlas_meta"]["usage_count"] == 5
    assert by_name["atlas__quiet-mcp-lookup"]["_atlas_meta"]["usage_count"] == 0


# ---------------------------------------------------------------------------
# TL-7: deprecation impact preview surfaces a real blast radius, or a real
# empty one -- never a stale or default-populated approximation
# ---------------------------------------------------------------------------


async def test_deprecation_impact_surfaces_a_real_downstream_dependent(
    scenario: _Scenario,
) -> None:
    customers_version = await scenario.tool_version(
        slug="customer-lookup", table="finance.customers"
    )
    orders_version = await scenario.tool_version(slug="orders-lookup", table="finance.orders")
    await scenario.context_product(
        key="customer-360",
        eligible_tool_version_ids=[str(customers_version.id)],
    )
    await scenario.execution(customers_version)

    impact = await get_tool_deprecation_impact(
        customers_version.id,
        context=scenario.platform_admin(),
        session=scenario.db,
        settings=Settings(),
    )

    downstream_ids = {node.node_id for node in impact.downstream_nodes}
    assert str(scenario.orders_table.id) in downstream_ids

    dependent_slugs = {item.slug for item in impact.dependent_tool_versions}
    assert dependent_slugs == {"orders-lookup"}
    assert orders_version.id in {item.tool_version_id for item in impact.dependent_tool_versions}

    cp_reasons = {item.reason for item in impact.dependent_context_products}
    assert cp_reasons == {"ELIGIBLE_TOOL"}

    assert impact.active_consumer_count == 1
    assert impact.recent_execution_count == 1
    assert impact.total_blast_radius >= 3


async def test_deprecation_impact_is_empty_for_a_tool_with_no_dependents(
    scenario: _Scenario,
) -> None:
    isolated_version = await scenario.tool_version(
        slug="isolated-lookup", table="finance.no_deps"
    )

    impact = await get_tool_deprecation_impact(
        isolated_version.id,
        context=scenario.platform_admin(),
        session=scenario.db,
        settings=Settings(),
    )

    assert impact.downstream_nodes == []
    assert impact.downstream_truncated is False
    assert impact.dependent_tool_versions == []
    assert impact.dependent_context_products == []
    assert impact.active_consumer_count == 0
    assert impact.recent_execution_count == 0
    assert impact.total_blast_radius == 0


async def test_submit_tool_deprecation_records_the_same_impact_as_evidence(
    scenario: _Scenario,
) -> None:
    customers_version = await scenario.tool_version(
        slug="customer-lookup-2", table="finance.customers"
    )
    await scenario.tool_version(slug="orders-lookup-2", table="finance.orders")

    await submit_tool_deprecation(
        customers_version.id,
        context=scenario.platform_admin(),
        session=scenario.db,
        settings=Settings(),
    )

    audit_rows = (
        await scenario.db.scalars(
            select(AuditEvent).where(AuditEvent.action == "tool.version.deprecation.submit")
        )
    ).all()
    assert len(audit_rows) == 1
    recorded = audit_rows[0].details["deprecation_impact"]
    assert recorded["dependent_tool_version_count"] == 1
    assert recorded["total_blast_radius"] >= 1
