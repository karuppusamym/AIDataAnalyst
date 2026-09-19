"""Stored-procedure lineage that recorded a wrong fact, or silently dropped a read.

Found by R11-FP07 (token ranges) and R11-FP03 (PL/SQL calls and declarations) and
deliberately left by both. Each was either a fact the text does not state or a read the
parse never reported:

* **Attribution by scope.** An unqualified column was given to "the statement's one source",
  counted over every table in the statement except its own write target. A DELETE's or
  UPDATE's own target *is* in scope, and a table named only inside a subquery is *not* in
  scope for the statement around it, so `DELETE FROM dbo.final WHERE id IN (SELECT r.id FROM
  dbo.rejects r)` recorded its outer `id` as `dbo.rejects.id`. A column now resolves against
  the scope it is written in -- its own FROM (and a DML statement's target), then the scopes
  it can correlate to -- and when more than one table there could own it, it is recorded the
  way this parser records any column it cannot place: `UNRESOLVED`, never the first or the
  only-inner table. A derived table or CTE passes a column through only by the same name.
* **Names the routine declares.** A cursor's parameter, a routine parameter, a local
  variable, a loop record: none is a column of a table. PL/pgSQL rejects a name that could be
  both (its default `variable_conflict = error`), so there a declared name is the variable and
  records nothing -- as a T-SQL `@variable` never has -- unless the body opts into
  `#variable_conflict use_column`. PL/SQL gives a *column* precedence over a variable of the
  same name, so there a declared name is ambiguous: `UNRESOLVED`, never attributed. A
  qualifier that names a declared record (`rec.amount`) is the record's field, never a table.
* **Cursor reads that were dropped.** `OPEN c FOR <query>` (PL/SQL and PL/pgSQL), T-SQL
  `DECLARE c CURSOR ... FOR <query>` and `SET @c = CURSOR FOR <query>`, and PL/pgSQL
  declaration sections -- `c CURSOR (p int) FOR <query>`, a variable whose default is a query --
  were read as having no lineage at all. `OPEN c FOR <string>` and `OPEN c FOR EXECUTE` are
  dynamic SQL.
* **An INSERT with a column list** took its query from the first UNION -- else SELECT --
  anywhere in the statement, zipped only a set operation's first branch, and dropped its
  WHERE's filter evidence (found writing a guard here).
* **Two PL/SQL shapes that read as gaps or as nothing**: `pkg.delete(x)` was taken for a
  collection method (a clean statement) though only a declared collection variable has one;
  a simple `CASE x WHEN ...` statement and a `<<label>>` were PARSE_ERROR gaps.

Every test here fails on the tree before this change except the ones marked *guard*, which
pin a behaviour the change must not lose (dynamic SQL stays dynamic, an ambiguous column stays
ambiguous, a `%TYPE` anchor is still not a read, a lineage-free declaration still produces no
statement).
"""

from __future__ import annotations

import pytest

from aida import procedure_lineage
from aida.procedure_lineage import (
    PROCEDURE_LOCAL_TARGET,
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    StatementRangeStatus,
    parse_procedure_lineage,
)
from aida.sql_lineage_parser import (
    FILTER_EVIDENCE_TARGET_COLUMN,
    PROCEDURE_RESULT_TARGET,
    UNRESOLVED_TABLE,
    Confidence,
)

FILTER = FILTER_EVIDENCE_TARGET_COLUMN


def _real(result: ProcedureParseResult) -> list[ProcedureLineageEdgeRecord]:
    return [e for e in result.edges if e.transformation_type != UNPARSED_TRANSFORMATION_TYPE]


def _gaps(result: ProcedureParseResult) -> list[str]:
    return [
        edge.unparsed_reason or ""
        for edge in result.edges
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]


def _facts(result: ProcedureParseResult) -> set[tuple[str, str, str, str]]:
    """`(source_table, source_column, target_table, target_column)` of every direct edge."""
    return {
        (e.source_table, e.source_column, e.target_table, e.target_column)
        for e in _real(result)
        if e.via_temp_table is None
    }


