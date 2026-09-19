"""Review 2026-09-16 §5: the engine capability matrix, and the certification gate.

Two things are proven here.

**The matrix is derived, not authored.** Every state is read out of live code at
build time, so a connector that gains or loses a capability changes the matrix
rather than leaving a stale claim in a document. The tests below drive that by
changing the code the matrix reads and asserting the matrix moves with it.

**`test_capability_matrix_matches_certification`.** ADR-0006 and
`Docs/40-engineering/04-testing-strategy.md` have listed this test as planned
since 2026-08-30, as INV-9's enforcement at the surface a customer actually
reads. It is deliberately **asymmetric**, in the spirit of
`tests/test_capability_register_claims.py`: a cell claiming `SUPPORTED` or
`PARTIAL` that the code cannot back is a contradiction the code can prove, so
it fails. An `UNSUPPORTED` or `NOT_APPLICABLE` cell is *not* checked against the
code, because "not wired" is routinely the honest answer and failing those would
punish the honest rows and teach people to claim support.

It does not duplicate `tests/test_inv9_capability_honesty.py`'s strict xfail.
That one records a different, still-open gap -- that the capability *flags* are
hand-declared rather than derived from a certification run. This gate takes the
flags as given and proves the published matrix never says more than they do.
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from pathlib import Path

import pytest

from aida import (
    multi_table_blueprint,
    procedure_tool_blueprint,
    sql_lineage_parser,
    view_tool_blueprint,
)
from aida.capability_states import (
    ADAPTER_STATES,
    CAPABILITY_REASON_CODES,
    CapabilityState,
)
from aida.connectors.base import ConnectorCapabilities
from aida.connectors.registry import connector_registry
from aida.discovery_selection import kind_capabilities
from aida.engine_capability_matrix import (
    _ADAPTER_CLASSES,
    FACET_CANDIDATE,
    FACET_DEFINITION,
    FACET_EXECUTION,
    FACET_INVENTORY,
    FACET_PARSING,
    FACET_PROFILE,
    FACETS,
    build_engine_capability_matrix,
    render_markdown,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = REPO_ROOT / "tests"
PUBLISHED_MARKDOWN = REPO_ROOT / "Docs" / "90-reference" / "engine-capability-matrix.md"

#: The two states that are a claim. Everything else is a declared absence, and
#: this file never holds an absence to the standard it holds a claim.
CLAIMS = frozenset({CapabilityState.SUPPORTED.value, CapabilityState.PARTIAL.value})


@pytest.fixture(scope="module")
def matrix():
    return build_engine_capability_matrix(tests_root=TESTS_ROOT)


def _engine(matrix, name: str):
    return next(row for row in matrix.engines if row.engine == name)


def _row(matrix, engine: str, kind: str):
    return next(
        row
        for row in matrix.rows
        if row.engine == engine and row.native_object_kind == kind
    )


# ---------------------------------------------------------------------------
# Shape and self-consistency.
# ---------------------------------------------------------------------------


def test_the_matrix_is_populated(matrix) -> None:
    """Tripwire: every test here iterates the matrix, so an empty one would
    turn the whole file into a no-op that reports green."""
    assert len(matrix.engines) >= 8, "six implemented adapters plus teradata and db2"
    assert len(matrix.rows) >= 60
    assert matrix.facets == FACETS
    assert len(matrix.facets) == 6


def test_it_covers_the_six_facets_review_5_names(matrix) -> None:
    assert set(matrix.facets) == {
        FACET_INVENTORY,
        FACET_DEFINITION,
        FACET_PARSING,
        FACET_PROFILE,
        FACET_CANDIDATE,
        FACET_EXECUTION,
    }
    for row in matrix.rows:
        assert [cell.facet for cell in row.cells] == list(FACETS), row


def test_every_state_and_reason_is_from_the_shared_vocabulary(matrix) -> None:
    """No cell invents a word. The nine states and the closed reason vocabulary
    are the whole answer space, which is what makes the published page
    comparable across engines."""
    valid_states = {state.value for state in CapabilityState}
    for row in matrix.rows:
        for cell in row.cells:
            assert cell.state in valid_states, cell
            assert cell.reason == "" or cell.reason in CAPABILITY_REASON_CODES, cell
            assert cell.evidence, f"a cell with no evidence is a bare assertion: {cell}"


def test_it_is_deterministic_given_the_installed_code() -> None:
    first = build_engine_capability_matrix(tests_root=TESTS_ROOT)
    second = build_engine_capability_matrix(tests_root=TESTS_ROOT)
    assert first.rows == second.rows
    assert first.engines == second.engines
    assert first.source_mapping == second.source_mapping


def test_every_registered_connector_has_rows(matrix) -> None:
    """A connector added to the registry must not drop silently out of the
    published matrix -- the failure mode the mapping guard exists for."""
    engines_with_rows = {row.engine for row in matrix.rows}
    for definition in connector_registry.definitions:
        assert definition.connector_type in engines_with_rows, definition.connector_type


def test_the_adapter_mapping_cannot_drift_from_the_registry(monkeypatch) -> None:
    """Proves the guard fires, rather than trusting that it would."""
    monkeypatch.setattr(
        "aida.engine_capability_matrix._ADAPTER_CLASSES",
        {name: cls for name, cls in _ADAPTER_CLASSES.items() if name != "postgres"},
    )
    with pytest.raises(RuntimeError, match="out of step with the connector registry"):
        build_engine_capability_matrix(tests_root=TESTS_ROOT)


# ---------------------------------------------------------------------------
# Native identity: review §5's own three examples.
# ---------------------------------------------------------------------------


def test_an_oracle_package_keeps_its_own_row_and_is_not_applicable_elsewhere(matrix) -> None:
    oracle = _row(matrix, "oracle", "PACKAGE")
    assert oracle.native_concept is True
    assert oracle.state(FACET_INVENTORY) == CapabilityState.SUPPORTED.value
    for engine in ("postgres", "sqlserver", "snowflake", "bigquery", "databricks"):
        row = _row(matrix, engine, "PACKAGE")
        assert row.native_concept is False
        assert row.state(FACET_INVENTORY) == CapabilityState.NOT_APPLICABLE.value, engine


def test_a_materialized_view_and_an_indexed_view_are_different_rows(matrix) -> None:
    """Review §5: they share a graph category and must not share a row.

    A PostgreSQL materialized view is its own selectable kind; SQL Server has
    no such object, and its indexed view is the row that carries that identity.
    Each reads NOT_APPLICABLE where the other is native.
    """
    pg_mview = _row(matrix, "postgres", "MATERIALIZED VIEW")
    ms_indexed = _row(matrix, "sqlserver", "INDEXED VIEW")
    assert pg_mview.graph_category == ms_indexed.graph_category == "MATERIALIZED_VIEW"
    assert pg_mview.native_object_kind != ms_indexed.native_object_kind
    assert pg_mview.state(FACET_INVENTORY) == CapabilityState.SUPPORTED.value
    assert ms_indexed.state(FACET_INVENTORY) == CapabilityState.SUPPORTED.value
    assert (
        _row(matrix, "sqlserver", "MATERIALIZED VIEW").state(FACET_INVENTORY)
        == CapabilityState.NOT_APPLICABLE.value
    )
    assert (
        _row(matrix, "postgres", "INDEXED VIEW").state(FACET_INVENTORY)
        == CapabilityState.NOT_APPLICABLE.value
    )


def test_an_oracle_materialized_view_is_unsupported_as_a_kind_not_absent(matrix) -> None:
    """Oracle has ALL_MVIEWS. Its adapter does not report a distinct kind, which
    is UNSUPPORTED -- saying NOT_APPLICABLE would deny the engine a concept it has."""
    row = _row(matrix, "oracle", "MATERIALIZED VIEW")
    assert row.native_concept is True
    assert row.state(FACET_INVENTORY) == CapabilityState.UNSUPPORTED.value
    assert "ALL_MVIEWS" in row.note


def test_a_package_members_body_is_unavailable_not_withheld(matrix) -> None:
    row = _row(matrix, "oracle", "PACKAGE MEMBER")
    assert row.state(FACET_INVENTORY) == CapabilityState.SUPPORTED.value
    assert row.state(FACET_DEFINITION) == CapabilityState.UNAVAILABLE.value
    cell = next(c for c in row.cells if c.facet == FACET_DEFINITION)
    assert cell.reason == "DEFINITION_HELD_BY_CONTAINER"


# ---------------------------------------------------------------------------
# Databricks: unsupported for parsing, never absent.
# ---------------------------------------------------------------------------


def test_databricks_is_unsupported_for_parsing_rather_than_missing(matrix) -> None:
    """The parser matrix has no Databricks row at all, because
    `sql_lineage_parser` refuses the dialect. A capability matrix that simply
    left the engine out would be the same silence this one exists to end."""
    engine = _engine(matrix, "databricks")
    assert engine.parser_dialect_supported is False
    for kind in ("VIEW", "PROCEDURE", "FUNCTION"):
        row = _row(matrix, "databricks", kind)
        assert row.state(FACET_PARSING) == CapabilityState.UNSUPPORTED.value, kind
    assert any(
        "Databricks" in gap and "parser" in gap for gap in matrix.declared_gaps
    ), matrix.declared_gaps


def test_databricks_now_declares_views_and_routines_and_still_not_grants(matrix) -> None:
    """R11-FP01 closed two of the three axes this engine used to skip, and the
    matrix follows the flags. `grants` stays UNSUPPORTED on purpose: Unity
    Catalog's privilege model is not the SQL grant model that axis records, so
    reading `TABLE_PRIVILEGES` would answer a different question while looking
    like a complete grant inventory.
    """
    engine = _engine(matrix, "databricks")
    assert engine.flags["views"] == CapabilityState.SUPPORTED.value
    assert engine.flags["routines"] == CapabilityState.SUPPORTED.value
    assert engine.flags["grants"] == CapabilityState.UNSUPPORTED.value
    assert (
        _row(matrix, "databricks", "PROCEDURE").state(FACET_INVENTORY)
        == CapabilityState.SUPPORTED.value
    )
    # And still never parsed, because the dialect is refused -- which is why the
    # row above and the parsing test below are two different facts.
    assert (
        _row(matrix, "databricks", "PROCEDURE").state(FACET_PARSING)
        == CapabilityState.UNSUPPORTED.value
    )


# ---------------------------------------------------------------------------
# Planned engines.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ["teradata", "db2"])
def test_a_planned_engine_never_claims_a_facet(matrix, engine: str) -> None:
    row = _engine(matrix, engine)
    assert row.implementation_status == "PLANNED"
    assert row.maturity == "NOT_CERTIFIED"
    assert row.live_validation == "NOT_EXERCISED"
    for kind_row in (r for r in matrix.rows if r.engine == engine):
        for cell in kind_row.cells:
            assert cell.state == CapabilityState.UNSUPPORTED.value, (engine, cell)
            assert cell.reason == "ADAPTER_NOT_CERTIFIED"


def test_a_planned_engine_gets_no_opinion_about_its_exotic_kinds(matrix) -> None:
    """Db2 has packages of its own. This repository holds no code-level opinion
    about them, so the row is absent rather than published as a blank."""
    kinds = {r.native_object_kind for r in matrix.rows if r.engine == "db2"}
    assert kinds == {"TABLE", "VIEW", "PROCEDURE", "FUNCTION"}


# ---------------------------------------------------------------------------
# Deferred gaps: named, with a reason, rather than quietly absent.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "kind", "definition"),
    [
        # R11-FP01: these seven rows were the matrix's own declared gap, and the
        # matrix moved with the code rather than being re-authored. PostgreSQL's
        # trigger definition is PARTIAL and that is the engine, not the adapter:
        # a PostgreSQL trigger has no body, so the adapter records the action
        # function and that function's body arrives on the routine axis.
        ("postgres", "TRIGGER", CapabilityState.PARTIAL.value),
        ("oracle", "TRIGGER", CapabilityState.SUPPORTED.value),
        ("sqlserver", "TRIGGER", CapabilityState.SUPPORTED.value),
        # A sequence's declaration is its metadata, the way a base relation's
        # columns are the fact, so there is no definition text anywhere.
        ("postgres", "SEQUENCE", CapabilityState.NOT_APPLICABLE.value),
        ("oracle", "SEQUENCE", CapabilityState.NOT_APPLICABLE.value),
        ("sqlserver", "SEQUENCE", CapabilityState.NOT_APPLICABLE.value),
        ("snowflake", "SEQUENCE", CapabilityState.NOT_APPLICABLE.value),
    ],
)
def test_a_discovered_native_kind_claims_it_and_cites_the_query(
    matrix, engine: str, kind: str, definition: str
) -> None:
    row = _row(matrix, engine, kind)
    assert row.native_concept is True
    cell = next(c for c in row.cells if c.facet == FACET_INVENTORY)
    assert cell.state == CapabilityState.SUPPORTED.value
    assert cell.reason == ""
    # The published evidence names the catalog objects the adapter actually
    # reads, so a flag flipped without a query behind it reads differently here
    # (and fails the test below).
    assert "backed by" in cell.evidence and f"{engine}.py" in cell.evidence
    assert row.state(FACET_DEFINITION) == definition


def test_every_claimed_inventory_is_backed_by_a_query_in_the_adapter(matrix) -> None:
    """INV-9 at the level the flags cannot enforce on their own. A capability
    flag is hand-declared, so "the adapter says it reads triggers" and "some
    query in the adapter reads a trigger catalog" are two different facts. The
    matrix cites the second beside the first; this is the gate that makes a
    divergence fail rather than merely read oddly.
    """
    from aida.engine_capability_matrix import _KIND_SPECS

    probed = {spec.name for spec in _KIND_SPECS if spec.catalog_probes}
    assert probed, "the probe mechanism has no kinds left; this test is a no-op"
    unbacked = [
        (row.engine, row.native_object_kind)
        for row in matrix.rows
        if row.native_object_kind in probed
        for cell in row.cells
        if cell.facet == FACET_INVENTORY
        and cell.state in CLAIMS
        and "which no query" in cell.evidence
    ]
    assert unbacked == []


@pytest.mark.parametrize("engine", ["snowflake", "bigquery", "databricks"])
def test_an_engine_without_triggers_says_not_applicable(matrix, engine: str) -> None:
    row = _row(matrix, engine, "TRIGGER")
    assert row.native_concept is False
    assert row.state(FACET_INVENTORY) == CapabilityState.NOT_APPLICABLE.value


@pytest.mark.parametrize("engine", ["bigquery", "databricks"])
def test_an_engine_without_sequences_says_not_applicable(matrix, engine: str) -> None:
    """The per-engine half of R11-FP01's honesty requirement: Snowflake has
    sequences and no triggers, and these two have neither, so a blanket verdict
    would be wrong about at least one of the three.
    """
    row = _row(matrix, engine, "SEQUENCE")
    assert row.native_concept is False
    assert row.state(FACET_INVENTORY) == CapabilityState.NOT_APPLICABLE.value


@pytest.mark.parametrize("kind", ["AGGREGATE FUNCTION", "WINDOW FUNCTION"])
def test_postgres_aggregates_are_discovered_with_their_definition_refused(
    matrix, kind: str
) -> None:
    """"The definition cannot be fetched" is not "the object does not exist".
    The identity, signature and parameters are discovered; the definition is
    UNAVAILABLE because `pg_get_functiondef` refuses the prokind, which is the
    source declining rather than the adapter not trying -- so the reason is
    SOURCE_RETURNED_NO_TEXT and not ADAPTER_NOT_IMPLEMENTED.
    """
    row = _row(matrix, "postgres", kind)
    inventory = next(c for c in row.cells if c.facet == FACET_INVENTORY)
    assert inventory.state == CapabilityState.SUPPORTED.value
    assert "prokind" in inventory.evidence

    definition = next(c for c in row.cells if c.facet == FACET_DEFINITION)
    assert definition.state == CapabilityState.UNAVAILABLE.value
    assert definition.reason == "SOURCE_RETURNED_NO_TEXT"
    assert "pg_get_functiondef" in definition.evidence


def test_the_prokind_claim_is_read_from_the_query_not_asserted(monkeypatch) -> None:
    """Proves the derivation, now in the other direction: narrow the routine
    queries' own prokind list back to 'f'/'p' and the two rows return to saying
    the kinds are missing. A hand-authored SUPPORTED would not move."""
    from aida import engine_capability_matrix as module

    monkeypatch.setattr(module, "_pg_prokinds", lambda: frozenset({"f", "p"}))
    narrowed = build_engine_capability_matrix(tests_root=TESTS_ROOT)
    for kind in ("AGGREGATE FUNCTION", "WINDOW FUNCTION"):
        cell = next(
            c
            for c in _row(narrowed, "postgres", kind).cells
            if c.facet == FACET_INVENTORY
        )
        assert cell.state == CapabilityState.UNSUPPORTED.value
        assert cell.reason == "ADAPTER_NOT_IMPLEMENTED"


def test_a_trigger_claim_is_read_from_the_adapter_source(monkeypatch) -> None:
    """The catalog probe still derives, and now cross-checks the flag: if
    `postgres.py` stopped mentioning `pg_trigger`, the flag would go on saying
    SUPPORTED and the published evidence would say no query backs it."""
    from aida import engine_capability_matrix as module

    monkeypatch.setattr(module, "_reads_any_catalog_object", lambda engine, probes: False)
    stripped = build_engine_capability_matrix(tests_root=TESTS_ROOT)
    cell = next(
        c
        for c in _row(stripped, "postgres", "TRIGGER").cells
        if c.facet == FACET_INVENTORY
    )
    assert cell.state == CapabilityState.SUPPORTED.value, "the flag is what it is"
    assert "which no query in postgres.py backs" in cell.evidence


def test_a_trigger_is_never_offered_as_a_governed_tool(matrix) -> None:
    """Inventoried is not draftable, and here that is the right answer rather
    than a gap: a trigger is fired by a statement rather than called, and
    reading a sequence means advancing it, which writes to the source.
    """
    for engine, kind in (("postgres", "TRIGGER"), ("postgres", "SEQUENCE")):
        cell = next(
            c for c in _row(matrix, engine, kind).cells if c.facet == FACET_CANDIDATE
        )
        assert cell.state == CapabilityState.UNSUPPORTED.value
        assert cell.reason == "CANDIDATE_SHAPE_REFUSED"


def test_a_triggers_own_body_is_parsed_and_says_how_far(matrix) -> None:
    """Corrected 2026-09-18. This test used to pin UNSUPPORTED -- "nothing hands the
    body to `procedure_lineage`" -- and the matrix went on publishing that on the live
    route after `procedure_lineage.parse_trigger_lineage` landed. A trigger body is now
    parsed by the same parser a routine body is, with its firing-row names bound, so it
    earns the routine's claim: PARTIAL with explicit degradation, and never SUPPORTED.
    Oracle's cell names its own degradation, and the declared gaps name it too.
    """
    for engine in ("postgres", "oracle", "sqlserver"):
        cell = next(
            c for c in _row(matrix, engine, "TRIGGER").cells if c.facet == FACET_PARSING
        )
        assert cell.state == CapabilityState.PARTIAL.value
        assert cell.reason == "PARSER_DEGRADES_EXPLICITLY"
        assert "parse_trigger_lineage" in cell.evidence
    oracle_cell = next(
        c for c in _row(matrix, "oracle", "TRIGGER").cells if c.facet == FACET_PARSING
    )
    assert "UNRESOLVED_TRIGGER_SUBJECT" in oracle_cell.evidence
    assert any(":NEW" in gap for gap in matrix.declared_gaps), matrix.declared_gaps
    joined = " ".join(matrix.declared_gaps)
    assert "no pass hands" not in joined
    assert "not persisted yet" not in joined


def test_a_sequence_has_nothing_to_parse_at_all(matrix) -> None:
    """NOT_APPLICABLE, not UNSUPPORTED: a sequence is a declaration rather than
    code, so there is no statement anyone could parse and no relation for it to
    read or write. UNSUPPORTED would send somebody to build a parser for it.
    """
    for engine in ("postgres", "oracle", "sqlserver", "snowflake"):
        cell = next(
            c for c in _row(matrix, engine, "SEQUENCE").cells if c.facet == FACET_PARSING
        )
        assert cell.state == CapabilityState.NOT_APPLICABLE.value


def test_the_declared_gaps_name_every_deferral(matrix) -> None:
    """The out-of-scope items are published, not merely decided. A deferral
    nobody can read is indistinguishable from an oversight."""
    joined = " ".join(matrix.declared_gaps).lower()
    # "sequence" left this list on 2026-09-18 with the deferral it named: discovered
    # sequences are persisted and reconciled (ce98bee), so there is nothing to declare.
    for phrase in (
        "trigger",
        "databricks",
        "aggregate and window",
        "pushdown",
        "push-ingestion",
        "definition-history",
        "query history",
        "source mapping",
        "engine-version",
    ):
        assert phrase in joined, phrase


# ---------------------------------------------------------------------------
# F06.4: inventoried is not understood.
# ---------------------------------------------------------------------------


def test_a_parseable_routine_is_partial_never_supported(matrix) -> None:
    """Finding F06.4, stated as a matrix rule: the procedure parser degrades
    explicitly on dynamic SQL, nested calls and unrecognised shapes, so no
    engine's routine lineage may read SUPPORTED."""
    for engine in ("postgres", "oracle", "sqlserver", "snowflake", "bigquery"):
        for kind in ("PROCEDURE", "FUNCTION"):
            cell = next(
                c for c in _row(matrix, engine, kind).cells if c.facet == FACET_PARSING
            )
            assert cell.state == CapabilityState.PARTIAL.value, (engine, kind)
            assert cell.reason == "PARSER_DEGRADES_EXPLICITLY"
            assert "routine_parse_coverage" in cell.evidence


