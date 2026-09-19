"""R11-FP07 remainder: loop records, ref cursors handed to the caller, and reads that name
no column.

Three things a routine's parse stated wrongly or not at all:

* **A cursor FOR loop's query was the routine's result set.** `FOR rec IN (SELECT ...) LOOP`
  (PL/SQL) and `FOR rec IN SELECT ... LOOP` (PL/pgSQL) fetch rows into `rec`; the parse
  recorded them as rows the routine *returns*, so a read-only routine with one loop was
  offered to tool generation with the loop's query as its output. And the write inside the
  loop that uses the record -- `INSERT INTO t (a) VALUES (rec.a)` -- had no source at all:
  `rec.a` is a record's field, so the lineage ended at the loop. Now each loop's rows are an
  intermediate of their own (`<LOCAL:rec@N>`), `rec.a` inside the loop reads it, and the hop
  pass joins the loop query's sources to what the loop writes. A loop over a declared cursor
  (`FOR r IN c LOOP`) fetches the cursor's query the same way.
* **An OUT ref cursor was local state.** `OPEN p_rc FOR <query>` on an OUT `SYS_REFCURSOR`
  (PL/SQL), an OUT/INOUT `refcursor` (PL/pgSQL), or the cursor a function returning one
  RETURNs, is how the routine hands its result set to its caller -- as `RETURN QUERY` does.
* **A read that names no column produced no edge.** `SELECT count(*) INTO v FROM t`, `WHERE
  EXISTS (SELECT 1 FROM t)`, `SELECT 1 FROM t`: the statement depends on `t`, and `t` was in
  no answer about who reads it. Each such table now gets one table-grain `TABLE_ROWS` edge
  (`*` at both ends, as a `SELECT *` edge has). An IF/ELSIF/WHILE condition that holds a query
  -- the usual home of `EXISTS (SELECT 1 ...)` -- was dropped with its header; it is read.

Plus a T-SQL body defect found writing the condition tests: an `END` followed by the next
statement's `IF` or `WHILE` was read as `END IF`/`END WHILE`, which T-SQL does not have, so
the body's BEGIN never closed and the whole definition was parsed as the body.

Every test fails on the tree before this change except those marked *guard*, which pin what
the change must not do.
"""

from __future__ import annotations

from typing import Any

import pytest

from aida.lineage_agent import proposable_procedure_edges
from aida.procedure_lineage import (
    CONDITION_CONTEXT,
    CURSOR_FOR_LOOP_CONTEXT,
    PROCEDURE_LOCAL_TARGET,
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    StatementRangeStatus,
    is_routine_local,
    parse_procedure_lineage,
)
from aida.procedure_tool_blueprint import (
    ProcedureNotEligibleError,
    find_single_read_only_result_statement,
)
from aida.routine_lineage_edges import persistable_table
from aida.sql_lineage_parser import (
    FILTER_EVIDENCE_TARGET_COLUMN,
    PROCEDURE_RESULT_TARGET,
    TransformationType,
)

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


def _direct(result: ProcedureParseResult) -> set[tuple[str, str, str, str]]:
    return {
        (e.source_table, e.source_column, e.target_table, e.target_column)
        for e in _real(result)
        if e.via_temp_table is None
    }


def _end_to_end(result: ProcedureParseResult) -> set[tuple[str, str, str, str, str | None]]:
    """Every edge into a table the routine writes, with what it was carried through."""
    return {
        (e.source_table, e.source_column, e.target_table, e.target_column, e.via_temp_table)
        for e in _real(result)
        if e.is_write and not e.is_intermediate
    }


def _text(sql: str, token: Any) -> str | None:
    return None if token is None else sql[token.start_offset : token.end_offset]


def _plpgsql(body: str, *, header: str = "FUNCTION s.f() RETURNS void", declare: str = "") -> str:
    section = f"DECLARE\n{declare}\n" if declare else ""
    return (
        f"CREATE OR REPLACE {header} LANGUAGE plpgsql AS $function$\n"
        f"{section}BEGIN\n{body}\nEND\n$function$"
    )


