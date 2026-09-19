"""R11-FP07 token-grain ranges: inside its statement, where each end of a lineage edge is named.

The statement range (`test_r11_fp07_statement_ranges.py`) says which statement of the stored
body an edge came from. This file pins the remaining clause of the row -- "a range is the
statement's span, not the column or table token inside it" -- and the rules that make a token
range safe to show a reviewer:

* **The offsets index the stored, redacted body**, exactly like the statement range: the text
  the parser was handed. A redaction that re-renders the body, or a lexical scrub whose
  placeholder is not the literal's length, moves every token after it; a token computed on the
  raw source would highlight the wrong characters.
* **A token is recorded only when exactly one reference can be the edge's evidence.** The same
  table named twice, one column read twice in one expression, a MERGE naming its target column
  in both branches -- all NULL, never the first occurrence. A transitive edge has no source
  token (its source is read in another statement); an edge read from a called routine has
  neither (the callee's positions index another text); an unparsed statement has neither.
* **A position is proved before it is kept.** sqlglot's identifier positions are relative to
  the string it parsed, which for PL/pgSQL is rewritten; every identifier must slice out of the
  stored text unchanged, or no token of that statement is recorded.

Every test here fails on the tree before this change: `ProcedureLineageEdgeRecord` had no
token range, and neither edge table had a column for one.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import sqlglot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.envelope_models import MetadataRoutine
from aida.procedure_lineage import (
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    StatementRangeStatus,
    parse_procedure_lineage,
    parse_trigger_lineage,
    unparsed_marker_result,
)
from aida.procedure_lineage_api import (
    list_deep_procedure_lineage,
    parse_deep_procedure_lineage_endpoint,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge, TriggerLineageEdge
from aida.procedure_token_ranges import (
    PARSED_TEXT_META,
    TokenRange,
    TokenRangeKind,
    locate_edge_tokens,
    remember_parsed_text,
)
from aida.routine_call_descent import Callee, descend_nested_calls
from aida.routine_lineage_edges import reconcile_decided_edges, routine_edge_row, trigger_edge_row
from aida.sql_redaction import redact_for_storage
from tests.test_r11_fp07_statement_ranges import RAW_TSQL
from tests.test_routine_parse_coverage import _context, _seed

COLUMN = TokenRangeKind.COLUMN.value
TABLE = TokenRangeKind.TABLE.value


def _edge(
    result: ProcedureParseResult,
    source: str,
    target: str,
    *,
    via_temp_table: str | None = None,
) -> ProcedureLineageEdgeRecord:
    """The one edge `source_table.source_column -> target_table.target_column`."""
    [found] = [
        edge
        for edge in result.edges
        if f"{edge.source_table}.{edge.source_column}" == source
        and f"{edge.target_table}.{edge.target_column}" == target
        and edge.via_temp_table == via_temp_table
    ]
    return found


def _slice(text: str, token: TokenRange | None) -> str | None:
    return None if token is None else text[token.start_offset : token.end_offset]


def _tokens(text: str, edge: ProcedureLineageEdgeRecord) -> tuple[str | None, str | None]:
    return _slice(text, edge.source_token_range), _slice(text, edge.target_token_range)


def _kinds(edge: ProcedureLineageEdgeRecord) -> tuple[str | None, str | None]:
    source, target = edge.source_token_range, edge.target_token_range
    return (source.kind if source else None, target.kind if target else None)


def _assert_inside_statements(result: ProcedureParseResult) -> None:
    """A token range refines a statement range: never without one, never outside it."""
    for edge in result.edges:
        for token in (edge.source_token_range, edge.target_token_range):
            if token is None:
                continue
            where = edge.statement_range
            assert where is not None, edge
            assert where.start_offset <= token.start_offset < token.end_offset
            assert token.end_offset <= where.end_offset, edge


# ---------------------------------------------------------------------------
# 1. PL/pgSQL: each rewrite the parser makes keeps the positions after it.
# ---------------------------------------------------------------------------

PLPGSQL = (
    "CREATE OR REPLACE FUNCTION s.refresh() RETURNS SETOF s.final LANGUAGE plpgsql AS $function$\n"
    "DECLARE v_total numeric; v_run int;\n"
    "BEGIN\n"
    "  CREATE TEMP TABLE t ON COMMIT DROP AS SELECT x.id, x.amount AS amt FROM s.x x;\n"
    "  INSERT INTO s.final (id, total) SELECT t.id, t.amt FROM t;\n"
    "  SELECT sum(o.amount) INTO STRICT v_total FROM s.orders o WHERE o.status > 0;\n"
    "  INSERT INTO s.runs (started_by) SELECT u.name FROM s.users u RETURNING id INTO v_run;\n"
    "  v_total := (SELECT max(p.amount) FROM s.payments p);\n"
    "  RETURN QUERY SELECT f.id, f.total FROM s.final f;\n"
    "END\n"
    "$function$"
)


def test_plpgsql_tokens_slice_their_references_out_of_the_definition() -> None:
    result = parse_procedure_lineage(PLPGSQL, dialect="postgres")
    _assert_inside_statements(result)

    # `ON COMMIT DROP` is blanked, not cut: the columns after it are still where they are.
    amount = _edge(result, "s.x.amount", "t.amt")
    assert _tokens(PLPGSQL, amount) == ("x.amount", "amt")
    assert _kinds(amount) == (COLUMN, COLUMN)
    # An INSERT column list names the target, not the projection feeding it.
    assert _tokens(PLPGSQL, _edge(result, "t.amt", "s.final.total")) == ("t.amt", "total")
    # `INTO STRICT`: the WHERE column after it is located.
    assert _tokens(PLPGSQL, _edge(result, "s.orders.status", "<LOCAL>.<FILTER_PREDICATE>")) == (
        "o.status",
        None,  # a local variable is not a table: no target token
    )
    assert _tokens(PLPGSQL, _edge(result, "s.orders.amount", "<LOCAL>._col0"))[0] == "o.amount"
    # `RETURNING id INTO v_run` at the tail: cutting it would have shifted every token.
    assert _tokens(PLPGSQL, _edge(result, "s.users.name", "s.runs.started_by")) == (
        "u.name",
        "started_by",
    )
    # `v := (...)` is parsed as `SELECT (...)`, a different prefix: still located.
    assert _tokens(PLPGSQL, _edge(result, "s.payments.amount", "<LOCAL>._col0"))[0] == "p.amount"
    # `RETURN QUERY` is cut from the front; the result columns are named by the projection.
    assert _tokens(PLPGSQL, _edge(result, "s.final.id", "<RESULT>.id")) == ("f.id", "id")


def test_a_transitive_edge_keeps_its_target_token_and_has_no_source_token() -> None:
    """Its source is read in the CREATE TEMP TABLE statement, outside the statement it is
    located at -- a source token there would point into a different statement."""
    result = parse_procedure_lineage(PLPGSQL, dialect="postgres")
    transitive = _edge(result, "s.x.amount", "s.final.total", via_temp_table="t")
    direct = _edge(result, "t.amt", "s.final.total")
    assert transitive.source_token_range is None
    assert transitive.target_token_range == direct.target_token_range
    assert _slice(PLPGSQL, transitive.target_token_range) == "total"


# ---------------------------------------------------------------------------
# 2. Another dialect: T-SQL targets, temp tables and table-grain edges.
# ---------------------------------------------------------------------------

TSQL = (
    "CREATE PROCEDURE dbo.usp_copy AS\n"
    "BEGIN\n"
    "    UPDATE o SET o.total = s.amt FROM dbo.orders o JOIN dbo.src s ON s.id = o.id\n"
    "        WHERE o.id > 0 AND s.id < 100;\n"
    "    SELECT * INTO #stage FROM dbo.src;\n"
    "    INSERT INTO dbo.final (id) SELECT st.id FROM #stage st;\n"
    "    DELETE FROM dbo.final WHERE id IN (SELECT r.id FROM dbo.rejects r);\n"
    "END"
)


def test_tsql_tokens_name_columns_tables_and_temp_tables_as_written() -> None:
    result = parse_procedure_lineage(TSQL, dialect="tsql")
    _assert_inside_statements(result)
    # The SET assignment names the target column; the target reference is as written.
    assert _tokens(TSQL, _edge(result, "dbo.src.amt", "dbo.orders.total")) == ("s.amt", "o.total")
    # A table-grain edge is located at table references, a temp table with its `#`.
    star = _edge(result, "dbo.src.*", "stage.*")
    assert _tokens(TSQL, star) == ("dbo.src", "#stage")
    assert _kinds(star) == (TABLE, TABLE)
    assert _tokens(TSQL, _edge(result, "stage.id", "dbo.final.id")) == ("st.id", "id")
    # A FILTERED edge's target is the table the statement writes, named as the statement
    # names it. Two `id` references sit in that WHERE, each qualified by a different table,
    # so each edge is located at its own.
    orders = _edge(result, "dbo.orders.id", "dbo.orders.<FILTER_PREDICATE>")
    assert _tokens(TSQL, orders) == ("o.id", "o")
    assert _kinds(orders) == (COLUMN, TABLE)
    assert _tokens(TSQL, _edge(result, "dbo.src.id", "dbo.orders.<FILTER_PREDICATE>")) == (
        "s.id",
        "o",
    )


def test_an_unqualified_column_and_a_qualified_one_are_two_facts_each_at_its_token() -> None:
    """Found building this: `WHERE id IN (SELECT r.id FROM dbo.rejects r)` holds two
    references. This test first pinned them as one fact, `dbo.rejects.id` filtering
    `dbo.final`, with no single token -- the unqualified `id` was attributed to "the
    statement's one source", which excluded the DELETE's own target and counted a table only
    the subquery names. That fact was wrong (2026-09-19, `procedure_column_owners`): the outer
    `id` is in the DELETE's scope, where only `dbo.final` is, so it is `dbo.final.id`. Two
    facts now, and each has exactly one reference to be located at."""
    result = parse_procedure_lineage(TSQL, dialect="tsql")
    facts = {
        (edge.source_table, edge.source_column): edge
        for edge in result.edges
        if edge.target_column == "<FILTER_PREDICATE>" and edge.target_table == "dbo.final"
    }
    assert set(facts) == {("dbo.final", "id"), ("dbo.rejects", "id")}
    assert _tokens(TSQL, facts[("dbo.final", "id")]) == ("id", "dbo.final")
    assert _tokens(TSQL, facts[("dbo.rejects", "id")]) == ("r.id", "dbo.final")


def test_an_oracle_merge_names_its_target_column_twice_so_it_is_not_located() -> None:
    """`v` is named by the UPDATE branch's SET and by the INSERT branch's column list; which
    one a reviewer should see is not something the parse knows. Each source is in exactly one
    branch, so each is located."""
    body = (
        "BEGIN\n"
        "  MERGE INTO app.t d USING app.s s ON (d.id = s.id)\n"
        "  WHEN MATCHED THEN UPDATE SET d.v = s.v\n"
        "  WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.w);\n"
        "END;"
    )
    result = parse_procedure_lineage(body, dialect="oracle")
    _assert_inside_statements(result)
    assert _tokens(body, _edge(result, "app.s.v", "app.t.v")) == ("s.v", None)
    assert _tokens(body, _edge(result, "app.s.w", "app.t.v")) == ("s.w", None)
    assert _tokens(body, _edge(result, "app.s.id", "app.t.id")) == ("s.id", "id")


# ---------------------------------------------------------------------------
# 3. The NULL rule: exactly one candidate, or nothing.
# ---------------------------------------------------------------------------

TWICE = (
    "CREATE PROCEDURE dbo.usp_twice AS\n"
    "BEGIN\n"
    "    INSERT INTO dbo.pairs (a, b) SELECT x1.v, x2.v\n"
    "        FROM dbo.x x1 JOIN dbo.x x2 ON x1.id = x2.pid;\n"
    "    INSERT INTO dbo.copy SELECT * FROM dbo.x x1 JOIN dbo.x x2 ON x1.id = x2.pid;\n"
    "    INSERT INTO dbo.first (v) SELECT COALESCE(x1.v, x2.v)\n"
    "        FROM dbo.x x1 JOIN dbo.x x2 ON x1.id = x2.pid;\n"
    "    INSERT INTO dbo.net (n) SELECT o.amount - o.amount * 0.1 FROM dbo.orders o;\n"
    "    INSERT INTO dbo.kept (amount) SELECT o.amount FROM dbo.orders o WHERE o.amount > 0;\n"
    "END"
)


def test_a_table_named_twice_is_not_located_but_its_distinct_references_are() -> None:
    result = parse_procedure_lineage(TWICE, dialect="tsql")
    _assert_inside_statements(result)
    # The same table under two aliases: each output column still reads one reference.
    assert _tokens(TWICE, _edge(result, "dbo.x.v", "dbo.pairs.a")) == ("x1.v", "a")
    assert _tokens(TWICE, _edge(result, "dbo.x.v", "dbo.pairs.b")) == ("x2.v", "b")
    # A table-grain read of a table named twice: which of the two is the evidence is unknown.
    star = _edge(result, "dbo.x.*", "dbo.copy.*")
    assert star.source_token_range is None
    assert _slice(TWICE, star.target_token_range) == "dbo.copy"
    # One fact, two references to the same table's column: NULL, not the first one.
    assert _tokens(TWICE, _edge(result, "dbo.x.v", "dbo.first.v")) == (None, "v")


def test_an_output_column_the_parse_named_itself_is_never_pinned_to_a_lookalike() -> None:
    """`sum(x.v)` has no name, so the parse calls it `_col0`. A subquery that happens to
    alias something `_col0` is not where that name came from: counted as a rival, never
    located as the name."""
    body = (
        "CREATE PROCEDURE dbo.p AS\nBEGIN\n"
        "  SELECT sum(x.v) FROM (SELECT a.v, a.k AS _col0 FROM dbo.a a) x;\nEND"
    )
    result = parse_procedure_lineage(body, dialect="tsql")
    [edge] = [e for e in result.edges if e.source_column == "v"]
    assert edge.target_column == "_col0"
    assert edge.target_token_range is None


def test_a_column_read_twice_in_one_expression_is_not_located() -> None:
    result = parse_procedure_lineage(TWICE, dialect="tsql")
    assert _tokens(TWICE, _edge(result, "dbo.orders.amount", "dbo.net.n")) == (None, "n")


def test_a_column_also_read_by_the_where_clause_is_located_where_it_is_projected() -> None:
    """The evidence for `amount -> kept.amount` is the projection, not the filter; the search
    is scoped to what can produce the target column, so the WHERE reference is no rival."""
    result = parse_procedure_lineage(TWICE, dialect="tsql")
    kept = _edge(result, "dbo.orders.amount", "dbo.kept.amount")
    assert _tokens(TWICE, kept) == ("o.amount", "amount")
    assert kept.source_token_range is not None
    statement = TWICE[kept.statement_range.start_offset : kept.statement_range.end_offset]  # type: ignore[union-attr]
    offset_in_statement = kept.source_token_range.start_offset - kept.statement_range.start_offset  # type: ignore[union-attr]
    assert offset_in_statement < statement.index("WHERE")


# ---------------------------------------------------------------------------
# 4. Quoted identifiers are located with their quotes.
# ---------------------------------------------------------------------------


def test_quoted_identifiers_are_located_as_written() -> None:
    body = (
        "CREATE FUNCTION s.load() RETURNS void LANGUAGE plpgsql AS $$\n"
        "BEGIN\n"
        '  INSERT INTO s."Order Items" ("Line Total")\n'
        '    SELECT "O"."Net Amount" * 2 FROM s."Orders" "O";\n'
        '  INSERT INTO s."Archive" SELECT * FROM s."Orders";\n'
        "END $$"
    )
    result = parse_procedure_lineage(body, dialect="postgres")
    _assert_inside_statements(result)
    assert _tokens(body, _edge(result, "s.Orders.Net Amount", "s.Order Items.Line Total")) == (
        '"O"."Net Amount"',
        '"Line Total"',
    )
    assert _tokens(body, _edge(result, "s.Orders.*", "s.Archive.*")) == (
        's."Orders"',
        's."Archive"',
    )

    bracketed = (
        "CREATE PROCEDURE dbo.p AS\nBEGIN\n"
        "  INSERT INTO [dbo].[audit] ([Big Amount]) SELECT o.[Big Amount] FROM [dbo].[orders] o;\n"
        "END"
    )
    tsql = parse_procedure_lineage(bracketed, dialect="tsql")
    assert _tokens(bracketed, _edge(tsql, "dbo.orders.Big Amount", "dbo.audit.Big Amount")) == (
        "o.[Big Amount]",
        "[Big Amount]",
    )


# ---------------------------------------------------------------------------
# 5. Which text: the stored one, including when redaction moved it.
# ---------------------------------------------------------------------------


def test_tokens_index_the_re_rendered_stored_body_not_the_source() -> None:
    """A PARSED redaction re-renders the T-SQL body: the same references sit elsewhere."""
    stored = redact_for_storage(RAW_TSQL, dialect="tsql")
    assert stored is not None and stored.redacted is not None and stored.status == "PARSED"
    from_raw = _edge(
        parse_procedure_lineage(RAW_TSQL, dialect="tsql"),
        "dbo.orders.amount",
        "dbo.orders_audit.amount",
    )
    from_stored = _edge(
        parse_procedure_lineage(stored.redacted, dialect="tsql"),
        "dbo.orders.amount",
        "dbo.orders_audit.amount",
    )
    assert _tokens(stored.redacted, from_stored) == ("o.amount", "amount")
    assert _tokens(RAW_TSQL, from_raw) == ("o.amount", "amount")
    # The raw parse's offsets, applied to the stored body, highlight something else.
    assert from_raw.source_token_range != from_stored.source_token_range
    assert _slice(stored.redacted, from_raw.source_token_range) != "o.amount"


def test_tokens_index_a_lexically_scrubbed_body_whose_placeholders_moved_them() -> None:
    """A LEXICAL scrub replaces each literal with a placeholder of another length, so every
    token after one moves; the stored body's own parse locates them where they now are, and
    the statement it could not read has no token at all."""
    raw = (
        "CREATE FUNCTION s.f() RETURNS void LANGUAGE plpgsql AS $$\n"
        "BEGIN\n"
        "  EXECUTE format('DELETE FROM %I WHERE note = %L', 'stage', 'a much longer literal');\n"
        "  INSERT INTO s.t (a) SELECT q.a FROM s.q q WHERE q.k = 'another literal';\n"
        "END $$"
    )
    stored = redact_for_storage(raw, dialect="postgres")
    assert stored is not None and stored.redacted is not None and stored.status == "LEXICAL"
    text = stored.redacted
    result = parse_procedure_lineage(text, dialect="postgres")
    _assert_inside_statements(result)
    located = _edge(result, "s.q.a", "s.t.a")
    assert _tokens(text, located) == ("q.a", "a")
    assert located.source_token_range is not None
    assert located.source_token_range.start_offset != raw.index("q.a")
    [gap] = [e for e in result.edges if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE]
    assert gap.statement_range_status == StatementRangeStatus.GAP_STATEMENT.value
    assert (gap.source_token_range, gap.target_token_range) == (None, None)


# ---------------------------------------------------------------------------
# 6. Never a wrong token: unparsed, unreached, unaligned and called-through edges.
# ---------------------------------------------------------------------------


def test_unparsed_and_unreached_edges_have_no_tokens() -> None:
    parsed = parse_procedure_lineage(RAW_TSQL, dialect="tsql")
    [marker] = [e for e in parsed.edges if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE]
    assert (marker.source_token_range, marker.target_token_range) == (None, None)
    unreached = unparsed_marker_result(reason="X", dialect="tsql", sql_hash="h").edges[0]
    assert (unreached.source_token_range, unreached.target_token_range) == (None, None)
    unreadable = parse_procedure_lineage(
        "CREATE PROCEDURE dbo.p AS\nBEGIN\n  INSERT INTO dbo.t (a) SELEC s.a FROM dbo.s s;\nEND",
        dialect="tsql",
    )
    assert all(
        (edge.source_token_range, edge.target_token_range) == (None, None)
        for edge in unreadable.edges
    )


def test_a_statement_whose_positions_do_not_align_yields_no_token_at_all() -> None:
    """The proof step: one identifier that does not slice out of the text unchanged and the
    whole statement goes unlocated -- no partial trust in a misaligned parse."""
    statement = "SELECT a.x FROM s.a a"
    node = sqlglot.parse_one(statement, dialect="postgres")
    edge = ProcedureLineageEdgeRecord(
        source_table="s.a", source_column="x", target_table="<RESULT>", target_column="x",
        transformation_type="DIRECT", confidence="FULL", dialect="postgres",
        source_resolved=True, statement_ordinal=0, is_write=False, is_intermediate=False,
    )
    aliases = {"a": "s.a", "s.a": "s.a"}
    # Never told what it was parsed from: nothing to align, nothing located.
    assert locate_edge_tokens(
        node, [edge], text=statement, start=0, end=len(statement), aliases=aliases
    ) == [(None, None)]
    remember_parsed_text(node, statement)
    assert node.meta[PARSED_TEXT_META] == statement
    [(source, target)] = locate_edge_tokens(
        node, [edge], text=statement, start=0, end=len(statement), aliases=aliases
    )
    assert _slice(statement, source) == "a.x" and _slice(statement, target) == "x"
    # The same parse against a text whose identifier differs at the same place.
    moved = "SELECT a.y FROM s.a a"
    assert locate_edge_tokens(
        node, [edge], text=moved, start=0, end=len(moved), aliases=aliases
    ) == [(None, None)]


def test_an_edge_read_from_a_called_routine_carries_none_of_the_callees_tokens() -> None:
    caller = "CREATE PROCEDURE dbo.outer AS\nBEGIN\n  EXEC dbo.inner;\nEND"
    callee = (
        "CREATE PROCEDURE dbo.inner AS\nBEGIN\n"
        "  INSERT INTO dbo.t (a) SELECT s.a FROM dbo.s s;\nEND"
    )
    [own] = parse_procedure_lineage(callee, dialect="tsql").edges
    assert own.source_token_range is not None  # located in the callee's own text ...
    result = descend_nested_calls(
        parse_procedure_lineage(caller, dialect="tsql"),
        dialect="tsql",
        resolve=lambda name: Callee("inner", "dbo.inner", callee),
        root_key="outer",
    )
    [read_through] = [edge for edge in result.edges if edge.via_routine == "dbo.inner"]
    assert read_through.statement_range_status == StatementRangeStatus.CALL_SITE.value
    # ... and not carried into the caller's, where those offsets index other text.
    assert (read_through.source_token_range, read_through.target_token_range) == (None, None)


def test_a_trigger_body_locates_its_firing_row_references() -> None:
    body = (
        "CREATE TRIGGER dbo.tr ON dbo.orders AFTER INSERT AS\nBEGIN\n"
        "  INSERT INTO dbo.audit (id) SELECT i.id FROM inserted i;\nEND"
    )
    result = parse_trigger_lineage(body, dialect="tsql", firing_table="dbo.orders")
    assert _tokens(body, _edge(result, "dbo.orders.id", "dbo.audit.id")) == ("i.id", "id")


# ---------------------------------------------------------------------------
# 7. Storage and the routes: positions, not text; NULL, not zero.
# ---------------------------------------------------------------------------

_TOKEN_COLUMNS = (
    "source_token_start_offset",
    "source_token_end_offset",
    "source_token_kind",
    "target_token_start_offset",
    "target_token_end_offset",
    "target_token_kind",
)


def _row(edge: ProcedureLineageEdgeRecord) -> DeepProcedureLineageEdge:
    return routine_edge_row(
        edge, organization_id=uuid4(), datasource_id=uuid4(), routine_id=uuid4(),
        sql_hash="h", table_ids={}, review_status="ACTIVE", created_by="tester",
    )


def test_a_stored_row_carries_both_token_ranges_and_an_unlocated_end_carries_nulls() -> None:
    result = parse_procedure_lineage(TWICE, dialect="tsql")
    kept = _edge(result, "dbo.orders.amount", "dbo.kept.amount")
    row = _row(kept)
    assert kept.source_token_range is not None and kept.target_token_range is not None
    assert (row.source_token_start_offset, row.source_token_end_offset, row.source_token_kind) == (
        kept.source_token_range.start_offset, kept.source_token_range.end_offset, COLUMN,
    )
    assert TWICE[row.target_token_start_offset : row.target_token_end_offset] == "amount"
    assert row.target_token_kind == COLUMN

    half = _row(_edge(result, "dbo.x.*", "dbo.copy.*"))
    assert (half.source_token_start_offset, half.source_token_end_offset) == (None, None)
    assert half.source_token_kind is None
    assert half.target_token_kind == TABLE

    unreached = _row(unparsed_marker_result(reason="X", dialect="tsql", sql_hash="h").edges[0])
    assert [getattr(unreached, name) for name in _TOKEN_COLUMNS] == [None] * 6

    trigger = parse_trigger_lineage(
        "CREATE TRIGGER dbo.tr ON dbo.orders AFTER INSERT AS\nBEGIN\n"
        "  INSERT INTO dbo.audit (id) SELECT i.id FROM inserted i;\nEND",
        dialect="tsql", firing_table="dbo.orders",
    )
    trigger_row = trigger_edge_row(
        trigger.edges[0], organization_id=uuid4(), datasource_id=uuid4(), trigger_id=uuid4(),
        routine_id=None, sql_hash="h", table_ids={}, review_status="ACTIVE", created_by="t",
    )
    assert trigger_row.source_token_kind == COLUMN
    assert trigger_row.target_token_kind == COLUMN


def test_no_token_column_can_hold_body_text() -> None:
    """INV-6: offsets and a ten-character kind code -- nothing wide enough for a statement."""
    for model in (DeepProcedureLineageEdge, TriggerLineageEdge):
        columns = {column.name: column for column in model.__table__.columns}
        for name in _TOKEN_COLUMNS:
            column = columns[name]
            assert column.nullable, name
            width = getattr(column.type, "length", None)
            assert width is None or width <= 10, name


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


async def test_the_parse_route_stores_token_ranges_and_the_list_route_returns_them(
    session,
) -> None:
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

    parsed = await parse_deep_procedure_lineage_endpoint(
        datasource.id, routine.id, _context(datasource), session
    )
    assert any(edge.source_token_range is not None for edge in parsed.edges)

    listed = await list_deep_procedure_lineage(
        datasource.id, routine.id, 200, 0, _context(datasource), session
    )
    by_fact = {(e.source_table, e.source_column, e.target_column): e for e in listed}
    amount = by_fact[("dbo.orders", "amount", "amount")]
    assert amount.source_token_range is not None and amount.target_token_range is not None
    source, target = amount.source_token_range, amount.target_token_range
    assert stored.redacted[source.start_offset : source.end_offset] == "o.amount"
    assert stored.redacted[target.start_offset : target.end_offset] == "amount"
    assert (source.kind, target.kind) == (COLUMN, COLUMN)
    for edge in listed:
        for token in (edge.source_token_range, edge.target_token_range):
            if token is not None:
                assert edge.statement_range is not None
                assert edge.statement_range.start_offset <= token.start_offset
                assert token.end_offset <= edge.statement_range.end_offset
    [gap] = [e for e in listed if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE]
    assert (gap.source_token_range, gap.target_token_range) == (None, None)


async def test_a_decided_edge_found_again_has_its_tokens_re_pointed(session) -> None:
    datasource, schema = await _seed(session)
    before = (
        "CREATE PROCEDURE dbo.p AS\nBEGIN\n  INSERT INTO dbo.a (x) SELECT s.x FROM dbo.s s;\nEND"
    )
    after = before.replace("BEGIN\n", "BEGIN\n  -- nightly load\n")
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
    assert before[row.source_token_start_offset : row.source_token_end_offset] == "s.x"

    second = parse_procedure_lineage(after, dialect="tsql")
    await reconcile_decided_edges(
        session, model=DeepProcedureLineageEdge, datasource=datasource, owner_id=routine.id,
        result=second, produced=second.edges,
    )
    kept = (await session.scalars(select(DeepProcedureLineageEdge))).one()
    assert after[kept.source_token_start_offset : kept.source_token_end_offset] == "s.x"
    assert after[kept.target_token_start_offset : kept.target_token_end_offset] == "x"
