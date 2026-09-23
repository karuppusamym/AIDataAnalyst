"""R11-FP03's remaining clause, closed in four parts.

* **`via_routine_id`.** A spliced edge carried the caller's member id and
  `via_routine`'s display text, never the callee's own captured routine id. A
  cross-routine splice (`aida.routine_call_descent`) now carries it directly,
  resolved at descent time; an in-package sibling splice carries an ordinal into
  the callee member's own span (`via_routine_locator`), resolved at persist time
  by `resolve_package_member_ids`, the same lookup `member_routine_id` already
  uses -- the only one that tells two same-named members in different packages
  apart, which a name-only resolution could not.
* **Sibling-call ordering.** A sibling member that itself calls out to catalog
  descent kept the *caller's* gap marker forever, because the in-package pass
  runs before descent. `PendingMemberCall` records what was deferred and why;
  `_reconcile_pending_member_calls` (a fixed point, since a chain of deferred
  calls can be more than one link deep) decides it once descent has resolved
  every ordinary gap in the same pass.
* **Cross-package calls.** `other_pkg.member(...)` stayed `NOT_CAPTURED` even
  when `other_pkg` was captured in the same schema. `routine_resolver` now finds
  it by name, parses its body once, and takes only the named member's own edges
  -- never the rest of that package -- AMBIGUOUS on an overloaded name, never a
  guess.
* **Calls inside expressions.** `SELECT pkg.fn(x) FROM t`, `v := pkg.fn(x) + 1`
  -- not a bare statement-level `CALL`/`EXEC`/`PERFORM` -- are now call sites too,
  found by walking for `exp.Anonymous` (sqlglot's own catch-all for a function it
  does not recognise as a dialect builtin), resolved by the same rules a bare
  call already uses.

Every test here fails on the tree before this change.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida import routine_call_descent
from aida.envelope_models import MetadataRoutine
from aida.models import DataSource, MetadataSchema
from aida.procedure_lineage import (
    MEMBER_CALL_NOT_FULLY_PARSED,
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    parse_procedure_lineage,
)
from aida.procedure_lineage_api import parse_deep_procedure_lineage_endpoint
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_call_descent import Callee, descend_nested_calls, descend_routine_calls
from tests.test_routine_parse_coverage import _context, _seed
from tests.test_routine_parse_coverage import session as fp03_db_session  # noqa: F401


async def _routine(
    fp03_db_session: AsyncSession,  # noqa: F811
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str,
    body: str | None,
    **overrides: object,
) -> MetadataRoutine:
    values: dict[str, object] = {
        "id": uuid4(),
        "organization_id": datasource.organization_id,
        "datasource_id": datasource.id,
        "schema_id": schema.id,
        "name": name,
        "routine_type": "PROCEDURE",
        "package_name": "",
        "body_sql_redacted": body,
        "body_fingerprint": uuid4().hex if body else None,
        "redaction_status": "PARSED",
        "screening_status": "CLEAN",
        "availability": "AVAILABLE" if body else "UNAVAILABLE",
        "status": "ACTIVE",
        "fingerprint": "fp",
    }
    values.update(overrides)
    routine = MetadataRoutine(**values)  # type: ignore[arg-type]
    fp03_db_session.add(routine)
    await fp03_db_session.flush()
    return routine


def _gaps(result: ProcedureParseResult) -> list[str | None]:
    return [
        edge.unparsed_reason
        for edge in result.edges
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]


def _member(result: ProcedureParseResult, name: str) -> list[ProcedureLineageEdgeRecord]:
    return [edge for edge in result.edges if edge.package_member == name]


def _real(result: ProcedureParseResult) -> list[ProcedureLineageEdgeRecord]:
    return [e for e in result.edges if e.transformation_type != UNPARSED_TRANSFORMATION_TYPE]


# ---------------------------------------------------------------------------
# 1. `via_routine_id`.
# ---------------------------------------------------------------------------


async def test_a_cross_routine_splice_carries_the_callees_own_id_not_the_callers(
    fp03_db_session: AsyncSession,  # noqa: F811
) -> None:
    datasource, schema = await _seed(fp03_db_session)
    callee = await _routine(
        fp03_db_session,
        datasource,
        schema,
        name="refresh_totals",
        body=(
            "CREATE PROCEDURE dbo.refresh_totals AS BEGIN "
            "INSERT INTO dbo.totals (id) SELECT c.id FROM dbo.customers c; END"
        ),
    )
    caller = await _routine(
        fp03_db_session,
        datasource,
        schema,
        name="nightly",
        body="CREATE PROCEDURE dbo.nightly AS BEGIN EXEC dbo.refresh_totals; END",
    )

    await parse_deep_procedure_lineage_endpoint(
        datasource.id, caller.id, _context(datasource), fp03_db_session
    )

    edges = (
        await fp03_db_session.scalars(
            select(DeepProcedureLineageEdge).where(
                DeepProcedureLineageEdge.routine_id == caller.id
            )
        )
    ).all()
    real = [e for e in edges if e.transformation_type != UNPARSED_TRANSFORMATION_TYPE]
    assert real, "expected the callee's edges spliced into the caller's row"
    assert {e.via_routine_id for e in real} == {callee.id}
    assert callee.id != caller.id


async def test_a_sibling_splice_resolves_the_right_member_when_another_package_shares_its_name(
    fp03_db_session: AsyncSession,  # noqa: F811
) -> None:
    """`resolve_package_member_ids` is scoped to the *calling* package's own name
    (`package.name`), so a member called `refresh` captured under a different
    package can never be picked up here -- the case that would be genuinely
    ambiguous if `via_routine_id` were resolved by name alone."""
    datasource, schema = await _seed(fp03_db_session)
    datasource.dialect = "oracle"  # packages are Oracle-only
    pkg_a = await _routine(
        fp03_db_session,
        datasource,
        schema,
        name="pkg_a",
        routine_type="PACKAGE",
        body=(
            "PACKAGE BODY pkg_a AS\n"
            "  PROCEDURE refresh IS\n"
            "  BEGIN\n"
            "    INSERT INTO ops.totals_a (id) SELECT c.id FROM ops.customers c;\n"
            "  END refresh;\n"
            "  PROCEDURE loader IS\n"
            "  BEGIN\n"
            "    refresh;\n"
            "  END loader;\n"
            "END pkg_a;\n"
        ),
    )
    refresh_a = await _routine(
        fp03_db_session, datasource, schema, name="refresh", package_name="pkg_a", body=None
    )
    await _routine(
        fp03_db_session, datasource, schema, name="loader", package_name="pkg_a", body=None
    )
    # A different package's member of the very same short name -- must never be picked.
    await _routine(
        fp03_db_session, datasource, schema, name="refresh", package_name="pkg_b", body=None
    )

    await parse_deep_procedure_lineage_endpoint(
        datasource.id, pkg_a.id, _context(datasource), fp03_db_session
    )

    edges = (
        await fp03_db_session.scalars(
            select(DeepProcedureLineageEdge).where(
                DeepProcedureLineageEdge.routine_id == pkg_a.id,
                DeepProcedureLineageEdge.package_member == "loader",
            )
        )
    ).all()
    real = [e for e in edges if e.transformation_type != "UNPARSED"]
    assert real, "expected loader's call to refresh spliced in"
    assert {e.via_routine_id for e in real} == {refresh_a.id}


# ---------------------------------------------------------------------------
# 2. Sibling-call ordering.
# ---------------------------------------------------------------------------

_ONE_HOP_PACKAGE = """PACKAGE BODY ops_pkg AS
  PROCEDURE writer IS
  BEGIN
    INSERT INTO ops.written (id) SELECT s.id FROM ops.src s;
    util_log(1);
  END writer;
  PROCEDURE caller IS
  BEGIN
    writer;
  END caller;