# ---------------------------------------------------------------------------
# 1. A cursor FOR loop reads into its record, and the record's fields carry its lineage.
# ---------------------------------------------------------------------------

ORACLE_LOOP = """CREATE OR REPLACE PROCEDURE ops.copy_rows IS
BEGIN
  FOR rec IN (SELECT s.a, s.b FROM ops.src s WHERE s.live = 1) LOOP
    INSERT INTO ops.dst (a, b) VALUES (rec.a, rec.b);
  END LOOP;
END copy_rows;
"""

PLPGSQL_LOOP = _plpgsql(
    "  FOR rec IN SELECT s.a, s.b FROM s.src s WHERE s.live LOOP\n"
    "    INSERT INTO s.dst (a, b) VALUES (rec.a, rec.b);\n"
    "  END LOOP;",
    declare="  rec record;",
)


@pytest.mark.parametrize(
    ("sql", "dialect", "source", "target"),
    [
        (ORACLE_LOOP, "oracle", "ops.src", "ops.dst"),
        (PLPGSQL_LOOP, "postgres", "s.src", "s.dst"),
    ],
)
def test_a_cursor_loop_query_reads_into_its_record_not_the_result_set(
    sql: str, dialect: str, source: str, target: str
) -> None:
    result = parse_procedure_lineage(sql, dialect=dialect)

    assert result.is_fully_parsed, _gaps(result)
    loop = [e for e in _real(result) if e.control_flow_context == CURSOR_FOR_LOOP_CONTEXT
            and e.source_table == source and e.via_temp_table is None]
    assert {(e.source_column, e.target_column) for e in loop} == {
        ("a", "a"), ("b", "b"), ("live", FILTER)
    }
    record = {e.target_table for e in loop}
    assert record == {"<LOCAL:rec@0>"}
    assert all(is_routine_local(name) for name in record)
    assert all(e.is_intermediate and not e.is_write for e in loop if e.target_table in record)
    # Nothing is the routine's result: the loop's rows never leave it.
    assert PROCEDURE_RESULT_TARGET not in {e.target_table for e in result.edges}


@pytest.mark.parametrize(
    ("sql", "dialect", "source", "target"),
    [
        (ORACLE_LOOP, "oracle", "ops.src", "ops.dst"),
        (PLPGSQL_LOOP, "postgres", "s.src", "s.dst"),
    ],
)
def test_a_write_that_uses_the_record_is_lineage_from_the_loop_query(
    sql: str, dialect: str, source: str, target: str
) -> None:
    result = parse_procedure_lineage(sql, dialect=dialect)
    record = "<LOCAL:rec@0>"

    # The hop out of the record, read at the field it uses ...
    assert {(record, "a", target, "a"), (record, "b", target, "b")} <= _direct(result)
    hop = next(e for e in _real(result) if (e.source_table, e.source_column) == (record, "a"))
    assert _text(sql, hop.source_token_range) == "rec.a"
    # ... and the end-to-end edges the hop pass joins it into, through the record.
    assert {
        (source, "a", target, "a", record),
        (source, "b", target, "b", record),
    } <= _end_to_end(result)
    carried = next(
        e for e in _real(result) if e.via_temp_table == record and e.source_column == "a"
    )
    assert carried.statement_range_status == StatementRangeStatus.STATEMENT.value
    assert carried.statement_range is not None
    assert sql[carried.statement_range.start_offset :].startswith(f"INSERT INTO {target}")
    assert _text(sql, carried.target_token_range) == "a"


def test_the_lineage_agent_proposes_the_end_to_end_edge_and_never_the_record() -> None:
    result = parse_procedure_lineage(ORACLE_LOOP, dialect="oracle")

    proposed = {(e.source_table, e.source_column, e.target_table, e.target_column)
                for e in proposable_procedure_edges(result)}
    assert ("ops.src", "a", "ops.dst", "a") in proposed
    assert not any(is_routine_local(s) or is_routine_local(t) for s, _c, t, _d in proposed)
    # guard: `<LOCAL>` itself was never a table, and still is not.
    assert persistable_table(PROCEDURE_LOCAL_TARGET, True) is None


