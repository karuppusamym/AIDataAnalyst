"""R11-FP07: real source-range maps -- where, in the stored body, each lineage edge came from.

Before this the only position anywhere was `statement_ordinal`, an integer statement index, and
the engine capability matrix recorded precise source mapping as unsupported. These tests pin
what replaced it and the two constraints that are the whole difficulty:

* **The offsets index the text that is stored** -- `body_sql_redacted`, the only text the parser
  is ever handed -- not the customer's source. A PARSED redaction re-renders the body, so the
  same INSERT sits on a different line in the stored form; a range computed on the raw text
  would point at the wrong place. Each located parse carries the SHA-256 of the text its ranges
  index, so a reader can prove a range is current before trusting it.
* **A position is only as good as the parse.** A statement the parser could not read is located
  as the gap it is (`GAP_STATEMENT`); a fact bound to no statement of this text carries no range
  and says `NOT_LOCATED`. A missing range is never a range of zero.

Every assertion here fails on the tree before R11-FP07: `ProcedureLineageEdgeRecord` had no
range, and the stored rows had no columns for one.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.envelope_models import MetadataRoutine
from aida.procedure_lineage import (
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    StatementRangeStatus,
    parse_procedure_lineage,
    parse_trigger_lineage,
    statement_text_digest,
    unparsed_marker_result,
)
from aida.procedure_lineage_api import (
    get_routine_parse_coverage,
    list_deep_procedure_lineage,
    parse_deep_procedure_lineage_endpoint,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge, StatementRangeColumns
from aida.routine_call_descent import Callee, descend_nested_calls
from aida.routine_lineage_edges import (
    SOURCE_MAPPING_GRANULARITY,
    UNLOCATED_SOURCE_MAPPING_GRANULARITY,
    reconcile_decided_edges,
    routine_edge_row,
)
from aida.sql_redaction import redact_for_storage
from tests.test_routine_parse_coverage import _context, _seed

# A raw T-SQL body whose literal spans three lines. Every line after it moves once
# the literal is redacted, and a PARSED redaction re-renders the whole layout.
RAW_TSQL = """CREATE PROCEDURE dbo.usp_load AS
BEGIN
    DECLARE @note NVARCHAR(200) = 'first line
second line
third line';
    IF @note IS NOT NULL
    BEGIN
        INSERT INTO dbo.orders_audit (order_id, amount)
        SELECT o.order_id, o.amount FROM dbo.orders o WHERE o.region = 'EMEA';
    END;
    EXEC(@sql);
