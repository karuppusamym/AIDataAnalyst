"""R11-FP03 remainder: PL/SQL call statements, calls between package members, and
declaration sections.

Three gaps the member-level split left, each a silent one:

* **A bare PL/SQL call statement** -- `other_proc(x);`, `pkg.other_proc(x);`, `other_proc;` --
  was not recognised as a call. PL/SQL writes a call with no CALL or EXEC, so sqlglot read it
  as a lone function or column expression, found no table, and the statement was classed as
  lineage-free: a body that calls a writer read as fully parsed, and read-only.
* **Calls between members of one package** therefore carried nothing. A member that calls a
  sibling now reads that sibling's lineage through at the call, resolved against the package's
  own members -- overloads by the arguments as written, and never guessed.
* **Declaration sections** were not walked: a `CURSOR c IS SELECT ...` read vanished, a
  nested subprogram hid a standalone routine's whole body, and package-level variables were
  PARSE_ERROR markers.

Plus the agent half: the lineage agent now records the member routine each package edge
belongs to, as a person's parse always did.

Every test here fails on the tree before this change; the negative cases are asserted
alongside a positive one in the same body, so they cannot pass by recognising nothing.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida import routine_call_descent
from aida.db import Base
from aida.envelope_models import MetadataRoutine, MetadataRoutineParameter
from aida.procedure_lineage import (
    MEMBER_CALL_AMBIGUOUS,
    MEMBER_CALL_NOT_FULLY_PARSED,
    PROCEDURE_LOCAL_TARGET,
    UNPARSED_TRANSFORMATION_TYPE,
    MemberAttribution,
    ProcedureParseResult,
    StatementRangeStatus,
    parse_procedure_lineage,
)
from aida.procedure_lineage_api import parse_deep_procedure_lineage_endpoint
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_call_descent import Callee, descend_nested_calls
from aida.task_agent import ACTION_PROPOSED
from tests.support.task_agents import register_agent, seed_estate, task_agent_session
from tests.test_lineage_agent import AGENT, _run
from tests.test_routine_parse_coverage import _context, _seed


def _gaps(result: ProcedureParseResult) -> list[str | None]:
    return [
        edge.unparsed_reason
        for edge in result.edges
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]


def _calls(result: ProcedureParseResult) -> list[str]:
    """The callees the parse left as nested-call gaps, as the reason spells them."""
    prefix = "NESTED_PROCEDURE_CALL: "
    return [r[len(prefix) :] for r in _gaps(result) if r and r.startswith(prefix)]


def _real(result: ProcedureParseResult) -> list[Any]:
    return [e for e in result.edges if e.transformation_type != UNPARSED_TRANSFORMATION_TYPE]


# ---------------------------------------------------------------------------
# 1. A bare PL/SQL call statement is a call.
# ---------------------------------------------------------------------------

# The declarations make this valid PL/SQL. They matter since 2026-09-19: a collection
# method is qualified by a declared collection (`v_ids.EXTEND`), and the same shape on a
# name the routine does not declare is a call into a package procedure called EXTEND.
NIGHTLY = """CREATE OR REPLACE PROCEDURE ops.nightly IS
  TYPE t_ids IS TABLE OF NUMBER;
  v_ids t_ids := t_ids();
  v_total NUMBER;
  v_id NUMBER;
BEGIN
  refresh_totals(SYSDATE);
  ops_pkg.load_flags;
  archive_all;
  IF 1 = 1 THEN
    notify(p_to => 'x', p_body => NULL);
  END IF;
  v_total := compute_total(v_id);
  v_ids(1) := 2;
  NULL;
  COMMIT;
  DBMS_OUTPUT.PUT_LINE('done');
  SYS.DBMS_OUTPUT.PUT_LINE('done');
  RAISE_APPLICATION_ERROR(-20001, 'bad');
  DBMS_SESSION.SLEEP(1);
  DBMS_APPLICATION_INFO.SET_MODULE('nightly', NULL);
  v_ids.EXTEND;
  v_ids.DELETE;
  FOR r IN (SELECT o.id FROM ops.orders o) LOOP
    NULL;
  END LOOP;
  RETURN;
EXCEPTION
  WHEN OTHERS THEN
    log_error(SQLCODE);
    RAISE;
