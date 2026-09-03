"""N12 exit-condition tests: procedure-to-tool ("tool generator C"), gated
on N3's read-only proof.

Three groups, mirroring `tests/test_view_tool_blueprint.py`'s split:

* Pure eligibility-gate tests (`find_single_read_only_result_statement`) --
  a write, an UNPARSED chunk, zero result statements, and more than one
  result statement are each refused with a reason naming exactly why.
* Pure `build_procedure_tool_blueprint` tests -- a literal in the result
  statement refuses generation; an IN parameter maps to a real tool
  parameter or, if unmappable, refuses generation; determinism.
* One real (in-memory SQLite) database integration test against the actual
  draft-creation endpoint (`aida.procedure_tool_api.create_procedure_tool_blueprint`),
  proving a genuinely read-only procedure produces a DRAFT `GovernedToolVersion`
  plus its `ProcedureToolGenerationRecord` provenance row, and that a
  procedure with a real write is refused (422) before ever reaching it.
"""

from __future__ import annotations

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
from aida.envelope_models import MetadataRoutine
from aida.models import (
    DataDomain,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.procedure_lineage_models import ProcedureToolGenerationRecord
from aida.procedure_tool_api import ProcedureToolBlueprintRequest, create_procedure_tool_blueprint
from aida.procedure_tool_blueprint import (
    ProcedureNotEligibleError,
    RoutineInParameter,
    build_procedure_tool_blueprint,
    find_single_read_only_result_statement,
)
from tests.support.doubles import security_context

# ---------------------------------------------------------------------------
# Eligibility gate: proves read-only, not just "no write found".
# ---------------------------------------------------------------------------


def test_a_write_statement_refuses_eligibility() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN INSERT INTO t (a) SELECT a FROM s; END"
    with pytest.raises(ProcedureNotEligibleError, match="not read-only"):
        find_single_read_only_result_statement(sql, "tsql")


def test_an_unparsed_chunk_refuses_eligibility_even_with_zero_writes_found() -> None:
    """The exact invariant this row exists to enforce: "no write statement
    found" because the parser gave up must never pass as read-only."""
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN SELECT a FROM t; EXEC(@dynamic_sql); END"
    with pytest.raises(ProcedureNotEligibleError, match="could not be parsed"):
        find_single_read_only_result_statement(sql, "tsql")


def test_zero_result_statements_refuses_eligibility() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN SET NOCOUNT ON; END"
    with pytest.raises(ProcedureNotEligibleError, match="no standalone result"):
        find_single_read_only_result_statement(sql, "tsql")


def test_more_than_one_result_statement_refuses_eligibility() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN SELECT a FROM t; SELECT b FROM s; END"
    with pytest.raises(ProcedureNotEligibleError, match="ambiguous"):
        find_single_read_only_result_statement(sql, "tsql")


def test_exactly_one_result_statement_is_eligible() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN SELECT a.id FROM t a; END"
    node, result = find_single_read_only_result_statement(sql, "tsql")
    assert node is not None
    assert result.is_read_only is True


# ---------------------------------------------------------------------------
# Pure builder: literal refusal, parameter remapping, determinism.
# ---------------------------------------------------------------------------


def test_a_literal_in_the_result_statement_refuses_generation() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN SELECT a.id FROM t a WHERE a.status = 'x'; END"
    node, result = find_single_read_only_result_statement(sql, "tsql")
    with pytest.raises(ProcedureNotEligibleError, match="literal value"):
        build_procedure_tool_blueprint(
            node, [], dialect="tsql",
            statement_count=result.statement_count, sql_hash=result.sql_hash,
        )


def test_an_in_parameter_maps_to_a_real_tool_parameter() -> None:
    sql = (
        "CREATE PROCEDURE dbo.usp_x @start_date DATE AS BEGIN "
        "SELECT a.id FROM t a WHERE a.created_at >= @start_date; END"
    )
    node, result = find_single_read_only_result_statement(sql, "tsql")
    blueprint = build_procedure_tool_blueprint(
        node,
        [RoutineInParameter(name="start_date", physical_type="DATE")],
        dialect="tsql", statement_count=result.statement_count, sql_hash=result.sql_hash,
    )
    assert [p.name for p in blueprint.parameters] == ["start_date"]
    assert blueprint.parameters[0].parameter_type == "DATE"
    assert ":start_date" in blueprint.sql_template


def test_an_unmapped_variable_reference_refuses_generation() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN SELECT a.id FROM t a WHERE a.x = @not_declared; END"
    node, result = find_single_read_only_result_statement(sql, "tsql")
    with pytest.raises(ProcedureNotEligibleError, match="cannot safely bind"):
        build_procedure_tool_blueprint(
            node, [], dialect="tsql",
            statement_count=result.statement_count, sql_hash=result.sql_hash,
        )


def test_same_input_twice_renders_byte_identical_sql() -> None:
    sql = "CREATE PROCEDURE dbo.usp_x AS BEGIN SELECT a.id FROM t a; END"
    node, result = find_single_read_only_result_statement(sql, "tsql")
    first = build_procedure_tool_blueprint(
        node, [], dialect="tsql", statement_count=result.statement_count, sql_hash=result.sql_hash
    )
    node2, result2 = find_single_read_only_result_statement(sql, "tsql")
    second = build_procedure_tool_blueprint(
        node2, [], dialect="tsql",
        statement_count=result2.statement_count, sql_hash=result2.sql_hash,
    )
    assert first.sql_template == second.sql_template


# ---------------------------------------------------------------------------
# Real endpoint, real database.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session
    await engine.dispose()


class _Scenario:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
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
            organization_id=self.organization.id, line_of_business_id=self.lob.id,
            name="Commerce", code="COMMERCE",
        )
        db.add(self.domain)
        await db.flush()
        self.project = Project(
            organization_id=self.organization.id, line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id, name="Retail Platform", slug="retail-platform",
        )
        db.add(self.project)
        await db.flush()
        self.datasource = DataSource(
            organization_id=self.organization.id, line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id, project_id=self.project.id, name="warehouse",
            connector_type="POSTGRES", dialect="postgres", environment="PRODUCTION",
            credential_reference="vault://warehouse",
        )
        db.add(self.datasource)
        await db.flush()
        catalog = MetadataCatalog(
            organization_id=self.organization.id, datasource_id=self.datasource.id,
            name="warehouse", fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()
        schema = MetadataSchema(
            organization_id=self.organization.id, catalog_id=catalog.id, name="public",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()
        self.orders_table = MetadataTable(
            organization_id=self.organization.id, datasource_id=self.datasource.id,
            schema_id=schema.id, name="orders", object_type="TABLE", status="ACTIVE",
            fingerprint="fp-orders",
        )
        self.customers_table = MetadataTable(
            organization_id=self.organization.id, datasource_id=self.datasource.id,
            schema_id=schema.id, name="customers", object_type="TABLE", status="ACTIVE",
            fingerprint="fp-customers",
        )
        db.add_all([self.orders_table, self.customers_table])
        await db.flush()
        return self

    def routine(self, *, body: str, name: str = "usp_report") -> MetadataRoutine:
        return MetadataRoutine(
            id=uuid4(), organization_id=self.organization.id, datasource_id=self.datasource.id,
            schema_id=self.customers_table.schema_id, name=name, routine_type="PROCEDURE",
            body_sql_redacted=body, redaction_status="PARSED", screening_status="CLEAN",
            availability="AVAILABLE", status="ACTIVE", fingerprint="fp-routine",
        )

    def maker(self) -> object:
        return security_context(
            organization_id=self.organization.id, roles=frozenset({"ToolDeveloper"})
        )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


async def test_endpoint_creates_a_draft_from_a_proven_read_only_procedure(
    scenario: _Scenario,
) -> None:
    routine = scenario.routine(
        body=(
            "CREATE PROCEDURE dbo.usp_report AS BEGIN "
            "SELECT c.customer_id, SUM(o.amount) AS total_amount "
            "FROM public.orders o JOIN public.customers c ON c.customer_id = o.customer_id "
            "GROUP BY c.customer_id; END"
        )
    )
    scenario.db.add(routine)
    await scenario.db.flush()

    request = ProcedureToolBlueprintRequest(
        slug="customer_order_totals",
        name="Customer order totals",
        description="Auto-generated read surface from a proven read-only procedure.",
        datasource_id=scenario.datasource.id,
        routine_id=routine.id,
        allowed_roles=["Analyst"],
    )

    created = await create_procedure_tool_blueprint(
        scenario.project.id, request,
        context=scenario.maker(), session=scenario.db, settings=Settings(),
    )

    assert created.status == "DRAFT"
    assert created.slug == "customer_order_totals"
    assert created.approved_by is None  # never auto-published (INV-3/INV-10)
    assert "public.orders" in created.referenced_tables
    assert "public.customers" in created.referenced_tables

    provenance = (
        await scenario.db.scalars(
            select(ProcedureToolGenerationRecord).where(
                ProcedureToolGenerationRecord.routine_id == routine.id
            )
        )
    ).all()
    assert len(provenance) == 1
    assert provenance[0].tool_version_id == created.id
    assert provenance[0].statement_count >= 1

    # And the draft is a real GovernedToolVersion row, reachable the same
    # way every other tool-generator's draft is -- proving this path funnels
    # through the exact same persistence tail, not a parallel one.
    tool = await scenario.db.scalar(
        select(GovernedTool).where(GovernedTool.slug == "customer_order_totals")
    )
    assert tool is not None
    version = await scenario.db.get(GovernedToolVersion, created.id)
    assert version is not None
    assert version.tool_id == tool.id


async def test_endpoint_refuses_a_procedure_with_a_real_write_before_persisting_anything(
    scenario: _Scenario,
) -> None:
    routine = scenario.routine(
        body=(
            "CREATE PROCEDURE dbo.usp_report AS BEGIN "
            "INSERT INTO public.orders (id) SELECT id FROM public.customers; END"
        ),
    )
    scenario.db.add(routine)
    await scenario.db.flush()

    request = ProcedureToolBlueprintRequest(
        slug="not_read_only",
        name="Should be refused",
        description="A procedure that writes must never generate a tool.",
        datasource_id=scenario.datasource.id,
        routine_id=routine.id,
        allowed_roles=["Analyst"],
    )

    with pytest.raises(HTTPException) as exc_info:
        await create_procedure_tool_blueprint(
            scenario.project.id, request,
            context=scenario.maker(), session=scenario.db, settings=Settings(),
        )
    assert exc_info.value.status_code == 422
    assert "not read-only" in exc_info.value.detail

    tool = await scenario.db.scalar(
        select(GovernedTool).where(GovernedTool.slug == "not_read_only")
    )
    assert tool is None