def test_a_read_only_routine_with_a_loop_is_not_offered_as_a_tool_of_the_loop_query() -> None:
    """The loop's query used to be the routine's one result statement, so tool generation
    would have exposed it as the routine's output. The routine returns nothing."""
    sql = """CREATE OR REPLACE PROCEDURE ops.walk IS
BEGIN
  FOR rec IN (SELECT c.id, c.name FROM ops.customers c) LOOP
    DBMS_OUTPUT.PUT_LINE(rec.name);
  END LOOP;
END walk;
"""
    with pytest.raises(ProcedureNotEligibleError) as refused:
        find_single_read_only_result_statement(sql, "oracle")
    assert refused.value.code == "PROCEDURE_NO_RESULT"


def test_two_loops_over_the_same_record_name_are_never_joined() -> None:
    """`rec` is every loop's record. One shared name would join the first loop's query to the
    second loop's write -- `ops.a.x -> ops.out_b.x`, a path that does not exist."""
    sql = """CREATE OR REPLACE PROCEDURE ops.two IS
BEGIN
  FOR rec IN (SELECT a.x FROM ops.a a) LOOP
    INSERT INTO ops.out_a (x) VALUES (rec.x);
  END LOOP;
  FOR rec IN (SELECT b.x FROM ops.b b) LOOP
    INSERT INTO ops.out_b (x) VALUES (rec.x);
  END LOOP;
END two;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")
    flows = {(s, t) for s, _c, t, _d, via in _end_to_end(result) if via is not None}
    assert flows == {("ops.a", "ops.out_a"), ("ops.b", "ops.out_b")}


def test_the_record_is_bound_inside_its_loop_only() -> None:
    """guard: after END LOOP a PL/SQL loop record is out of scope; `rec.x` there is another
    name -- here an unresolved one -- and never the loop's rows."""
    sql = """CREATE OR REPLACE PROCEDURE ops.after_loop IS
BEGIN
  FOR rec IN (SELECT a.x FROM ops.a a) LOOP
    NULL;
  END LOOP;
  INSERT INTO ops.out (x) VALUES (rec.x);
END after_loop;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")
    into_out = [e for e in _real(result) if e.target_table == "ops.out"]
    assert not [e for e in into_out if e.source_table == "ops.a"]
    assert not [e for e in _real(result)
                if e.target_table == "ops.out" and is_routine_local(e.source_table)]


def test_nested_loops_each_bind_their_own_record() -> None:
    sql = """CREATE OR REPLACE PROCEDURE ops.nested IS
BEGIN
  FOR o IN (SELECT h.id, h.region FROM ops.orders h) LOOP
    FOR l IN (SELECT i.qty FROM ops.lines i WHERE i.order_id = o.id) LOOP
      INSERT INTO ops.out (region, qty) VALUES (o.region, l.qty);
    END LOOP;
  END LOOP;
END nested;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")
    assert result.is_fully_parsed, _gaps(result)
    flows = {(s, c, d) for s, c, _t, d, via in _end_to_end(result) if via is not None}
    assert {("ops.orders", "region", "region"), ("ops.lines", "qty", "qty")} <= flows
    # The inner query's filter reads the outer record, so it is the outer loop's rows.
    inner = [e for e in _real(result) if e.source_column == "id" and e.target_column == FILTER]
    assert {e.source_table for e in inner if e.via_temp_table is None} == {"<LOCAL:o@0>"}
    assert "ops.orders" in {e.source_table for e in inner}


DECLARED_CURSOR = """CREATE OR REPLACE PROCEDURE ops.walk_open IS
  CURSOR c_open IS SELECT o.id, o.amount FROM ops.orders o WHERE o.status = 1;
BEGIN
  FOR r IN c_open LOOP
    INSERT INTO ops.open_log (id, amount) VALUES (r.id, r.amount);
  END LOOP;
END walk_open;
"""