END nightly;
"""


def test_only_the_call_statements_become_calls() -> None:
    """Five statements call a routine. Assignments (even one whose expression calls a
    function), NULL, COMMIT, RETURN, RAISE, collection methods, and the Oracle-supplied
    housekeeping calls do not: they move no table data, and a gap for each would make every
    body with a debug line read as not understood."""
    result = parse_procedure_lineage(NIGHTLY, dialect="oracle")

    assert _calls(result) == [
        "refresh_totals",
        "ops_pkg.load_flags",
        "archive_all",
        "notify",
        "log_error",
    ]
    assert result.is_fully_parsed is False
    # A call can write anything its callee writes: this is not proven read-only any more.
    assert result.is_read_only is False
    # The one real read is still read.
    assert {(e.source_table, e.source_column) for e in _real(result)} == {("ops.orders", "id")}


def test_a_call_marker_names_the_call_and_is_located_at_it() -> None:
    result = parse_procedure_lineage(NIGHTLY, dialect="oracle")
    [notify] = [
        e for e in result.edges if e.unparsed_reason == "NESTED_PROCEDURE_CALL: notify"
    ]
    assert notify.control_flow_context == "IF_BRANCH"
    assert notify.statement_range is not None
    assert notify.statement_range_status == StatementRangeStatus.GAP_STATEMENT.value
    where = notify.statement_range
    assert NIGHTLY[where.start_offset : where.end_offset] == "notify(p_to => 'x', p_body => NULL)"


def test_oracles_dynamic_sql_package_is_dynamic_sql_not_a_clean_call() -> None:
    body = (
        "PROCEDURE run_it(p_sql VARCHAR2) IS\n  c INTEGER;\nBEGIN\n"
        "  DBMS_SQL.PARSE(c, p_sql, DBMS_SQL.NATIVE);\n"
        "  INSERT INTO ops.t (id) SELECT s.id FROM ops.s s;\nEND run_it;\n"
    )
    result = parse_procedure_lineage(body, dialect="oracle")
    assert [r for r in _gaps(result) if r and r.startswith("DYNAMIC_SQL")]
    assert _calls(result) == []
    assert result.is_fully_parsed is False


def test_a_bare_call_is_read_through_like_any_nested_call() -> None:
    """The marker has the shape `CALL p()` always had, so catalog descent reads a captured
    callee through without knowing the call was written bare."""
    caller = "PROCEDURE nightly IS\nBEGIN\n  refresh_totals(SYSDATE);\nEND nightly;\n"
    callee = (
        "PROCEDURE refresh_totals(p_day DATE) IS\nBEGIN\n"
        "  INSERT INTO ops.totals (id) SELECT o.id FROM ops.orders o;\nEND refresh_totals;\n"
    )

    def resolve(name: str) -> Callee:
        if name.lower() == "refresh_totals":
            return Callee("k", "ops.refresh_totals", callee)
        return Callee(None, None, None, routine_call_descent.CALLEE_NOT_CAPTURED)

    result = descend_nested_calls(
        parse_procedure_lineage(caller, dialect="oracle"),
        dialect="oracle",
        resolve=resolve,
        root_key="nightly",
    )
    assert result.is_fully_parsed is True
    assert {(e.source_table, e.target_table, e.via_routine) for e in result.edges} == {
        ("ops.orders", "ops.totals", "ops.refresh_totals")
    }


def test_the_label_after_end_is_not_a_call() -> None:
    """A package that cannot be split is parsed whole, so every `END member_name` reaches the
    classifier as `END` plus a lone name -- the exact shape of `other_proc;`. Only the real
    call in it is a call."""
    # The whole-body parse glues each member's first statement to its header, which
    # is why the call is not refresh's first statement.
    truncated = (
        "PACKAGE BODY ops_pkg AS\n"
        "  PROCEDURE refresh IS\n  BEGIN\n    NULL;\n    audit_it(1);\n  END refresh;\n"
        "  PROCEDURE rebuild IS\n  BEGIN\n    NULL;\n    rebuild_all;\n  END rebuild;\n"
        "  PROCEDURE archive IS\n  BEGIN\n    INSERT INTO ops.t (id) SELECT s.id FROM ops.s s;\n"
    )
    result = parse_procedure_lineage(truncated, dialect="oracle")
    assert result.member_attribution == MemberAttribution.PACKAGE_FALLBACK.value
    # `END refresh;` and `END rebuild;` leave `refresh` and `rebuild`: neither is a call.
    assert _calls(result) == ["audit_it", "rebuild_all"]


# ---------------------------------------------------------------------------
# 2. Calls between members of one package.
# ---------------------------------------------------------------------------

MEMBER_CALLS = """PACKAGE ops_pkg AS
  PROCEDURE refresh(p_day DATE);
