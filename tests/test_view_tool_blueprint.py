"""N11 exit-condition tests: view-to-tool blueprints, deterministically
rendered.

Three groups, mirroring `tests/test_multi_table_blueprint.py`'s split:

* Pure `build_view_tool_blueprint` tests -- no database. Prove (a) the same
  view columns always render byte-identical SQL and an identically-ordered
  parameter list regardless of caller-supplied column order, and (b) a
  column whose declared type does not resolve to a known, filterable family
  is still selected into the output but never turned into a parameter.
* A redaction-gating test -- a view whose `MetadataViewDefinition` is
  quarantined (or otherwise not eligible) is refused outright, both at
  `resolve_view_tool_source` and through the real endpoint (422).
* One real (in-memory SQLite) database integration test against the actual
  draft-creation endpoint (`aida.tool_api.create_view_tool_blueprint`),
  seeding a genuine SQLite VIEW (not just governance metadata describing
  one) plus its `MetadataViewDefinition`, then proving the persisted
  draft's rendered SQL actually executes against that real view and
  returns the right rows -- both unfiltered and with a parameter bound.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
from aida.db import Base
from aida.envelope_models import MetadataViewDefinition
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.sql_guard import SqlGuard
from aida.tool_api import ViewToolBlueprintRequest, create_view_tool_blueprint
from aida.tool_rendering import render_tool_sql, template_placeholders
from aida.view_tool_blueprint import (
    ViewNotEligibleError,
    ViewToolBlueprintError,
    ViewToolColumn,
    ViewToolSource,
    build_view_tool_blueprint,
    resolve_view_tool_source,
)
from tests.support.doubles import security_context

# ---------------------------------------------------------------------------
# Pure builder: determinism, column selection vs. parameterization. No
# database.
# ---------------------------------------------------------------------------


def _active_customers_view() -> ViewToolSource:
    return ViewToolSource(
        table_id=uuid4(),
        qualified_name="main.active_customers",
        columns=(
            ViewToolColumn("id", "INTEGER", 1),
            ViewToolColumn("name", "VARCHAR(100)", 2),
            ViewToolColumn("is_active", "BOOLEAN", 3),
        ),
    )


def test_same_input_twice_renders_byte_identical_sql() -> None:
    view = _active_customers_view()

    first = build_view_tool_blueprint(view, dialect="postgres")
    second = build_view_tool_blueprint(view, dialect="postgres")

    assert first.sql_template == second.sql_template
    assert first.parameters == second.parameters
    assert first.selected_columns == second.selected_columns


def test_determinism_is_independent_of_caller_supplied_column_order() -> None:
    view = _active_customers_view()
    reordered = ViewToolSource(
        table_id=view.table_id,
        qualified_name=view.qualified_name,
        columns=tuple(reversed(view.columns)),
    )

    forward = build_view_tool_blueprint(view, dialect="postgres")
    backward = build_view_tool_blueprint(reordered, dialect="postgres")

    assert forward.sql_template == backward.sql_template
    assert forward.parameters == backward.parameters
    assert forward.selected_columns == backward.selected_columns


def test_every_recognized_typed_column_becomes_an_optional_parameter() -> None:
    view = _active_customers_view()

    blueprint = build_view_tool_blueprint(view, dialect="postgres")

    assert blueprint.selected_columns == ("id", "name", "is_active")
    assert blueprint.parameterized_columns == ("id", "name", "is_active")
    parameter_types = {
        parameter.name: parameter.parameter_type for parameter in blueprint.parameters
    }
    assert parameter_types == {"id": "NUMBER", "name": "STRING", "is_active": "BOOLEAN"}
    assert all(not parameter.required for parameter in blueprint.parameters)
    assert '"active_customers"' in blueprint.sql_template
    assert "SELECT *" not in blueprint.sql_template.upper().replace(" *", "*")


def test_a_column_with_an_unrecognized_type_is_selected_but_never_parameterized() -> None:
    # GEOMETRY/JSON/array types don't resolve to a known, filterable family
    # via relationship_naming.physical_type_family -- the same typing logic
    # multi_table_blueprint.py already reuses. This generator does not
    # invent a filter type it cannot honestly validate: the column stays in
    # the output, it just never becomes a WHERE parameter.
    view = ViewToolSource(
        table_id=uuid4(),
        qualified_name="geo.store_locations",
        columns=(
            ViewToolColumn("store_id", "INTEGER", 1),
            ViewToolColumn("footprint", "GEOMETRY", 2),
        ),
    )

    blueprint = build_view_tool_blueprint(view, dialect="postgres")

    assert blueprint.selected_columns == ("store_id", "footprint")
    assert blueprint.parameterized_columns == ("store_id",)
    assert {parameter.name for parameter in blueprint.parameters} == {"store_id"}
    assert '"footprint"' in blueprint.sql_template


def test_a_view_with_no_active_columns_is_a_structural_error() -> None:
    empty_view = ViewToolSource(table_id=uuid4(), qualified_name="retail.empty_view", columns=())

    with pytest.raises(ViewToolBlueprintError, match="no active columns"):
        build_view_tool_blueprint(empty_view, dialect="postgres")


def test_rendered_sql_passes_the_real_sql_guard_and_renders_at_execution_time() -> None:
    view = _active_customers_view()
    blueprint = build_view_tool_blueprint(view, dialect="postgres")

    # The declared placeholders match the generated parameter schema exactly
    # -- the same invariant `create_tool_version` enforces for a hand-
    # written template.
    placeholders = template_placeholders(blueprint.sql_template, dialect="postgres")
    assert placeholders == {parameter.name for parameter in blueprint.parameters}

    guard = SqlGuard(default_row_limit=1000, hard_row_limit=10_000)
    validation = guard.validate(blueprint.sql_template, dialect="postgres")
    assert validation.valid, validation.violations
    assert set(validation.referenced_tables) == {"main.active_customers"}

    # No filter supplied -> the optional predicate must not exclude rows.
    unfiltered = render_tool_sql(
        blueprint.sql_template,
        dialect="postgres",
        definitions=list(blueprint.parameters),
        values={},
    )
    assert "IS NULL" in unfiltered.sql

    # A filter value renders as a real equality predicate.
    filtered = render_tool_sql(
        blueprint.sql_template,
        dialect="postgres",
        definitions=list(blueprint.parameters),
        values={"id": 42},
    )
    assert "42" in filtered.sql


# ---------------------------------------------------------------------------
# Real-database integration: seeded catalog + a genuine SQLite VIEW, through
# the actual draft-creation endpoint.
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
    """One organization/project/datasource with a real, physical
    `customers` table plus a real `active_customers` VIEW over it -- both
    the governance-metadata description (`MetadataTable`/`MetadataColumn`/
    `MetadataViewDefinition`) *and* the actual executable SQLite objects,
    sharing the same underlying `:memory:` connection (`StaticPool`), so a
    tool's rendered SQL can be executed for real and checked against real
    rows -- not merely validated for shape.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self, *, screening_status: str = "CLEAN") -> "_Scenario":
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
            dialect="sqlite",
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

        # SQLite's own default database is literally named "main" -- using
        # it as the schema name means the governance-catalog qualified name
        # ("main.active_customers") is *also* valid, executable SQLite
        # syntax, so the generated tool can be run for real against it.
        schema = MetadataSchema(
            organization_id=self.organization.id, catalog_id=catalog.id, name="main",
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
        self.view_table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="active_customers",
            object_type="VIEW",
            status="ACTIVE",
            fingerprint="fp-active-customers",
        )
        db.add(self.view_table)
        await db.flush()

        db.add_all(
            [
                MetadataColumn(
                    organization_id=self.organization.id,
                    table_id=self.view_table.id,
                    name="id",
                    ordinal_position=1,
                    physical_type="INTEGER",
                    nullable=False,
                    fingerprint="fp-view-id",
                ),
                MetadataColumn(
                    organization_id=self.organization.id,
                    table_id=self.view_table.id,
                    name="name",
                    ordinal_position=2,
                    physical_type="VARCHAR(100)",
                    nullable=False,
                    fingerprint="fp-view-name",
                ),
            ]
        )
        db.add(
            MetadataViewDefinition(
                organization_id=self.organization.id,
                datasource_id=self.datasource.id,
                table_id=self.view_table.id,
                fingerprint="fp-view-definition",
                definition_sql_redacted=(
                    "SELECT id, name FROM customers WHERE is_active = <REDACTED>"
                ),
                definition_fingerprint="fp-definition-text",
                redaction_status="PARSED",
                screening_status=screening_status,
                screening_reason_codes=(
                    [] if screening_status == "CLEAN" else ["INJECTION_PATTERN"]
                ),
                is_materialized=False,
            )
        )
        await db.flush()

        # The real, executable SQLite objects -- a physical table with data,
        # and a genuine VIEW over it. Shares the connection `db` already
        # holds (StaticPool), so this is the same database the ORM rows
        # above were written into.
        await db.execute(
            text(
                'CREATE TABLE "main"."customers" '
                "(id INTEGER, name TEXT, is_active INTEGER)"
            )
        )
        await db.execute(
            text(
                'INSERT INTO "main"."customers" (id, name, is_active) VALUES '
                "(1, 'Alice', 1), (2, 'Bob', 0), (3, 'Carol', 1)"
            )
        )
        await db.execute(
            text(
                'CREATE VIEW "main"."active_customers" AS '
                'SELECT id, name FROM "main"."customers" WHERE is_active = 1'
            )
        )
        await db.commit()
        return self

    def maker(self) -> object:
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"ToolDeveloper"})
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def test_endpoint_creates_a_draft_and_its_rendered_sql_executes_against_the_real_view(
    scenario: _Scenario,
) -> None:
    request = ViewToolBlueprintRequest(
        slug="active_customers_lookup",
        name="Active customers lookup",
        description="Auto-generated read surface over the curated active_customers view.",
        datasource_id=scenario.datasource.id,
        table_id=scenario.view_table.id,
        allowed_roles=["Analyst"],
    )

    created = await create_view_tool_blueprint(
        scenario.project.id,
        request,
        context=scenario.maker(),
        session=scenario.db,
        settings=Settings(),
    )

    assert created.status == "DRAFT"
    assert created.slug == "active_customers_lookup"
    assert created.referenced_tables == ["main.active_customers"]
    assert {parameter.name for parameter in created.parameters} == {"id", "name"}
    # Never approved/published by this generative path either.
    assert created.approved_by is None
    assert created.approved_at is None

    # Round-trip: render the persisted draft's own SQL template with real
    # parameter values and execute it against the real SQLite view.
    unfiltered = render_tool_sql(
        created.sql_template, dialect="sqlite", definitions=created.parameters, values={}
    )
    all_rows = (await scenario.db.execute(text(unfiltered.sql))).all()
    assert sorted(row.name for row in all_rows) == ["Alice", "Carol"]

    filtered = render_tool_sql(
        created.sql_template,
        dialect="sqlite",
        definitions=created.parameters,
        values={"name": "Alice"},
    )
    filtered_rows = (await scenario.db.execute(text(filtered.sql))).all()
    assert [(row.id, row.name) for row in filtered_rows] == [(1, "Alice")]


