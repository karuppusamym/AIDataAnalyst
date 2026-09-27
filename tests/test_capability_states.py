"""The shared capability vocabulary, and the boolean rule it must not break.

`aida.capability_states` exists so that five separate enumerations and two
booleans can answer the same question in the same words (review 2026-09-16 §5).
The risk in doing that is the obvious shortcut: replacing the two booleans with
sentinel strings so that storage speaks the vocabulary directly.

`procedure_lineage_models.DeepProcedureLineageEdge.source_resolved` carries a
comment recording exactly why that is wrong -- an unresolved source is "never
inferred by string-comparing `source_table` against a cosmetic sentinel like
`\"UNRESOLVED\"`", because a real table could be called `UNRESOLVED`. The same
holds for a truncated definition: the flag says the text is partial, and putting
`"TRUNCATED"` where the text goes would destroy the partial text and collide
with a real definition.

The tests below pin both halves: the mappers produce the right states, and the
stored columns are still booleans.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Boolean

from aida.capability_states import (
    ADAPTER_STATES,
    CAPABILITY_REASON_CODES,
    CAPABILITY_STATE_VALUES,
    PRIVILEGE_SQLSTATES,
    READ_OUTCOME_STATES,
    REASON_SOURCE_DENIED_READ,
    REASON_UNRECORDED,
    CapabilityState,
    definition_read_state,
    flag_state,
    is_permission_refusal,
    parse_coverage_state,
    reason_code,
    resolution_state,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge, RoutineParseCoverage


def test_the_vocabulary_covers_every_state_review_5_asks_for() -> None:
    """Review §5 names six distinctions the old vocabularies could not make.
    All six are here, plus the three the existing surfaces already had."""
    assert set(CAPABILITY_STATE_VALUES) == {
        "SUPPORTED",
        "PARTIAL",
        "UNSUPPORTED",
        "NOT_APPLICABLE",
        "NOT_SELECTED",
        "PERMISSION_DENIED",
        "UNAVAILABLE",
        "TRUNCATED",
        "UNRESOLVED",
    }


def test_an_adapter_is_never_permission_denied_and_a_read_is_never_unsupported() -> None:
    """The architecture document's own split: installed adapter support and the
    current connection's permissions are different facts, so the vocabulary
    says which states may answer which question."""
    assert CapabilityState.PERMISSION_DENIED not in ADAPTER_STATES
    assert CapabilityState.NOT_SELECTED not in ADAPTER_STATES
    assert CapabilityState.UNSUPPORTED not in READ_OUTCOME_STATES
    assert CapabilityState.PERMISSION_DENIED in READ_OUTCOME_STATES


def test_verified_is_deliberately_not_a_state() -> None:
    """The design target lists `VERIFIED` as a sixth presentation outcome.
    Making it a state would let "this code path exists" and "this adapter was
    exercised against a live instance" collapse into one cell, which is the
    conflation review §5 names in its own sentence about dialects."""
    assert "VERIFIED" not in CAPABILITY_STATE_VALUES


# ---------------------------------------------------------------------------
# The mappers.
# ---------------------------------------------------------------------------


def test_a_flag_that_was_never_declared_reads_unsupported_not_not_applicable() -> None:
    """INV-9: an absent flag means the axis is not implemented. Deciding that an
    engine has no such concept needs engine knowledge this function does not
    have, and guessing NOT_APPLICABLE would over-claim in the one direction
    that matters."""
    assert flag_state(None) is CapabilityState.UNSUPPORTED
    assert flag_state(False) is CapabilityState.UNSUPPORTED
    assert flag_state(True) is CapabilityState.SUPPORTED


@pytest.mark.parametrize(
    ("available", "truncated", "denied", "expected"),
    [
        (True, False, False, CapabilityState.SUPPORTED),
        (True, True, False, CapabilityState.TRUNCATED),
        (False, False, False, CapabilityState.UNAVAILABLE),
        (False, True, False, CapabilityState.UNAVAILABLE),
        (True, False, True, CapabilityState.PERMISSION_DENIED),
        (True, True, True, CapabilityState.PERMISSION_DENIED),
    ],
)
def test_definition_read_state_precedence(available, truncated, denied, expected) -> None:
    """A refusal outranks everything (nothing arrived), then unavailability,
    then truncation. A truncated definition is never SUPPORTED, which is the
    whole reason the flag is kept."""
    assert (
        definition_read_state(available=available, truncated=truncated, denied=denied)
        is expected
    )


def test_resolution_state_is_the_only_way_to_report_an_unresolved_source() -> None:
    assert resolution_state(True) is CapabilityState.SUPPORTED
    assert resolution_state(False) is CapabilityState.UNRESOLVED


def test_parse_coverage_needs_completion_not_merely_statements() -> None:
    """F06.4: an object being inventoried is not every path through it being
    understood. SUPPORTED requires the parse to have completed."""
    assert (
        parse_coverage_state(parse_completed=True, statement_count=4)
        is CapabilityState.SUPPORTED
    )
    assert (
        parse_coverage_state(parse_completed=False, statement_count=4)
        is CapabilityState.PARTIAL
    )
    assert (
        parse_coverage_state(parse_completed=False, statement_count=0)
        is CapabilityState.UNAVAILABLE
    )
    # A body with no statements is not "fully parsed": nothing was read.
    assert (
        parse_coverage_state(parse_completed=True, statement_count=0)
        is CapabilityState.UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# The boolean rule.
# ---------------------------------------------------------------------------


def test_source_resolved_is_still_a_boolean_column() -> None:
    """The regression this vocabulary must not cause. If this column ever
    becomes a string, an edge whose real source table is named `UNRESOLVED`
    becomes indistinguishable from one the parser could not resolve."""
    column = DeepProcedureLineageEdge.__table__.c["source_resolved"]
    assert isinstance(column.type, Boolean)
    assert column.nullable is False


def test_parse_coverage_stores_booleans_not_states() -> None:
    """Same rule for the new table: the reporting boundary renders a state, the
    row holds the facts `ProcedureParseResult` computed."""
    table = RoutineParseCoverage.__table__
    for name in ("parse_completed", "is_read_only"):
        assert isinstance(table.c[name].type, Boolean), name
    assert "state" not in table.c, (
        "a stored state would collapse the two booleans and lose the reason "
        "either of them is false"
    )


def test_no_column_anywhere_stores_a_capability_state_string() -> None:
    """Swept over both tables this workstream touches, so a later column called
    something like `coverage_state` has to argue with a test."""
    for model in (DeepProcedureLineageEdge, RoutineParseCoverage):
        for column in model.__table__.c:
            default = getattr(column, "server_default", None)
            rendered = str(getattr(default, "arg", "")) if default is not None else ""
            assert rendered not in CAPABILITY_STATE_VALUES, (model.__name__, column.name)


# ---------------------------------------------------------------------------
# Reason codes and refusal classification.
# ---------------------------------------------------------------------------


def test_an_unknown_reason_is_dropped_rather_than_stored_as_free_text() -> None:
    """INV-6. The honest-looking implementation of a reason is to pass the
    driver's message through, and a driver's message quotes rows."""
    assert reason_code(REASON_SOURCE_DENIED_READ) == REASON_SOURCE_DENIED_READ
    assert reason_code(None) == REASON_UNRECORDED
    assert (
        reason_code("permission denied for relation customers where id = 42")
        == REASON_UNRECORDED
    )
    assert REASON_UNRECORDED in CAPABILITY_REASON_CODES


def test_a_privilege_sqlstate_is_a_refusal() -> None:
    class DriverError(Exception):
        sqlstate = "42501"

    assert is_permission_refusal(DriverError()) is True


def test_a_wrapped_privilege_sqlstate_is_still_a_refusal() -> None:
    """SQLAlchemy puts the driver's error on `.orig`, and `raise ... from` puts
    it on `__cause__`. A refusal must survive both wrappings."""

    class DriverError(Exception):
        sqlstate = "28000"

    class Wrapper(Exception):
        def __init__(self) -> None:
            super().__init__("wrapped")
            self.orig = DriverError()

    assert is_permission_refusal(Wrapper()) is True

    try:
        try:
            raise DriverError
        except DriverError as exc:
            raise RuntimeError("outer") from exc
    except RuntimeError as outer:
        assert is_permission_refusal(outer) is True


def test_a_syntax_error_is_not_read_as_a_refusal() -> None:
    """`42000` is "syntax error or access rule violation" and covers a plain
    typo, so it is deliberately not in the set: a failure reported as a denial
    would send a source administrator to grant access they already gave."""

    class DriverError(Exception):
        sqlstate = "42000"

    assert "42000" not in PRIVILEGE_SQLSTATES
    assert is_permission_refusal(DriverError()) is False
    assert is_permission_refusal(None) is False
    assert is_permission_refusal(RuntimeError("timeout")) is False


def test_a_message_that_merely_says_permission_denied_is_not_a_refusal() -> None:
    """The classification is by SQLSTATE, never by text. A driver whose message
    says one thing and whose code says nothing is recorded as UNAVAILABLE --
    the under-claiming direction INV-9 requires."""
    assert is_permission_refusal(RuntimeError("ERROR: permission denied")) is False


def test_a_cyclic_exception_chain_terminates() -> None:
    """A chain that points at itself must not hang the classifier."""
    first = RuntimeError("a")
    second = RuntimeError("b")
    first.__cause__ = second
    second.__cause__ = first
    assert is_permission_refusal(first) is False
