"""R11-FP07 remainder: `FOREACH` loops, gap-marker identity, T-SQL `SET @v = (SELECT ...)`,
and a table only an `ON`/`GROUP BY`/`HAVING` clause reads.

Four defects the loop-record slice left in its `**Remaining:**` clause, in the order they
can make a routine's lineage *wrong* rather than merely thin:

* **`FOREACH x IN ARRAY <expr> LOOP ... END LOOP` was not a loop.** The header was never
  peeled, so it glued itself to the loop's first statement and the pair was one PARSE_ERROR
  gap -- every write inside a `FOREACH` was lost -- but its `END LOOP` was still counted:
  it closed the *enclosing* loop's record binding. A `rec.col` read after the `FOREACH` was
  then a plain variable (no edge), and where an inner loop shadows an outer record of the same
  name -- legal PL/pgSQL, the loop variable is scoped to its loop -- the popped binding
  revealed the *outer* loop's rows: a write recorded as coming from the wrong table.
* **Two gap markers from one statement shared a statement number**, so the `_dedupe_edges`
  key (and the stored natural key) collapsed them. `FROM dbo.fn1(1) f JOIN dbo.fn2(2) g`
  kept `fn1`; descent then read `fn1` through, dropped its marker, and the routine read as
  *fully parsed* with `fn2`'s sources never read.
* **`SET @v = (SELECT ...)` read no table.** The `SET` keyword marked the statement lineage-
  free, so a scalar subquery read into a variable -- and the tables it names -- was absent
  from every answer about who reads them, on a routine reported fully parsed and read-only.
* **A table only a clause the extractors do not read names got no edge.** `_table_rows_read`
  counted a table as *named* whenever any column reference resolved to it, but the edge
  extractors read only the select list and the WHERE: `JOIN b ON a.id = b.id`, `MERGE ... ON
  ... AND EXISTS (SELECT 1 FROM c WHERE c.k = s.k)`, `GROUP BY a.k` and `HAVING ... >
  (SELECT MAX(b.n) FROM b)` all left a table read that no edge mentioned.

Every test here fails on the tree before this change except those marked *guard*, which pin
what the change must not do.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida import routine_call_descent
from aida.envelope_models import MetadataRoutine
from aida.procedure_lineage import (
    CONDITION_CONTEXT,
    FOREACH_ARRAY_CONTEXT,
    PROCEDURE_LOCAL_TARGET,
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    is_routine_local,
    parse_procedure_lineage,
    walk_procedure_statements,
)
from aida.procedure_lineage_api import parse_deep_procedure_lineage_endpoint
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_call_descent import Callee, descend_nested_calls
from aida.routine_lineage_edges import routine_edge_key
from aida.sql_lineage_parser import (
    FILTER_EVIDENCE_TARGET_COLUMN,
    PROCEDURE_RESULT_TARGET,
    TransformationType,
)
from tests.test_routine_parse_coverage import _context, _seed
from tests.test_routine_parse_coverage import session as marker_db_session  # noqa: F401

ROWS = TransformationType.TABLE_ROWS.value
FILTER = FILTER_EVIDENCE_TARGET_COLUMN


def _real(result: ProcedureParseResult) -> list[ProcedureLineageEdgeRecord]:
    return [e for e in result.edges if e.transformation_type != UNPARSED_TRANSFORMATION_TYPE]


def _gaps(result: ProcedureParseResult) -> list[str]:
    return [
        e.unparsed_reason or ""
        for e in result.edges
        if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]


def _end_to_end(result: ProcedureParseResult) -> set[tuple[str, str, str, str, str | None]]:
    """Every edge into a table the routine writes, with what it was carried through."""
    return {
        (e.source_table, e.source_column, e.target_table, e.target_column, e.via_temp_table)
        for e in _real(result)
        if e.is_write and not e.is_intermediate
    }


def _flows(result: ProcedureParseResult) -> set[tuple[str, str]]:
    """`(source table, target table)` of every hop-joined end-to-end edge."""
    return {(s, t) for s, _c, t, _d, via in _end_to_end(result) if via is not None}


def _text(sql: str, token: Any) -> str | None:
    return None if token is None else sql[token.start_offset : token.end_offset]


def _rows(result: ProcedureParseResult) -> list[ProcedureLineageEdgeRecord]:
    return [e for e in result.edges if e.transformation_type == ROWS]


def _read_tables(result: ProcedureParseResult) -> set[tuple[str, str]]:
    """`(source, target)` of every table-grain read the parse recorded."""
    return {(e.source_table, e.target_table) for e in _rows(result)}


def _plpgsql(body: str, *, header: str = "FUNCTION s.f() RETURNS void", declare: str = "") -> str:
    section = f"DECLARE\n{declare}\n" if declare else ""
    return (
        f"CREATE OR REPLACE {header} LANGUAGE plpgsql AS $function$\n"
        f"{section}BEGIN\n{body}\nEND\n$function$"
    )


def _tsql(*statements: str) -> str:
    body = "".join(f"    {statement};\n" for statement in statements)
    return f"CREATE PROCEDURE dbo.p AS\nBEGIN\n{body}END"


# ---------------------------------------------------------------------------
# 1. FOREACH is a loop.
# ---------------------------------------------------------------------------


def test_a_foreach_loops_body_is_read_not_glued_to_its_header() -> None:
    sql = _plpgsql(
        "  FOREACH x IN ARRAY arr LOOP\n"
        "    INSERT INTO s.dst (a) SELECT t.a FROM s.t t WHERE t.k = x;\n"
        "  END LOOP;",
        declare="  arr int[]; x int;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, _gaps(result)
    assert ("s.t", "a", "s.dst", "a", None) in _end_to_end(result)


def test_a_foreach_end_loop_does_not_end_the_enclosing_loops_record() -> None:
    """The `END LOOP` belongs to the FOREACH. Counted as the FOR loop's, it dropped the FOR
    loop's record binding early, and the write that reads `rec.a` after the FOREACH lost its
    source."""
    sql = _plpgsql(
        "  FOR rec IN SELECT s.a FROM s.src s LOOP\n"
        "    FOREACH x IN ARRAY arr LOOP\n"
        "      NULL;\n"
        "    END LOOP;\n"
        "    INSERT INTO s.dst (a) VALUES (rec.a);\n"
        "  END LOOP;",
        declare="  rec record; arr int[]; x int;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, _gaps(result)
    assert ("s.src", "a", "s.dst", "a", "<LOCAL:rec@0>") in _end_to_end(result)


def test_a_foreach_inside_an_inner_loop_does_not_reveal_the_shadowed_outer_record() -> None:
    """The wrong-edge case. An inner FOR loop's variable shadows an outer one of the same
    name -- PL/pgSQL scopes it to its loop. Popped early by the FOREACH's END LOOP, the inner
    binding uncovered the outer's: `s.a.x -> s.out.x`, a path that does not exist."""
    sql = _plpgsql(
        "  FOR rec IN SELECT a.x FROM s.a a LOOP\n"
        "    FOR rec IN SELECT b.x FROM s.b b LOOP\n"
        "      FOREACH y IN ARRAY arr LOOP\n"
        "        NULL;\n"
        "      END LOOP;\n"
        "      INSERT INTO s.out (x) VALUES (rec.x);\n"
        "    END LOOP;\n"
        "  END LOOP;",
        declare="  rec record; arr int[]; y int;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, _gaps(result)
    assert _flows(result) == {("s.b", "s.out")}


def test_a_foreach_between_two_nested_loops_keeps_both_records() -> None:
    sql = _plpgsql(
        "  FOR o IN SELECT h.id, h.region FROM s.orders h LOOP\n"
        "    FOREACH y IN ARRAY arr LOOP\n"
        "      NULL;\n"
        "    END LOOP;\n"
        "    FOR l IN SELECT i.qty FROM s.lines i WHERE i.oid = o.id LOOP\n"
        "      INSERT INTO s.out (region, qty) VALUES (o.region, l.qty);\n"
        "    END LOOP;\n"
        "  END LOOP;",
        declare="  o record; l record; arr int[]; y int;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, _gaps(result)
    flows = {(s, c, d) for s, c, _t, d, via in _end_to_end(result) if via is not None}
    assert {("s.orders", "region", "region"), ("s.lines", "qty", "qty")} <= flows


def test_a_foreach_array_that_holds_a_query_is_read() -> None:
    """The array a FOREACH walks is an expression, and `(SELECT array_agg(...) FROM t)` is the
    usual one: the routine depends on `t`, and into routine-local state its rows go."""
    sql = _plpgsql(
        "  FOREACH x IN ARRAY (SELECT array_agg(t.id) FROM s.t t WHERE t.live) LOOP\n"
        "    NULL;\n"
        "  END LOOP;",
        declare="  x int;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, _gaps(result)
    read = [e for e in _real(result) if e.source_table == "s.t"]
    assert {(e.source_column, e.target_table) for e in read} == {
        ("id", PROCEDURE_LOCAL_TARGET),
        ("live", PROCEDURE_LOCAL_TARGET),
    }
    assert all(e.is_intermediate and not e.is_write for e in read)
    assert {e.control_flow_context for e in read} == {FOREACH_ARRAY_CONTEXT}
    where = read[0].statement_range
    assert where is not None
    assert sql[where.start_offset : where.end_offset].startswith("(SELECT array_agg(t.id)")
    assert PROCEDURE_RESULT_TARGET not in {e.target_table for e in result.edges}


@pytest.mark.parametrize(
    "header",
    [
        "FOREACH x SLICE 1 IN ARRAY arr LOOP",
        # A LOOP inside a literal ends nothing -- at depth 0 and inside parentheses alike.
        "FOREACH x IN ARRAY '{a,LOOP}'::text[] LOOP",
        "FOREACH x IN ARRAY string_to_array('a LOOP b', ',') LOOP",
        "<<each>>\n  FOREACH x IN ARRAY arr LOOP",
    ],
)
def test_foreach_header_variants_are_peeled(header: str) -> None:
    sql = _plpgsql(
        f"  {header}\n"  # noqa: S608 -- a body handed to the parser, never executed
        "    INSERT INTO s.dst (a) SELECT t.a FROM s.t t;\n"
        "  END LOOP;",
        declare="  arr text[]; x text;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, _gaps(result)
    assert ("s.t", "a", "s.dst", "a", None) in _end_to_end(result)


def test_a_foreach_loop_with_no_loop_keyword_stays_a_gap() -> None:
    """guard: a header the parse cannot find the end of is reported, never guessed at."""
    sql = _plpgsql("  FOREACH x IN ARRAY arr", declare="  arr int[]; x int;")
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert not result.is_fully_parsed
    assert [g for g in _gaps(result) if g.startswith("PARSE_ERROR")]


def test_a_for_loop_after_a_foreach_still_binds_its_own_record() -> None:
    """guard: sequential loops, the FOREACH first."""
    sql = _plpgsql(
        "  FOREACH x IN ARRAY arr LOOP\n"
        "    NULL;\n"
        "  END LOOP;\n"
        "  FOR rec IN SELECT b.x FROM s.b b LOOP\n"
        "    INSERT INTO s.out_b (x) VALUES (rec.x);\n"
        "  END LOOP;",
        declare="  rec record; arr int[]; x int;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, _gaps(result)
    assert _flows(result) == {("s.b", "s.out_b")}


# ---------------------------------------------------------------------------
# 2. Two gap markers from one statement are two gaps.
# ---------------------------------------------------------------------------

TWO_FUNCTIONS = _tsql(
    "INSERT INTO dbo.out (a, b) SELECT f.x, g.y FROM dbo.fn1(1) f JOIN dbo.fn2(2) g ON f.k = g.k"
)
FN1_BODY = (
    "CREATE FUNCTION dbo.fn1(@p int) RETURNS TABLE AS RETURN "
    "(SELECT s1.x, s1.k FROM dbo.src1 s1)"
)
FN2_BODY = (
    "CREATE FUNCTION dbo.fn2(@p int) RETURNS TABLE AS RETURN "
    "(SELECT s2.y, s2.k FROM dbo.src2 s2)"
)


def _resolver(bodies: dict[str, str]) -> routine_call_descent.Resolver:
    def resolve(name: str) -> Callee:
        key = name.lower()
        if key in bodies:
            return Callee(key, key, bodies[key])
        return Callee(None, None, None, routine_call_descent.CALLEE_NOT_CAPTURED)

    return resolve


def test_two_table_functions_in_one_statement_are_two_gaps() -> None:
    result = parse_procedure_lineage(TWO_FUNCTIONS, dialect="tsql")

    assert sorted(_gaps(result)) == [
        "TABLE_FUNCTION_READ: dbo.fn1",
        "TABLE_FUNCTION_READ: dbo.fn2",
    ]
    assert not result.is_fully_parsed


def test_every_gap_marker_has_its_own_stored_natural_key() -> None:
    """The stored key is (ordinal, source, column, target, column, type, via): a marker's
    identity is its ordinal, so two markers sharing one collapse in the table too."""
    result = parse_procedure_lineage(TWO_FUNCTIONS, dialect="tsql")

    keys = [routine_edge_key(edge) for edge in result.edges]
    assert len(keys) == len(set(keys))
    # Distinct keys alone would hold after a dedupe that had already dropped one.
    assert len(_gaps(result)) == 2


def test_the_marker_slots_are_the_ones_the_walk_already_reserved() -> None:
    """guard: a real statement's ordinal is what it always was. The walk advanced its counter
    once per marker but numbered the marker with the statement's own ordinal; the reserved slots
    are where the markers now sit, so nothing that is not a marker moves."""
    sql = _tsql(
        "INSERT INTO dbo.out (a, b) SELECT f.x, g.y FROM dbo.fn1(1) f JOIN dbo.fn2(2) g "
        "ON f.k = g.k",
        "INSERT INTO dbo.out2 (c) SELECT z.c FROM dbo.z z",
    )
    result = parse_procedure_lineage(sql, dialect="tsql")

    by_target = {
        target: {e.statement_ordinal for e in _real(result) if e.target_table == target}
        for target in ("dbo.out", "dbo.out2")
    }
    assert by_target == {"dbo.out": {0}, "dbo.out2": {3}}
    markers = {e.statement_ordinal for e in result.edges if e.unparsed_reason}
    assert markers == {1, 2}


def test_a_table_function_and_a_call_in_one_statement_are_both_gaps() -> None:
    sql = _tsql(
        "INSERT INTO dbo.out (a, b) SELECT f.x, pkg.helper(f.y) FROM dbo.fn1(1) f"
    )
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert sorted(_gaps(result)) == [
        "NESTED_PROCEDURE_CALL: pkg.helper",
        "TABLE_FUNCTION_READ: dbo.fn1",
    ]


def test_a_loop_over_a_table_function_and_a_call_in_its_body_are_both_gaps() -> None:
    """One chunk, one ordinal: the loop's query and the `CALL` under its header."""
    sql = _plpgsql(
        "  FOR rec IN SELECT * FROM s.tf(1) LOOP\n"
        "    CALL s.audit(rec.id);\n"
        "  END LOOP;",
        declare="  rec record;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert sorted(_gaps(result)) == [
        "NESTED_PROCEDURE_CALL: s.audit",
        "TABLE_FUNCTION_READ: s.tf",
    ]


def test_descent_reads_both_functions_and_reaches_what_the_caller_writes() -> None:
    result = descend_nested_calls(
        parse_procedure_lineage(TWO_FUNCTIONS, dialect="tsql"),
        dialect="tsql",
        resolve=_resolver({"dbo.fn1": FN1_BODY, "dbo.fn2": FN2_BODY}),
        root_key="root",
    )

    assert _gaps(result) == []
    assert result.is_fully_parsed
    assert {("dbo.src1", "dbo.out"), ("dbo.src2", "dbo.out")} <= {
        (s, t) for s, _c, t, _d, via in _end_to_end(result) if via is not None
    }


def test_a_function_that_cannot_be_read_keeps_the_routine_from_reading_as_understood() -> None:
    """The false-clean case: `fn1` resolves, `fn2` is not captured. With one marker kept,
    descent removed it and the routine read as fully parsed, `fn2`'s sources never read."""
    result = descend_nested_calls(
        parse_procedure_lineage(TWO_FUNCTIONS, dialect="tsql"),
        dialect="tsql",
        resolve=_resolver({"dbo.fn1": FN1_BODY}),
        root_key="root",
    )

    assert _gaps(result) == ["TABLE_FUNCTION_READ: dbo.fn2 (NOT_CAPTURED)"]
    assert not result.is_fully_parsed
    assert ("dbo.src1", "dbo.out") in {
        (s, t) for s, _c, t, _d, via in _end_to_end(result) if via is not None
    }


async def test_both_markers_are_stored(
    marker_db_session: AsyncSession,  # noqa: F811
) -> None:
    """Persistence keeps what the parse found: both gaps, as rows of their own."""
    datasource, schema = await _seed(marker_db_session)
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="two_functions",
        routine_type="PROCEDURE",
        package_name="",
        body_sql_redacted=TWO_FUNCTIONS,
        body_fingerprint=uuid4().hex,
        redaction_status="PARSED",
        screening_status="CLEAN",
        availability="AVAILABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    marker_db_session.add(routine)
    await marker_db_session.flush()

    await parse_deep_procedure_lineage_endpoint(
        datasource.id, routine.id, _context(datasource), marker_db_session
    )

    rows = (
        await marker_db_session.scalars(
            select(DeepProcedureLineageEdge).where(
                DeepProcedureLineageEdge.routine_id == routine.id,
                DeepProcedureLineageEdge.transformation_type == UNPARSED_TRANSFORMATION_TYPE,
            )
        )
    ).all()
    # The endpoint runs descent, and neither function is captured here.
    assert sorted(row.unparsed_reason or "" for row in rows) == [
        "TABLE_FUNCTION_READ: dbo.fn1 (NOT_CAPTURED)",
        "TABLE_FUNCTION_READ: dbo.fn2 (NOT_CAPTURED)",
    ]


# ---------------------------------------------------------------------------
# 3. T-SQL: SET @v = (SELECT ...) reads its tables.
# ---------------------------------------------------------------------------


def test_a_set_that_reads_a_table_without_a_column_is_a_table_read() -> None:
    sql = _tsql("DECLARE @n int", "SET @n = (SELECT COUNT(*) FROM dbo.orders)")
    result = parse_procedure_lineage(sql, dialect="tsql")

    [edge] = _rows(result)
    assert (edge.source_table, edge.target_table) == ("dbo.orders", PROCEDURE_LOCAL_TARGET)
    assert edge.is_intermediate and not edge.is_write
    assert _text(sql, edge.source_token_range) == "dbo.orders"
    where = edge.statement_range
    assert where is not None
    assert sql[where.start_offset : where.end_offset] == (
        "SET @n = (SELECT COUNT(*) FROM dbo.orders)"
    )
    assert result.is_fully_parsed


def test_a_set_that_selects_columns_records_them_as_local_reads() -> None:
    sql = _tsql(
        "DECLARE @m int",
        "SET @m = (SELECT MAX(o.amount) FROM dbo.orders o WHERE o.status = 1)",
        "INSERT INTO dbo.limits (m) VALUES (@m)",
    )
    result = parse_procedure_lineage(sql, dialect="tsql")

    read = [e for e in _real(result) if e.source_table == "dbo.orders"]
    # A subquery in a select list is read as any is (`v := (SELECT ...)` in PL/pgSQL): every
    # column in it, its own WHERE's included, is a source of that one expression.
    assert {(e.source_column, e.target_table, e.target_column) for e in read} == {
        ("amount", PROCEDURE_LOCAL_TARGET, "_col0"),
        ("status", PROCEDURE_LOCAL_TARGET, "_col0"),
    }
    assert all(e.is_intermediate and not e.is_write for e in read)
    # `@m` is a variable, never a source, and the INSERT that uses it is no read of `orders`.
    assert {e.target_table for e in _real(result)} == {PROCEDURE_LOCAL_TARGET}


@pytest.mark.parametrize(
    "statement",
    [
        "SET @v = @v + (SELECT COUNT(*) FROM dbo.a)",
        "SET @v += (SELECT COUNT(*) FROM dbo.a)",
        "SET @v = CASE WHEN EXISTS (SELECT 1 FROM dbo.a) THEN 1 ELSE 0 END",
        "SET @v=(SELECT COUNT(*) FROM dbo.a)",
    ],
)
def test_every_spelling_of_a_set_with_a_subquery_is_read(statement: str) -> None:
    result = parse_procedure_lineage(_tsql("DECLARE @v int", statement), dialect="tsql")

    assert {e.source_table for e in _real(result)} == {"dbo.a"}
    assert result.is_fully_parsed


@pytest.mark.parametrize(
    "statement",
    [
        "SET NOCOUNT ON",
        "SET XACT_ABORT ON",
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
        "SET @v = 1",
        "SET @v = @v + 1",
        "SET @v = @w",
        "SET @v = 'a select b'",
    ],
)
def test_a_set_that_reads_no_table_stays_lineage_free(statement: str) -> None:
    """guard: including the value that merely contains the word."""
    sql = _tsql(
        "DECLARE @v int",
        "DECLARE @w int",
        statement,
        "INSERT INTO dbo.t (a) SELECT s.a FROM dbo.s s",
    )
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert {(e.source_table, e.target_table) for e in _real(result)} == {("dbo.s", "dbo.t")}
    # Statement numbers are what they always were.
    assert {e.statement_ordinal for e in _real(result)} == {3}


@pytest.mark.parametrize(
    "statement",
    ["SET @a = dbo.compute_total(5)", "SET @a = dbo.compute_total('select')"],
)
def test_a_set_that_only_calls_a_function_is_not_made_a_gap_by_this_rule(statement: str) -> None:
    """guard: read as a query, a `SET` holding a scalar function call would reach the
    expression-call hook and turn every such routine into one that is not fully parsed. Only
    a SET whose expression holds a query (the word, outside a literal) is read; a scalar
    function's call in a SET is a call site nobody has claimed to read, and stays as it was."""
    result = parse_procedure_lineage(_tsql("DECLARE @a int", statement), dialect="tsql")

    assert result.is_fully_parsed, _gaps(result)
    assert result.edges == []


def test_a_cursor_assigned_by_set_is_still_a_cursor_declaration() -> None:
    """guard: `SET @c = CURSOR FOR <query>` is read as a declaration, ahead of this rule."""
    sql = _tsql("DECLARE @c CURSOR", "SET @c = CURSOR FOR SELECT o.id FROM dbo.orders o")
    result = parse_procedure_lineage(sql, dialect="tsql")

    read = [e for e in _real(result) if e.source_table == "dbo.orders"]
    assert read and {e.control_flow_context for e in read} == {"CURSOR_DECLARATION"}


def test_a_set_is_no_write_and_the_routine_stays_read_only() -> None:
    sql = _tsql("DECLARE @n int", "SET @n = (SELECT COUNT(*) FROM dbo.orders)")
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert result.is_fully_parsed and result.is_read_only
    assert not any(edge.is_write for edge in result.edges)
    assert not any(not is_routine_local(e.target_table) for e in _real(result))


# ---------------------------------------------------------------------------
# 4. A table only an unread clause names.
# ---------------------------------------------------------------------------


def _table_read(sql: str, dialect: str, table: str, target: str) -> ProcedureLineageEdgeRecord:
    result = parse_procedure_lineage(sql, dialect=dialect)
    assert result.is_fully_parsed, _gaps(result)
    matches = [e for e in _rows(result) if (e.source_table, e.target_table) == (table, target)]
    assert len(matches) == 1, [(e.source_table, e.target_table) for e in _rows(result)]
    [edge] = matches
    assert (edge.source_column, edge.target_column) == ("*", "*")
    assert (edge.confidence, edge.source_resolved) == ("PARTIAL", True)
    return edge


def test_a_table_joined_only_in_on_is_a_table_read() -> None:
    sql = _tsql("INSERT INTO dbo.out (x) SELECT a.x FROM dbo.a a JOIN dbo.b b ON a.id = b.id")
    edge = _table_read(sql, "tsql", "dbo.b", "dbo.out")
    assert edge.is_write and not edge.is_intermediate
    assert _text(sql, edge.source_token_range) == "dbo.b"
    assert _text(sql, edge.target_token_range) == "dbo.out"


def test_a_left_joined_table_only_in_on_is_a_table_read() -> None:
    _table_read(
        _tsql("INSERT INTO dbo.out (x) SELECT a.x FROM dbo.a a LEFT JOIN dbo.b b ON a.id = b.id"),
        "tsql", "dbo.b", "dbo.out",
    )


def test_a_table_an_exists_in_an_on_clause_names_is_a_table_read() -> None:
    sql = _tsql(
        "INSERT INTO dbo.out (x) SELECT a.x FROM dbo.a a JOIN dbo.b b ON a.id = b.id "
        "AND EXISTS (SELECT 1 FROM dbo.c c WHERE c.k = a.k)"
    )
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert _read_tables(result) == {("dbo.b", "dbo.out"), ("dbo.c", "dbo.out")}
    [c_edge] = [e for e in _rows(result) if e.source_table == "dbo.c"]
    assert _text(sql, c_edge.source_token_range) == "dbo.c"


def test_a_table_an_in_subquery_in_an_on_clause_names_is_a_table_read() -> None:
    sql = _tsql(
        "INSERT INTO dbo.out (x) SELECT a.x FROM dbo.a a JOIN dbo.b b ON a.id = b.id "
        "AND b.k IN (SELECT d.k FROM dbo.d d)"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {
        ("dbo.b", "dbo.out"),
        ("dbo.d", "dbo.out"),
    }


def test_a_table_a_merge_on_clause_names_is_a_table_read() -> None:
    sql = _tsql(
        "MERGE INTO dbo.tgt t USING dbo.src s ON t.id = s.id "
        "AND EXISTS (SELECT 1 FROM dbo.blocked b WHERE b.id = s.id) "
        "WHEN MATCHED THEN UPDATE SET t.v = s.v"
    )
    result = parse_procedure_lineage(sql, dialect="tsql")
    # `dbo.src` is named by the SET's edge; only `blocked` is read and unnamed.
    assert _read_tables(result) == {("dbo.blocked", "dbo.tgt")}


def test_a_table_a_merge_when_condition_names_is_a_table_read() -> None:
    sql = _tsql(
        "MERGE INTO dbo.tgt t USING dbo.src s ON t.id = s.id "
        "WHEN MATCHED AND EXISTS (SELECT 1 FROM dbo.blocked b WHERE b.id = s.id) "
        "THEN UPDATE SET t.v = s.v"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {
        ("dbo.blocked", "dbo.tgt")
    }


def test_a_merge_that_only_deletes_still_reads_its_source() -> None:
    """No branch names a column of `src`: only ON does, and ON is read by no edge."""
    sql = _tsql(
        "MERGE INTO dbo.tgt t USING dbo.src s ON t.id = s.id WHEN MATCHED THEN DELETE"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {("dbo.src", "dbo.tgt")}


def test_a_merge_in_oracle_is_read_the_same_way() -> None:
    sql = (
        "CREATE OR REPLACE PROCEDURE ops.m IS\nBEGIN\n"
        "  MERGE INTO ops.tgt t USING ops.src s ON (t.id = s.id AND EXISTS "
        "(SELECT 1 FROM ops.blocked b WHERE b.id = s.id))\n"
        "  WHEN MATCHED THEN UPDATE SET t.v = s.v;\nEND m;\n"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="oracle")) == {
        ("ops.blocked", "ops.tgt")
    }


def test_a_table_joined_only_in_an_update_from_is_a_table_read() -> None:
    sql = _tsql(
        "UPDATE t SET t.v = a.v FROM dbo.tgt t JOIN dbo.a a ON t.id = a.id "
        "JOIN dbo.b b ON b.id = a.id"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {("dbo.b", "dbo.tgt")}


def test_a_table_joined_only_in_a_delete_is_a_table_read() -> None:
    sql = _tsql(
        "DELETE t FROM dbo.tgt t JOIN dbo.a a ON t.id = a.id "
        "AND EXISTS (SELECT 1 FROM dbo.c c WHERE c.k = a.k)"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {
        ("dbo.a", "dbo.tgt"),
        ("dbo.c", "dbo.tgt"),
    }


def test_a_table_only_a_group_by_names_is_a_table_read() -> None:
    sql = _tsql("INSERT INTO dbo.out (n) SELECT COUNT(*) FROM dbo.a a GROUP BY a.k")
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {("dbo.a", "dbo.out")}


def test_a_table_only_a_having_subquery_names_is_a_table_read() -> None:
    sql = _tsql(
        "INSERT INTO dbo.out (k) SELECT a.k FROM dbo.a a GROUP BY a.k "
        "HAVING COUNT(*) > (SELECT MAX(b.limit_n) FROM dbo.b b)"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {("dbo.b", "dbo.out")}


def test_a_table_only_an_order_by_names_is_a_table_read() -> None:
    sql = _tsql("INSERT INTO dbo.out (n) SELECT COUNT(*) FROM dbo.a a ORDER BY a.k")
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {("dbo.a", "dbo.out")}


def test_a_table_a_derived_tables_own_join_names_is_a_table_read() -> None:
    sql = _tsql(
        "INSERT INTO dbo.out (id) SELECT d.id FROM "
        "(SELECT y.id FROM dbo.y y JOIN dbo.z z ON z.id = y.id) d"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {("dbo.z", "dbo.out")}


def test_a_returned_query_reads_a_table_only_its_join_names() -> None:
    sql = _plpgsql("  RETURN QUERY SELECT a.x FROM s.a a JOIN s.b b ON a.id = b.id;")
    assert _read_tables(parse_procedure_lineage(sql, dialect="postgres")) == {
        ("s.b", PROCEDURE_RESULT_TARGET)
    }


def test_a_postgres_update_reads_a_table_only_a_join_in_its_from_names() -> None:
    sql = _plpgsql(
        "  UPDATE s.t SET a = x.a FROM s.x x JOIN s.b b ON b.id = x.id WHERE s.t.id = x.id;"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="postgres")) == {("s.b", "s.t")}


def test_a_table_read_through_a_select_into_reads_into_the_variable() -> None:
    sql = (
        "CREATE OR REPLACE PROCEDURE ops.p IS\n  v NUMBER;\nBEGIN\n"
        "  SELECT a.x INTO v FROM ops.a a JOIN ops.b b ON a.id = b.id;\nEND p;\n"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="oracle")) == {
        ("ops.b", PROCEDURE_LOCAL_TARGET)
    }


@pytest.mark.parametrize(
    "statement",
    [
        # Every table has an edge, or is the write target.
        "INSERT INTO dbo.out (x, y) SELECT a.x, b.y FROM dbo.a a JOIN dbo.b b ON a.id = b.id",
        "INSERT INTO dbo.out (x) SELECT a.x FROM dbo.a a, dbo.b b WHERE a.id = b.id",
        "INSERT INTO dbo.out (x) SELECT a.x FROM dbo.a a WHERE a.id IN (SELECT b.id FROM dbo.b b)",
        # A self-join: the table is named, once, by the column that is selected.
        "INSERT INTO dbo.out (x) SELECT a.x FROM dbo.a a JOIN dbo.a b ON a.id = b.pid",
        # T-SQL: the FROM item an UPDATE or DELETE names is its target, not a source.
        "UPDATE t SET t.v = 1 FROM dbo.tgt t",
        "UPDATE dbo.tgt SET v = 1 FROM dbo.tgt",
        "DELETE t FROM dbo.tgt t",
        "UPDATE t SET t.v = 1 FROM dbo.tgt t WHERE t.id = 1",
    ],
)
def test_a_table_an_edge_names_or_the_statement_writes_gets_no_table_read(statement: str) -> None:
    """guard."""
    result = parse_procedure_lineage(_tsql(statement), dialect="tsql")
    assert _rows(result) == []


def test_a_tsql_update_from_a_target_it_joins_still_reads_the_join_and_not_the_target() -> None:
    """guard: the target alias `t` resolves to `dbo.tgt`, which is what is written."""
    sql = _tsql("UPDATE t SET t.v = 1 FROM dbo.tgt t JOIN dbo.a a ON a.id = t.id")
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {("dbo.a", "dbo.tgt")}


def test_a_target_read_again_under_another_alias_is_a_read() -> None:
    """The exclusion is by node, not by name: a second instance of the target in the FROM
    list is read as any table is."""
    sql = _tsql(
        "UPDATE dbo.tgt SET v = a.v FROM dbo.a a JOIN dbo.tgt t2 ON t2.id = a.id"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {("dbo.tgt", "dbo.tgt")}


# ---------------------------------------------------------------------------
# 5. An explicit FETCH c INTO rec loop reads its cursor's rows into the record.
# ---------------------------------------------------------------------------

ORACLE_FETCH_LOOP = """CREATE OR REPLACE PROCEDURE ops.walk IS
  CURSOR c IS SELECT o.id, o.amount FROM ops.orders o WHERE o.status = 1;
  r c%ROWTYPE;
BEGIN
  OPEN c;
  LOOP
    FETCH c INTO r;
    EXIT WHEN c%NOTFOUND;
    INSERT INTO ops.open_log (id, amount) VALUES (r.id, r.amount);
  END LOOP;
  CLOSE c;
END walk;
"""


def _fetch_flows(result: ProcedureParseResult) -> set[tuple[str, str, str, str]]:
    """End-to-end edges the hop pass carried through a record a FETCH filled."""
    return {
        (e.source_table, e.source_column, e.target_table, e.target_column)
        for e in _real(result)
        if e.is_write and e.via_temp_table is not None and e.via_temp_table.startswith("<LOCAL:")
    }


def test_an_explicit_fetch_loop_reads_its_cursors_rows_into_the_record() -> None:
    result = parse_procedure_lineage(ORACLE_FETCH_LOOP, dialect="oracle")

    assert result.is_fully_parsed, _gaps(result)
    assert _fetch_flows(result) == {
        ("ops.orders", "id", "ops.open_log", "id"),
        ("ops.orders", "amount", "ops.open_log", "amount"),
    }
    fetched = [e for e in _real(result) if e.control_flow_context == "CURSOR_FETCH"]
    assert {e.target_table for e in fetched} == {"<LOCAL:r@2>"}
    assert all(e.is_intermediate and not e.is_write for e in fetched)
    # Located at the FETCH, where the row arrives -- with no token: the query's tokens are in
    # the declaration, which keeps its own read.
    where = fetched[0].statement_range
    assert where is not None
    assert ORACLE_FETCH_LOOP[where.start_offset : where.end_offset] == "FETCH c INTO r"
    assert all(e.source_token_range is None for e in fetched)
    assert PROCEDURE_RESULT_TARGET not in {e.target_table for e in result.edges}


def test_a_priming_read_fills_the_same_record_and_the_body_reads_it() -> None:
    """`FETCH c INTO r; WHILE c%FOUND LOOP ... FETCH c INTO r; END LOOP;`: the same cursor
    twice is one intermediate, not two."""
    sql = """CREATE OR REPLACE PROCEDURE ops.walk IS
  CURSOR c IS SELECT o.id FROM ops.orders o;
  r c%ROWTYPE;
BEGIN
  OPEN c;
  FETCH c INTO r;
  WHILE c%FOUND LOOP
    INSERT INTO ops.open_log (id) VALUES (r.id);
    FETCH c INTO r;
  END LOOP;
  CLOSE c;
END walk;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")

    assert _fetch_flows(result) == {("ops.orders", "id", "ops.open_log", "id")}
    fetched = [e for e in _real(result) if e.control_flow_context == "CURSOR_FETCH"]
    assert {e.target_table for e in fetched} == {"<LOCAL:r@2>"}
    assert len({e.statement_ordinal for e in fetched}) == 2


@pytest.mark.parametrize("fetch", ["FETCH c INTO r", "FETCH NEXT FROM c INTO r", "fetch c into r"])
def test_a_plpgsql_fetch_loop_reads_the_cursors_rows_into_the_record(fetch: str) -> None:
    sql = _plpgsql(
        "  OPEN c;\n"  # noqa: S608 -- a body handed to the parser, never executed
        "  LOOP\n"
        f"    {fetch};\n"
        "    EXIT WHEN NOT FOUND;\n"
        "    INSERT INTO s.open_log (id) VALUES (r.id);\n"
        "  END LOOP;\n"
        "  CLOSE c;",
        declare="  c CURSOR FOR SELECT o.id FROM s.orders o;\n  r record;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert result.is_fully_parsed, _gaps(result)
    assert _fetch_flows(result) == {("s.orders", "id", "s.open_log", "id")}


def test_the_fetched_record_is_a_fact_the_agent_can_propose_and_never_a_table() -> None:
    from aida.lineage_agent import proposable_procedure_edges

    result = parse_procedure_lineage(ORACLE_FETCH_LOOP, dialect="oracle")

    proposed = {
        (e.source_table, e.source_column, e.target_table, e.target_column)
        for e in proposable_procedure_edges(result)
    }
    assert ("ops.orders", "id", "ops.open_log", "id") in proposed
    assert not any(is_routine_local(s) or is_routine_local(t) for s, _c, t, _d in proposed)


def test_statements_after_a_fetch_keep_the_numbers_they_had() -> None:
    """guard: a FETCH was one lineage-free statement and is one statement still."""
    result = parse_procedure_lineage(ORACLE_FETCH_LOOP, dialect="oracle")

    assert {e.statement_ordinal for e in _real(result) if e.target_table == "ops.open_log"} == {4}


def test_a_record_fetched_from_two_cursors_is_left_unbound() -> None:
    """guard: which cursor's rows `r` holds where it is read depends on the path taken, and no
    one intermediate stands for both. Nothing is claimed, as before."""
    sql = """CREATE OR REPLACE PROCEDURE ops.walk(p_flag NUMBER) IS
  CURSOR c1 IS SELECT a.id FROM ops.a a;
  CURSOR c2 IS SELECT b.id FROM ops.b b;
  r c1%ROWTYPE;
BEGIN
  IF p_flag = 1 THEN
    FETCH c1 INTO r;
  ELSE
    FETCH c2 INTO r;
  END IF;
  INSERT INTO ops.out (id) VALUES (r.id);
END walk;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")

    assert _fetch_flows(result) == set()
    assert not [e for e in _real(result) if e.control_flow_context == "CURSOR_FETCH"]
    assert not [e for e in _real(result) if e.target_table == "ops.out"]


def test_two_cursors_fetched_in_turn_never_lend_each_other_their_rows() -> None:
    """The case a shared intermediate would get wrong: straight-line, so the first INSERT can
    only see `c1`'s row and the second only `c2`'s. One intermediate filled by both would put
    `ops.b -> ops.out1` and `ops.a -> ops.out2` in the lineage -- paths that do not exist."""
    sql = """CREATE OR REPLACE PROCEDURE ops.walk IS
  CURSOR c1 IS SELECT a.id FROM ops.a a;
  CURSOR c2 IS SELECT b.id FROM ops.b b;
  r c1%ROWTYPE;
BEGIN
  FETCH c1 INTO r;
  INSERT INTO ops.out1 (id) VALUES (r.id);
  FETCH c2 INTO r;
  INSERT INTO ops.out2 (id) VALUES (r.id);
END walk;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")

    assert _fetch_flows(result) == set()
    assert not [
        e for e in _real(result)
        if e.target_table in ("ops.out1", "ops.out2") and is_routine_local(e.source_table)
    ]


@pytest.mark.parametrize(
    "fetch",
    [
        "FETCH c INTO a, b",  # scalar variables
        "FETCH c BULK COLLECT INTO r",  # a collection
        "FETCH other INTO r",  # a cursor this parse never read
    ],
)
def test_a_fetch_that_is_not_one_record_of_a_declared_cursor_reads_nothing(fetch: str) -> None:
    """guard."""
    sql = f"""CREATE OR REPLACE PROCEDURE ops.walk IS
  CURSOR c IS SELECT o.id FROM ops.orders o;
  r c%ROWTYPE;
BEGIN
  OPEN c;
  {fetch};
  INSERT INTO ops.open_log (id) VALUES (r.id);
END walk;
"""  # noqa: S608 -- a body handed to the parser, never executed
    result = parse_procedure_lineage(sql, dialect="oracle")

    assert _fetch_flows(result) == set()
    assert not [e for e in _real(result) if e.control_flow_context == "CURSOR_FETCH"]
    # And no source from nowhere: a record nothing fills is not an intermediate to read.
    assert not [
        e for e in _real(result)
        if e.target_table == "ops.open_log" and is_routine_local(e.source_table)
    ]


def test_a_record_read_before_its_fetch_is_not_the_cursors_rows() -> None:
    """guard: bound from the FETCH on, never before it."""
    sql = """CREATE OR REPLACE PROCEDURE ops.walk IS
  CURSOR c IS SELECT o.id FROM ops.orders o;
  r c%ROWTYPE;
BEGIN
  INSERT INTO ops.early (id) VALUES (r.id);
  OPEN c;
  FETCH c INTO r;
  INSERT INTO ops.late (id) VALUES (r.id);
END walk;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")

    assert _fetch_flows(result) == {("ops.orders", "id", "ops.late", "id")}


def test_a_tsql_fetch_into_variables_is_unchanged() -> None:
    """guard: `FETCH NEXT FROM c INTO @a` fills scalar variables, not a record."""
    sql = _tsql(
        "DECLARE c CURSOR FOR SELECT o.id FROM dbo.orders o",
        "DECLARE @a int",
        "OPEN c",
        "FETCH NEXT FROM c INTO @a",
        "INSERT INTO dbo.open_log (id) VALUES (@a)",
    )
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert _fetch_flows(result) == set()
    assert not [e for e in _real(result) if e.control_flow_context == "CURSOR_FETCH"]


def test_a_loop_variable_of_the_same_name_shadows_the_fetched_record_inside_its_loop() -> None:
    sql = """CREATE OR REPLACE PROCEDURE ops.walk IS
  CURSOR c IS SELECT o.id FROM ops.orders o;
  r c%ROWTYPE;
BEGIN
  OPEN c;
  FETCH c INTO r;
  FOR r IN (SELECT b.id FROM ops.b b) LOOP
    INSERT INTO ops.inner_log (id) VALUES (r.id);
  END LOOP;
  INSERT INTO ops.outer_log (id) VALUES (r.id);
END walk;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")

    flows = {(s, t) for s, _c, t, _d in _fetch_flows(result)}
    assert flows == {("ops.orders", "ops.outer_log")} | {("ops.b", "ops.inner_log")}


def test_a_package_member_never_reads_a_record_another_member_fetched() -> None:
    """Each subprogram is its own unit: `r` in `b_member` is a different variable."""
    sql = """PACKAGE BODY ops_pkg AS
  CURSOR c IS SELECT o.id FROM ops.orders o;
  PROCEDURE a_member IS
    r c%ROWTYPE;
  BEGIN
    OPEN c;
    FETCH c INTO r;
    INSERT INTO ops.log_a (id) VALUES (r.id);
    CLOSE c;
  END a_member;
  PROCEDURE b_member IS
    r c%ROWTYPE;
  BEGIN
    INSERT INTO ops.log_b (id) VALUES (r.id);
  END b_member;
END ops_pkg;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")

    assert {t for _s, _c, t, _d in _fetch_flows(result)} == {"ops.log_a"}
    # Nor a source from nowhere: `b_member`'s `r` is no intermediate anything fills.
    assert not [
        e for e in _real(result)
        if e.target_table == "ops.log_b" and is_routine_local(e.source_table)
    ]


def _delete_facts(result: ProcedureParseResult) -> set[tuple[str, str, str, str, bool]]:
    return {
        (e.source_table, e.source_column, e.target_table, e.target_column, e.is_write)
        for e in _real(result)
    }


def test_a_tsql_delete_of_a_joined_item_deletes_from_that_item() -> None:
    """Found alongside: `DELETE t FROM dbo.a a JOIN dbo.tgt t ON ...` deletes from the joined
    `dbo.tgt`, which its `tables` designate. sqlglot's `this` is the first FROM item, and taking
    it recorded the delete -- and the filter evidence in its WHERE -- as a write to `dbo.a`, a
    table the statement only reads."""
    sql = _tsql("DELETE t FROM dbo.a a JOIN dbo.tgt t ON t.id = a.id WHERE a.x = 1")
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert _delete_facts(result) == {("dbo.a", "x", "dbo.tgt", FILTER, True)}


def test_a_tsql_delete_of_a_joined_item_reads_the_first_from_item() -> None:
    sql = _tsql("DELETE t FROM dbo.a a JOIN dbo.tgt t ON t.id = a.id")
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert _read_tables(result) == {("dbo.a", "dbo.tgt")}
    assert all(e.is_write for e in _rows(result))


@pytest.mark.parametrize(
    ("statement", "facts"),
    [
        (
            "DELETE t FROM dbo.tgt t JOIN dbo.a a ON t.id = a.id WHERE a.x = 1",
            {("dbo.a", "x", "dbo.tgt", FILTER, True)},
        ),
        ("DELETE FROM dbo.tgt WHERE tgt.id = 1", {("dbo.tgt", "id", "dbo.tgt", FILTER, True)}),
        ("DELETE TOP (1) FROM dbo.queue", set()),
    ],
)
def test_a_delete_whose_target_was_already_right_is_unchanged(
    statement: str, facts: set[tuple[str, str, str, str, bool]]
) -> None:
    """guard: the alias names the first FROM item, or there is no alias to name one."""
    result = parse_procedure_lineage(_tsql(statement), dialect="tsql")
    assert _delete_facts(result) == facts


def test_a_subquery_that_reads_the_target_again_is_still_a_read() -> None:
    """The designation is of the statement's own FROM. The FROM item `dbo.tgt` is the target;
    the same spelling in the EXISTS is another scope's reference to it, which reads its rows."""
    sql = _tsql(
        "UPDATE dbo.tgt SET v = 1 FROM dbo.tgt WHERE EXISTS (SELECT 1 FROM dbo.tgt)"
    )
    assert _read_tables(parse_procedure_lineage(sql, dialect="tsql")) == {
        ("dbo.tgt", "dbo.tgt")
    }


def test_the_walk_still_numbers_a_statement_with_a_new_table_read_as_one_statement() -> None:
    """guard: a table read is an edge of the statement, never a statement."""
    sql = _tsql(
        "INSERT INTO dbo.out (x) SELECT a.x FROM dbo.a a JOIN dbo.b b ON a.id = b.id",
        "INSERT INTO dbo.out2 (x) SELECT c.x FROM dbo.c c",
    )
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert [s.ordinal for s in walk_procedure_statements(sql, "tsql")] == [0, 1]
    assert {e.statement_ordinal for e in _real(result) if e.target_table == "dbo.out2"} == {1}
    assert CONDITION_CONTEXT not in {e.control_flow_context for e in result.edges}