# ---------------------------------------------------------------------------
# Redaction gating: a view whose definition text is not eligible must never
# produce a tool draft, at either layer.
# ---------------------------------------------------------------------------


async def test_resolver_refuses_a_view_with_no_captured_definition(scenario: _Scenario) -> None:
    existing = await scenario.db.scalar(
        select(MetadataViewDefinition).where(
            MetadataViewDefinition.table_id == scenario.view_table.id
        )
    )
    await scenario.db.delete(existing)
    await scenario.db.flush()

    with pytest.raises(ViewNotEligibleError, match="no captured view definition"):
        await resolve_view_tool_source(
            scenario.db,
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            table_id=scenario.view_table.id,
        )


async def test_resolver_refuses_a_quarantined_view_definition(db: AsyncSession) -> None:
    scenario = await _Scenario(db).build(screening_status="QUARANTINED")

    with pytest.raises(ViewNotEligibleError, match="quarantined") as excinfo:
        await resolve_view_tool_source(
            db,
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            table_id=scenario.view_table.id,
        )
    # A clear, actionable error -- names the exact reason, not a generic denial.
    assert "screening_status=QUARANTINED" in str(excinfo.value)


async def test_endpoint_rejects_a_quarantined_view_with_a_clear_422(db: AsyncSession) -> None:
    scenario = await _Scenario(db).build(screening_status="QUARANTINED")
    request = ViewToolBlueprintRequest(
        slug="quarantined_view_lookup",
        name="Quarantined view lookup",
        description="Must be refused -- the view's definition is quarantined.",
        datasource_id=scenario.datasource.id,
        table_id=scenario.view_table.id,
        allowed_roles=["Analyst"],
    )

    with pytest.raises(HTTPException) as excinfo:
        await create_view_tool_blueprint(
            scenario.project.id,
            request,
            context=scenario.maker(),
            session=db,
            settings=Settings(),
        )

    assert excinfo.value.status_code == 422
    assert "quarantined" in str(excinfo.value.detail)


async def test_resolver_rejects_a_table_id_outside_the_datasource(scenario: _Scenario) -> None:
    with pytest.raises(ViewToolBlueprintError, match="unknown or inactive"):
        await resolve_view_tool_source(
            scenario.db,
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            table_id=uuid4(),
        )