END ops_pkg;
PACKAGE BODY ops_pkg AS
  PROCEDURE load_orders(p_day DATE) IS
  BEGIN
    INSERT INTO ops.orders_stage (id, amount)
      SELECT o.id, o.amount FROM ops.orders o;
  END load_orders;
  PROCEDURE load_flags IS
  BEGIN
    UPDATE ops.flags f SET f.seen = f.seen + 1;
  END load_flags;
  PROCEDURE refresh(p_day DATE) IS
  BEGIN
    load_orders(p_day);
    ops_pkg.load_flags;
    DBMS_OUTPUT.PUT_LINE('done');
  END refresh;
END ops_pkg;
"""


def _member(result: ProcedureParseResult, member: str) -> list[Any]:
    return [e for e in _real(result) if e.package_member == member]


def test_a_call_to_a_sibling_member_reads_its_lineage_through_at_the_call() -> None:
    result = parse_procedure_lineage(MEMBER_CALLS, dialect="oracle")

    assert result.member_attribution == MemberAttribution.MEMBER.value
    assert result.is_fully_parsed is True
    assert _gaps(result) == []
    refresh = _member(result, "refresh")
    assert {(e.source_table, e.source_column, e.target_table, e.via_routine) for e in refresh} == {
        ("ops.orders", "id", "ops.orders_stage", "ops_pkg.load_orders"),
        ("ops.orders", "amount", "ops.orders_stage", "ops_pkg.load_orders"),
        ("ops.flags", "seen", "ops.flags", "ops_pkg.load_flags"),
    }
    assert all(e.is_write and e.member_attribution == "MEMBER" for e in refresh)
    # Read through, so never more certain than the callee's own reading.
    assert {e.confidence for e in refresh} <= {"PARTIAL", "LOW"}
    # Each is located at the call in refresh's text, not at the callee's statement --
    # and so carries no token: the call names neither end of the fact.
    for edge in refresh:
        assert edge.statement_range_status == StatementRangeStatus.CALL_SITE.value
        assert (edge.source_token_range, edge.target_token_range) == (None, None)
        where = edge.statement_range
        assert where is not None
        assert MEMBER_CALLS[where.start_offset : where.end_offset] in (
            "load_orders(p_day)",
            "ops_pkg.load_flags",
        )
    # The callee keeps its own edges, as its own.
    assert {(e.target_table, e.via_routine) for e in _member(result, "load_orders")} == {
        ("ops.orders_stage", None)
    }


OVERLOADS = """PACKAGE BODY log_pkg AS
  PROCEDURE log_event(p_id NUMBER) IS
  BEGIN
    INSERT INTO ops.event_log_short (id) SELECT s.id FROM ops.src s;
  END log_event;
  PROCEDURE log_event(p_id NUMBER, p_note VARCHAR2) IS
  BEGIN
    INSERT INTO ops.event_log_long (id) SELECT s.id FROM ops.src s;
  END log_event;
  PROCEDURE archive(p_days NUMBER DEFAULT 30, p_hard BOOLEAN := FALSE) IS
  BEGIN
    DELETE FROM ops.archive_log a WHERE a.age > p_days;
  END archive;
  PROCEDURE stamp(p_id NUMBER) IS
  BEGIN
    INSERT INTO ops.stamp_number (id) SELECT s.id FROM ops.src s;
  END stamp;
  PROCEDURE stamp(p_id DATE) IS
  BEGIN
    INSERT INTO ops.stamp_date (id) SELECT s.id FROM ops.src s;
  END stamp;
  PROCEDURE run_short IS BEGIN log_event(1); END run_short;
  PROCEDURE run_long IS BEGIN log_event(p_id => 1, p_note => NULL); END run_long;
  PROCEDURE run_mixed IS BEGIN log_event(1, p_note => NULL); END run_mixed;
  PROCEDURE run_archive IS BEGIN archive; END run_archive;
  PROCEDURE run_stamp IS BEGIN stamp(1); END run_stamp;
  PROCEDURE run_unknown IS BEGIN log_event(p_nope => 1); END run_unknown;