def test_a_loop_over_a_declared_cursor_fetches_its_query_into_the_record() -> None:
    result = parse_procedure_lineage(DECLARED_CURSOR, dialect="oracle")

    assert result.is_fully_parsed, _gaps(result)
    assert {
        ("ops.orders", "id", "ops.open_log", "id"),
        ("ops.orders", "amount", "ops.open_log", "amount"),
    } <= {(s, c, t, d) for s, c, t, d, via in _end_to_end(result) if via}
    fetched = [e for e in _real(result) if e.control_flow_context == CURSOR_FOR_LOOP_CONTEXT
               and e.source_table == "ops.orders"]
    # Located at the loop, where the cursor is opened -- with no token: the query's
    # tokens are in the declaration, which keeps its own read.
    assert fetched and all(e.statement_range is not None for e in fetched)
    where = fetched[0].statement_range
    assert where is not None
    assert DECLARED_CURSOR[where.start_offset : where.end_offset] == "FOR r IN c_open LOOP"
    assert all(e.source_token_range is None for e in fetched)
    assert [e for e in _real(result) if e.control_flow_context == "CURSOR_DECLARATION"]


def test_a_plpgsql_loop_over_a_bound_cursor_fetches_its_query_into_the_record() -> None:
    sql = _plpgsql(
        "  FOR r IN c_open(5) LOOP\n"
        "    INSERT INTO s.open_log (id) VALUES (r.id);\n"
        "  END LOOP;",
        declare="  c_open CURSOR (k int) FOR SELECT o.id FROM s.orders o WHERE o.kind = k;\n"
        "  r record;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")
    assert ("s.orders", "id", "s.open_log", "id") in {
        (s, c, t, d) for s, c, t, d, via in _end_to_end(result) if via
    }


def test_rows_this_parse_cannot_see_bind_no_record() -> None:
    """guard: `FOR r IN EXECUTE` is dynamic SQL. Its record is no intermediate -- an edge out
    of one nothing fills would be a source from nowhere -- so `r.a` stays the field it was."""
    sql = _plpgsql(
        "  FOR r IN EXECUTE v_sql LOOP\n"
        "    INSERT INTO s.t (a) VALUES (r.a);\n"
        "  END LOOP;",
        declare="  r record; v_sql text;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")
    assert [g for g in _gaps(result) if g.startswith("DYNAMIC_SQL")]
    assert not [e for e in _real(result) if is_routine_local(e.source_table)]


# ---------------------------------------------------------------------------
# 2. A ref cursor handed to the caller carries the routine's result set.
# ---------------------------------------------------------------------------


def _opened(result: ProcedureParseResult) -> set[tuple[str, str]]:
    return {
        (e.source_table, e.target_table)
        for e in _real(result)
        if e.control_flow_context == "CURSOR_OPEN" and e.target_column != FILTER
    }


def test_an_oracle_out_sys_refcursor_is_the_result_and_a_local_one_is_not() -> None:
    sql = """CREATE OR REPLACE PROCEDURE ops.open_both(p_rc OUT SYS_REFCURSOR, p_id IN NUMBER) IS
  v_local SYS_REFCURSOR;
BEGIN
  OPEN v_local FOR SELECT f.id FROM ops.flags f;
  OPEN p_rc FOR SELECT o.id, o.amount FROM ops.orders o WHERE o.customer_id = p_id;
END open_both;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")
    assert _opened(result) == {
        ("ops.flags", PROCEDURE_LOCAL_TARGET),
        ("ops.orders", PROCEDURE_RESULT_TARGET),
    }
    result_edges = [e for e in _real(result) if e.target_table == PROCEDURE_RESULT_TARGET]
    assert all(not e.is_intermediate and not e.is_write for e in result_edges)
    assert result.is_fully_parsed and result.is_read_only


def test_an_oracle_function_returning_a_ref_cursor_returns_what_it_opened() -> None:
    sql = """CREATE OR REPLACE FUNCTION ops.orders_for(p_id NUMBER) RETURN SYS_REFCURSOR IS
  v_rc SYS_REFCURSOR;
  v_other SYS_REFCURSOR;
BEGIN
  OPEN v_other FOR SELECT f.id FROM ops.flags f;
  OPEN v_rc FOR SELECT o.id FROM ops.orders o;
  RETURN v_rc;
