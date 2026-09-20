"""R11-FP07 remainder: Oracle's `TABLE(...)` wrapper was read as the source itself.

`FROM TABLE(pkg.fn(x))` is Oracle unnesting a collection into rows. sqlglot parses it as a
table whose function is the wrapper, so the parser named the source `TABLE`: an edge from a
table nothing has, and a `TABLE_FUNCTION_READ` gap naming a routine no source can resolve --
so call descent could never read the function through, and the routine stayed not fully
parsed forever. It is the highest-value item that row lists, because it is the one that
writes a *wrong* edge rather than a thin one.

What the wrapper unnests is the source: a function's result, named after the function exactly
as `FROM pkg.fn(x)` is, or a collection the routine declared and filled, which names no
function and no table. The older `THE(...)` wrapper reads the same way, and a
`CAST(... AS a_collection_type)` in between says what type the rows are, not where they come
from.

Every test fails on the tree before this change except those marked *guard*.
"""

from __future__ import annotations

import pytest

from aida import routine_call_descent
from aida.procedure_lineage import (
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureParseResult,
    parse_procedure_lineage,
)
from aida.routine_call_descent import Callee, descend_nested_calls

FUNCTION = "billing.pkg_rates.rated_rows"
TARGET = "reporting.sample_totals"


def _body(source: str, *, declare: str = "") -> str:
    return (
        f"PROCEDURE load_sample IS {declare}\n"  # noqa: S608 -- a body to parse, never run
        "BEGIN\n"
        f"  INSERT INTO {TARGET} (customer_id, total)\n"
        f"  SELECT v.customer_id, v.total FROM {source} v;\n"
        "END;"
    )


def _gaps(result: ProcedureParseResult) -> list[str]:
    return sorted(
        edge.unparsed_reason or ""
        for edge in result.edges
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    )


def _sources(result: ProcedureParseResult) -> set[str]:
    return {
        edge.source_table
        for edge in result.edges
        if edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE
    }


def _resolver(bodies: dict[str, str]) -> routine_call_descent.Resolver:
    def resolve(name: str) -> Callee:
        key = name.lower()
        if key in bodies:
            return Callee(key, key, bodies[key])
        return Callee(None, None, None, routine_call_descent.CALLEE_NOT_CAPTURED)

    return resolve


@pytest.mark.parametrize(
    ("source", "label"),
    [
        (f"TABLE({FUNCTION}(42))", "the wrapper"),
        (f"TABLE(CAST({FUNCTION}(42) AS billing.rate_tab))", "a cast in between"),
        (f"THE({FUNCTION}(42))", "the older wrapper"),
    ],
)
def test_the_source_is_the_function_the_wrapper_unnests(source: str, label: str) -> None:
    result = parse_procedure_lineage(_body(source), dialect="oracle")

    assert _sources(result) == {FUNCTION}, label
    assert f"TABLE_FUNCTION_READ: {FUNCTION}" in _gaps(result)
    assert "TABLE" not in _sources(result)
    assert not any(gap.endswith(": TABLE") or gap.endswith(": THE") for gap in _gaps(result))


def test_the_gap_names_what_the_call_gap_names_so_descent_can_read_it_through() -> None:
    """The payoff: both gaps from one statement now name the same routine, so a captured
    function resolves and the routine can read as fully parsed. Named `TABLE`, it never could."""
    callee = (
        "FUNCTION rated_rows(p_id NUMBER) RETURN billing.rate_tab IS\n"
        "BEGIN\n"
        "  SELECT r.customer_id, r.total FROM billing.rate_source r;\n"
        "END;"
    )

    parsed = parse_procedure_lineage(_body(f"TABLE({FUNCTION}(42))"), dialect="oracle")
    result = descend_nested_calls(
        parsed, dialect="oracle", resolve=_resolver({FUNCTION: callee}), root_key="root"
    )

    assert {gap for gap in _gaps(parsed)} == {
        f"NESTED_PROCEDURE_CALL: {FUNCTION}",
        f"TABLE_FUNCTION_READ: {FUNCTION}",
    }
    assert _gaps(result) == []
    assert result.is_fully_parsed
    assert "billing.rate_source" in _sources(result)


def test_a_collection_the_routine_declared_names_no_function_and_no_table() -> None:
    """`TABLE(l_rows)` unnests what the routine filled. There is no function to descend into
    and no table to read, so the read is unresolved -- not a routine named `TABLE` that no
    descent can ever resolve, which is what kept such a routine not fully parsed."""
    result = parse_procedure_lineage(
        _body("TABLE(l_rows)", declare="l_rows billing.rate_tab;"), dialect="oracle"
    )

    assert _gaps(result) == []
    assert "TABLE" not in _sources(result)
    assert _sources(result) == {"UNRESOLVED"}


def test_guard_a_function_called_without_the_wrapper_is_unchanged() -> None:
    result = parse_procedure_lineage(_body(f"{FUNCTION}(42)"), dialect="oracle")

    assert _sources(result) == {FUNCTION}
    assert f"TABLE_FUNCTION_READ: {FUNCTION}" in _gaps(result)


def test_guard_a_plain_table_is_still_a_table() -> None:
    result = parse_procedure_lineage(_body("billing.rate_source"), dialect="oracle")

    assert _sources(result) == {"billing.rate_source"}
    assert _gaps(result) == []