END log_pkg;
"""


@pytest.mark.parametrize(
    ("caller", "target", "callee"),
    [
        # By arity: only the one-parameter overload accepts one argument.
        ("run_short", "ops.event_log_short", "log_pkg.log_event"),
        # By parameter name, which is how the existing member linking tells overloads apart.
        ("run_long", "ops.event_log_long", "log_pkg.log_event"),
        ("run_mixed", "ops.event_log_long", "log_pkg.log_event"),
        # Parameters with a default -- DEFAULT or := -- may be left out.
        ("run_archive", "ops.archive_log", "log_pkg.archive"),
    ],
)
def test_an_overloaded_call_resolves_by_its_arguments(
    caller: str, target: str, callee: str
) -> None:
    result = parse_procedure_lineage(OVERLOADS, dialect="oracle")
    edges = _member(result, caller)
    assert {(e.target_table, e.via_routine) for e in edges} == {(target, callee)}
    assert [e for e in result.edges if e.package_member == caller and e.unparsed_reason] == []


@pytest.mark.parametrize("caller", ["run_stamp", "run_unknown"])
def test_a_call_no_single_member_accepts_stays_a_gap_never_a_guess(caller: str) -> None:
    """`stamp(1)`: two overloads differ only by type, which the text cannot settle.
    `log_event(p_nope => 1)`: no overload has that parameter. Either way nothing is read
    through, and the gap says why -- in routine_call_descent's own word."""
    result = parse_procedure_lineage(OVERLOADS, dialect="oracle")
    assert _member(result, caller) == []
    [marker] = [
        e for e in result.edges
        if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE and e.package_member == caller
    ]
    assert marker.unparsed_reason is not None
    assert marker.unparsed_reason.endswith(f"({MEMBER_CALL_AMBIGUOUS})")
    assert result.is_fully_parsed is False


SHADOW = """PACKAGE BODY shadow_pkg AS
  PROCEDURE log_event IS
  BEGIN
    INSERT INTO ops.package_log (id) SELECT s.id FROM ops.src s;
  END log_event;
  PROCEDURE refresh IS
    PROCEDURE log_event IS
    BEGIN
      INSERT INTO ops.local_log (id) SELECT s.id FROM ops.src s;
    END log_event;
  BEGIN
    log_event;
  END refresh;
END shadow_pkg;
"""


def test_a_local_subprogram_shadows_the_package_member_of_the_same_name() -> None:
    """PL/SQL resolves `log_event` inside refresh to refresh's own nested procedure. Its body
    is part of refresh and is walked there, so the call adds nothing and is no gap -- and the
    package's `log_event` is not what refresh writes."""
    result = parse_procedure_lineage(SHADOW, dialect="oracle")
    assert _gaps(result) == []
    assert result.is_fully_parsed is True
    assert {(e.target_table, e.via_routine) for e in _member(result, "refresh")} == {
        ("ops.local_log", None)
    }


RECURSIVE = """PACKAGE walk_pkg AS
  PROCEDURE visit(p_id NUMBER);
  PROCEDURE descend(p_id NUMBER);
END walk_pkg;
PACKAGE BODY walk_pkg AS
  PROCEDURE visit(p_id NUMBER) IS
  BEGIN
    INSERT INTO ops.visited (id) SELECT n.id FROM ops.nodes n;
    descend(p_id);
  END visit;
  PROCEDURE descend(p_id NUMBER) IS
  BEGIN
    UPDATE ops.nodes n SET n.depth = n.depth + 1;
    visit(p_id);
    descend(p_id);
  END descend;
END walk_pkg;
"""