END orders_for;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")
    assert _opened(result) == {
        ("ops.flags", PROCEDURE_LOCAL_TARGET),
        ("ops.orders", PROCEDURE_RESULT_TARGET),
    }


def test_a_package_ref_cursor_type_is_known_to_its_members() -> None:
    sql = """PACKAGE rc_pkg AS
  TYPE t_rc IS REF CURSOR;
  PROCEDURE open_orders(p_rc IN OUT rc_pkg.t_rc);
END rc_pkg;
PACKAGE BODY rc_pkg AS
  PROCEDURE open_orders(p_rc IN OUT rc_pkg.t_rc) IS
  BEGIN
    OPEN p_rc FOR SELECT o.id FROM ops.orders o;
  END open_orders;
  PROCEDURE load IS
    v_rc t_rc;
  BEGIN
    open_orders(v_rc);
  END load;
END rc_pkg;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")
    opened = [e for e in _real(result) if e.package_member == "open_orders"]
    assert {e.target_table for e in opened} == {PROCEDURE_RESULT_TARGET}
    # Read through at a sibling's call, the rows land in the caller's variable: local.
    load = [e for e in _real(result) if e.package_member == "load"]
    assert load and {e.target_table for e in load} == {PROCEDURE_LOCAL_TARGET}


def test_a_nested_subprograms_out_cursor_is_not_the_routines_result() -> None:
    """guard: the nested procedure hands its cursor to the routine that calls it."""
    sql = """CREATE OR REPLACE PROCEDURE ops.outer_proc IS
  v_rc SYS_REFCURSOR;
  PROCEDURE fill(p_rc OUT SYS_REFCURSOR) IS
  BEGIN
    OPEN p_rc FOR SELECT o.id FROM ops.orders o;
  END fill;
BEGIN
  fill(v_rc);
END outer_proc;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")
    assert _opened(result) == {("ops.orders", PROCEDURE_LOCAL_TARGET)}


@pytest.mark.parametrize(
    ("header", "declare", "body"),
    [
        ("FUNCTION s.f(OUT c refcursor) RETURNS refcursor", "",
         "  OPEN c FOR SELECT o.id FROM s.orders o;"),
        ("PROCEDURE s.p(INOUT c refcursor)", "",
         "  OPEN c FOR SELECT o.id FROM s.orders o;"),
        ("FUNCTION s.f() RETURNS refcursor", "  c refcursor;",
         "  OPEN c FOR SELECT o.id FROM s.orders o;\n  RETURN c;"),
        ("FUNCTION s.f() RETURNS SETOF refcursor", "  c refcursor;",
         "  OPEN c FOR SELECT o.id FROM s.orders o;\n  RETURN NEXT c;"),
    ],
)
def test_a_plpgsql_refcursor_handed_back_is_the_result(
    header: str, declare: str, body: str
) -> None:
    sql = _plpgsql(body, header=header, declare=declare)
    result = parse_procedure_lineage(sql, dialect="postgres")
    assert _opened(result) == {("s.orders", PROCEDURE_RESULT_TARGET)}


def test_a_plpgsql_refcursor_kept_inside_is_local() -> None:
    """guard: opened and read here, never handed back."""
    sql = _plpgsql(
        "  OPEN c FOR SELECT o.id FROM s.orders o;\n  CLOSE c;",
        header="FUNCTION s.f() RETURNS integer",
        declare="  c refcursor;",
    )
    result = parse_procedure_lineage(sql, dialect="postgres")
    assert _opened(result) == {("s.orders", PROCEDURE_LOCAL_TARGET)}


def test_the_result_cursors_query_is_what_tool_generation_exposes() -> None:
    """As `RETURN QUERY` is: the routine's one read-only result statement."""
    sql = """CREATE OR REPLACE PROCEDURE ops.open_orders(p_rc OUT SYS_REFCURSOR) IS
BEGIN
  OPEN p_rc FOR SELECT o.id, o.amount FROM ops.orders o;
END open_orders;
"""
    node, result = find_single_read_only_result_statement(sql, "oracle")
    assert node.sql(dialect="oracle").startswith("SELECT o.id, o.amount FROM ops.orders")
    assert result.is_read_only


