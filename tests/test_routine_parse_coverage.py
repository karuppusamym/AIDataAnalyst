"""Per-object routine parse coverage (review 2026-09-16, finding F06.4).

`ProcedureParseResult.is_fully_parsed` and `.is_read_only` are the platform's
only "every branch accounted for" signals, and they lived in memory: they
reached the parse response and the agent's ledger entry and were then gone. So
"which routines are not fully understood?" had to be re-derived by scanning
`deep_procedure_lineage_edge` for `UNPARSED` rows -- which answers a different
question. A routine whose parse produced no edges at all reads the same as one
that was fully understood, and a re-parse under review mode replaces the
markers.

These tests drive the real endpoint against a real ORM session (the pattern
`tests/test_procedure_lineage_api.py` uses) and assert the measurement is
stored, that it is replaced rather than accumulated, and that a body the parser
degrades on is recorded as PARTIAL with its own reason codes -- never as
understood.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.capability_states import CapabilityState
from aida.db import Base
from aida.envelope_models import MetadataRoutine
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.procedure_lineage import UnparsedReason, parse_procedure_lineage
from aida.procedure_lineage_api import (
    get_routine_parse_coverage,
    parse_deep_procedure_lineage_endpoint,
)
from aida.procedure_lineage_models import RoutineParseCoverage
from aida.routine_lineage_edges import (
    SOURCE_MAPPING_GRANULARITY,
    record_routine_parse_coverage,
    unparsed_reason_codes,
)
from aida.security_types import SecurityContext

_READ_ONLY_BODY = """
CREATE PROCEDURE dbo.usp_report AS
BEGIN
    SELECT customer_id FROM dbo.customers;
END
"""

_DYNAMIC_BODY = """
CREATE PROCEDURE dbo.usp_dynamic AS
BEGIN
    SELECT customer_id FROM dbo.customers;
    EXEC sp_executesql @stmt;