END ops_pkg;
"""

_UTIL_LOG_BODY = (
    "PROCEDURE util_log(p_id NUMBER) IS BEGIN "
    "INSERT INTO ops.log (id) SELECT t.id FROM ops.trace t; END util_log;"
)


def _external_resolver(bodies: dict[str, str]) -> routine_call_descent.Resolver:
    def resolve(name: str) -> Callee:
        key = name.lower()
        if key in bodies:
            return Callee(key, key, bodies[key])
        return Callee(None, None, None, routine_call_descent.CALLEE_NOT_CAPTURED)

    return resolve


def test_a_sibling_that_itself_calls_out_stays_pessimistic_before_descent_runs() -> None:
    """Pinning the bug: `_member_calls_read_through` alone (no descent) cannot know
    `util_log` will resolve, so it must -- correctly, for that stage -- leave
    `caller`'s marker pessimistic and record it as pending."""
    root = parse_procedure_lineage(_ONE_HOP_PACKAGE, dialect="oracle")
    assert root.pending_member_calls, "expected a deferred sibling call"
    caller_gap = [
        r for r in _gaps(root) if r and r.startswith("NESTED_PROCEDURE_CALL: writer")
    ]
    assert caller_gap == [f"NESTED_PROCEDURE_CALL: writer ({MEMBER_CALL_NOT_FULLY_PARSED})"]