def _sources(result: ProcedureParseResult) -> set[tuple[str, str]]:
    return {(e.source_table, e.source_column) for e in _real(result)}


def _token(text: str, edge: ProcedureLineageEdgeRecord) -> str | None:
    token = edge.source_token_range
    return None if token is None else text[token.start_offset : token.end_offset]


def _edge(
    result: ProcedureParseResult, source: tuple[str, str], target: tuple[str, str]
) -> ProcedureLineageEdgeRecord:
    [found] = [
        e
        for e in _real(result)
        if (e.source_table, e.source_column) == source
        and (e.target_table, e.target_column) == target
        and e.via_temp_table is None
    ]
    return found


def _tsql(statement: str) -> str:
    return f"CREATE PROCEDURE dbo.p AS\nBEGIN\n    {statement};\nEND"


def _plpgsql(body: str, *, header: str = "FUNCTION s.f() RETURNS void", declare: str = "") -> str:
    section = f"DECLARE\n{declare}\n" if declare else ""
    return (
        f"CREATE OR REPLACE {header} LANGUAGE plpgsql AS $function$\n"
        f"{section}BEGIN\n{body}\nEND\n$function$"
    )


# ---------------------------------------------------------------------------
# 1. An unqualified column resolves against the scope it is written in.
# ---------------------------------------------------------------------------


def test_a_deletes_own_target_owns_its_unqualified_outer_column() -> None:
    """The defect as found: the outer `id` is `dbo.final.id`, and only `r.id` is a reject's."""
    sql = _tsql("DELETE FROM dbo.final WHERE id IN (SELECT r.id FROM dbo.rejects r)")
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert _facts(result) == {
        ("dbo.final", "id", "dbo.final", FILTER),
        ("dbo.rejects", "id", "dbo.final", FILTER),
    }
    # Two facts, each located at its own reference -- no longer one fact competing for both.
    assert _token(sql, _edge(result, ("dbo.final", "id"), ("dbo.final", FILTER))) == "id"
    assert _token(sql, _edge(result, ("dbo.rejects", "id"), ("dbo.final", FILTER))) == "r.id"


def test_an_updates_own_target_is_in_scope_for_its_set_and_where() -> None:
    sql = _tsql(
        "UPDATE dbo.final SET flag = src_flag WHERE id IN (SELECT r.id FROM dbo.rejects r)"
    )
    result = parse_procedure_lineage(sql, dialect="tsql")

    assert ("dbo.final", "src_flag", "dbo.final", "flag") in _facts(result)
    assert ("dbo.final", "id", "dbo.final", FILTER) in _facts(result)
    assert ("dbo.rejects", "src_flag") not in _sources(result)