def test_no_facet_anywhere_claims_supported_for_routine_parsing(matrix) -> None:
    routine_parsing = {
        cell.state
        for row in matrix.rows
        if row.graph_category == "ROUTINE"
        for cell in row.cells
        if cell.facet == FACET_PARSING
    }
    assert CapabilityState.SUPPORTED.value not in routine_parsing


def test_value_free_profiling_is_partial_where_ranges_are_refused(matrix) -> None:
    for engine in ("oracle", "sqlserver", "snowflake", "bigquery", "databricks"):
        assert (
            _row(matrix, engine, "TABLE").state(FACET_PROFILE)
            == CapabilityState.PARTIAL.value
        ), engine
    assert (
        _row(matrix, "postgres", "TABLE").state(FACET_PROFILE)
        == CapabilityState.SUPPORTED.value
    )


# ---------------------------------------------------------------------------
# Source mapping: the decision, published.
# ---------------------------------------------------------------------------


def test_source_mapping_is_statement_ranges_and_says_it_is_partial(matrix) -> None:
    """R11-FP07, 2026-09-18: was `..._recorded_as_unsupported_with_its_reason`. Parsed
    facts now carry their statement's range into the stored, redacted body, so the cell
    is PARTIAL with explicit degradation -- statement grain, NOT_LOCATED where no
    statement holds a fact -- and never SUPPORTED."""
    mapping = matrix.source_mapping
    assert mapping.state == CapabilityState.PARTIAL.value
    assert mapping.reason == "PARSER_DEGRADES_EXPLICITLY"
    assert mapping.granularity == "statement_ordinal, statement_range, statement_range_status"
    assert "R11-D16" in mapping.rationale
    for phrase in ("line", "column", "statement_text_digest", "NOT_LOCATED", "GAP_STATEMENT"):
        assert phrase in mapping.evidence + mapping.rationale, phrase