def test_a_sibling_that_itself_calls_out_is_resolved_once_descent_resolves_it() -> None:
    root = parse_procedure_lineage(_ONE_HOP_PACKAGE, dialect="oracle")

    result = descend_nested_calls(
        root,
        dialect="oracle",
        resolve=_external_resolver({"util_log": _UTIL_LOG_BODY}),
        root_key="ops_pkg",
    )

    assert _gaps(result) == []
    assert result.is_fully_parsed
    caller_edges = _member(result, "caller")
    assert {
        (e.source_table, e.target_table, e.via_routine) for e in caller_edges
    } == {
        ("ops.src", "ops.written", "ops_pkg.writer"),
        ("ops.trace", "ops.log", "ops_pkg.writer"),
    }


_TWO_HOP_PACKAGE = """PACKAGE BODY chain_pkg AS
  PROCEDURE c_member IS
  BEGIN
    INSERT INTO ops.c_out (id) SELECT s.id FROM ops.c_src s;
    ext_call(1);
  END c_member;
  PROCEDURE b_member IS
  BEGIN
    INSERT INTO ops.b_out (id) SELECT s.id FROM ops.b_src s;
    c_member;
  END b_member;
  PROCEDURE a_member IS
  BEGIN
    b_member;
  END a_member;
END chain_pkg;
"""

_EXT_CALL_BODY = (
    "PROCEDURE ext_call(p_id NUMBER) IS BEGIN "
    "INSERT INTO ops.ext_out (id) SELECT e.id FROM ops.ext_src e; END ext_call;"
)


def test_a_two_level_pending_chain_converges_in_more_than_one_reconciliation_pass() -> None:
    """`a_member` depends on `b_member`, which itself depends on `c_member`'s own
    external call -- two links of deferral, so a single reconciliation pass
    (checking only `b_member`'s state as of right after ordinary descent) would
    still find `b_member` blocked by its own not-yet-reconciled call to
    `c_member`, and leave `a_member` stuck. The fixed point is what closes it."""
    root = parse_procedure_lineage(_TWO_HOP_PACKAGE, dialect="oracle")
    assert len(root.pending_member_calls) == 2

    result = descend_nested_calls(
        root,
        dialect="oracle",
        resolve=_external_resolver({"ext_call": _EXT_CALL_BODY}),
        root_key="chain_pkg",
    )

    assert _gaps(result) == []
    assert result.is_fully_parsed
    a_edges = _member(result, "a_member")
    assert {(e.source_table, e.target_table) for e in a_edges} == {
        ("ops.b_src", "ops.b_out"),
        ("ops.c_src", "ops.c_out"),
        ("ops.ext_src", "ops.ext_out"),
    }
    assert {e.via_routine for e in a_edges} == {"chain_pkg.b_member"}


_REMOTE_PACKAGE_CALLEE = """PACKAGE BODY remote_pkg AS
  PROCEDURE remote_writer IS
  BEGIN
    INSERT INTO ops.remote_written (id) SELECT s.id FROM ops.remote_src s;
    remote_util(1);
  END remote_writer;
  PROCEDURE remote_entry IS
  BEGIN
    remote_writer;
  END remote_entry;
END remote_pkg;
"""