END
"""


def _written(edges: list[ProcedureLineageEdgeRecord]) -> list[ProcedureLineageEdgeRecord]:
    return [edge for edge in edges if edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE]


# ---------------------------------------------------------------------------
# 1. Which text the offsets index: the stored one.
# ---------------------------------------------------------------------------


def test_a_range_slices_its_own_statement_out_of_the_text_that_was_parsed() -> None:
    result = parse_procedure_lineage(RAW_TSQL, dialect="tsql")

    [write, *_] = _written(result.edges)
    where = write.statement_range
    assert where is not None
    assert write.statement_range_status == StatementRangeStatus.STATEMENT.value
    located = RAW_TSQL[where.start_offset : where.end_offset]
    # The peel took `IF ... BEGIN` off the front: the range is the INSERT, not its guard.
    assert located.startswith("INSERT INTO dbo.orders_audit")
    assert located.endswith("o.region = 'EMEA'")
    assert (where.start_line, where.start_column) == (8, 9)
    assert (where.end_line, where.end_column) == (9, 77)
    assert write.statement_text_digest == hashlib.sha256(RAW_TSQL.encode()).hexdigest()


def test_offsets_are_computed_against_the_stored_body_not_the_source() -> None:
    """The steward is shown the stored body. The same statement is somewhere else in it."""
    stored = redact_for_storage(RAW_TSQL, dialect="tsql")
    assert stored is not None and stored.redacted is not None
    assert stored.redacted != RAW_TSQL

    from_raw = _written(parse_procedure_lineage(RAW_TSQL, dialect="tsql").edges)[0]
    from_stored = _written(parse_procedure_lineage(stored.redacted, dialect="tsql").edges)[0]
    raw_range, stored_range = from_raw.statement_range, from_stored.statement_range
    assert raw_range is not None and stored_range is not None

    # The stored range points at the INSERT in the stored text ...
    assert stored.redacted[stored_range.start_offset :].startswith("INSERT INTO dbo.orders_audit")
    # ... and the raw range, applied to the stored text, would not -- which is the error
    # a range computed on the wrong text makes.
    assert not stored.redacted[raw_range.start_offset :].startswith("INSERT INTO")
    assert (raw_range.start_line, raw_range.start_column) != (
        stored_range.start_line,
        stored_range.start_column,
    )
    # The digest names the stored text, so a reader can check the pairing.
    assert from_stored.statement_text_digest == statement_text_digest(stored.redacted)
    assert from_stored.statement_text_digest != from_raw.statement_text_digest


def test_a_dollar_quoted_body_is_located_in_the_whole_definition() -> None:
    """`pg_get_functiondef` output: the ranges index the full CREATE FUNCTION text the
    connector stores, not the body the parser cut out of it."""
    sql = (
        "CREATE OR REPLACE FUNCTION s.refresh() RETURNS void LANGUAGE plpgsql AS $function$\n"
        "BEGIN\n"
        "  FOR rec IN SELECT a.id FROM s.a a LOOP\n"
        "    NULL;\n"
        "  END LOOP;\n"
        "  CREATE TEMP TABLE t ON COMMIT DROP AS SELECT x.id FROM s.x x;\n"
        "  INSERT INTO s.final (id) SELECT t.id FROM t;\n"
        "END\n"
        "$function$"
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    def text_of(edge: ProcedureLineageEdgeRecord) -> str:
        assert edge.statement_range is not None
        return sql[edge.statement_range.start_offset : edge.statement_range.end_offset]

    cursor = next(edge for edge in result.edges if edge.source_table == "s.a")
    # The loop's own query, located inside the FOR ... LOOP header it was peeled from.
    assert text_of(cursor) == "SELECT a.id FROM s.a a"
    assert cursor.statement_range is not None
    assert (cursor.statement_range.start_line, cursor.statement_range.start_column) == (3, 14)
    transitive = next(edge for edge in result.edges if edge.via_temp_table == "t")
    # A transitive edge sits where `statement_ordinal` says it does: the statement that
    # writes its target.
    assert transitive.source_table == "s.x"
    assert text_of(transitive) == "INSERT INTO s.final (id) SELECT t.id FROM t"
    assert transitive.statement_range_status == StatementRangeStatus.STATEMENT.value


def test_lines_end_at_newline_so_a_crlf_body_counts_the_same() -> None:
    lf = "CREATE PROCEDURE dbo.p AS\nBEGIN\n  SELECT c.id\n  FROM dbo.c c;\nEND"
    crlf = lf.replace("\n", "\r\n")
    [lf_edge] = parse_procedure_lineage(lf, dialect="tsql").edges
    [crlf_edge] = parse_procedure_lineage(crlf, dialect="tsql").edges
    assert lf_edge.statement_range is not None and crlf_edge.statement_range is not None
    lf_pos = lf_edge.statement_range
    crlf_pos = crlf_edge.statement_range
    assert (lf_pos.start_line, lf_pos.start_column, lf_pos.end_line) == (3, 3, 4)
    assert (crlf_pos.start_line, crlf_pos.start_column, crlf_pos.end_line) == (3, 3, 4)
    # The end column is the statement's last character, inclusive.
    assert lf_pos.end_column == len("  FROM dbo.c c")


# ---------------------------------------------------------------------------
# 2. A position is only as good as the parse.
# ---------------------------------------------------------------------------


def test_an_unparsed_statement_is_located_as_the_gap_it_is() -> None:
    result = parse_procedure_lineage(RAW_TSQL, dialect="tsql")
    [marker] = [e for e in result.edges if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE]
    assert marker.statement_range_status == StatementRangeStatus.GAP_STATEMENT.value
    assert marker.statement_range is not None
    where = marker.statement_range
    assert RAW_TSQL[where.start_offset : where.end_offset] == "EXEC(@sql)"


def test_a_body_that_was_never_reached_has_no_range_rather_than_a_zero_one() -> None:
    result = unparsed_marker_result(reason="NESTED_PROCEDURE_CALL: x (NOT_CAPTURED)",
                                    dialect="postgres", sql_hash="h")
    [marker] = result.edges
    assert marker.statement_range is None
    assert marker.statement_range_status == StatementRangeStatus.NOT_LOCATED.value
    assert marker.statement_text_digest is None
    assert result.statement_text_digest is None


def test_a_finding_about_the_whole_body_is_not_pinned_to_one_statement() -> None:
    body = (
        "BEGIN INSERT INTO app.audit (id) SELECT o.id FROM app.orders o "
        "WHERE o.id = :NEW.id; END"
    )
    result = parse_trigger_lineage(body, dialect="oracle", firing_table="app.orders")
    [subject] = [
        edge for edge in result.edges
        if (edge.unparsed_reason or "").startswith("UNRESOLVED_TRIGGER_SUBJECT")
    ]
    assert subject.statement_range is None
    assert subject.statement_range_status == StatementRangeStatus.NOT_LOCATED.value
    # The statements that *were* read are still located.
    assert any(edge.statement_range is not None for edge in result.edges if edge is not subject)


def test_a_called_routines_edge_is_located_at_the_call_in_the_callers_text() -> None:
    """The callee's own offsets index the callee's body. Carried over, they would point at
    the right characters of the wrong routine."""
    caller = "CREATE PROCEDURE dbo.outer AS\nBEGIN\n  EXEC dbo.inner;\nEND"
    callee = (
        "CREATE PROCEDURE dbo.inner AS\nBEGIN\n"
        "  INSERT INTO dbo.t (a) SELECT s.a FROM dbo.s s;\nEND"
    )
    root = parse_procedure_lineage(caller, dialect="tsql")

    def resolve(name: str) -> Callee:
        return Callee("inner", "dbo.inner", callee)

    result = descend_nested_calls(root, dialect="tsql", resolve=resolve, root_key="outer")
    [read_through] = [edge for edge in result.edges if edge.via_routine == "dbo.inner"]
    assert read_through.statement_range_status == StatementRangeStatus.CALL_SITE.value
    assert read_through.statement_range is not None
    where = read_through.statement_range
    assert caller[where.start_offset : where.end_offset] == "EXEC dbo.inner"
    assert read_through.statement_text_digest == statement_text_digest(caller)
    assert result.statement_text_digest == statement_text_digest(caller)


def test_a_case_expression_no_longer_truncates_the_body_it_is_in() -> None:
    """Found building the ranges: a CASE expression's bare END closed the procedure's BEGIN,
    so every statement after it was never seen -- the silent truncation this parser exists to
    prevent. Before the fix this body produced one UNPARSED marker and nothing else."""
    sql = (
        "CREATE PROCEDURE dbo.p AS\nBEGIN\n"
        "  INSERT INTO dbo.a (x) SELECT CASE WHEN s.k = 1 THEN s.v ELSE 0 END FROM dbo.s s;\n"
        "  INSERT INTO dbo.b (y) SELECT t.y FROM dbo.t t;\n"
        "END"
    )
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert result.is_fully_parsed is True
    assert {edge.target_table for edge in result.edges} == {"dbo.a", "dbo.b"}


# ---------------------------------------------------------------------------
# 3. Storage: positions, not text; NULL, not zero.
# ---------------------------------------------------------------------------


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


def _row(edge: ProcedureLineageEdgeRecord) -> DeepProcedureLineageEdge:
    return routine_edge_row(
        edge,
        organization_id=uuid4(),
        datasource_id=uuid4(),
        routine_id=uuid4(),
        sql_hash="h",
        table_ids={},
        review_status="ACTIVE",
        created_by="tester",
    )


def test_a_stored_row_carries_the_range_and_an_unlocated_one_carries_nulls() -> None:
    result = parse_procedure_lineage(RAW_TSQL, dialect="tsql")
    write = _written(result.edges)[0]
    row = _row(write)
    assert write.statement_range is not None
    assert (row.statement_start_line, row.statement_start_column) == (8, 9)
    assert (row.statement_start_offset, row.statement_end_offset) == (
        write.statement_range.start_offset,
        write.statement_range.end_offset,
    )
    assert row.statement_range_status == "STATEMENT"
    assert row.statement_text_digest == write.statement_text_digest

    unreached = _row(unparsed_marker_result(reason="X", dialect="tsql", sql_hash="h").edges[0])
    positions = [getattr(unreached, name) for name in (
        "statement_start_offset", "statement_end_offset", "statement_start_line",
        "statement_start_column", "statement_end_line", "statement_end_column",
    )]
    assert positions == [None] * 6
    assert unreached.statement_range_status == "NOT_LOCATED"
    assert unreached.statement_text_digest is None


def test_no_new_column_can_hold_body_text() -> None:
    """INV-6: a line/column offset is not a value; an excerpt would be. The range columns are
    integers, a status code and a fixed-width digest -- nothing wide enough for a statement."""
    columns = {
        column.name: column
        for column in DeepProcedureLineageEdge.__table__.columns
        if column.name.startswith("statement_") and column.name != "statement_ordinal"
    }
    assert set(columns) == {
        name for name in vars(StatementRangeColumns) if name.startswith("statement_")
    }
    for name, column in columns.items():
        width = getattr(column.type, "length", None)
        assert width is None or width <= 64, name
    rendered = " ".join(str(getattr(_row(e), c)) for e in parse_procedure_lineage(
        RAW_TSQL, dialect="tsql").edges for c in columns)
    assert "EMEA" not in rendered and "INSERT" not in rendered.upper()


async def test_the_parse_route_stores_ranges_and_the_list_route_returns_them(session) -> None:
    datasource, schema = await _seed(session)
    stored = redact_for_storage(RAW_TSQL, dialect="tsql")
    assert stored is not None and stored.redacted is not None
    routine = MetadataRoutine(
        id=uuid4(), organization_id=datasource.organization_id, datasource_id=datasource.id,
        schema_id=schema.id, name="usp_load", routine_type="PROCEDURE",
        body_sql_redacted=stored.redacted, redaction_status=stored.status,
        screening_status="CLEAN", availability="AVAILABLE", status="ACTIVE", fingerprint="fp",
    )
    session.add(routine)
    await session.flush()

    response = await parse_deep_procedure_lineage_endpoint(
        datasource.id, routine.id, _context(datasource), session
    )
    assert response.statement_text_digest == statement_text_digest(stored.redacted)

    listed = await list_deep_procedure_lineage(
        datasource.id, routine.id, 200, 0, _context(datasource), session
    )
    located = [edge for edge in listed if edge.statement_range is not None]
    assert located, "a parse of a stored body stores where its statements are"
    for edge in located:
        assert edge.statement_range is not None
        start = edge.statement_range.start_offset
        end = edge.statement_range.end_offset
        # Every stored range slices a statement out of the body that is stored.
        assert stored.redacted[start:end].strip() == stored.redacted[start:end]
        assert edge.statement_text_digest == statement_text_digest(stored.redacted)

    coverage = await get_routine_parse_coverage(
        datasource.id, routine.id, _context(datasource), session
    )
    assert coverage.source_mapping_granularity == SOURCE_MAPPING_GRANULARITY == "STATEMENT_RANGE"
    assert UNLOCATED_SOURCE_MAPPING_GRANULARITY == "STATEMENT_ORDINAL"


async def test_a_decided_edge_found_again_is_re_pointed_at_where_it_now_is(session) -> None:
    """A person decided the fact, not the line number. When the body moves -- here, a
    comment added above the statement -- a decided edge the new parse finds again follows it."""
    datasource, schema = await _seed(session)
    before = (
        "CREATE PROCEDURE dbo.p AS\nBEGIN\n  INSERT INTO dbo.a (x) SELECT s.x FROM dbo.s s;\nEND"
    )
    after = before.replace("BEGIN\n", "BEGIN\n  -- nightly load\n  -- owner: risk\n")
    first = parse_procedure_lineage(before, dialect="tsql")
    [edge] = first.edges
    row = routine_edge_row(
        edge, organization_id=datasource.organization_id, datasource_id=datasource.id,
        routine_id=uuid4(), sql_hash=first.sql_hash, table_ids={}, review_status="ACTIVE",
        created_by="tester",
    )
    routine = MetadataRoutine(
        id=row.routine_id, organization_id=datasource.organization_id,
        datasource_id=datasource.id, schema_id=schema.id, name="p", routine_type="PROCEDURE",
        body_sql_redacted=after, redaction_status="LEXICAL", screening_status="CLEAN",
        availability="AVAILABLE", status="ACTIVE", fingerprint="fp",
    )
    session.add_all([routine, row])
    await session.flush()
    assert row.statement_start_line == 3

    second = parse_procedure_lineage(after, dialect="tsql")
    await reconcile_decided_edges(
        session, model=DeepProcedureLineageEdge, datasource=datasource, owner_id=routine.id,
        result=second, produced=second.edges,
    )
    stored = (await session.scalars(select(DeepProcedureLineageEdge))).one()
    assert stored.review_status == "ACTIVE"
    assert stored.statement_start_line == 5
    assert stored.statement_text_digest == statement_text_digest(after)