def test_a_package_body_is_parsed_per_member_with_a_named_fallback(matrix) -> None:
    """R11-FP03, 2026-09-18: the PACKAGE parsing cell used to say the body was parsed
    as one and never attributed to its members. It is split now, and says how it
    degrades when it cannot be -- PARTIAL, with the fallback named."""
    cell = next(c for c in _row(matrix, "oracle", "PACKAGE").cells if c.facet == FACET_PARSING)
    assert cell.state == CapabilityState.PARTIAL.value
    assert cell.reason == "PARSER_DEGRADES_EXPLICITLY"
    for phrase in ("member_attribution=MEMBER", "PACKAGE_FALLBACK", "UNBALANCED_BLOCKS"):
        assert phrase in cell.evidence, phrase
    assert "not attributed" not in cell.evidence


def test_adding_a_positional_field_would_change_the_source_mapping_record() -> None:
    """The granularity is read from the record's own fields, not asserted, so a
    real source range would have to change this cell rather than be omitted
    from it."""
    positional = [
        field.name
        for field in dataclass_fields(
            __import__("aida.procedure_lineage", fromlist=["x"]).ProcedureLineageEdgeRecord
        )
        if any(
            token in field.name
            for token in ("ordinal", "line", "offset", "range", "position", "span")
        )
    ]
    assert positional == ["statement_ordinal", "statement_range", "statement_range_status"], (
        "a positional field landed or left; regenerate the matrix and revisit the "
        "source-mapping decision rather than leaving the published record stale"
    )