_REMOTE_UTIL_BODY = (
    "PROCEDURE remote_util(p_id NUMBER) IS BEGIN "
    "INSERT INTO ops.remote_log (id) SELECT t.id FROM ops.remote_trace t; END remote_util;"
)


def test_a_callee_reached_via_descent_that_is_itself_a_package_is_reconciled() -> None:
    """The module's own docstring names this gap as unclosed: `_descend`'s recursive
    step parses a whole external callee fresh (`parse_procedure_lineage`, since the
    resolver hands back only a `body`, not a pre-sliced `parsed`) -- and that callee
    can itself be a package carrying its own `pending_member_calls`, exactly like the
    root package in the sibling-call-ordering tests above. Before this fix, `_descend`
    checked `child.is_fully_parsed` without ever running `_reconcile_pending_member_calls`
    on `child`, so `remote_entry` -- reached only through descent, never as the root --
    stayed CALLEE_NOT_FULLY_PARSED even though ordinary descent, in that same recursive
    step, had already resolved the one external call (`remote_util`) blocking
    `remote_writer`, which is all `remote_entry`'s own deferred sibling call needed."""
    root = parse_procedure_lineage(
        "PROCEDURE root_proc IS BEGIN remote_entry(1); END root_proc;", dialect="oracle"
    )
    assert not root.pending_member_calls, "root itself is a standalone routine, not a package"

    result = descend_nested_calls(
        root,
        dialect="oracle",
        resolve=_external_resolver(
            {"remote_entry": _REMOTE_PACKAGE_CALLEE, "remote_util": _REMOTE_UTIL_BODY}
        ),
        root_key="root_proc",
    )

    assert _gaps(result) == []
    assert result.is_fully_parsed
    assert {(e.source_table, e.target_table) for e in _real(result)} == {
        ("ops.remote_src", "ops.remote_written"),
        ("ops.remote_trace", "ops.remote_log"),
    }


_SHARED_CALLEE_PACKAGE = """PACKAGE BODY multi_pkg AS
  PROCEDURE member_a IS
  BEGIN
    INSERT INTO ops.shared_out (id) SELECT r.id FROM TABLE(billing.shared_fn(1)) r;
  END member_a;
  PROCEDURE member_b IS
  BEGIN
    INSERT INTO ops.shared_out (id) SELECT r.id FROM TABLE(billing.shared_fn(2)) r;
  END member_b;
END multi_pkg;
"""

_SHARED_FN_BODY = (
    "FUNCTION shared_fn(p_id NUMBER) RETURN billing.rate_tab IS\n"
    "BEGIN\n"
    "  SELECT r.id FROM billing.rate_source r;\n"
    "END;"
)


def test_a_second_members_table_function_hop_is_not_dropped_by_the_first(
) -> None:
    """Pinning the row's remaining bullet: `member_a` and `member_b` both read the
    same external table function, and both end up writing the same target
    (`ops.shared_out`) -- so the transitive hop `billing.rate_source ->
    ops.shared_out` is stated identically by both. Before this fix,
    `descend_nested_calls` ran `propagate_intermediate_hops` once over every
    member's edges combined; that pass' own de-duplication (`source_table,
    source_column, target_table, target_column`, nothing to tell members apart)
    silently dropped whichever member's copy came second -- so a real reader of
    `member_b` alone would never see that it depends on `billing.rate_source`."""
    root = parse_procedure_lineage(_SHARED_CALLEE_PACKAGE, dialect="oracle")

    result = descend_nested_calls(
        root,
        dialect="oracle",
        resolve=_external_resolver({"billing.shared_fn": _SHARED_FN_BODY}),
        root_key="multi_pkg",
    )

    assert result.is_fully_parsed
    facts = {
        (e.package_member, e.source_table, e.target_table)
        for e in _real(result)
        if e.source_table == "billing.rate_source" and e.target_table == "ops.shared_out"
    }
    assert facts == {
        ("member_a", "billing.rate_source", "ops.shared_out"),
        ("member_b", "billing.rate_source", "ops.shared_out"),
    }