def test_mutual_and_self_recursion_between_members_read_through_and_terminate() -> None:
    """A call back into a member already being read through adds nothing that is not
    already there, so it ends the walk rather than leaving a gap."""
    result = parse_procedure_lineage(RECURSIVE, dialect="oracle")
    assert _gaps(result) == []
    assert result.is_fully_parsed is True
    visit = {(e.target_table, e.via_routine) for e in _member(result, "visit")}
    assert visit == {("ops.visited", None), ("ops.nodes", "walk_pkg.descend")}
    descend = {(e.target_table, e.via_routine) for e in _member(result, "descend")}
    assert descend == {("ops.nodes", None), ("ops.visited", "walk_pkg.visit")}


GAP_CALLEE = """PACKAGE BODY gap_pkg AS
  PROCEDURE rebuild IS
  BEGIN
    INSERT INTO ops.rebuilt (id) SELECT s.id FROM ops.src s;
    EXECUTE IMMEDIATE 'TRUNCATE TABLE ops.scratch';
  END rebuild;
  PROCEDURE nightly IS
  BEGIN
    rebuild;
    util_log('nightly');
  END nightly;
END gap_pkg;
"""


def test_a_member_callee_with_a_gap_keeps_the_call_marker_and_what_it_did_read() -> None:
    result = parse_procedure_lineage(GAP_CALLEE, dialect="oracle")
    nightly = [e for e in result.edges if e.package_member == "nightly"]
    assert {(e.target_table, e.via_routine) for e in nightly if e.unparsed_reason is None} == {
        ("ops.rebuilt", "gap_pkg.rebuild")
    }
    assert sorted(e.unparsed_reason for e in nightly if e.unparsed_reason) == [
        f"NESTED_PROCEDURE_CALL: rebuild ({MEMBER_CALL_NOT_FULLY_PARSED})",
        # Not a member: left for catalog descent, which knows the schema's routines.
        "NESTED_PROCEDURE_CALL: util_log",
    ]


def test_catalog_descent_leaves_a_decided_member_call_alone() -> None:
    """A member routine is captured with no body of its own, so catalog descent would call
    `rebuild` BODY_WITHHELD -- a gap routed to someone who can release a body, when the body
    is right here and was read. An outcome already recorded is kept."""
    root = parse_procedure_lineage(GAP_CALLEE, dialect="oracle")

    def resolve(name: str) -> Callee:
        if name.lower() == "rebuild":
            return Callee("m", "ops.rebuild", None, routine_call_descent.CALLEE_BODY_WITHHELD)
        return Callee(None, None, None, routine_call_descent.CALLEE_NOT_CAPTURED)

    result = descend_nested_calls(root, dialect="oracle", resolve=resolve, root_key="pkg")
    assert sorted(r for r in _gaps(result) if r and r.startswith("NESTED")) == [
        f"NESTED_PROCEDURE_CALL: rebuild ({MEMBER_CALL_NOT_FULLY_PARSED})",
        f"NESTED_PROCEDURE_CALL: util_log ({routine_call_descent.CALLEE_NOT_CAPTURED})",
    ]


def test_the_member_call_codes_are_routine_call_descents_words() -> None:
    assert MEMBER_CALL_AMBIGUOUS == routine_call_descent.CALLEE_AMBIGUOUS
    assert MEMBER_CALL_NOT_FULLY_PARSED == routine_call_descent.CALLEE_NOT_FULLY_PARSED


# ---------------------------------------------------------------------------
# 3. Declaration sections.
# ---------------------------------------------------------------------------

CURSORS = """CREATE OR REPLACE PROCEDURE ops.walk_orders IS
  CURSOR c_open IS
    SELECT o.id, o.amount FROM ops.orders o WHERE o.status = 'OPEN' FOR UPDATE;
  CURSOR c_flag(p_id NUMBER) RETURN ops.flags%ROWTYPE IS
    SELECT f.id, f.seen FROM ops.flags f WHERE f.id = p_id;
  v_row ops.customers%ROWTYPE;
  v_id ops.accounts.id%TYPE;
  v_total NUMBER := 0;
  c_limit CONSTANT NUMBER := CASE WHEN 1 = 1 THEN 10 ELSE 20 END;
  e_stop EXCEPTION;
  PRAGMA EXCEPTION_INIT(e_stop, -20001);
  TYPE t_ids IS TABLE OF NUMBER INDEX BY PLS_INTEGER;
  SUBTYPE t_money IS NUMBER(12, 2);
BEGIN
  FOR r IN c_open LOOP
    v_total := v_total + r.amount;
  END LOOP;
END walk_orders;
"""