def test_a_column_two_tables_in_scope_could_own_is_ambiguous_never_the_first() -> None:
    """`flag` could be `s.final`'s or the USING table's: recorded unresolved, the parser's
    one convention for a column it cannot place, never guessed onto either."""
    sql = _plpgsql(
        "  DELETE FROM s.final USING s.rejects r WHERE final.id = r.id AND flag = 1;"
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    flag = _edge(result, (UNRESOLVED_TABLE, "flag"), ("s.final", FILTER))
    assert flag.source_resolved is False
    assert flag.confidence == Confidence.PARTIAL.value
    assert {("s.final", "id"), ("s.rejects", "id")} <= _sources(result)
    assert ("s.rejects", "flag") not in _sources(result)


def test_update_from_join_with_an_unqualified_source_stays_ambiguous() -> None:
    """*Guard*: the target is `dbo.orders` through its alias, and `dbo.src` is also in scope."""
    result = parse_procedure_lineage(
        _tsql("UPDATE o SET total = amt FROM dbo.orders o JOIN dbo.src s ON s.id = o.id"),
        dialect="tsql",
    )
    assert _facts(result) == {(UNRESOLVED_TABLE, "amt", "dbo.orders", "total")}


def test_a_merge_branch_sees_what_that_branch_can_see() -> None:
    """WHEN MATCHED sees target and source, so its unqualified `w` could be either's; a WHEN
    NOT MATCHED INSERT sees only the source -- the target row does not exist."""
    body = (
        "BEGIN\n"
        "  MERGE INTO app.t d USING app.s s ON (d.id = s.id)\n"
        "  WHEN MATCHED THEN UPDATE SET d.v = w\n"
        "  WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, w);\n"
        "END;"
    )
    result = parse_procedure_lineage(body, dialect="oracle")

    assert (UNRESOLVED_TABLE, "w", "app.t", "v") in _facts(result)
    assert ("app.s", "w", "app.t", "v") in _facts(result)
    assert ("app.s", "id", "app.t", "id") in _facts(result)


def test_an_outer_column_never_resolves_to_a_table_only_a_subquery_names() -> None:
    """`a` and `b` are in the outer scope, which names only `dbo.x`. `c` is in the subquery,
    where `dbo.y` is in scope and `dbo.x` is still visible for correlation: ambiguous."""
    result = parse_procedure_lineage(
        _tsql("INSERT INTO dbo.out (a) SELECT a FROM dbo.x WHERE b IN (SELECT c FROM dbo.y)"),
        dialect="tsql",
    )
    assert _facts(result) == {
        ("dbo.x", "a", "dbo.out", "a"),
        ("dbo.x", "b", "dbo.out", FILTER),
        (UNRESOLVED_TABLE, "c", "dbo.out", FILTER),
        # 2026-09-19: no column is attributed to `dbo.y`, yet the statement reads it, so it
        # is stated at table grain (TABLE_ROWS) -- never by giving it `c`.
        ("dbo.y", "*", "dbo.out", "*"),
    }


def test_a_correlated_subquery_does_not_capture_the_outer_statements_columns() -> None:
    result = parse_procedure_lineage(
        _tsql(
            "INSERT INTO dbo.out (id) SELECT id FROM dbo.orders "
            "WHERE EXISTS (SELECT 1 FROM dbo.lines l WHERE l.oid = orders.id AND qty > 0)"
        ),
        dialect="tsql",
    )
    assert ("dbo.orders", "id", "dbo.out", "id") in _facts(result)
    # `qty` could be a line's or (correlated) an order's.
    assert (UNRESOLVED_TABLE, "qty", "dbo.out", FILTER) in _facts(result)
    assert ("dbo.lines", "qty") not in _sources(result)


def test_a_derived_table_passes_a_column_through_only_by_the_same_name() -> None:
    """`(SELECT b AS a FROM s.x) d`: the derived `a` is `s.x.b`, so neither `s.x.a` nor a
    table called `d` is a fact the text states. A same-name pass-through still resolves."""
    body = (
        "  INSERT INTO s.t (a) SELECT a FROM (SELECT b AS a FROM s.x) d;\n"
        "  INSERT INTO s.t (a) SELECT d.a FROM (SELECT b AS a FROM s.x) d;\n"
        "  INSERT INTO s.t (a) SELECT a FROM (SELECT x.a FROM s.x x) d;"
    )
    result = parse_procedure_lineage(_plpgsql(body), dialect="postgres")

    assert ("s.x", "a", "s.t", "a") in _facts(result)  # the third statement only
    by_ordinal = {
        e.statement_ordinal: (e.source_table, e.source_column) for e in _real(result)
    }
    assert by_ordinal == {0: (UNRESOLVED_TABLE, "a"), 1: (UNRESOLVED_TABLE, "a"), 2: ("s.x", "a")}
    assert "d" not in {table for table, _column in _sources(result)}


def test_an_insert_with_a_column_list_keeps_its_filter_evidence() -> None:
    """The attribution this replaces was right where one table is in scope, and still is.
    Found writing this as a guard: with a column list, the INSERT's WHERE produced no
    FILTERED edge at all -- a read the same statement without a column list reported."""
    result = parse_procedure_lineage(
        _tsql("INSERT INTO dbo.out (a) SELECT a FROM dbo.x WHERE b > 0"), dialect="tsql"
    )
    assert _facts(result) == {("dbo.x", "a", "dbo.out", "a"), ("dbo.x", "b", "dbo.out", FILTER)}
    assert all(e.source_resolved for e in _real(result))


def test_an_insert_reads_its_own_source_query_and_every_branch() -> None:
    """The source used to be the first UNION -- else SELECT -- anywhere in the statement: a
    UNION inside a WHERE became the rows inserted, and a set operation's second branch was
    never zipped onto the column list."""
    nested = parse_procedure_lineage(
        _tsql(
            "INSERT INTO dbo.out (a) SELECT p.x FROM dbo.p p "
            "WHERE p.k IN (SELECT 1 UNION SELECT 2)"
        ),
        dialect="tsql",
    )
    assert _facts(nested) == {("dbo.p", "x", "dbo.out", "a"), ("dbo.p", "k", "dbo.out", FILTER)}

    union = parse_procedure_lineage(
        _tsql("INSERT INTO dbo.out (a) SELECT p.x FROM dbo.p p UNION SELECT q.y FROM dbo.q q"),
        dialect="tsql",
    )
    assert _facts(union) == {("dbo.p", "x", "dbo.out", "a"), ("dbo.q", "y", "dbo.out", "a")}


# ---------------------------------------------------------------------------
# 2. A name the routine declares is not a column of a table.
# ---------------------------------------------------------------------------

ORACLE_CURSOR_PARAMETER = """CREATE OR REPLACE PROCEDURE ops.flag_walk(p_min NUMBER) IS
  CURSOR c(p_id NUMBER) IS SELECT flag FROM ops.flags WHERE id = p_id;
  v_limit NUMBER := 10;
BEGIN
  UPDATE ops.limits SET cap = v_limit WHERE id = p_min;
END flag_walk;
"""


def test_an_oracle_cursor_parameter_is_never_a_column_of_the_cursors_table() -> None:
    """PL/SQL gives a column precedence over a variable of the same name, so without the
    catalog `p_id` may be either: recorded unresolved, never as `ops.flags.p_id`."""
    result = parse_procedure_lineage(ORACLE_CURSOR_PARAMETER, dialect="oracle")

    assert ("ops.flags", "p_id") not in _sources(result)
    assert (UNRESOLVED_TABLE, "p_id", PROCEDURE_LOCAL_TARGET, FILTER) in _facts(result)
    # The cursor's own columns still resolve to its one table.
    assert {("ops.flags", "flag"), ("ops.flags", "id")} <= _sources(result)


def test_an_oracle_routine_parameter_and_local_are_ambiguous_once_the_target_is_in_scope() -> None:
    """With the UPDATE's target now in scope, `v_limit` and `p_min` would have been
    attributed to `ops.limits` had declared names not been told apart; `id` is its own."""
    result = parse_procedure_lineage(ORACLE_CURSOR_PARAMETER, dialect="oracle")

    assert ("ops.limits", "id", "ops.limits", FILTER) in _facts(result)
    assert (UNRESOLVED_TABLE, "v_limit", "ops.limits", "cap") in _facts(result)
    assert (UNRESOLVED_TABLE, "p_min", "ops.limits", FILTER) in _facts(result)
    assert not {("ops.limits", "v_limit"), ("ops.limits", "p_min")} & _sources(result)


PLPGSQL_NAMES = _plpgsql(
    "  SELECT o.amount INTO v_amount FROM s.orders o WHERE o.id = p_id;\n"
    "  RETURN QUERY SELECT a FROM s.t WHERE b = p_id AND c = v_limit AND d = v_amount;",
    header="FUNCTION s.amounts(p_id integer) RETURNS TABLE(amount integer)",
    declare="  v_limit integer := 10;\n  v_amount numeric;",
)


def test_a_plpgsql_parameter_or_variable_is_the_variable_and_records_nothing() -> None:
    result = parse_procedure_lineage(PLPGSQL_NAMES, dialect="postgres")

    names = {"p_id", "v_limit", "v_amount"}
    assert not names & {column for _table, column in _sources(result)}
    assert {("s.t", "a"), ("s.t", "b"), ("s.t", "c"), ("s.t", "d")} <= _sources(result)
    assert ("s.orders", "id") in _sources(result)
    assert result.is_fully_parsed


def test_variable_conflict_use_column_makes_a_declared_name_ambiguous() -> None:
    sql = PLPGSQL_NAMES.replace("DECLARE\n", "#variable_conflict use_column\nDECLARE\n")
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert (UNRESOLVED_TABLE, "v_limit") in _sources(result)
    assert ("s.t", "v_limit") not in _sources(result)


def test_a_declared_record_field_is_never_a_table() -> None:
    body = """CREATE OR REPLACE PROCEDURE ops.copy_rows IS
  v_row ops.src%ROWTYPE;
BEGIN
  FOR rec IN (SELECT s.a, s.b FROM ops.src s) LOOP
    INSERT INTO ops.dst (a, b) VALUES (rec.a, v_row.b);
  END LOOP;
END copy_rows;
"""
    result = parse_procedure_lineage(body, dialect="oracle")

    tables = {table for table, _column in _sources(result)}
    assert not {"rec", "v_row"} & tables
    assert {("ops.src", "a"), ("ops.src", "b")} <= _sources(result)


def test_a_qualifier_that_names_nothing_in_scope_is_not_invented_as_a_table() -> None:
    """`seq.NEXTVAL` and `EXCLUDED.x` qualify with names no FROM declares; recording them as
    tables called `seq` or `EXCLUDED` states a table the text does not name."""
    oracle = parse_procedure_lineage(
        "BEGIN\n  INSERT INTO ops.dst (id, a) SELECT ops_seq.NEXTVAL, s.a FROM ops.src s;\nEND;",
        dialect="oracle",
    )
    assert "ops_seq" not in {table for table, _column in _sources(oracle)}
    assert ("ops.src", "a", "ops.dst", "a") in _facts(oracle)

    pg = parse_procedure_lineage(
        _plpgsql(
            "  INSERT INTO s.t (id, v) SELECT u.id, u.v FROM s.u u\n"
            "  ON CONFLICT (id) DO UPDATE SET v = EXCLUDED.v;"
        ),
        dialect="postgres",
    )
    assert "EXCLUDED" not in {table for table, _column in _sources(pg)}


# ---------------------------------------------------------------------------
# 3. OPEN c FOR <query> is a read; OPEN c FOR <string> is dynamic SQL.
# ---------------------------------------------------------------------------

ORACLE_OPEN_FOR = """CREATE OR REPLACE PROCEDURE ops.open_orders(p_rc OUT SYS_REFCURSOR) IS
  v_sql VARCHAR2(200);
BEGIN
  OPEN p_rc FOR SELECT o.id, o.amount FROM ops.orders o WHERE o.status = 1;
END open_orders;
"""


def test_an_oracle_open_for_query_is_read_and_located() -> None:
    result = parse_procedure_lineage(ORACLE_OPEN_FOR, dialect="oracle")

    assert {("ops.orders", "id"), ("ops.orders", "amount"), ("ops.orders", "status")} == (
        _sources(result)
    )
    edges = _real(result)
    # 2026-09-19: `p_rc` is an OUT SYS_REFCURSOR, so what it is opened for is the routine's
    # result set (tests/test_r11_fp07_loop_records_and_result_cursors.py); it used to be
    # read into local state, because an OUT ref cursor was not told apart from a local one.
    assert {e.target_table for e in edges} == {PROCEDURE_RESULT_TARGET}
    assert all(not e.is_intermediate and not e.is_write for e in edges)
    assert {e.control_flow_context for e in edges} == {procedure_lineage.CURSOR_OPEN_CONTEXT}
    assert result.is_fully_parsed and result.is_read_only

    status = _edge(result, ("ops.orders", "status"), (PROCEDURE_RESULT_TARGET, FILTER))
    where = status.statement_range
    assert where is not None
    assert status.statement_range_status == StatementRangeStatus.STATEMENT.value
    assert ORACLE_OPEN_FOR[where.start_offset : where.end_offset].startswith("OPEN p_rc FOR")
    assert _token(ORACLE_OPEN_FOR, status) == "o.status"


@pytest.mark.parametrize(
    "statement",
    ["OPEN p_rc FOR v_sql", "OPEN p_rc FOR v_sql USING p_min", "OPEN p_rc FOR 'SELECT 1'"],
)
def test_an_oracle_open_for_a_string_is_dynamic_sql(statement: str) -> None:
    body = ORACLE_OPEN_FOR.replace(
        "OPEN p_rc FOR SELECT o.id, o.amount FROM ops.orders o WHERE o.status = 1", statement
    )
    result = parse_procedure_lineage(body, dialect="oracle")

    assert [g for g in _gaps(result) if g.startswith("DYNAMIC_SQL")]
    assert result.is_fully_parsed is False


PLPGSQL_OPEN = _plpgsql(
    "  OPEN c1 FOR SELECT a.id FROM s.alpha a;\n"
    "  OPEN c2 SCROLL FOR SELECT b.id FROM s.beta b;\n"
    "  OPEN c3 NO SCROLL FOR WITH g AS (SELECT g.id FROM s.gamma g) SELECT id FROM g;\n"
    "  OPEN c4;\n"
    "  OPEN c5(7);\n"
    "  RETURN QUERY SELECT d.id FROM s.delta d;",
    header="FUNCTION s.cursors() RETURNS SETOF integer",
    declare="  c1 refcursor; c2 refcursor; c3 refcursor;\n"
    "  c4 CURSOR FOR SELECT e.id FROM s.epsilon e;\n"
    "  c5 CURSOR (k int) FOR SELECT z.id FROM s.zeta z WHERE z.k = k;",
)


def test_plpgsql_open_for_query_is_read_and_a_bound_open_is_lineage_free() -> None:
    result = parse_procedure_lineage(PLPGSQL_OPEN, dialect="postgres")

    opened = {e.source_table for e in _real(result) if e.control_flow_context == "CURSOR_OPEN"}
    assert opened == {"s.alpha", "s.beta", "s.gamma"}
    # RETURN QUERY was already the routine's result and stays so.
    assert ("s.delta", "id", PROCEDURE_RESULT_TARGET, "id") in _facts(result)
    assert result.is_fully_parsed, _gaps(result)


def test_plpgsql_open_for_execute_is_dynamic_sql() -> None:
    result = parse_procedure_lineage(
        _plpgsql("  OPEN c FOR EXECUTE 'SELECT 1' USING 5;", declare="  c refcursor;"),
        dialect="postgres",
    )
    assert [g for g in _gaps(result) if g.startswith("DYNAMIC_SQL")]


# ---------------------------------------------------------------------------
# 4. A T-SQL cursor's query is a read.
# ---------------------------------------------------------------------------

TSQL_CURSORS = """CREATE PROCEDURE dbo.walk AS
BEGIN
    DECLARE @id int, @n int = 0;
    DECLARE @rows TABLE (id int);
    DECLARE open_orders CURSOR LOCAL FAST_FORWARD FOR
        SELECT o.id FROM dbo.orders o WHERE o.status = 1 FOR READ ONLY;
    DECLARE flagged INSENSITIVE SCROLL CURSOR FOR SELECT f.id FROM dbo.flags f FOR UPDATE OF seen;
    DECLARE @c CURSOR;
    SET @c = CURSOR FORWARD_ONLY STATIC FOR SELECT l.id FROM dbo.lines l;
    OPEN open_orders;
    FETCH NEXT FROM open_orders INTO @id;
    CLOSE open_orders;
    DEALLOCATE open_orders;
END"""


def test_a_tsql_cursor_declaration_is_read_not_dropped() -> None:
    result = parse_procedure_lineage(TSQL_CURSORS, dialect="tsql")

    assert {("dbo.orders", "id"), ("dbo.orders", "status"), ("dbo.flags", "id"),
            ("dbo.lines", "id")} == _sources(result)
    assert {e.target_table for e in _real(result)} == {PROCEDURE_LOCAL_TARGET}
    assert {e.control_flow_context for e in _real(result)} == {"CURSOR_DECLARATION"}
    assert result.is_fully_parsed, _gaps(result)

    status = _edge(result, ("dbo.orders", "status"), (PROCEDURE_LOCAL_TARGET, FILTER))
    where = status.statement_range
    assert where is not None
    assert TSQL_CURSORS[where.start_offset : where.end_offset].startswith(
        "DECLARE open_orders CURSOR"
    )
    assert _token(TSQL_CURSORS, status) == "o.status"


def test_the_module_no_longer_claims_a_capability_it_lacked() -> None:
    """The docstring said a `DECLARE cur CURSOR FOR SELECT` query was captured while it was
    dropped (INV-9). It now is; the claim and the behaviour are pinned together."""
    doc = procedure_lineage.__doc__ or ""
    assert "DECLARE cur CURSOR" in doc
    assert parse_procedure_lineage(
        _tsql("DECLARE cur CURSOR FOR SELECT o.id FROM dbo.orders o"), dialect="tsql"
    ).edges


# ---------------------------------------------------------------------------
# 5. PL/pgSQL declaration sections are walked.
# ---------------------------------------------------------------------------

PLPGSQL_DECLARE = _plpgsql(
    "  NULL;",
    declare=(
        "  c1 CURSOR (p int) FOR SELECT t.a FROM s.t t WHERE t.b = p;\n"
        "  c2 NO SCROLL CURSOR FOR SELECT u.a FROM s.u u;\n"
        "  c3 SCROLL CURSOR IS SELECT w.a FROM s.w w;\n"
        "  v_count integer := (SELECT count(x.id) FROM s.x x);\n"
        "  v_anchor s.anchor.a%TYPE;\n"
        "  v_row s.anchor_row%ROWTYPE;\n"
        "  v_zero CONSTANT integer NOT NULL DEFAULT 0;\n"
        "  v_alias ALIAS FOR $1;"
    ),
)


def test_plpgsql_cursor_declarations_and_query_defaults_are_read() -> None:
    result = parse_procedure_lineage(PLPGSQL_DECLARE, dialect="postgres")

    assert {("s.t", "a"), ("s.t", "b"), ("s.u", "a"), ("s.w", "a"), ("s.x", "id")} == (
        _sources(result)
    )
    edges = _real(result)
    assert {e.target_table for e in edges} == {PROCEDURE_LOCAL_TARGET}
    contexts = {e.source_table: e.control_flow_context for e in edges}
    assert contexts["s.t"] == contexts["s.u"] == contexts["s.w"] == "CURSOR_DECLARATION"
    assert contexts["s.x"] == "DECLARATION"
    assert result.is_fully_parsed, _gaps(result)


def test_a_plpgsql_cursor_parameter_is_not_a_column() -> None:
    result = parse_procedure_lineage(PLPGSQL_DECLARE, dialect="postgres")
    assert ("s.t", "b") in _sources(result)  # the filter that compares with it is read
    assert "p" not in {column for _table, column in _sources(result)}


def test_a_plpgsql_anchor_is_still_not_a_read() -> None:
    """R11-FP03's decision carried to PL/pgSQL: `%TYPE`/`%ROWTYPE` take a table's structure,
    not its rows -- asserted beside a read from the same section, so it cannot pass by the
    section not being walked."""
    result = parse_procedure_lineage(PLPGSQL_DECLARE, dialect="postgres")
    tables = {table for table, _column in _sources(result)}
    assert "s.t" in tables
    assert not {"s.anchor", "s.anchor_row"} & tables


def test_a_plpgsql_declaration_read_is_located_at_the_declaration() -> None:
    result = parse_procedure_lineage(PLPGSQL_DECLARE, dialect="postgres")
    [b] = [e for e in _real(result) if e.source_column == "b"]
    where = b.statement_range
    assert where is not None
    assert PLPGSQL_DECLARE[where.start_offset : where.end_offset].startswith("c1 CURSOR (p int)")
    assert _token(PLPGSQL_DECLARE, b) == "t.b"
    [count] = [e for e in _real(result) if e.source_table == "s.x"]
    assert _token(PLPGSQL_DECLARE, count) == "x.id"


def test_a_plpgsql_declaration_that_cannot_be_read_is_a_located_gap() -> None:
    sql = _plpgsql("  NULL;", declare="  c CURSOR FOR SELEC t.a FROM s.t t;")
    result = parse_procedure_lineage(sql, dialect="postgres")

    [gap] = [e for e in result.edges if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE]
    assert result.is_fully_parsed is False
    assert gap.statement_range is not None
    assert sql[gap.statement_range.start_offset :].startswith("c CURSOR FOR SELEC")


def test_a_lineage_free_declaration_produces_no_statement() -> None:
    """*Guard*: ordinals of the body do not move for a declaration section with no read."""
    plain = _plpgsql("  INSERT INTO s.t (a) SELECT u.a FROM s.u u;")
    declared = _plpgsql(
        "  INSERT INTO s.t (a) SELECT u.a FROM s.u u;",
        declare="  v integer := 0;\n  w s.t%ROWTYPE;",
    )
    ordinals = {e.statement_ordinal for e in _real(parse_procedure_lineage(plain, "postgres"))}
    assert ordinals == {
        e.statement_ordinal for e in _real(parse_procedure_lineage(declared, "postgres"))
    }


def test_a_nested_plpgsql_blocks_declarations_are_read() -> None:
    sql = _plpgsql(
        "  DECLARE\n"
        "    v integer := (SELECT count(t.id) FROM s.t t);\n"
        "    c CURSOR FOR SELECT u.a FROM s.u u;\n"
        "    w integer := (SELECT max(m.b) FROM s.m m);\n"
        "  BEGIN\n"
        "    NULL;\n"
        "  END;"
    )
    result = parse_procedure_lineage(sql, dialect="postgres")

    assert {("s.t", "id"), ("s.u", "a"), ("s.m", "b")} == _sources(result)
    assert result.is_fully_parsed, _gaps(result)


# ---------------------------------------------------------------------------
# 6. (Stretch) A collection method is a declared collection variable's.
# ---------------------------------------------------------------------------


def test_delete_extend_and_trim_on_a_package_are_calls_not_collection_methods() -> None:
    body = """CREATE OR REPLACE PROCEDURE ops.tidy IS
  TYPE t_ids IS TABLE OF NUMBER;
  v_ids t_ids := t_ids();
BEGIN
  v_ids.EXTEND;
  v_ids.DELETE(1);
  v_ids.TRIM;
  cleanup.delete(5);
  cleanup.trim;
  archive_pkg.extend(p_days => 3);
END tidy;
"""
    result = parse_procedure_lineage(body, dialect="oracle")

    prefix = "NESTED_PROCEDURE_CALL: "
    calls = [g[len(prefix) :] for g in _gaps(result) if g.startswith(prefix)]
    assert calls == ["cleanup.delete", "cleanup.trim", "archive_pkg.extend"]


# ---------------------------------------------------------------------------
# 7. (Stretch) A simple CASE statement and a label parse.
# ---------------------------------------------------------------------------


def test_a_simple_case_statement_and_labels_parse() -> None:
    body = """CREATE OR REPLACE PROCEDURE ops.route(p_k NUMBER) IS
BEGIN
  <<outer>>
  FOR i IN 1..3 LOOP
    NULL;
  END LOOP outer;
  CASE p_k
    WHEN 1 THEN INSERT INTO ops.y (a) SELECT z.a FROM ops.z z;
    WHEN 2 THEN DELETE FROM ops.y;
    ELSE NULL;
  END CASE;
  <<blk>> BEGIN INSERT INTO ops.q (a) SELECT r.b FROM ops.r r; END blk;
END route;
"""
    result = parse_procedure_lineage(body, dialect="oracle")

    assert result.is_fully_parsed, _gaps(result)
    assert {("ops.z", "a", "ops.y", "a"), ("ops.r", "b", "ops.q", "a")} <= _facts(result)