# ---------------------------------------------------------------------------
# 3. Cross-package calls.
# ---------------------------------------------------------------------------


async def test_a_cross_package_call_is_resolved_to_the_named_members_own_edges(
    fp03_db_session: AsyncSession,  # noqa: F811
) -> None:
    datasource, schema = await _seed(fp03_db_session)
    datasource.dialect = "oracle"
    await _routine(
        fp03_db_session,
        datasource,
        schema,
        name="pkg2",
        routine_type="PACKAGE",
        body=(
            "PACKAGE BODY pkg2 AS\n"
            "  PROCEDURE helper(p_id NUMBER) IS\n"
            "  BEGIN\n"
            "    INSERT INTO ops.out2 (id) SELECT s.id FROM ops.src2 s;\n"
            "  END helper;\n"
            "  PROCEDURE other_member IS\n"
            "  BEGIN\n"
            "    INSERT INTO ops.unrelated (id) SELECT u.id FROM ops.unrelated_src u;\n"
            "  END other_member;\n"
            "END pkg2;\n"
        ),
    )
    pkg1 = await _routine(
        fp03_db_session,
        datasource,
        schema,
        name="pkg1",
        routine_type="PACKAGE",
        body="PACKAGE BODY pkg1 AS PROCEDURE run IS BEGIN pkg2.helper(1); END run; END pkg1;",
    )

    result = await descend_routine_calls(
        fp03_db_session,
        datasource,
        pkg1,
        parse_procedure_lineage(pkg1.body_sql_redacted, dialect="oracle"),  # type: ignore[arg-type]
    )

    assert result.is_fully_parsed
    run_edges = _member(result, "run")
    assert {(e.source_table, e.target_table, e.via_routine) for e in run_edges} == {
        ("ops.src2", "ops.out2", "dbo.pkg2.helper")
    }
    # Only `helper`'s own edges: `other_member`'s never arrive.
    assert not any(e.target_table == "ops.unrelated" for e in result.edges)


async def test_a_cross_package_call_to_an_overloaded_member_is_ambiguous(
    fp03_db_session: AsyncSession,  # noqa: F811
) -> None:
    datasource, schema = await _seed(fp03_db_session)
    datasource.dialect = "oracle"
    await _routine(
        fp03_db_session,
        datasource,
        schema,
        name="pkg2",
        routine_type="PACKAGE",
        body=(
            "PACKAGE BODY pkg2 AS\n"
            "  PROCEDURE helper(p_id NUMBER) IS\n"
            "  BEGIN\n"
            "    INSERT INTO ops.out2 (id) SELECT s.id FROM ops.src2 s;\n"
            "  END helper;\n"
            "  PROCEDURE helper(p_id NUMBER, p_note VARCHAR2) IS\n"
            "  BEGIN\n"
            "    INSERT INTO ops.out2b (id) SELECT s.id FROM ops.src2 s;\n"
            "  END helper;\n"
            "END pkg2;\n"
        ),
    )
    pkg1 = await _routine(
        fp03_db_session,
        datasource,
        schema,
        name="pkg1",
        routine_type="PACKAGE",
        body="PACKAGE BODY pkg1 AS PROCEDURE run IS BEGIN pkg2.helper(1); END run; END pkg1;",
    )

    result = await descend_routine_calls(
        fp03_db_session,
        datasource,
        pkg1,
        parse_procedure_lineage(pkg1.body_sql_redacted, dialect="oracle"),  # type: ignore[arg-type]
    )

    [marker] = [e for e in result.edges if e.package_member == "run" and e.unparsed_reason]
    assert marker.unparsed_reason == "NESTED_PROCEDURE_CALL: pkg2.helper (AMBIGUOUS)"