# ---------------------------------------------------------------------------
# 3. A table read with no column named is a table-grain edge.
# ---------------------------------------------------------------------------


def _rows(result: ProcedureParseResult) -> list[ProcedureLineageEdgeRecord]:
    return [e for e in result.edges if e.transformation_type == ROWS]


def test_a_count_into_a_variable_depends_on_the_table() -> None:
    sql = """CREATE OR REPLACE PROCEDURE ops.counter IS
  v_n NUMBER;
BEGIN
  SELECT count(*) INTO v_n FROM ops.orders;
END counter;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")
    [edge] = _rows(result)
    assert (edge.source_table, edge.source_column, edge.target_table, edge.target_column) == (
        "ops.orders", "*", PROCEDURE_LOCAL_TARGET, "*"
    )
    assert (edge.confidence, edge.is_intermediate, edge.is_write) == ("PARTIAL", True, False)
    assert edge.statement_range_status == StatementRangeStatus.STATEMENT.value
    assert _text(sql, edge.source_token_range) == "ops.orders"
    # The INTO target is a variable, never a source.
    assert {e.source_table for e in result.edges} == {"ops.orders"}


def test_exists_select_1_in_a_write_is_a_dependency_of_the_write() -> None:
    sql = (
        "CREATE PROCEDURE dbo.purge AS\nBEGIN\n"
        "    DELETE FROM dbo.stage WHERE EXISTS (SELECT 1 FROM dbo.blocklist);\nEND"
    )
    result = parse_procedure_lineage(sql, dialect="tsql")
    [edge] = _rows(result)
    assert (edge.source_table, edge.target_table, edge.is_write) == (
        "dbo.blocklist", "dbo.stage", True
    )
    assert _text(sql, edge.source_token_range) == "dbo.blocklist"
    assert _text(sql, edge.target_token_range) == "dbo.stage"


def test_select_1_from_a_table_is_a_result_that_depends_on_it() -> None:
    result = parse_procedure_lineage(
        "CREATE PROCEDURE dbo.ping AS\nBEGIN\n    SELECT 1 FROM dbo.heartbeat;\nEND", dialect="tsql"
    )
    assert {(e.source_table, e.target_table) for e in _rows(result)} == {
        ("dbo.heartbeat", PROCEDURE_RESULT_TARGET)
    }


def test_a_table_named_twice_has_no_token() -> None:
    """The NULL rule: two references could be the evidence, so neither is recorded."""
    sql = (
        "CREATE PROCEDURE dbo.p AS\nBEGIN\n    DELETE FROM dbo.stage WHERE EXISTS "
        "(SELECT 1 FROM dbo.blocked) OR EXISTS (SELECT 1 FROM dbo.blocked);\nEND"
    )
    [edge] = _rows(parse_procedure_lineage(sql, dialect="tsql"))
    assert edge.source_table == "dbo.blocked"
    assert edge.source_token_range is None
    assert _text(sql, edge.target_token_range) == "dbo.stage"


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO dbo.out (id) SELECT x.id FROM dbo.x x",  # every source named
        "DELETE FROM dbo.stage",  # reads nothing: its table is what it deletes
        "DELETE TOP (1) FROM dbo.queue",  # sqlglot keeps TOP as a "table" to delete from
        "UPDATE f SET f.seen = 1 FROM dbo.flags f",  # T-SQL: the FROM item is the target
        "WITH c AS (SELECT y.id FROM dbo.y y) INSERT INTO dbo.out (id) SELECT id FROM c",
    ],
)
def test_a_table_the_statement_names_or_writes_gets_no_table_grain_edge(statement: str) -> None:
    """guard."""
    result = parse_procedure_lineage(
        f"CREATE PROCEDURE dbo.p AS\nBEGIN\n    {statement};\nEND", dialect="tsql"
    )
    assert _rows(result) == []


def test_a_statement_that_reads_its_own_target_again_says_so() -> None:
    """The target is excluded as the node it is, never by its name: the subquery reads the
    same table's rows, and the delete depends on them."""
    sql = (
        "CREATE PROCEDURE dbo.p AS\nBEGIN\n"
        "    DELETE FROM dbo.q WHERE EXISTS (SELECT 1 FROM dbo.q);\nEND"
    )
    [edge] = _rows(parse_procedure_lineage(sql, dialect="tsql"))
    assert (edge.source_table, edge.target_table) == ("dbo.q", "dbo.q")
    # Two references to dbo.q, but only one can be a source: the other is what is written.
    assert edge.source_token_range is not None
    assert sql[edge.source_token_range.start_offset :].startswith("dbo.q)")