# ---------------------------------------------------------------------------
# dbt macro and hook coverage.
# ---------------------------------------------------------------------------


def test_dbt_macro_and_hook_coverage_is_published_as_bounded(matrix) -> None:
    aspects = {row.aspect: row for row in matrix.dbt_coverage}
    assert "macro definitions" in aspects
    assert aspects["macro definitions"].state == CapabilityState.UNSUPPORTED.value
    hooks = [row for row in matrix.dbt_coverage if "hook" in row.aspect]
    assert len(hooks) == 2
    for row in hooks:
        assert row.state == CapabilityState.UNSUPPORTED.value, row
    macro_model = aspects["a model whose SQL a macro produced"]
    assert macro_model.state == CapabilityState.PARTIAL.value
    assert "not resolved" in macro_model.evidence or "not modelled" in macro_model.evidence


def test_the_macro_row_cites_the_manifest_keys_actually_read(matrix) -> None:
    from aida import dbt_artifacts

    row = next(r for r in matrix.dbt_coverage if r.aspect == "macro definitions")
    for key in dbt_artifacts.MANIFEST_COLLECTION_KEYS:
        assert key in row.evidence
    assert "macros" not in dbt_artifacts.MANIFEST_COLLECTION_KEYS


# ---------------------------------------------------------------------------
# "Supported" is not "verified".
# ---------------------------------------------------------------------------