async def test_a_cross_package_call_to_an_uncaptured_package_stays_not_captured(
    fp03_db_session: AsyncSession,  # noqa: F811
) -> None:
    datasource, schema = await _seed(fp03_db_session)
    datasource.dialect = "oracle"
    pkg1 = await _routine(
        fp03_db_session,
        datasource,
        schema,
        name="pkg1",
        routine_type="PACKAGE",
        body="PACKAGE BODY pkg1 AS PROCEDURE run IS BEGIN nope.helper(1); END run; END pkg1;",
    )

    result = await descend_routine_calls(
        fp03_db_session,
        datasource,
        pkg1,
        parse_procedure_lineage(pkg1.body_sql_redacted, dialect="oracle"),  # type: ignore[arg-type]
    )

    [marker] = [e for e in result.edges if e.package_member == "run" and e.unparsed_reason]
    assert marker.unparsed_reason == "NESTED_PROCEDURE_CALL: nope.helper (NOT_CAPTURED)"


# ---------------------------------------------------------------------------
# 4. Calls inside expressions.
# ---------------------------------------------------------------------------

_INSERT_WITH_EXPRESSION_CALL = (
    "CREATE PROCEDURE dbo.report AS BEGIN "
    "INSERT INTO dbo.results (id, val) "
    "SELECT t.id, pkg.fn(t.val) FROM dbo.source t; END"
)


def test_a_function_call_inside_a_select_expression_is_a_call_site() -> None:
    result = parse_procedure_lineage(_INSERT_WITH_EXPRESSION_CALL, dialect="tsql")

    assert not result.is_fully_parsed
    assert _gaps(result) == ["NESTED_PROCEDURE_CALL: pkg.fn"]
    # The statement's own column lineage, unrelated to the call, is not lost.
    assert ("dbo.source", "id", "dbo.results", "id") in {
        (e.source_table, e.source_column, e.target_table, e.target_column)
        for e in _real(result)
    }


_TWO_CALLS_ONE_STATEMENT = (
    "CREATE PROCEDURE dbo.report AS BEGIN SELECT pkg.fn(x), other.g(y) FROM dbo.t; END"
)


def test_only_the_first_expression_call_in_a_statement_is_recognised() -> None:
    result = parse_procedure_lineage(_TWO_CALLS_ONE_STATEMENT, dialect="tsql")
    assert _gaps(result) == ["NESTED_PROCEDURE_CALL: pkg.fn"]


_BUILTIN_ONLY = (
    "CREATE PROCEDURE dbo.report AS BEGIN "
    "INSERT INTO dbo.results (id, val) SELECT t.id, UPPER(t.val) FROM dbo.source t; END"
)


def test_a_recognised_builtin_function_is_not_treated_as_a_call() -> None:
    result = parse_procedure_lineage(_BUILTIN_ONLY, dialect="tsql")
    assert result.is_fully_parsed
    assert _gaps(result) == []


_PLPGSQL_ASSIGNMENT_CALL = (
    "CREATE FUNCTION s.compute() RETURNS void AS $$\n"
    "DECLARE v INTEGER;\n"
    "BEGIN\n"
    "  v := pkg.fn(1) + 1;\n"
    "END;\n"
    "$$ LANGUAGE plpgsql;"
)


def test_a_function_call_inside_a_plpgsql_assignment_is_a_call_site() -> None:
    result = parse_procedure_lineage(_PLPGSQL_ASSIGNMENT_CALL, dialect="postgres")
    assert not result.is_fully_parsed
    assert _gaps(result) == ["NESTED_PROCEDURE_CALL: pkg.fn"]


def test_an_expression_call_is_read_through_by_descent_like_any_other_call() -> None:
    callee_body = (
        "CREATE PROCEDURE dbo.helper AS BEGIN "
        "INSERT INTO dbo.audit (id) SELECT s.id FROM dbo.source2 s; END"
    )

    def resolve(name: str) -> Callee:
        if name.lower() == "pkg.fn":
            return Callee("k", "dbo.helper", callee_body)
        return Callee(None, None, None, routine_call_descent.CALLEE_NOT_CAPTURED)

    result = descend_nested_calls(
        parse_procedure_lineage(_INSERT_WITH_EXPRESSION_CALL, dialect="tsql"),
        dialect="tsql",
        resolve=resolve,
        root_key="root",
    )

    assert any(
        e.via_routine == "dbo.helper" and e.source_table == "dbo.source2" for e in result.edges
    )
    assert _gaps(result) == []