def test_oracle_dual_is_not_a_dependency() -> None:
    """guard: `FROM dual` is Oracle's one-row dummy table; every sequence read names it."""
    sql = """CREATE OR REPLACE PROCEDURE ops.next_id IS
  v NUMBER;
BEGIN
  SELECT ops_seq.NEXTVAL INTO v FROM dual;
END next_id;
"""
    assert _rows(parse_procedure_lineage(sql, dialect="oracle")) == []


def test_a_count_through_a_cursor_loop_reaches_the_write() -> None:
    """The record's rows depend on the table even though no column of it is named."""
    result = parse_procedure_lineage(
        _plpgsql(
            "  FOR r IN SELECT 1 AS one FROM s.flags LOOP\n"
            "    INSERT INTO s.pings (n) VALUES (1);\n"
            "  END LOOP;",
            declare="  r record;",
        ),
        dialect="postgres",
    )
    assert {(e.source_table, e.target_table) for e in _rows(result)} == {("s.flags", "<LOCAL:r@0>")}


@pytest.mark.parametrize(
    ("sql", "dialect", "table", "condition"),
    [
        (
            "CREATE PROCEDURE dbo.p AS\nBEGIN\n    IF EXISTS (SELECT 1 FROM dbo.flags) BEGIN\n"
            "        INSERT INTO dbo.out (id) SELECT x.id FROM dbo.x x;\n    END\nEND",
            "tsql",
            "dbo.flags",
            "EXISTS (SELECT 1 FROM dbo.flags)",
        ),
        (
            _plpgsql(
                "  IF EXISTS (SELECT 1 FROM s.flags f WHERE f.on) THEN\n"
                "    INSERT INTO s.out (id) SELECT x.id FROM s.x x;\n  END IF;"
            ),
            "postgres",
            "s.flags",
            "EXISTS (SELECT 1 FROM s.flags f WHERE f.on)",
        ),
        (
            "CREATE PROCEDURE dbo.drain AS\nBEGIN\n"
            "    WHILE (SELECT COUNT(*) FROM dbo.queue) > 0 BEGIN\n"
            "        DELETE FROM dbo.out WHERE 1 = 1;\n    END\nEND",
            "tsql",
            "dbo.queue",
            "(SELECT COUNT(*) FROM dbo.queue) > 0",
        ),
    ],
)
def test_a_condition_that_holds_a_query_is_read(
    sql: str, dialect: str, table: str, condition: str
) -> None:
    result = parse_procedure_lineage(sql, dialect=dialect)
    assert result.is_fully_parsed, _gaps(result)
    read = [e for e in _real(result) if e.control_flow_context == CONDITION_CONTEXT]
    assert read and {e.source_table for e in read} == {table}
    assert all(e.target_table == PROCEDURE_LOCAL_TARGET and e.is_intermediate for e in read)
    where = read[0].statement_range
    assert where is not None
    assert sql[where.start_offset : where.end_offset] == condition


def test_a_condition_without_a_query_adds_no_statement() -> None:
    """guard: the ordinals of a body whose conditions read nothing do not move."""
    sql = (
        "CREATE PROCEDURE dbo.p @x int AS\nBEGIN\n    IF @x = 1 BEGIN\n"
        "        INSERT INTO dbo.out (id) SELECT y.id FROM dbo.y y;\n    END\nEND"
    )
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert {e.statement_ordinal for e in result.edges} == {0}