def test_live_validation_is_a_separate_column_from_any_state(matrix) -> None:
    """Review §5: the architecture representing a dialect is not verified
    support. Two engines have live tests here and four do not, and the matrix
    must show that beside -- not instead of -- their facet states."""
    exercised = {row.engine for row in matrix.engines if row.live_validation == "EXERCISED"}
    assert exercised == {"postgres", "sqlserver"}, exercised
    for engine in ("oracle", "snowflake", "bigquery", "databricks"):
        row = _engine(matrix, engine)
        assert row.live_validation == "NOT_EXERCISED"
        # ... and yet several facets are SUPPORTED, which is the point: the two
        # facts are independent, and collapsing them would hide one of them.
        assert _row(matrix, engine, "TABLE").state(FACET_INVENTORY) == (
            CapabilityState.SUPPORTED.value
        )


def test_live_validation_is_unknown_without_the_test_tree() -> None:
    """The route serves this from an installed package with no `tests/`. Saying
    UNKNOWN there is honest; saying NOT_EXERCISED would not be."""
    built = build_engine_capability_matrix(tests_root=Path("no-such-directory"))
    assert {row.live_validation for row in built.engines if row.implementation_status
            == "IMPLEMENTED"} == {"UNKNOWN"}


def test_the_key_names_the_two_dimensions_nothing_records(matrix) -> None:
    key = " ".join(matrix.matrix_key)
    assert "engine_version (NOT RECORDED)" in key
    assert "deployment_variant (NOT RECORDED)" in key