def test_a_cursor_declaration_is_read_not_dropped() -> None:
    result = parse_procedure_lineage(CURSORS, dialect="oracle")

    reads = {(e.source_table, e.source_column, e.transformation_type) for e in _real(result)}
    assert {
        ("ops.orders", "id", "DIRECT"),
        ("ops.orders", "amount", "DIRECT"),
        ("ops.orders", "status", "FILTERED"),
        ("ops.flags", "id", "DIRECT"),
        ("ops.flags", "seen", "DIRECT"),
    } <= reads
    # Rows fetched into routine-local state: never a table write, never the result.
    declared = [e for e in _real(result) if e.control_flow_context == "CURSOR_DECLARATION"]
    assert {e.target_table for e in declared} == {PROCEDURE_LOCAL_TARGET}
    assert all(not e.is_write and e.is_intermediate for e in _real(result))
    # 2026-09-19: `FOR r IN c_open LOOP` fetches that query's rows into `r`, which is
    # routine-local too (tests/test_r11_fp07_loop_records_and_result_cursors.py).
    assert {e.control_flow_context for e in _real(result)} == {
        "CURSOR_DECLARATION",
        "CURSOR_FOR_LOOP",
    }
    fetched = [e for e in _real(result) if e.control_flow_context == "CURSOR_FOR_LOOP"]
    # The loop is statement 2, after the two cursors' reads.
    assert {e.target_table for e in fetched} == {"<LOCAL:r@2>"}
    assert result.is_fully_parsed is True
    assert result.is_read_only is True


def test_an_anchored_type_is_a_structure_dependency_not_a_read() -> None:
    """`v ops.customers%ROWTYPE` takes the table's column list, not its rows: no data flows
    from it. Recording it as a read would light the routine up in every "who reads
    customers?" question it has no part in."""
    result = parse_procedure_lineage(CURSORS, dialect="oracle")
    sources = {e.source_table for e in result.edges}
    assert "ops.customers" not in sources
    assert "ops.accounts" not in sources
    # The cursor's `RETURN ops.flags%ROWTYPE` is structure too; flags is read by its query.
    flags = {e.source_column for e in result.edges if e.source_table == "ops.flags"}
    assert {"id", "seen"} <= flags


def test_a_cursor_read_is_located_at_its_declaration() -> None:
    result = parse_procedure_lineage(CURSORS, dialect="oracle")
    [edge] = [
        e for e in result.edges
        if e.source_column == "status" and e.control_flow_context == "CURSOR_DECLARATION"
    ]
    where = edge.statement_range
    assert where is not None
    assert edge.statement_range_status == StatementRangeStatus.STATEMENT.value
    assert CURSORS[where.start_offset : where.end_offset].startswith("CURSOR c_open IS")
    # And, inside it, at the column the filter reads (R11-FP07's token grain).
    token = edge.source_token_range
    assert token is not None
    assert CURSORS[token.start_offset : token.end_offset] == "o.status"


NESTED_FIRST = """CREATE OR REPLACE PROCEDURE ops.rebuild IS
  FUNCTION batch_size RETURN NUMBER IS
  BEGIN
    RETURN 100;
  END batch_size;
  PROCEDURE note IS
  BEGIN
    INSERT INTO ops.notes (id) SELECT s.id FROM ops.src s;
  END note;
BEGIN
  INSERT INTO ops.rebuilt (id) SELECT o.id FROM ops.orders o;
  note;
END rebuild;
"""


def test_a_nested_subprogram_no_longer_hides_the_routines_own_body() -> None:
    """The body used to be taken from the first BEGIN in the text -- here batch_size's -- so
    `INSERT INTO ops.rebuilt` was never seen and the routine read as fully parsed and
    writing nothing. The call to the local `note` is resolved where it is declared."""
    result = parse_procedure_lineage(NESTED_FIRST, dialect="oracle")
    assert {e.target_table for e in _real(result)} == {"ops.rebuilt", "ops.notes"}
    assert _gaps(result) == []
    assert result.is_fully_parsed is True