# ---------------------------------------------------------------------------
# 4. T-SQL: END then IF / WHILE is two statements, not END IF / END WHILE.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("separator", [";\n", "\n"])
@pytest.mark.parametrize(
    "follower",
    [
        "IF @x = 1 BEGIN\n        INSERT INTO dbo.b (id) SELECT s.id FROM dbo.src s;\n    END",
        "WHILE @x < 3 BEGIN\n        INSERT INTO dbo.b (id) SELECT s.id FROM dbo.src s;\n    END",
    ],
)
def test_a_tsql_end_followed_by_if_or_while_closes_its_block(separator: str, follower: str) -> None:
    # A body handed to the parser, never executed.
    head = (
        "CREATE PROCEDURE dbo.p @x int AS\nBEGIN\n"
        "    IF @x = 0 BEGIN\n        INSERT INTO dbo.a (id) SELECT s.id FROM dbo.src s;\n"
    )
    sql = head + "    END" + separator + "    " + follower + "\nEND"
    result = parse_procedure_lineage(sql, dialect="tsql")
    assert result.is_fully_parsed, _gaps(result)
    assert {e.target_table for e in result.edges} == {"dbo.a", "dbo.b"}
    first = next(e for e in result.edges if e.target_table == "dbo.a")
    assert first.statement_range is not None
    assert sql[first.statement_range.start_offset :].startswith("INSERT INTO dbo.a")


def test_plsql_end_if_and_end_loop_still_close_nothing_but_their_own() -> None:
    """guard: `END IF;` and `END LOOP label;` are PL/SQL's compound closers."""
    sql = """CREATE OR REPLACE PROCEDURE ops.p IS
BEGIN
  <<outer>>
  FOR i IN 1..3 LOOP
    IF i = 2 THEN
      INSERT INTO ops.a (id) SELECT s.id FROM ops.src s;
    END IF;
  END LOOP outer;
  INSERT INTO ops.b (id) SELECT s.id FROM ops.src s;
END p;
"""
    result = parse_procedure_lineage(sql, dialect="oracle")
    assert result.is_fully_parsed, _gaps(result)
    assert {e.target_table for e in result.edges} == {"ops.a", "ops.b"}


# ---------------------------------------------------------------------------
# 5. A called routine's loop records stay the callee's.
# ---------------------------------------------------------------------------


def test_a_callees_loop_record_is_never_joined_to_the_callers() -> None:
    """Catalog descent splices a callee's edges into the caller and runs the hop pass again.
    Both bodies name their first loop's record `rec` at statement 0, so without the callee's
    name on its record the caller's `ops.a` would reach the callee's `ops.out_b`, and the
    callee's `ops.b` the caller's `ops.out_a` -- two paths that do not exist."""
    from aida.routine_call_descent import Callee, descend_nested_calls

    caller = """CREATE OR REPLACE PROCEDURE ops.outer_p IS
BEGIN
  FOR rec IN (SELECT a.x FROM ops.a a) LOOP
    INSERT INTO ops.out_a (x) VALUES (rec.x);
  END LOOP;
  inner_p;
END outer_p;
"""
    callee = """CREATE OR REPLACE PROCEDURE ops.inner_p IS
BEGIN
  FOR rec IN (SELECT b.x FROM ops.b b) LOOP
    INSERT INTO ops.out_b (x) VALUES (rec.x);
  END LOOP;
END inner_p;
"""

    def resolve(name: str) -> Callee:
        return Callee("inner", "ops.inner_p", callee)

    result = descend_nested_calls(
        parse_procedure_lineage(caller, dialect="oracle"),
        dialect="oracle",
        resolve=resolve,
        root_key="outer",
    )
    assert result.is_fully_parsed, _gaps(result)
    flows = {(s, t) for s, _c, t, _d, via in _end_to_end(result) if via is not None}
    assert flows == {("ops.a", "ops.out_a"), ("ops.b", "ops.out_b")}
    # The callee's record is still routine-local, and says whose it is.
    spliced = {
        e.source_table for e in _real(result) if e.via_routine and e.target_table == "ops.out_b"
    }
    assert {name for name in spliced if is_routine_local(name)} == {"<LOCAL:ops.inner_p:rec@0>"}