# ---------------------------------------------------------------------------
# Agreement with the surface that already answers two of the facets.
# ---------------------------------------------------------------------------


def test_inventory_and_definition_agree_with_the_discovery_selection_routes(matrix) -> None:
    """The published matrix and `GET /v1/datasources/{id}/discovery-selection`
    must not be able to disagree about the same two facets for the same kind.
    Facets 1 and 2 are taken from `kind_capabilities`, and this is the assertion
    that keeps them taken from it."""
    selection_kinds = {
        "TABLE": "TABLE",
        "VIEW": "VIEW",
        "MATERIALIZED VIEW": "MATERIALIZED_VIEW",
        "PROCEDURE": "PROCEDURE",
        "FUNCTION": "FUNCTION",
        "PACKAGE": "PACKAGE",
        # R11-FP01: two more kinds the discovery-selection routes now answer
        # for, which is exactly why they are held to the same agreement.
        "TRIGGER": "TRIGGER",
        "SEQUENCE": "SEQUENCE",
    }
    for definition in connector_registry.definitions:
        if definition.implementation_status != "IMPLEMENTED":
            continue
        by_kind = {
            read.kind: read
            for read in kind_capabilities(
                definition.connector_type, definition.capabilities
            )
        }
        for native, selection_kind in selection_kinds.items():
            row = _row(matrix, definition.connector_type, native)
            assert row.state(FACET_INVENTORY) == by_kind[selection_kind].inventory, (
                definition.connector_type,
                native,
            )
            assert row.state(FACET_DEFINITION) == by_kind[selection_kind].definition, (
                definition.connector_type,
                native,
            )


# ---------------------------------------------------------------------------
# The published document.
# ---------------------------------------------------------------------------


def test_the_published_markdown_is_not_stale(matrix) -> None:
    """The same comparison `scripts/generate_engine_capability_matrix.py --check`
    makes, as a test, so a capability change that nobody regenerated fails here
    too rather than only in whichever CI step happens to run the script."""
    assert PUBLISHED_MARKDOWN.exists()
    assert PUBLISHED_MARKDOWN.read_text(encoding="utf-8") == render_markdown(matrix), (
        "Docs/90-reference/engine-capability-matrix.md is out of date with the code; "
        "run scripts/generate_engine_capability_matrix.py"
    )