DECLARED_PACKAGE = """PACKAGE cfg_pkg AS
  g_limit CONSTANT NUMBER := 10;
  CURSOR c_spec RETURN ops.flags%ROWTYPE;
  CURSOR c_active IS SELECT a.id FROM ops.accounts a WHERE a.active = 1;
  PROCEDURE refresh;
END cfg_pkg;
PACKAGE BODY cfg_pkg AS
  g_count NUMBER := 0;
  TYPE t_ids IS TABLE OF NUMBER INDEX BY PLS_INTEGER;
  CURSOR c_spec RETURN ops.flags%ROWTYPE IS SELECT * FROM ops.flags f;
  PROCEDURE refresh IS
    CURSOR c_orders IS SELECT o.id FROM ops.orders o;
    v_row ops.orders%ROWTYPE;
  BEGIN
    OPEN c_orders;
    FETCH c_orders INTO v_row;
    CLOSE c_orders;
    INSERT INTO ops.refreshed (id) SELECT s.id FROM ops.src s;
  END refresh;
END cfg_pkg;
"""


def test_package_and_member_declarations_are_read_at_their_grain() -> None:
    result = parse_procedure_lineage(DECLARED_PACKAGE, dialect="oracle")

    assert result.member_attribution == MemberAttribution.MEMBER.value
    # Package-level variables, constants and types used to be PARSE_ERROR markers.
    assert _gaps(result) == []
    assert result.is_fully_parsed is True
    grains = {
        (e.source_table, e.package_member, e.member_attribution)
        for e in _real(result)
        if e.target_table == PROCEDURE_LOCAL_TARGET
    }
    assert grains == {
        ("ops.accounts", None, "PACKAGE_LEVEL"),
        ("ops.flags", None, "PACKAGE_LEVEL"),
        ("ops.orders", "refresh", "MEMBER"),
    }
    # The member's span of statements covers its declarations, so the member routine id
    # reaches the cursor's edges too.
    [refresh] = result.package_members
    cursor_edges = [e for e in result.edges if e.source_table == "ops.orders"]
    # (2026-09-19) The declaration's read, and the `FETCH c_orders INTO v_row` that lands its
    # rows in the record: two statements of the member, both read.
    assert {e.control_flow_context for e in cursor_edges} == {
        "CURSOR_DECLARATION",
        "CURSOR_FETCH",
    }
    assert refresh.first_ordinal is not None and refresh.last_ordinal is not None
    assert all(
        refresh.first_ordinal <= e.statement_ordinal <= refresh.last_ordinal for e in cursor_edges
    )


UNREADABLE = """CREATE OR REPLACE PROCEDURE ops.broken IS
  CURSOR c_bad IS SELEC o.id FROM ops.orders o;
  v_count NUMBER := (SELECT COUNT(*) FROM ops.orders);
  v_ok NUMBER;
BEGIN
  INSERT INTO ops.t (id) SELECT s.id FROM ops.s s;
END broken;
"""


def test_a_declaration_that_cannot_be_read_is_reported_never_dropped() -> None:
    result = parse_procedure_lineage(UNREADABLE, dialect="oracle")
    gaps = _gaps(result)
    assert len(gaps) == 2
    assert sorted(g.split(":")[0] for g in gaps if g) == [
        "PARSE_ERROR",  # the cursor's query
        "UNSUPPORTED_STATEMENT_SHAPE",  # a default holding a query, which PL/SQL refuses
    ]
    assert result.is_fully_parsed is False
    # What could be read still is.
    assert {e.target_table for e in _real(result)} == {"ops.t"}
    markers = [e for e in result.edges if e.transformation_type == UNPARSED_TRANSFORMATION_TYPE]
    starts = sorted(
        UNREADABLE[e.statement_range.start_offset :].split()[0]
        for e in markers
        if e.statement_range is not None
    )
    assert starts == ["CURSOR", "v_count"]


def test_an_anonymous_blocks_declare_section_is_walked() -> None:
    """What ALL_TRIGGERS.TRIGGER_BODY holds for an Oracle trigger with declarations."""
    block = (
        "DECLARE\n"
        "  CURSOR c_limits IS SELECT l.max_amount FROM ops.limits l;\n"
        "  v_max NUMBER;\n"
        "BEGIN\n"
        "  INSERT INTO ops.audit (id) SELECT s.id FROM ops.src s;\n"
        "END;\n"
    )
    result = parse_procedure_lineage(block, dialect="oracle")
    assert _gaps(result) == []
    assert {(e.source_table, e.target_table) for e in _real(result)} >= {
        ("ops.limits", PROCEDURE_LOCAL_TARGET),
        ("ops.src", "ops.audit"),
    }