END
"""


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


async def _seed(session: AsyncSession) -> tuple[DataSource, MetadataSchema]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
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
        name="primary",
        connector_type="mssql",
        dialect="tsql",
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
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="dbo", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    session.add(
        MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name="customers",
            object_type="BASE_TABLE",
            fingerprint="fp",
        )
    )
    await session.flush()
    return datasource, schema


def _routine(
    datasource: DataSource, schema: MetadataSchema, *, body: str, name: str = "usp_report"
) -> MetadataRoutine:
    return MetadataRoutine(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        routine_type="PROCEDURE",
        body_sql_redacted=body,
        redaction_status="PARSED",
        screening_status="CLEAN",
        availability="AVAILABLE",
        status="ACTIVE",
        fingerprint="fp",
    )


def _context(datasource: DataSource) -> SecurityContext:
    return SecurityContext(
        principal_id="tester",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"PlatformAdmin"}),
    )


async def _coverage(session: AsyncSession, routine_id) -> RoutineParseCoverage | None:
    return (
        await session.scalars(
            select(RoutineParseCoverage).where(
                RoutineParseCoverage.routine_id == routine_id
            )
        )
    ).first()


# ---------------------------------------------------------------------------
# The reason-code summary.
# ---------------------------------------------------------------------------


def test_a_reason_summary_keeps_the_code_and_drops_the_detail() -> None:
    """A reason is stored per edge as a prefix plus a short suffix carrying the
    specific detail -- a callee name, a parse-error text. The summary takes the
    prefix only: a callee name is a source identifier and a parse error can
    quote a value (INV-6)."""
    result = parse_procedure_lineage(_DYNAMIC_BODY, dialect="tsql")
    codes = unparsed_reason_codes(result)
    assert codes, "a dynamic-SQL body must produce at least one reason code"
    assert all(code in {reason.value for reason in UnparsedReason} for code in codes)
    for code in codes:
        assert ":" not in code
        assert " " not in code


def test_an_unrecognised_reason_is_dropped_rather_than_stored() -> None:
    """The gap is still counted; only the unknown label is lost."""
    result = parse_procedure_lineage(_READ_ONLY_BODY, dialect="tsql")
    result.errors = ["something a future parser invented", "DYNAMIC_SQL: detail"]
    assert unparsed_reason_codes(result) == (UnparsedReason.DYNAMIC_SQL.value,)


def test_codes_are_sorted_and_deduplicated() -> None:
    result = parse_procedure_lineage(_READ_ONLY_BODY, dialect="tsql")
    result.errors = [
        "PARSE_ERROR: x",
        "DYNAMIC_SQL",
        "PARSE_ERROR: y",
    ]
    assert unparsed_reason_codes(result) == ("DYNAMIC_SQL", "PARSE_ERROR")


# ---------------------------------------------------------------------------
# Persistence through the real endpoint.
# ---------------------------------------------------------------------------


async def test_a_parse_records_coverage_for_a_fully_understood_body(session) -> None:
    datasource, schema = await _seed(session)
    routine = _routine(datasource, schema, body=_READ_ONLY_BODY)
    session.add(routine)
    await session.flush()

    response = await parse_deep_procedure_lineage_endpoint(
        datasource.id, routine.id, _context(datasource), session
    )
    assert response.is_fully_parsed is True

    coverage = await _coverage(session, routine.id)
    assert coverage is not None
    assert coverage.parse_completed is True
    assert coverage.is_read_only is True
    assert coverage.unparsed_statement_count == 0
    assert coverage.unparsed_reason_codes == ""
    assert coverage.statement_count == response.statement_count
    assert coverage.dialect == "tsql"
    assert coverage.source_mapping_granularity == SOURCE_MAPPING_GRANULARITY
    assert coverage.measured_by == "tester"


async def test_a_body_with_dynamic_sql_is_recorded_as_not_fully_parsed(session) -> None:
    """F06.4 in one assertion: the routine was inventoried, its body was
    captured, one statement produced real lineage -- and the coverage record
    still says the body is not fully understood, and why."""
    datasource, schema = await _seed(session)
    routine = _routine(datasource, schema, body=_DYNAMIC_BODY, name="usp_dynamic")
    session.add(routine)
    await session.flush()

    response = await parse_deep_procedure_lineage_endpoint(
        datasource.id, routine.id, _context(datasource), session
    )
    assert response.is_fully_parsed is False
    assert response.edges, "the parse still found real lineage for the readable statement"

    coverage = await _coverage(session, routine.id)
    assert coverage is not None
    assert coverage.parse_completed is False
    assert coverage.is_read_only is False
    assert coverage.unparsed_statement_count >= 1
    assert coverage.unparsed_reason_codes != ""


async def test_the_state_is_rendered_at_the_boundary_not_stored(session) -> None:
    datasource, schema = await _seed(session)
    routine = _routine(datasource, schema, body=_DYNAMIC_BODY, name="usp_dynamic")
    session.add(routine)
    await session.flush()
    await parse_deep_procedure_lineage_endpoint(
        datasource.id, routine.id, _context(datasource), session
    )

    read = await get_routine_parse_coverage(
        datasource.id, routine.id, _context(datasource), session
    )
    assert read.state == CapabilityState.PARTIAL.value
    assert read.parse_completed is False
    assert read.unparsed_reason_codes
    assert read.source_mapping_granularity == "STATEMENT_ORDINAL"


async def test_a_reparse_replaces_the_measurement_rather_than_accumulating(session) -> None:
    """One row per routine: a reader must never have to work out which of
    several measurements is current."""
    datasource, schema = await _seed(session)
    routine = _routine(datasource, schema, body=_DYNAMIC_BODY, name="usp_dynamic")
    session.add(routine)
    await session.flush()
    await parse_deep_procedure_lineage_endpoint(
        datasource.id, routine.id, _context(datasource), session
    )
    first = await _coverage(session, routine.id)
    assert first is not None and first.parse_completed is False

    # The source fixed the body: the same routine now parses completely.
    routine.body_sql_redacted = _READ_ONLY_BODY
    await session.flush()
    await parse_deep_procedure_lineage_endpoint(
        datasource.id, routine.id, _context(datasource), session
    )

    rows = (
        await session.scalars(
            select(RoutineParseCoverage).where(
                RoutineParseCoverage.routine_id == routine.id
            )
        )
    ).all()
    assert len(rows) == 1, "a re-parse must update the measurement, not add one"
    assert rows[0].parse_completed is True
    assert rows[0].unparsed_reason_codes == ""


async def test_not_measured_is_a_different_answer_from_fully_understood(session) -> None:
    """A zeroed row would read as a clean bill of health for a routine nobody
    has parsed, which is the confusion this table exists to end."""
    from fastapi import HTTPException

    datasource, schema = await _seed(session)
    routine = _routine(datasource, schema, body=_READ_ONLY_BODY)
    session.add(routine)
    await session.flush()

    with pytest.raises(HTTPException) as raised:
        await get_routine_parse_coverage(
            datasource.id, routine.id, _context(datasource), session
        )
    assert raised.value.status_code == 404


async def test_coverage_is_recorded_even_when_the_parse_found_no_lineage(session) -> None:
    """The routines with no edges at all are exactly the ones an UNPARSED-edge
    scan cannot see, so they are the reason this record exists."""
    datasource, schema = await _seed(session)
    routine = _routine(
        datasource,
        schema,
        body="CREATE PROCEDURE dbo.usp_noop AS BEGIN DECLARE @x INT; SET @x = 1; END",
        name="usp_noop",
    )
    session.add(routine)
    await session.flush()

    result = parse_procedure_lineage(routine.body_sql_redacted or "", dialect="tsql")
    await record_routine_parse_coverage(
        session,
        datasource=datasource,
        routine=routine,
        result=result,
        measured_by="agent:lineage",
    )
    await session.flush()

    coverage = await _coverage(session, routine.id)
    assert coverage is not None
    assert coverage.statement_count >= 1
    assert coverage.measured_by == "agent:lineage"


async def test_two_routines_keep_separate_measurements(session) -> None:
    datasource, schema = await _seed(session)
    good = _routine(datasource, schema, body=_READ_ONLY_BODY, name="usp_good")
    bad = _routine(datasource, schema, body=_DYNAMIC_BODY, name="usp_bad")
    session.add_all([good, bad])
    await session.flush()
    for routine in (good, bad):
        await parse_deep_procedure_lineage_endpoint(
            datasource.id, routine.id, _context(datasource), session
        )
    good_coverage = await _coverage(session, good.id)
    bad_coverage = await _coverage(session, bad.id)
    assert good_coverage is not None and good_coverage.parse_completed is True
    assert bad_coverage is not None and bad_coverage.parse_completed is False