def test_the_published_markdown_carries_no_timestamp(matrix) -> None:
    """A timestamp would make the staleness comparison above impossible."""
    markdown = render_markdown(matrix)
    assert "generated_at" not in markdown
    assert "Generated by `scripts/generate_engine_capability_matrix.py`" in markdown


def test_the_markdown_renders_every_engine_and_native_kind(matrix) -> None:
    markdown = render_markdown(matrix)
    for row in matrix.engines:
        assert row.engine in markdown
    for row in matrix.rows:
        assert row.native_object_kind in markdown
    for state in matrix.states:
        assert state in markdown, f"the glossary omits {state}"


# ---------------------------------------------------------------------------
# ADR-0006 / testing-strategy INV-9: the certification gate.
# ---------------------------------------------------------------------------


def _adapter_class(engine: str) -> type:
    return _ADAPTER_CLASSES[engine]


def test_capability_matrix_matches_certification(matrix) -> None:
    """INV-9 at the surface a customer reads: the published matrix never claims
    a capability the code cannot provide.

    Listed as planned in `Docs/10-architecture/adr/ADR-0006-connector-deployment.md`
    and `Docs/40-engineering/04-testing-strategy.md` since 2026-08-30.

    Asymmetric on purpose. Each rule below fires only on a `SUPPORTED` or
    `PARTIAL` cell -- a claim -- and checks it against the code that would have
    to exist for the claim to hold. An `UNSUPPORTED` / `NOT_APPLICABLE` /
    `UNAVAILABLE` cell is never checked against the code, because an adapter
    that could do something it does not claim is an honest under-claim (INV-9's
    own "under-claiming is the correct direction to fail"), and failing it would
    make silence cheaper than honesty.

    A connector that *loses* a capability fails here through the flags: its
    matrix cell goes on claiming support the flag no longer backs until the
    matrix is regenerated, and `test_the_published_markdown_is_not_stale` above
    catches the document half of the same change.
    """
    contradictions: list[str] = []

    for definition in connector_registry.definitions:
        engine = definition.connector_type
        engine_row = _engine(matrix, engine)
        implemented = definition.implementation_status == "IMPLEMENTED"

        # The flag table must be exactly the registry's, field for field.
        for field in dataclass_fields(ConnectorCapabilities):
            expected = (
                CapabilityState.SUPPORTED.value
                if implemented and definition.capabilities.get(field.name)
                else CapabilityState.UNSUPPORTED.value
            )
            if engine_row.flags[field.name] != expected:
                contradictions.append(
                    f"{engine}.{field.name}: matrix says {engine_row.flags[field.name]}, "
                    f"registry says {expected}"
                )

        # A PLANNED connector advertises nothing, so it may claim nothing.
        if not implemented:
            for row in (r for r in matrix.rows if r.engine == engine):
                for cell in row.cells:
                    if cell.state in CLAIMS:
                        contradictions.append(
                            f"{engine} is {definition.implementation_status} but claims "
                            f"{cell.state} for {row.native_object_kind}.{cell.facet}"
                        )
            continue

        adapter = _adapter_class(engine)
        for row in (r for r in matrix.rows if r.engine == engine):
            if not row.native_concept:
                for cell in row.cells:
                    if cell.state in CLAIMS:
                        contradictions.append(
                            f"{engine} claims {cell.state} for "
                            f"{row.native_object_kind}.{cell.facet}, a kind the matrix "
                            "itself says the engine does not have"
                        )
                continue
            for cell in row.cells:
                if cell.state not in CLAIMS:
                    continue
                reason = _claim_is_unbacked(
                    engine=engine,
                    adapter=adapter,
                    capabilities=definition.capabilities,
                    dialect=definition.dialect,
                    facet=cell.facet,
                    graph_category=row.graph_category,
                )
                if reason is not None:
                    contradictions.append(
                        f"{engine}.{row.native_object_kind}.{cell.facet} claims "
                        f"{cell.state}, but {reason}"
                    )

    assert contradictions == [], (
        "the published capability matrix claims support the code cannot provide:\n  "
        + "\n  ".join(contradictions)
    )


