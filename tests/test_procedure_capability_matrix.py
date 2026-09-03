"""AT-22 exit-condition tests: the capability matrix is derived from the
parsers' own code, not hand-typed, and stays in sync with a real change to
either parser's dispatcher.
"""

from __future__ import annotations

from aida import procedure_lineage
from aida.procedure_capability_matrix import (
    _dispatched_node_types,
    _explicitly_unparsed_node_types,
    build_capability_matrix,
    render_markdown,
)


def test_matrix_is_deterministic_given_the_installed_code() -> None:
    first = build_capability_matrix()
    second = build_capability_matrix()
    assert first.dialects == second.dialects
    assert first.constructs == second.constructs
    assert first.unparsed_reasons == second.unparsed_reasons


def test_dialects_come_from_the_shared_dialect_map_not_a_hand_typed_list() -> None:
    from aida.sql_lineage_parser import _SQLGLOT_DIALECT_MAP

    matrix = build_capability_matrix()
    assert set(matrix.dialects) == set(_SQLGLOT_DIALECT_MAP)


def test_insert_is_supported_by_both_parsers() -> None:
    matrix = build_capability_matrix()
    row = next(r for r in matrix.constructs if r.construct == "INSERT ... SELECT")
    assert row.view_parser_status == "SUPPORTED"
    assert row.procedure_parser_status == "SUPPORTED"


def test_update_is_procedure_only_not_a_view_shape() -> None:
    matrix = build_capability_matrix()
    row = next(r for r in matrix.constructs if "UPDATE" in r.construct)
    assert row.view_parser_status == "N/A"
    assert row.procedure_parser_status == "SUPPORTED"


def test_dynamic_sql_is_explicitly_unparsed_never_supported_or_silently_dropped() -> None:
    matrix = build_capability_matrix()
    row = next(r for r in matrix.constructs if "dynamic SQL" in r.construct)
    assert row.procedure_parser_status == "EXPLICIT_UNPARSED"


def test_no_construct_is_marked_supported_and_unparsed_at_once() -> None:
    matrix = build_capability_matrix()
    valid_statuses = {
        "SUPPORTED", "EXPLICIT_UNPARSED", "RECOGNISED_NO_LINEAGE", "UNSUPPORTED", "N/A",
    }
    for row in matrix.constructs:
        assert row.procedure_parser_status in valid_statuses, row
        assert row.view_parser_status in valid_statuses, row


def test_unparsed_reasons_come_from_the_real_enum() -> None:
    matrix = build_capability_matrix()
    assert set(matrix.unparsed_reasons) == {r.value for r in procedure_lineage.UnparsedReason}


def test_markdown_renders_every_construct_row() -> None:
    matrix = build_capability_matrix()
    markdown = render_markdown(matrix)
    for row in matrix.constructs:
        assert row.construct in markdown
    for dialect in matrix.dialects:
        assert dialect in markdown


# ---------------------------------------------------------------------------
# The introspection primitives themselves, proven against small synthetic
# functions -- so a change to sqlglot's isinstance-check idiom, or to the
# _unparsed_statement helper's name, breaks a test here rather than silently
# producing a matrix that quietly stopped reflecting the real code.
# ---------------------------------------------------------------------------


def test_dispatched_node_types_reads_isinstance_checks_from_source() -> None:
    from sqlglot import exp

    def fake_dispatcher(node: object) -> str:
        if isinstance(node, exp.Insert):
            return "insert"
        if isinstance(node, exp.Select | exp.Union):
            return "select-or-union"
        return "other"

    assert _dispatched_node_types(fake_dispatcher) == {"Insert", "Select", "Union"}


def test_explicitly_unparsed_node_types_requires_the_unparsed_call_in_that_branch() -> None:
    from sqlglot import exp

    def _unparsed_statement(*args: object, **kwargs: object) -> str:
        return "unparsed"

    def fake_dispatcher(node: object) -> str:
        if isinstance(node, exp.Insert):
            return "insert"  # supported, no degrade call
        if isinstance(node, exp.Execute):
            return _unparsed_statement()  # explicitly degraded
        return "other"

    assert _explicitly_unparsed_node_types(fake_dispatcher) == {"Execute"}


def test_a_real_change_to_the_dispatcher_changes_the_matrix() -> None:
    """Pins the "derived, not hand-typed" claim itself: adding a new,
    genuinely-supported isinstance branch to a throwaway dispatcher changes
    what `_dispatched_node_types` reports, with nothing else touched."""
    from sqlglot import exp

    def before(node: object) -> str:
        if isinstance(node, exp.Insert):
            return "insert"
        return "other"

    def after(node: object) -> str:
        if isinstance(node, exp.Insert):
            return "insert"
        if isinstance(node, exp.Delete):
            return "delete"
        return "other"

    assert _dispatched_node_types(before) == {"Insert"}
    assert _dispatched_node_types(after) == {"Insert", "Delete"}