# ---------------------------------------------------------------------------
# 4. Storage: a person's parse and the lineage agent record the same member ids.
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


def _captured(organization_id, datasource_id, schema_id, name: str, **values) -> MetadataRoutine:
    defaults: dict[str, Any] = {
        "id": uuid4(),
        "organization_id": organization_id,
        "datasource_id": datasource_id,
        "schema_id": schema_id,
        "name": name,
        "routine_type": "PROCEDURE",
        "availability": "UNAVAILABLE",
        "body_sql_redacted": None,
        "package_name": "OPS_PKG",
        "redaction_status": "LEXICAL",
        "screening_status": "CLEAN",
        "status": "ACTIVE",
        "fingerprint": "fp",
    }
    defaults.update(values)
    return MetadataRoutine(**defaults)


async def _member_estate(db: AsyncSession, organization_id, datasource_id, schema_id):
    package = _captured(
        organization_id, datasource_id, schema_id, "OPS_PKG",
        routine_type="PACKAGE", availability="AVAILABLE", body_sql_redacted=MEMBER_CALLS,
        package_name=None, body_fingerprint=uuid4().hex,
    )
    members = {
        name: _captured(organization_id, datasource_id, schema_id, name.upper())
        for name in ("refresh", "load_orders", "load_flags")
    }
    db.add_all([package, *members.values()])
    await db.flush()
    db.add_all(
        MetadataRoutineParameter(
            organization_id=organization_id,
            datasource_id=datasource_id,
            routine_id=members[name].id,
            name="P_DAY",
            ordinal_position=1,
            physical_type="DATE",
            fingerprint="fp",
        )
        for name in ("refresh", "load_orders")
    )
    await db.flush()
    return package, members


def _assert_member_ids(rows, members) -> None:
    by_target = {
        (row.target_table, row.via_routine): row.member_routine_id
        for row in rows
        if row.transformation_type != UNPARSED_TRANSFORMATION_TYPE
    }
    # Each member's own statements are that member's.
    assert by_target[("ops.orders_stage", None)] == members["load_orders"].id
    assert by_target[("ops.flags", None)] == members["load_flags"].id
    # A sibling's lineage read through at refresh's call is refresh's.
    assert by_target[("ops.orders_stage", "ops_pkg.load_orders")] == members["refresh"].id
    assert by_target[("ops.flags", "ops_pkg.load_flags")] == members["refresh"].id


async def test_a_persons_parse_links_read_through_edges_to_the_calling_member(session) -> None:
    datasource, schema = await _seed(session)
    datasource.dialect = "oracle"
    package, members = await _member_estate(
        session, datasource.organization_id, datasource.id, schema.id
    )

    await parse_deep_procedure_lineage_endpoint(
        datasource.id, package.id, _context(datasource), session
    )
    rows = (await session.scalars(select(DeepProcedureLineageEdge))).all()
    _assert_member_ids(rows, members)


@pytest_asyncio.fixture
async def agent_session() -> Any:
    async with task_agent_session() as active:
        yield active


async def test_the_lineage_agent_records_the_member_routine_each_edge_belongs_to(
    agent_session: AsyncSession,
) -> None:
    """The agent parsed the package the same way a person's parse does, and recorded every
    edge's member by name and grain -- but left `member_routine_id` NULL, so an agent's
    proposal about a member could not be found from that member."""
    org, datasource, schema = await seed_estate(agent_session, dialect="oracle")
    package, members = await _member_estate(agent_session, org.id, datasource.id, schema.id)
    await register_agent(agent_session, org, principal=AGENT)

    outcome = await _run(agent_session, org)

    [item] = outcome.items
    assert (item.action, item.subject_id) == (ACTION_PROPOSED, package.id)
    rows = (await agent_session.scalars(select(DeepProcedureLineageEdge))).all()
    assert rows and {row.routine_id for row in rows} == {package.id}
    assert {row.review_status for row in rows} == {"PROPOSED"}
    _assert_member_ids(rows, members)