def _claim_is_unbacked(
    *,
    engine: str,
    adapter: type,
    capabilities: dict[str, bool],
    dialect: str,
    facet: str,
    graph_category: str,
) -> str | None:
    """Why a claimed facet is not backed by code, or None if it is.

    One rule per facet, each naming the code the claim depends on. Kept as a
    function rather than inlined so a new facet has one obvious place to be
    checked, and so a facet with no rule is visible as such.
    """
    if facet == FACET_DEFINITION:
        if graph_category in {"VIEW", "MATERIALIZED_VIEW"} and not capabilities.get("views"):
            return "the adapter declares views=False"
        if graph_category in {"ROUTINE", "ROUTINE_CONTAINER"} and not capabilities.get(
            "routines"
        ):
            return "the adapter declares routines=False"
        # R11-FP01. The two new categories get their own rule rather than
        # sharing the catch-all, which is half the reason they stopped being
        # `OTHER`: a claimed trigger definition must be backed by the flag that
        # reads triggers, and a sequence may not claim a definition at all --
        # its declaration is its metadata, so any SUPPORTED or PARTIAL here
        # would be a definition nobody can produce.
        if graph_category == "TRIGGER" and not capabilities.get("triggers"):
            return "the adapter declares triggers=False"
        if graph_category == "SEQUENCE":
            return "a sequence has no defining text, so no definition can be claimed"
        return None

    if facet == FACET_PARSING:
        if dialect not in sql_lineage_parser._SQLGLOT_DIALECT_MAP:
            return (
                f"neither parser accepts dialect {dialect!r} "
                "(not a key of _SQLGLOT_DIALECT_MAP)"
            )
        return None

    if facet == FACET_PROFILE:
        if not hasattr(adapter, "profile_table"):
            return "the adapter has no profile_table"
        if not capabilities.get("approximate_statistics"):
            return "the adapter declares approximate_statistics=False"
        return None

    if facet == FACET_CANDIDATE:
        generators = {
            "TABLE": (multi_table_blueprint, "build_multi_table_blueprint"),
            "VIEW": (view_tool_blueprint, "build_view_tool_blueprint"),
            "MATERIALIZED_VIEW": (view_tool_blueprint, "build_view_tool_blueprint"),
            "ROUTINE": (procedure_tool_blueprint, "build_procedure_tool_blueprint"),
        }
        target = generators.get(graph_category)
        if target is None:
            return f"no blueprint generator drafts a {graph_category}"
        module, name = target
        if not callable(getattr(module, name, None)):
            return f"{module.__name__}.{name} is not callable"
        return None

    if facet == FACET_EXECUTION:
        if not capabilities.get("explain"):
            return (
                "the adapter declares explain=False, and the gateway refuses a "
                "statement it cannot cost"
            )
        for method in ("estimate_read_query", "execute_read_query"):
            if not hasattr(adapter, method):
                return f"the adapter has no {method}, so it is not a SqlExecutor"
        return None

    if facet == FACET_INVENTORY:
        # Facet 1 comes from `kind_capabilities`, and
        # `test_inventory_and_definition_agree_with_the_discovery_selection_routes`
        # holds the two to each other. R11-FP01 added a second handle for the
        # kinds with a catalog probe --
        # `test_every_claimed_inventory_is_backed_by_a_query_in_the_adapter`
        # checks the flag against the adapter's own source. Nothing further to
        # check here, and saying so explicitly keeps an unchecked facet from
        # looking checked.
        return None

    return f"facet {facet!r} has no certification rule in this gate"


def test_the_certification_gate_would_catch_an_overclaim(matrix, monkeypatch) -> None:
    """The gate above passes. Without this, a gate whose rules never fired would
    pass for the wrong reason.

    Drives the real rule function with a claim the code cannot back -- execution
    on a connector that declares `explain=False`, the one flag with teeth -- and
    asserts it is named as a contradiction.
    """
    unbacked = _claim_is_unbacked(
        engine="oracle",
        adapter=_adapter_class("oracle"),
        capabilities={"explain": False},
        dialect="oracle",
        facet=FACET_EXECUTION,
        graph_category="TABLE",
    )
    assert unbacked is not None
    assert "explain=False" in unbacked

    backed = _claim_is_unbacked(
        engine="postgres",
        adapter=_adapter_class("postgres"),
        capabilities={"explain": True},
        dialect="postgres",
        facet=FACET_EXECUTION,
        graph_category="TABLE",
    )
    assert backed is None


def test_every_facet_has_a_certification_rule() -> None:
    """A facet added to the matrix without a rule here would be published
    unchecked. This makes that a failure rather than a silent hole."""
    for facet in FACETS:
        outcome = _claim_is_unbacked(
            engine="postgres",
            adapter=_adapter_class("postgres"),
            capabilities=dict.fromkeys(
                (field.name for field in dataclass_fields(ConnectorCapabilities)), True
            ),
            dialect="postgres",
            facet=facet,
            graph_category="TABLE",
        )
        assert outcome is None, f"{facet}: {outcome}"


def test_only_adapter_states_answer_the_adapter_question(matrix) -> None:
    """The vocabulary's own split, checked where it matters: a facet cell is an
    answer about installed code, so it may not carry a state that only one
    read with one login can produce."""
    read_only_states = {
        CapabilityState.NOT_SELECTED.value,
        CapabilityState.PERMISSION_DENIED.value,
        CapabilityState.TRUNCATED.value,
        CapabilityState.UNRESOLVED.value,
    }
    adapter_states = {state.value for state in ADAPTER_STATES} | {
        # UNAVAILABLE is allowed, and appears exactly once: an Oracle package
        # member's body is held by its package, which is a property of the
        # adapter's design rather than of any one read.
        CapabilityState.UNAVAILABLE.value
    }
    for row in matrix.rows:
        for cell in row.cells:
            assert cell.state not in read_only_states, (row.native_object_kind, cell)
            assert cell.state in adapter_states, (row.native_object_kind, cell)
