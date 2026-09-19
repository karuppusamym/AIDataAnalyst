"""Review 2026-09-16 §5: the engine x native-object-kind x facet capability matrix.

Review §5 asks for one published matrix, per supported engine, at the level of
**inventory, definition retrieval, parsing/lineage, profile access, candidate
generation and execution**, distinguishing `unsupported`, `not applicable`,
`permission denied`, `unavailable`, `truncated` and `unresolved` -- and it adds
the sentence this module exists to respect: "the architecture can represent
multiple dialects, but that is not equivalent to verified support for every
database/object kind."

Before this module there were four differently-scoped artifacts and no matrix:

1. `discovery_selection.kind_capabilities` -- per object kind, two facets
   (inventory, definition), served on the discovery-selection routes.
2. `GET /v1/connectors/capability-matrix` -- a flat `dict[str, bool]` of
   connector flags, per engine, with no facet or kind axis at all.
3. `procedure_capability_matrix` -- the parser's construct coverage, keyed by
   sqlglot **dialect** rather than by engine, with no row for Databricks
   because the parser refuses that dialect outright.
4. The surface-control matrix -- REST/MCP surfaces against controls, unrelated
   to engines and named here only so the two are never confused.

This joins them. Every status is **derived from code at generation time**, the
way `procedure_capability_matrix` derives the parser's: from the connector
registry's own definitions, from `ConnectorCapabilities`' own dataclass fields,
from method identity against the `Connector` base class, from the parser's own
dialect map and construct matrix, and from a targeted scan of each adapter's
own source for the catalog objects a facet would have to read. Nothing below is
a hand-maintained verdict.

**Native identity survives.** An Oracle `PACKAGE`, a PostgreSQL materialized
view and a SQL Server indexed view each keep their own row even though they
collapse into a shared `graph_category`, and the row says which engines have
the concept at all -- so `NOT_APPLICABLE` ("your engine has no such thing") is
never shown as `UNSUPPORTED` ("we have not built it"), which is the distinction
review §4.1 requires before a UI may offer a native type.

**What is hand-authored, and what is not.** The kind specifications below name
which engines have each native concept -- that is engine domain knowledge, not
a fact in this repository, exactly as `procedure_capability_matrix`'s construct
-> sqlglot-node-name mapping is. Every *state* is derived. Where a kind's
absence could instead be a missing adapter feature, the specification also
names the catalog objects an adapter would have to read (`catalog_probes`), and
the generator greps the adapter's own source for them: the day somebody adds
trigger discovery to `postgres.py`, this matrix stops saying it is missing.

**No timestamp.** Unlike the parser matrix, the published files carry no
`generated_at`, so `--check` can compare them byte for byte and a stale
document is a failing gate rather than a diff that is always dirty. The live
route stamps its own response instead.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Final

from aida import dbt_artifacts, procedure_capability_matrix, procedure_lineage, sql_lineage_parser
from aida.capability_states import (
    REASON_ADAPTER_NOT_CERTIFIED,
    REASON_ADAPTER_NOT_IMPLEMENTED,
    REASON_CANDIDATE_SHAPE_REFUSED,
    REASON_DEFINITION_HELD_BY_CONTAINER,
    REASON_ENGINE_LACKS_CONCEPT,
    REASON_NO_QUERY_ESTIMATE,
    REASON_PARSER_DEGRADES_EXPLICITLY,
    REASON_PARSER_REFUSES_DIALECT,
    REASON_SOURCE_RETURNED_NO_TEXT,
    CapabilityState,
)
from aida.connectors.base import Connector, ConnectorCapabilities
from aida.connectors.bigquery import BigQueryConnector
from aida.connectors.databricks import DatabricksConnector
from aida.connectors.oracle import OracleConnector
from aida.connectors.postgres import PostgresConnector
from aida.connectors.registry import ConnectorDefinition, connector_registry
from aida.connectors.snowflake import SnowflakeConnector
from aida.connectors.sqlserver import SqlServerConnector
from aida.discovery_selection import ObjectKindCapabilityRead, kind_capabilities

# ---------------------------------------------------------------------------
# The six facets review §5 names, in its own order.
# ---------------------------------------------------------------------------
FACET_INVENTORY: Final = "inventory"
FACET_DEFINITION: Final = "definition_retrieval"
FACET_PARSING: Final = "parsing_lineage"
FACET_PROFILE: Final = "profile_access"
FACET_CANDIDATE: Final = "candidate_generation"
FACET_EXECUTION: Final = "execution"

FACETS: Final[tuple[str, ...]] = (
    FACET_INVENTORY,
    FACET_DEFINITION,
    FACET_PARSING,
    FACET_PROFILE,
    FACET_CANDIDATE,
    FACET_EXECUTION,
)

#: The composite key `Docs/10-architecture/20-database-footprint-and-agent-context.md`
#: §5.2 asks the matrix to carry. `engine_version` and `deployment_variant` are
#: named and left unresolved on purpose: nothing in this repository records the
#: version or edition a given adapter was certified against, so publishing a
#: column for them would be publishing a blank that reads as "any". They stay in
#: the key as the named next acceptance criterion.
MATRIX_KEY: Final[tuple[str, ...]] = (
    "engine",
    "engine_version (NOT RECORDED)",
    "deployment_variant (NOT RECORDED)",
    "adapter_version",
    "native_object_kind",
    "capability_facet",
)

#: Connector type -> the adapter class, for the facts that are properties of
#: the class rather than of its registry row (which methods it overrides).
#:
#: `ConnectorRegistry` exposes definitions, not factories, and this module does
#: not edit it to add an accessor -- the connector package is under concurrent
#: edit. The imports above are the same six `registry.py` itself makes, and
#: `_require_every_registered_connector` below fails generation outright if this
#: mapping and the registry ever disagree, so a seventh connector cannot be
#: added and quietly left out of the matrix.
_ADAPTER_CLASSES: Final[Mapping[str, type[Connector]]] = {
    "bigquery": BigQueryConnector,
    "databricks": DatabricksConnector,
    "oracle": OracleConnector,
    "postgres": PostgresConnector,
    "snowflake": SnowflakeConnector,
    "sqlserver": SqlServerConnector,
}

#: `Connector` methods whose presence on an adapter is itself a capability fact
#: -- each has a base implementation that honestly declines, so "does this
#: adapter override it?" is the question, not "does it exist?".
_OPTIONAL_METHODS: Final[tuple[str, ...]] = (
    "scope_discovery",
    "count_invisible_objects",
    "discover_streaming",
    "profile_column_values",
    "get_query_history",
)

#: Graph categories. Several native kinds share one, which is exactly why the
#: rows are keyed by the native kind and carry this beside it.
_CATEGORY_TABLE: Final = "TABLE"
_CATEGORY_VIEW: Final = "VIEW"
_CATEGORY_MATERIALIZED: Final = "MATERIALIZED_VIEW"
_CATEGORY_ROUTINE: Final = "ROUTINE"
_CATEGORY_ROUTINE_CONTAINER: Final = "ROUTINE_CONTAINER"
#: R11-FP01. A trigger and a sequence were both `OTHER` while neither was
#: discovered, which cost nothing then and would cost the certification gate its
#: only handle now: `_claim_is_unbacked` in
#: `tests/test_engine_capability_matrix.py` dispatches on the graph category, so
#: two kinds sharing a catch-all category could only ever share one rule. They
#: are also genuinely different categories -- a trigger is code attached to a
#: table, a sequence is a generator read by a column -- which is the same
#: argument the native-kind rows themselves rest on.
_CATEGORY_TRIGGER: Final = "TRIGGER"
_CATEGORY_SEQUENCE: Final = "SEQUENCE"
_CATEGORY_OTHER: Final = "OTHER"

#: R11-FP01: what a trigger's parsing cell says per engine -- the one place its
#: degradation differs from a routine body's. Engine knowledge taken from
#: `procedure_lineage.TRIGGER_SUBJECT_RELATIONS` and `unbound_trigger_subject`.
_TRIGGER_PARSING_COMMON: Final = (
    "procedure_lineage.parse_trigger_lineage parses the captured body with the same "
    "explicit UNPARSED markers a routine body gets; the lineage agent drives it, edges "
    "land PROPOSED in trigger_lineage_edge for review, and per-trigger completion is "
    "recorded on trigger_parse_coverage"
)
_TRIGGER_PARSING_EVIDENCE: Final[dict[str, str]] = {
    "postgres": (
        f"{_TRIGGER_PARSING_COMMON}. A PostgreSQL trigger has no body: the parse follows "
        "action_routine to the function that holds it, with NEW / OLD bound to the "
        "firing table"
    ),
    "sqlserver": (
        f"{_TRIGGER_PARSING_COMMON}. INSERTED / DELETED -- aliased or not -- are bound "
        "to the firing table, so a write from the firing row names its source"
    ),
    "oracle": (
        f"{_TRIGGER_PARSING_COMMON}. Oracle's :NEW / :OLD is recorded as "
        "UNRESOLVED_TRIGGER_SUBJECT rather than bound: sqlglot reads the leading colon "
        "as a bind placeholder, so a read from the firing row carries no source table"
    ),
    "": _TRIGGER_PARSING_COMMON,
}

_ALL_IMPLEMENTED: Final[frozenset[str]] = frozenset(_ADAPTER_CLASSES)


@dataclass(frozen=True, slots=True)
class _KindSpec:
    """One native object kind, and how each facet's state is to be derived.

    `native_on` is the hand-authored half (see the module docstring): which
    engines have this concept at all. Everything else names where the state
    comes from.
    """

    name: str
    graph_category: str
    #: Engines whose SQL has this concept. An engine outside it answers
    #: `NOT_APPLICABLE` for every facet.
    native_on: frozenset[str]
    #: Whether a row is emitted for engines outside `native_on`. True only
    #: where the absence is a confident fact about the engine (only Oracle has
    #: PL/SQL packages; only SQL Server has indexed views), so a blank is never
    #: published as a claim.
    emit_when_not_native: bool
    #: When set, this kind is one of the six kinds `discovery_selection`
    #: already reports, and its inventory/definition states come from
    #: `kind_capabilities` rather than from a second opinion here.
    selection_kind: str | None
    #: Catalog objects an adapter would have to read to inventory this kind, by
    #: connector type. Present only for kinds no adapter reads today: the
    #: generator greps each adapter's own source, so an added implementation
    #: flips the row instead of leaving a stale claim.
    catalog_probes: Mapping[str, tuple[str, ...]]
    #: For a PostgreSQL routine kind, the `pg_proc.prokind` letter that
    #: identifies it. Set only where the adapter's own routine query is the
    #: evidence, and read from that query's IN-list rather than from a
    #: substring probe.
    prokind: str | None
    #: A statement over this kind is what the query gateway executes.
    queryable: bool
    #: Rows of this kind can be profiled.
    profilable: bool
    note: str


_TRIGGER_PROBES: Final[Mapping[str, tuple[str, ...]]] = {
    "postgres": ("pg_trigger",),
    "oracle": ("ALL_TRIGGERS",),
    "sqlserver": ("sys.triggers",),
}
_SEQUENCE_PROBES: Final[Mapping[str, tuple[str, ...]]] = {
    "postgres": ("pg_sequence",),
    "oracle": ("ALL_SEQUENCES",),
    "sqlserver": ("sys.sequences",),
    "snowflake": ("INFORMATION_SCHEMA.SEQUENCES", "SHOW SEQUENCES"),
}
#: PostgreSQL classifies an aggregate as `prokind = 'a'` and a window function
#: as `'w'`. `postgres._ROUTINE_SQL` restricts itself to `'f'` and `'p'`,
#: because `pg_get_functiondef` raises on the other two. `_pg_prokinds()` reads
#: that IN-list out of the query's own text, so the two rows below flip the day
#: somebody widens it -- a substring probe would not, because a bare `'a'`
#: occurs all over a SQL module.
_PROKIND_AGGREGATE: Final = "a"
_PROKIND_WINDOW: Final = "w"
_PROKIND_IN_LIST = re.compile(r"prokind\s+IN\s*\(([^)]*)\)", re.IGNORECASE)


_KIND_SPECS: Final[tuple[_KindSpec, ...]] = (
    _KindSpec(
        name="TABLE",
        graph_category=_CATEGORY_TABLE,
        native_on=_ALL_IMPLEMENTED,
        emit_when_not_native=True,
        selection_kind="TABLE",
        catalog_probes={},
        prokind=None,
        queryable=True,
        profilable=True,
        note="Base relation. No definition text exists to retrieve or parse.",
    ),
    _KindSpec(
        name="VIEW",
        graph_category=_CATEGORY_VIEW,
        native_on=_ALL_IMPLEMENTED,
        emit_when_not_native=True,
        selection_kind="VIEW",
        catalog_probes={},
        prokind=None,
        queryable=True,
        profilable=True,
        note="A view is inventoried in the table roster on every adapter; its "
        "defining text is a separate read, gated by the `views` flag.",
    ),
    _KindSpec(
        name="MATERIALIZED VIEW",
        graph_category=_CATEGORY_MATERIALIZED,
        native_on=frozenset({"postgres", "oracle", "snowflake", "bigquery", "databricks"}),
        emit_when_not_native=True,
        selection_kind="MATERIALIZED_VIEW",
        catalog_probes={},
        prokind=None,
        queryable=True,
        profilable=True,
        note="Its own selectable kind on PostgreSQL, Snowflake and BigQuery. "
        "Oracle has the concept (ALL_MVIEWS) and its adapter captures the "
        "definition with `is_materialized` set, but does not report it as a "
        "distinct kind; Databricks SQL has the concept and its adapter reads "
        "neither. Both are therefore UNSUPPORTED as a kind rather than absent "
        "as a concept. SQL Server has no such object at all: an indexed view "
        "is its own row below.",
    ),
    _KindSpec(
        name="INDEXED VIEW",
        graph_category=_CATEGORY_MATERIALIZED,
        native_on=frozenset({"sqlserver"}),
        emit_when_not_native=True,
        selection_kind=None,
        catalog_probes={},
        prokind=None,
        queryable=True,
        profilable=True,
        note="SQL Server only. Native identity is preserved rather than "
        "flattened: `sqlserver.py` reports OBJECTPROPERTY(..., 'IsIndexed') as "
        "`is_materialized` on a VIEW, so the object keeps its own native "
        "identity while sharing the MATERIALIZED_VIEW graph category with a "
        "PostgreSQL materialized view.",
    ),
    _KindSpec(
        name="PROCEDURE",
        graph_category=_CATEGORY_ROUTINE,
        native_on=_ALL_IMPLEMENTED,
        emit_when_not_native=True,
        selection_kind="PROCEDURE",
        catalog_probes={},
        prokind=None,
        queryable=False,
        profilable=False,
        note="",
    ),
    _KindSpec(
        name="FUNCTION",
        graph_category=_CATEGORY_ROUTINE,
        native_on=_ALL_IMPLEMENTED,
        emit_when_not_native=True,
        selection_kind="FUNCTION",
        catalog_probes={},
        prokind=None,
        queryable=False,
        profilable=False,
        note="SQL Server's SCALAR / INLINE_TABLE / MULTI_STATEMENT_TABLE and "
        "BigQuery's SCALAR_FUNCTION / TABLE_FUNCTION are kept as "
        "`native_subtype` on the stored routine; no other adapter reports one.",
    ),
    _KindSpec(
        name="PACKAGE",
        graph_category=_CATEGORY_ROUTINE_CONTAINER,
        native_on=frozenset({"oracle"}),
        emit_when_not_native=True,
        selection_kind="PACKAGE",
        catalog_probes={},
        prokind=None,
        queryable=False,
        profilable=False,
        note="Oracle only, and never a function in disguise: it is its own "
        "selectable kind, its grants follow it, and tool generation refuses it "
        "with PACKAGE_NOT_CALLABLE before reading its body.",
    ),
    _KindSpec(
        name="PACKAGE MEMBER",
        graph_category=_CATEGORY_ROUTINE,
        native_on=frozenset({"oracle"}),
        emit_when_not_native=True,
        selection_kind=None,
        catalog_probes={},
        prokind=None,
        queryable=False,
        profilable=False,
        note="A subprogram a package declares (ALL_PROCEDURES with "
        "SUBPROGRAM_ID and OVERLOAD). Each is its own routine with its own "
        "parameter list, and its body is deliberately absent: the source is "
        "the package's own, so it is UNAVAILABLE here rather than withheld.",
    ),
    _KindSpec(
        name="AGGREGATE FUNCTION",
        graph_category=_CATEGORY_ROUTINE,
        native_on=frozenset({"postgres"}),
        emit_when_not_native=False,
        selection_kind=None,
        catalog_probes={},
        prokind=_PROKIND_AGGREGATE,
        queryable=False,
        profilable=False,
        note="PostgreSQL `prokind = 'a'`. R11-FP01: discovered with its "
        "identity, signature, parameters and return type; only the *definition* "
        "is absent, because `pg_get_functiondef` raises on an aggregate and "
        "PostgreSQL exposes no CREATE statement for one. That is the difference "
        "between \"the definition cannot be fetched\" and \"the object does not "
        "exist\", and it is what `availability` + `unavailable_reason` are for. "
        "Listed for PostgreSQL only -- the other engines' equivalents are real "
        "concepts this repository has no code-level opinion about, and a blank "
        "NOT_APPLICABLE would be a claim.",
    ),
    _KindSpec(
        name="WINDOW FUNCTION",
        graph_category=_CATEGORY_ROUTINE,
        native_on=frozenset({"postgres"}),
        emit_when_not_native=False,
        selection_kind=None,
        catalog_probes={},
        prokind=_PROKIND_WINDOW,
        queryable=False,
        profilable=False,
        note="PostgreSQL `prokind = 'w'`; discovered on the same terms and with "
        "the same definition gap as the aggregate row above.",
    ),
    _KindSpec(
        name="TRIGGER",
        graph_category=_CATEGORY_TRIGGER,
        native_on=frozenset({"postgres", "oracle", "sqlserver"}),
        emit_when_not_native=True,
        selection_kind="TRIGGER",
        catalog_probes=_TRIGGER_PROBES,
        prokind=None,
        queryable=False,
        profilable=False,
        note="R11-FP01: its own selectable kind, never a routine in disguise -- "
        "it is not called but fires, on a named table, for a named event, at a "
        "named time, and the envelope carries all four. The firing table is a "
        "data path nothing else can see. PostgreSQL's definition facet is "
        "PARTIAL and that is the engine, not the adapter: a PostgreSQL trigger "
        "has no body of its own, so the adapter records the action function's "
        "name and that function's body arrives on the routine axis. Snowflake, "
        "BigQuery and Databricks have no trigger object at all.",
    ),
    _KindSpec(
        name="SEQUENCE",
        graph_category=_CATEGORY_SEQUENCE,
        native_on=frozenset({"postgres", "oracle", "sqlserver", "snowflake"}),
        emit_when_not_native=True,
        selection_kind="SEQUENCE",
        catalog_probes=_SEQUENCE_PROBES,
        prokind=None,
        queryable=False,
        profilable=False,
        note="R11-FP01: its own selectable kind, never a table in disguise -- it "
        "holds no rows and is read by somebody else's default expression, which "
        "PostgreSQL's `pg_depend` names. Its declaration is its metadata, so "
        "there is no definition text to retrieve and that facet is "
        "NOT_APPLICABLE everywhere. The sequence's current position is "
        "deliberately never read: it is the value the next insert writes into a "
        "customer's row, which is source data (INV-6). BigQuery and Databricks "
        "have no sequence object.",
    ),
)


@dataclass(frozen=True, slots=True)
class FacetCell:
    """One (engine, native object kind, facet) answer."""

    facet: str
    state: str
    reason: str
    evidence: str


@dataclass(frozen=True, slots=True)
class ObjectKindRow:
    engine: str
    native_object_kind: str
    graph_category: str
    native_concept: bool
    cells: tuple[FacetCell, ...]
    note: str

    def state(self, facet: str) -> str:
        return next(cell.state for cell in self.cells if cell.facet == facet)


@dataclass(frozen=True, slots=True)
class EngineRow:
    """What the registry says about one engine, and how far it has been proven."""

    engine: str
    display_name: str
    dialect: str
    adapter_version: str
    implementation_status: str
    maturity: str
    #: Whether either lineage parser will attempt this engine's dialect at all.
    parser_dialect_supported: bool
    #: Whether any test in this repository exercises this engine against a live
    #: instance. `SUPPORTED` in a facet cell means the code path exists; this
    #: column is the separate fact review §5 insists is not the same thing.
    live_validation: str
    #: Every `ConnectorCapabilities` field, as a state.
    flags: Mapping[str, str]
    #: Optional `Connector` methods this adapter overrides.
    overridden_methods: tuple[str, ...]
    notes: str


@dataclass(frozen=True, slots=True)
class SourceMappingCoverage:
    """R11-FP07 / finding F06.4: how precisely a parsed fact can be located.

    Published as its own record rather than as prose, because "precise source
    mapping" was the one thing in F06.4 that was not implemented, and the matrix
    is required to say how far it now goes: statement ranges, not token ranges.
    """

    granularity: str
    state: str
    reason: str
    evidence: str
    rationale: str


@dataclass(frozen=True, slots=True)
class DbtCoverageRow:
    """F06.4: bounded coverage reporting for dbt macros and hooks."""

    aspect: str
    state: str
    reason: str
    evidence: str


@dataclass(frozen=True, slots=True)
class EngineCapabilityMatrix:
    matrix_key: tuple[str, ...]
    facets: tuple[str, ...]
    states: tuple[str, ...]
    engines: tuple[EngineRow, ...]
    rows: tuple[ObjectKindRow, ...]
    source_mapping: SourceMappingCoverage
    dbt_coverage: tuple[DbtCoverageRow, ...]
    parser_degradation_reasons: tuple[str, ...]
    #: Named gaps this matrix reports as not implemented, so the deferral is
    #: published rather than merely decided.
    declared_gaps: tuple[str, ...]


# ---------------------------------------------------------------------------
# Derivation helpers. Each reads a fact out of live code.
# ---------------------------------------------------------------------------


def _require_every_registered_connector() -> None:
    """Fail generation when `_ADAPTER_CLASSES` and the registry disagree.

    Without this, adding a seventh adapter would silently produce a matrix
    that omits it -- the exact "published coverage quietly stopped describing
    the code" failure this whole module exists to prevent.
    """
    registered = set(connector_registry.supported_types)
    mapped = set(_ADAPTER_CLASSES)
    if registered != mapped:
        raise RuntimeError(
            "aida.engine_capability_matrix._ADAPTER_CLASSES is out of step with the "
            f"connector registry: registry-only={sorted(registered - mapped)}, "
            f"mapping-only={sorted(mapped - registered)}"
        )


def _adapter_source(connector_type: str) -> str:
    """The adapter module's own source text, for a catalog-object probe."""
    module = inspect.getmodule(_ADAPTER_CLASSES[connector_type])
    if module is None:  # pragma: no cover -- an adapter always has a module
        return ""
    return inspect.getsource(module)


def _reads_any_catalog_object(connector_type: str, probes: tuple[str, ...]) -> bool:
    source = _adapter_source(connector_type)
    return any(probe.casefold() in source.casefold() for probe in probes)


def _pg_prokinds() -> frozenset[str]:
    """The `pg_proc.prokind` letters `postgres.py`'s routine query accepts.

    Read out of the query's own `prokind IN (...)` list, so widening it to
    aggregates or window functions changes this matrix instead of leaving a
    stale "not discovered" claim behind.
    """
    source = _adapter_source("postgres")
    letters: set[str] = set()
    for match in _PROKIND_IN_LIST.finditer(source):
        letters |= set(re.findall(r"'([a-z])'", match.group(1)))
    return frozenset(letters)


def _live_validation(engine: str, tests_root: Path | None) -> str:
    """Whether a live test in this repository exercises `engine`.

    Derived from the test tree: files whose name marks them as live
    (`*_live*.py`) that mention the connector type. This is the column review
    §5's "not equivalent to verified support" sentence asks for, and it is
    deliberately a boolean per engine rather than a file list, so a peer adding
    a second live PostgreSQL test does not churn the published document.

    `UNKNOWN` when the test tree is not present -- an installed package serving
    the live route has no `tests/` directory, and saying UNKNOWN there is
    honest where claiming NOT_EXERCISED would not be.
    """
    if tests_root is None or not tests_root.is_dir():
        return "UNKNOWN"
    for path in sorted(tests_root.glob("*live*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover -- unreadable test file
            continue
        if engine in text:
            return "EXERCISED"
    return "NOT_EXERCISED"


def _default_tests_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / "tests"
    return candidate if candidate.is_dir() else None


def _parser_degrades_on_routines() -> tuple[int, int]:
    """(degraded construct rows, total construct rows) for the procedure parser.

    Read from `procedure_capability_matrix.build_capability_matrix()`, so the
    claim "routine lineage is PARTIAL, not SUPPORTED" carries the parser's own
    count behind it rather than an opinion.
    """
    constructs = procedure_capability_matrix.build_capability_matrix().constructs
    degraded = sum(
        1
        for row in constructs
        if row.procedure_parser_status in {"EXPLICIT_UNPARSED", "UNSUPPORTED"}
    )
    return degraded, len(constructs)


def _view_parser_degrades() -> bool:
    """Whether the view/flat-DML parser publishes a degraded outcome of its own.

    `sql_lineage_parser` exports `UNRESOLVED_TABLE` and a `Confidence` ladder
    with `PARTIAL`/`LOW` rungs, which is the module declaring in code that a
    view definition can come back incompletely resolved. Checked by attribute
    presence, so removing the ladder would change this matrix rather than leave
    a stale PARTIAL behind.
    """
    return hasattr(sql_lineage_parser, "UNRESOLVED_TABLE") and hasattr(
        sql_lineage_parser, "Confidence"
    )


def _selection_states(
    definition: ConnectorDefinition,
) -> Mapping[str, ObjectKindCapabilityRead]:
    """`discovery_selection`'s own per-kind answer, keyed by kind.

    Facets 1 and 2 are taken from here rather than recomputed, so the published
    matrix and the live discovery-selection routes cannot contradict each other
    -- and `tests/test_engine_capability_matrix.py` asserts that agreement.
    """
    return {
        read.kind: read
        for read in kind_capabilities(definition.connector_type, definition.capabilities)
    }


_SELECTION_STATE_REASONS: Final[Mapping[str, str]] = {
    CapabilityState.UNSUPPORTED.value: REASON_ADAPTER_NOT_IMPLEMENTED,
    CapabilityState.NOT_APPLICABLE.value: REASON_ENGINE_LACKS_CONCEPT,
}


def _not_applicable(facet: str, kind: _KindSpec, engine: str) -> FacetCell:
    return FacetCell(
        facet=facet,
        state=CapabilityState.NOT_APPLICABLE.value,
        reason=REASON_ENGINE_LACKS_CONCEPT,
        evidence=f"{engine} has no native {kind.name.lower()} object",
    )


def _planned(facet: str, engine: str) -> FacetCell:
    return FacetCell(
        facet=facet,
        state=CapabilityState.UNSUPPORTED.value,
        reason=REASON_ADAPTER_NOT_CERTIFIED,
        evidence=(
            f"registry declares {engine} PLANNED / NOT_CERTIFIED with no factory; "
            "canonical push ingestion accepts an externally produced envelope, "
            "which is not adapter coverage of this facet"
        ),
    )


def _inventory_cell(kind: _KindSpec, definition: ConnectorDefinition, engine: str) -> FacetCell:
    selection = _selection_states(definition)
    if kind.selection_kind is not None and kind.selection_kind in selection:
        state = selection[kind.selection_kind].inventory
        return FacetCell(
            facet=FACET_INVENTORY,
            state=state,
            reason=_SELECTION_STATE_REASONS.get(state, ""),
            evidence=(
                "discovery_selection.kind_capabilities("
                f"{engine!r}).{kind.selection_kind}.inventory, derived from the "
                "adapter's own capability flags"
            )
            + _probe_evidence(kind, engine, state),
        )
    if kind.name == "INDEXED VIEW":
        state = selection["VIEW"].inventory
        return FacetCell(
            facet=FACET_INVENTORY,
            state=state,
            reason=_SELECTION_STATE_REASONS.get(state, ""),
            evidence=(
                "inventoried as a VIEW carrying is_materialized from "
                "OBJECTPROPERTY(..., 'IsIndexed'); native identity kept on the object"
            ),
        )
    if kind.name == "PACKAGE MEMBER":
        state = selection["PACKAGE"].inventory
        return FacetCell(
            facet=FACET_INVENTORY,
            state=state,
            reason=_SELECTION_STATE_REASONS.get(state, ""),
            evidence=(
                "oracle.py reads ALL_PROCEDURES with SUBPROGRAM_ID/OVERLOAD; each "
                "member is its own routine keyed by (schema, package, name, signature)"
            ),
        )
    if kind.prokind is not None:
        accepted = _pg_prokinds()
        if kind.prokind in accepted:
            return FacetCell(
                facet=FACET_INVENTORY,
                state=CapabilityState.SUPPORTED.value,
                reason="",
                evidence=(
                    f"{engine}.py's routine queries accept prokind "
                    f"{sorted(accepted)}, which includes {kind.prokind!r}; the "
                    "object's identity, signature, parameters and return type are "
                    "discovered, and only its definition is absent (see the "
                    "definition facet)"
                ),
            )
        return FacetCell(
            facet=FACET_INVENTORY,
            state=CapabilityState.UNSUPPORTED.value,
            reason=REASON_ADAPTER_NOT_IMPLEMENTED,
            evidence=(
                f"{engine}.py's routine query restricts itself to prokind "
                f"{sorted(accepted)}, because pg_get_functiondef raises on a "
                f"prokind {kind.prokind!r} routine"
            ),
        )
    probes = kind.catalog_probes.get(engine, ())
    return FacetCell(
        facet=FACET_INVENTORY,
        state=CapabilityState.UNSUPPORTED.value,
        reason=REASON_ADAPTER_NOT_IMPLEMENTED,
        evidence=(
            f"no discovery query in {engine}.py reads "
            f"{', '.join(probes) if probes else 'this kind'}, and "
            "ConnectorCapabilities declares no flag for it"
        ),
    )


def _probe_evidence(kind: _KindSpec, engine: str, state: str) -> str:
    """The catalog objects behind a claimed inventory, cited or named as missing.

    R11-FP01 turned the trigger and sequence rows from "no adapter reads this"
    into flag-derived rows, which would have left `catalog_probes` and
    `_reads_any_catalog_object` as dead machinery. They are kept and repointed
    at a better job: cross-checking the flag. A claimed inventory whose adapter
    source does not mention the catalog objects the kind would have to be read
    from is a flag with no query behind it -- the exact INV-9 failure the flag
    convention exists to prevent -- and the published evidence says so in words
    rather than being silently identical to a backed claim.
    `tests/test_engine_capability_matrix.py` holds every claimed cell to citing
    its probe, so the discrepancy is a failing gate and not only a footnote.
    """
    probes = kind.catalog_probes.get(engine, ())
    if not probes or state not in {
        CapabilityState.SUPPORTED.value,
        CapabilityState.PARTIAL.value,
    }:
        return ""
    if _reads_any_catalog_object(engine, probes):
        return f", and backed by {', '.join(probes)} in {engine}.py"
    return (
        f", which no query in {engine}.py backs: none of {', '.join(probes)} is "
        "read there"
    )


def _definition_cell(kind: _KindSpec, definition: ConnectorDefinition, engine: str) -> FacetCell:
    selection = _selection_states(definition)
    if kind.selection_kind is not None and kind.selection_kind in selection:
        state = selection[kind.selection_kind].definition
        if kind.graph_category == _CATEGORY_TABLE:
            evidence = "a base relation has no defining text; its columns are the fact"
        elif kind.graph_category == _CATEGORY_SEQUENCE:
            evidence = (
                "a sequence has no defining text; its declaration -- increment, "
                "bounds, cache, cycle -- is the fact, and is carried by the "
                "inventory. Its current position is never read (INV-6)"
            )
        elif kind.graph_category == _CATEGORY_TRIGGER:
            evidence = (
                "discovery_selection.kind_capabilities("
                f"{engine!r}).TRIGGER.definition; a captured trigger body is "
                "literal-redacted, fingerprinted and screened exactly as a routine "
                "body is, and keeps `truncated` / `unavailable_reason`. PARTIAL "
                "where the engine keeps the code outside the trigger: a PostgreSQL "
                "trigger has no body, and the adapter records the action function "
                "whose own body arrives on the routine axis"
            )
        else:
            evidence = (
                "discovery_selection.kind_capabilities("
                f"{engine!r}).{kind.selection_kind}.definition; a captured definition "
                "keeps `truncated` and `unavailable_reason`, reported per object as "
                "TRUNCATED / UNAVAILABLE by capability_states.definition_read_state"
            )
        return FacetCell(
            facet=FACET_DEFINITION,
            state=state,
            reason=_SELECTION_STATE_REASONS.get(state, ""),
            evidence=evidence,
        )
    if kind.name == "INDEXED VIEW":
        state = selection["VIEW"].definition
        return FacetCell(
            facet=FACET_DEFINITION,
            state=state,
            reason=_SELECTION_STATE_REASONS.get(state, ""),
            evidence="sys.views + sys.sql_modules.definition, as for any SQL Server view",
        )
    if kind.name == "PACKAGE MEMBER":
        return FacetCell(
            facet=FACET_DEFINITION,
            state=CapabilityState.UNAVAILABLE.value,
            reason=REASON_DEFINITION_HELD_BY_CONTAINER,
            evidence=(
                "a member's body is deliberately absent with its reason recorded; "
                "the package's own source holds it, and the member is not counted "
                "as withheld code"
            ),
        )
    if kind.prokind is not None and kind.prokind in _pg_prokinds():
        # R11-FP01. The one place in this matrix where the *source* is the thing
        # that cannot answer: `pg_get_functiondef` raises on an aggregate or a
        # window function, so PostgreSQL exposes no CREATE statement for either.
        # UNAVAILABLE with SOURCE_RETURNED_NO_TEXT rather than UNSUPPORTED,
        # because the adapter asks and the engine declines -- blaming the adapter
        # would send somebody to implement a read that cannot exist.
        return FacetCell(
            facet=FACET_DEFINITION,
            state=CapabilityState.UNAVAILABLE.value,
            reason=REASON_SOURCE_RETURNED_NO_TEXT,
            evidence=(
                f"{engine}.py discovers the object without asking for a definition: "
                "pg_get_functiondef raises on this prokind, so the routine is stored "
                "with availability=UNAVAILABLE and the refusal as its "
                "unavailable_reason rather than being left out of the inventory"
            ),
        )
    return FacetCell(
        facet=FACET_DEFINITION,
        state=CapabilityState.UNSUPPORTED.value,
        reason=REASON_ADAPTER_NOT_IMPLEMENTED,
        evidence=f"the kind is not inventoried by {engine}.py, so no definition is read",
    )


def _parsing_cell(
    kind: _KindSpec,
    definition: ConnectorDefinition,
    engine: str,
    *,
    dialect_supported: bool,
    degraded: int,
    total: int,
    definition_cell: FacetCell,
) -> FacetCell:
    if kind.graph_category == _CATEGORY_TABLE:
        return FacetCell(
            facet=FACET_PARSING,
            state=CapabilityState.NOT_APPLICABLE.value,
            reason="",
            evidence="a base relation has no defining text, so there is nothing to parse",
        )
    if kind.graph_category == _CATEGORY_SEQUENCE:
        return FacetCell(
            facet=FACET_PARSING,
            state=CapabilityState.NOT_APPLICABLE.value,
            reason="",
            evidence=(
                "a sequence is a declaration, not code: there is no statement to "
                "parse and no relation for it to read or write"
            ),
        )
    if kind.graph_category == _CATEGORY_TRIGGER:
        # R11-FP01. This cell used to be UNSUPPORTED / ADAPTER_NOT_IMPLEMENTED with
        # the evidence "no pass hands a trigger body to procedure_lineage" -- true
        # when written, and published by the live capability-matrix route after it
        # stopped being true (corrected 2026-09-18). `procedure_lineage.
        # parse_trigger_lineage` now parses a trigger body with its firing-row
        # names bound to the firing table; the lineage agent drives it
        # (`lineage_agent`), edges land PROPOSED in `trigger_lineage_edge` for the
        # same review every parsed edge gets, and coverage is recorded on
        # `trigger_parse_coverage`. PARTIAL with PARSER_DEGRADES_EXPLICITLY, the
        # same claim a routine body earns, because it is the same parser with the
        # same named UNPARSED markers -- plus one of its own, per engine, below.
        if definition_cell.state not in {
            CapabilityState.SUPPORTED.value,
            CapabilityState.PARTIAL.value,
        }:
            return FacetCell(
                facet=FACET_PARSING,
                state=definition_cell.state,
                reason=definition_cell.reason,
                evidence=f"no text reaches the parser: {definition_cell.evidence}",
            )
        if not dialect_supported:
            return FacetCell(
                facet=FACET_PARSING,
                state=CapabilityState.UNSUPPORTED.value,
                reason=REASON_PARSER_REFUSES_DIALECT,
                evidence=(
                    f"dialect {definition.dialect!r} is not a key of "
                    "sql_lineage_parser._SQLGLOT_DIALECT_MAP, so the trigger body is "
                    "captured and never parsed"
                ),
            )
        return FacetCell(
            facet=FACET_PARSING,
            state=CapabilityState.PARTIAL.value,
            reason=REASON_PARSER_DEGRADES_EXPLICITLY,
            evidence=_TRIGGER_PARSING_EVIDENCE.get(engine, _TRIGGER_PARSING_EVIDENCE[""]),
        )
    if kind.graph_category == _CATEGORY_OTHER:
        # Any future kind in the catch-all category: carried from the definition
        # facet, because UNSUPPORTED there means no text reaches the parser and
        # NOT_APPLICABLE would claim the engine has no such code.
        return FacetCell(
            facet=FACET_PARSING,
            state=definition_cell.state,
            reason=definition_cell.reason,
            evidence=f"no text reaches the parser: {definition_cell.evidence}",
        )
    if not dialect_supported:
        return FacetCell(
            facet=FACET_PARSING,
            state=CapabilityState.UNSUPPORTED.value,
            reason=REASON_PARSER_REFUSES_DIALECT,
            evidence=(
                f"dialect {definition.dialect!r} is not a key of "
                "sql_lineage_parser._SQLGLOT_DIALECT_MAP, so both parsers refuse it "
                "outright ('unsupported dialect: ...', Confidence.LOW) rather than "
                "guessing -- this engine is unsupported for parsing, not absent from "
                "the parser matrix"
            ),
        )
    if definition_cell.reason == REASON_DEFINITION_HELD_BY_CONTAINER:
        # R11-FP03 (2026-09-19). A package member has no definition of its own -- its
        # body lives in the package source, which is why the definition cell above
        # reads UNAVAILABLE -- but "no definition of its own" stopped meaning "no text
        # reaches the parser" when the parser learned to split a package body. The
        # rule below would have inherited UNAVAILABLE and published "no text reaches
        # the parser", which is false. Matched on the container reason rather than on
        # the kind's name, so only the case where the text lives one level up is
        # exempted from inheriting, and any other unavailable definition still is.
        return FacetCell(
            facet=FACET_PARSING,
            state=CapabilityState.PARTIAL.value,
            reason=REASON_PARSER_DEGRADES_EXPLICITLY,
            evidence=(
                "read from the package's own source, split per member: each member's "
                "edges carry package_member and member_attribution=MEMBER; when the "
                "package body cannot be split every edge falls back to PACKAGE_FALLBACK "
                "with the reason recorded, so a member's lineage is never silently "
                "attributed to the package as a whole"
            ),
        )
    if definition_cell.state != CapabilityState.SUPPORTED.value:
        return FacetCell(
            facet=FACET_PARSING,
            state=definition_cell.state,
            reason=definition_cell.reason,
            evidence=f"no text reaches the parser: {definition_cell.evidence}",
        )
    if kind.graph_category in {_CATEGORY_VIEW, _CATEGORY_MATERIALIZED}:
        state = (
            CapabilityState.PARTIAL.value
            if _view_parser_degrades()
            else CapabilityState.SUPPORTED.value
        )
        return FacetCell(
            facet=FACET_PARSING,
            state=state,
            reason=REASON_PARSER_DEGRADES_EXPLICITLY
            if state == CapabilityState.PARTIAL.value
            else "",
            evidence=(
                "sql_lineage_parser extracts column-level lineage and publishes its own "
                "degraded outcomes (UNRESOLVED_TABLE for a source it cannot resolve, "
                "Confidence.PARTIAL/LOW); see the construct matrix at "
                "Docs/90-reference/procedure-lineage-capability-matrix.md"
            ),
        )
    if kind.graph_category == _CATEGORY_ROUTINE_CONTAINER:
        return FacetCell(
            facet=FACET_PARSING,
            state=CapabilityState.PARTIAL.value,
            reason=REASON_PARSER_DEGRADES_EXPLICITLY,
            # R11-FP03 (2026-09-18): this read "parsed as one body ... not attributed
            # to the member subprogram" until the parser learned to split a package
            # body. PARTIAL still, because the split can fail and then falls back --
            # recorded with its reason, never a silent mix of the two grains.
            evidence=(
                "a package body is split into its member subprograms; each member's "
                "edges carry package_member and member_attribution=MEMBER, package-level "
                "code is PACKAGE_LEVEL, and a body that cannot be split (NO_PACKAGE_BODY, "
                "UNBALANCED_BLOCKS, UNREADABLE_MEMBER) is parsed whole with every edge "
                "PACKAGE_FALLBACK and the reason on the result; tool generation still "
                "refuses the package"
            ),
        )
    return FacetCell(
        facet=FACET_PARSING,
        state=CapabilityState.PARTIAL.value,
        reason=REASON_PARSER_DEGRADES_EXPLICITLY,
        evidence=(
            f"procedure_lineage explicitly degrades on {degraded} of {total} recognised "
            "constructs, each producing a named UNPARSED marker rather than a silent "
            "drop; per-object completion is recorded on routine_parse_coverage, so "
            "'inventoried' is never read as 'understood'"
        ),
    )


def _profile_cell(kind: _KindSpec, definition: ConnectorDefinition, engine: str) -> FacetCell:
    if not kind.profilable:
        return FacetCell(
            facet=FACET_PROFILE,
            state=CapabilityState.NOT_APPLICABLE.value,
            reason="",
            evidence="this kind holds no rows, so there is nothing to profile",
        )
    if definition.capabilities.get("value_range_profiling"):
        return FacetCell(
            facet=FACET_PROFILE,
            state=CapabilityState.SUPPORTED.value,
            reason="",
            evidence=(
                "value-free statistics from profile_table, plus ranges and top values "
                "from an overridden profile_column_values under the "
                "value_range_profiling flag (ADR-0014's opt-in)"
            ),
        )
    return FacetCell(
        facet=FACET_PROFILE,
        state=CapabilityState.PARTIAL.value,
        reason=REASON_ADAPTER_NOT_IMPLEMENTED,
        evidence=(
            "value-free statistics only (row estimates, null rates, distinct estimates, "
            f"lengths) -- {engine} declares value_range_profiling=False, so ranges and "
            "top values are refused rather than approximated"
        ),
    )


def _candidate_cell(
    kind: _KindSpec, engine: str, *, inventory: FacetCell, definition_cell: FacetCell
) -> FacetCell:
    if kind.graph_category == _CATEGORY_TABLE:
        if inventory.state != CapabilityState.SUPPORTED.value:
            return FacetCell(
                facet=FACET_CANDIDATE,
                state=inventory.state,
                reason=inventory.reason,
                evidence=f"nothing to draft from: {inventory.evidence}",
            )
        return FacetCell(
            facet=FACET_CANDIDATE,
            state=CapabilityState.SUPPORTED.value,
            reason="",
            evidence=(
                "multi_table_blueprint.build_multi_table_blueprint drafts a single- or "
                "multi-table candidate; a join step requires an APPROVED relationship "
                "and is refused with UnjoinableTablesError otherwise"
            ),
        )
    if kind.graph_category in {_CATEGORY_TRIGGER, _CATEGORY_SEQUENCE}:
        # R11-FP01: inventoried now, and still not draftable -- which is the
        # right answer rather than a gap. A governed tool is a read the gateway
        # can cost and run; a trigger is not invoked by a caller at all and a
        # sequence can only be read by advancing it, which mutates the source.
        # `REASON_CANDIDATE_SHAPE_REFUSED` is the same code the package row
        # carries for the same class of reason: the shape is not one callable
        # read, so no generator is offered one.
        return FacetCell(
            facet=FACET_CANDIDATE,
            state=CapabilityState.UNSUPPORTED.value,
            reason=REASON_CANDIDATE_SHAPE_REFUSED,
            evidence=(
                "no blueprint generator drafts this kind, and none should: a trigger "
                "is fired by a statement rather than called, and reading a sequence "
                "means advancing it, which writes to the source"
            ),
        )
    if kind.graph_category == _CATEGORY_OTHER:
        return FacetCell(
            facet=FACET_CANDIDATE,
            state=CapabilityState.UNSUPPORTED.value,
            reason=REASON_ADAPTER_NOT_IMPLEMENTED,
            evidence="the kind is not inventoried, so no generator can be handed one",
        )
    if kind.graph_category == _CATEGORY_ROUTINE_CONTAINER:
        return FacetCell(
            facet=FACET_CANDIDATE,
            state=CapabilityState.UNSUPPORTED.value,
            reason=REASON_CANDIDATE_SHAPE_REFUSED,
            evidence=(
                "procedure_tool_blueprint refuses a package with PACKAGE_NOT_CALLABLE "
                "before reading its body -- a container is not one callable thing"
            ),
        )
    if definition_cell.state != CapabilityState.SUPPORTED.value:
        return FacetCell(
            facet=FACET_CANDIDATE,
            state=definition_cell.state,
            reason=definition_cell.reason,
            evidence=f"no eligible definition to draft from: {definition_cell.evidence}",
        )
    if kind.graph_category in {_CATEGORY_VIEW, _CATEGORY_MATERIALIZED}:
        return FacetCell(
            facet=FACET_CANDIDATE,
            state=CapabilityState.SUPPORTED.value,
            reason="",
            evidence=(
                "view_tool_blueprint.build_view_tool_blueprint drafts from any view "
                "whose captured definition passes the eligibility gate (present, "
                "AVAILABLE, value-free, not quarantined)"
            ),
        )
    return FacetCell(
        facet=FACET_CANDIDATE,
        state=CapabilityState.PARTIAL.value,
        reason=REASON_CANDIDATE_SHAPE_REFUSED,
        evidence=(
            "procedure_tool_blueprint drafts only from a routine with a single "
            "read-only result statement (find_single_read_only_result_statement); a "
            "write, a nested call, dynamic SQL or an unparsed chunk is refused with a "
            "named code rather than approximated"
        ),
    )


def _execution_cell(
    kind: _KindSpec, definition: ConnectorDefinition, engine: str, *, inventory: FacetCell
) -> FacetCell:
    if not kind.queryable:
        return FacetCell(
            facet=FACET_EXECUTION,
            state=CapabilityState.UNSUPPORTED.value,
            reason=REASON_ADAPTER_NOT_IMPLEMENTED,
            evidence=(
                "the gateway's only execution surface is estimate_read_query / "
                "execute_read_query (INV-2); nothing invokes a routine, and a "
                "procedure tool runs the SELECT its blueprint derived, never a CALL"
            ),
        )
    if inventory.state != CapabilityState.SUPPORTED.value:
        return FacetCell(
            facet=FACET_EXECUTION,
            state=inventory.state,
            reason=inventory.reason,
            evidence=(
                "the gateway resolves every name against the catalog, so a kind this "
                f"adapter does not inventory cannot be named in a governed statement: "
                f"{inventory.evidence}"
            ),
        )
    if not definition.capabilities.get("explain"):
        return FacetCell(
            facet=FACET_EXECUTION,
            state=CapabilityState.UNSUPPORTED.value,
            reason=REASON_NO_QUERY_ESTIMATE,
            evidence=(
                f"{engine} declares explain=False, and the gateway will not run a "
                "statement it cannot cost first: QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR"
            ),
        )
    return FacetCell(
        facet=FACET_EXECUTION,
        state=CapabilityState.SUPPORTED.value,
        reason="",
        evidence=(
            "governed read execution through the query gateway, cost-estimated first "
            "via the adapter's explain path"
        ),
    )


def _kind_rows(
    definition: ConnectorDefinition,
    *,
    degraded: int,
    total: int,
) -> list[ObjectKindRow]:
    engine = definition.connector_type
    implemented = definition.implementation_status == "IMPLEMENTED"
    dialect_supported = definition.dialect in sql_lineage_parser._SQLGLOT_DIALECT_MAP
    rows: list[ObjectKindRow] = []
    for kind in _KIND_SPECS:
        native = engine in kind.native_on
        if not implemented:
            # A PLANNED engine gets a row only for the kinds every relational
            # engine has. Emitting a PACKAGE or an INDEXED VIEW row for Db2
            # would publish an opinion about Db2's own SQL that nothing in this
            # repository holds; leaving the row out says less and claims less.
            if kind.native_on != _ALL_IMPLEMENTED:
                continue
            native = True
        elif not native and not kind.emit_when_not_native:
            continue
        if not implemented:
            cells = tuple(_planned(facet, engine) for facet in FACETS)
        elif not native:
            cells = tuple(_not_applicable(facet, kind, engine) for facet in FACETS)
        else:
            inventory = _inventory_cell(kind, definition, engine)
            definition_cell = _definition_cell(kind, definition, engine)
            cells = (
                inventory,
                definition_cell,
                _parsing_cell(
                    kind,
                    definition,
                    engine,
                    dialect_supported=dialect_supported,
                    degraded=degraded,
                    total=total,
                    definition_cell=definition_cell,
                ),
                _profile_cell(kind, definition, engine),
                _candidate_cell(
                    kind, engine, inventory=inventory, definition_cell=definition_cell
                ),
                _execution_cell(kind, definition, engine, inventory=inventory),
            )
        rows.append(
            ObjectKindRow(
                engine=engine,
                native_object_kind=kind.name,
                graph_category=kind.graph_category,
                native_concept=native,
                cells=cells,
                note=kind.note,
            )
        )
    return rows


def _engine_row(definition: ConnectorDefinition, tests_root: Path | None) -> EngineRow:
    engine = definition.connector_type
    implemented = definition.implementation_status == "IMPLEMENTED"
    flags = {
        field.name: (
            CapabilityState.SUPPORTED.value
            if definition.capabilities.get(field.name)
            else CapabilityState.UNSUPPORTED.value
        )
        if implemented
        else CapabilityState.UNSUPPORTED.value
        for field in dataclass_fields(ConnectorCapabilities)
    }
    overridden: tuple[str, ...] = ()
    if implemented:
        adapter = _ADAPTER_CLASSES[engine]
        overridden = tuple(
            name
            for name in _OPTIONAL_METHODS
            if getattr(adapter, name, None) is not getattr(Connector, name, None)
        )
    return EngineRow(
        engine=engine,
        display_name=definition.display_name,
        dialect=definition.dialect,
        adapter_version=definition.version,
        implementation_status=definition.implementation_status,
        maturity=definition.maturity,
        parser_dialect_supported=definition.dialect in sql_lineage_parser._SQLGLOT_DIALECT_MAP,
        live_validation=_live_validation(engine, tests_root) if implemented else "NOT_EXERCISED",
        flags=flags,
        overridden_methods=overridden,
        notes=definition.notes,
    )


def _source_mapping_coverage() -> SourceMappingCoverage:
    """F06.4's "precise source mapping", read from the parser's own record.

    Derived, not asserted: the position-bearing fields on the record that carries a
    parsed fact are enumerated into the granularity, so a positional field added or
    removed moves this cell. R11-FP07 (2026-09-18) added `statement_range` and
    `statement_range_status`; this cell was UNSUPPORTED until then, and its own tripwire
    (`test_adding_a_positional_field_would_change_the_source_mapping_record`) is what
    made the change visible here rather than leaving the published record stale.
    R11-FP07 token grain (2026-09-19) added `source_token_range` and
    `target_token_range`, and the same tripwire moved this cell again.

    PARTIAL, not SUPPORTED: a range exists per *statement*, into the stored redacted
    body, and a fact no statement of that text holds is NOT_LOCATED with NULL positions
    -- the cell degrades per statement rather than being uniformly available. The
    token ranges inside it degrade per edge end: NULL wherever the reference is not
    exactly one token of the statement.
    """
    positional = sorted(
        {
            field.name
            for field in dataclass_fields(procedure_lineage.ProcedureLineageEdgeRecord)
            if any(
                token in field.name
                for token in ("ordinal", "line", "offset", "range", "position", "span")
            )
        }
    )
    granularity = ", ".join(positional) if positional else "none"
    ranged = "statement_range" in positional
    if not ranged:
        # The shape this cell had before R11-FP07, kept so that removing the range
        # fields would publish the honest answer again rather than a stale PARTIAL.
        return SourceMappingCoverage(
            granularity=granularity,
            state=CapabilityState.UNSUPPORTED.value,
            reason=REASON_ADAPTER_NOT_IMPLEMENTED,
            evidence=(
                "the only positional field on a parsed lineage fact is "
                f"{granularity}; no line, column, character-offset or range field exists "
                "on ProcedureLineageEdgeRecord or on deep_procedure_lineage_edge"
            ),
            rationale=(
                "Recorded as unsupported rather than approximated: a precise-looking "
                "number that points at the wrong text is worse than an honest statement "
                "ordinal."
            ),
        )
    return SourceMappingCoverage(
        granularity=granularity,
        state=CapabilityState.PARTIAL.value,
        reason=REASON_PARSER_DEGRADES_EXPLICITLY,
        evidence=(
            "each parsed lineage fact carries statement_range (half-open code-point "
            "offsets, 1-based start/end line and column) and statement_range_status "
            "(STATEMENT / GAP_STATEMENT / CALL_SITE / NOT_LOCATED) into the stored, "
            "redacted body, pinned by statement_text_digest (SHA-256 of that text); "
            "persisted on deep_procedure_lineage_edge and trigger_lineage_edge; NULL "
            "positions with NOT_LOCATED where no statement of the text holds the fact, "
            "never a range of zero; inside that statement, source_token_range and "
            "target_token_range (COLUMN or TABLE, half-open offsets into the same body) "
            "name the token each end of the fact was read from, NULL where it is not "
            "exactly one token"
        ),
        rationale=(
            "Ranges index the text Atlas holds and parsed -- body_sql_redacted, the text "
            "a steward is shown -- never the customer's source bytes, which Atlas does not "
            "keep (R11-D16): a PARSED redaction re-renders the body, so a raw-text offset "
            "would point at the wrong place, and the digest lets a reader prove a range "
            "still indexes the stored body. Statement ranges come from the parser's own "
            "splitter and control-flow peel. Token ranges come from sqlglot's identifier "
            "positions, which are relative to the peeled, sometimes rewritten, remainder: "
            "every rewrite keeps the statement's tail in place, and every identifier must "
            "slice out of the stored body unchanged before any token of that statement is "
            "recorded. A token is recorded only when exactly one reference can be the "
            "fact's evidence -- the same table named twice, or a column read twice in one "
            "expression, is NULL rather than the first occurrence. An unparsed statement "
            "reads GAP_STATEMENT with the span of the text it could not read, and no "
            "token. PARTIAL because a fact with no statement of the text is NOT_LOCATED, "
            "and an end with no single token is NULL, rather than approximated."
        ),
    )


def _dbt_coverage() -> tuple[DbtCoverageRow, ...]:
    """F06.4's dbt macro and hook coverage, read from `dbt_artifacts`' own code."""
    supported = dbt_artifacts.SUPPORTED_RESOURCE_TYPES
    ingested_keys = ", ".join(dbt_artifacts.MANIFEST_COLLECTION_KEYS)
    rows = [
        DbtCoverageRow(
            aspect="macro definitions",
            state=CapabilityState.UNSUPPORTED.value
            if "macro" not in supported
            else CapabilityState.PARTIAL.value,
            reason=REASON_ADAPTER_NOT_IMPLEMENTED,
            evidence=(
                "'macro' is not in dbt_artifacts.SUPPORTED_RESOURCE_TYPES and the "
                f"manifest keys read are {ingested_keys} -- 'macros' is not among "
                "them, so no macro is stored, named or linked to the model it "
                "produced"
            ),
        ),
        DbtCoverageRow(
            aspect="a model whose SQL a macro produced",
            state=CapabilityState.PARTIAL.value,
            reason=REASON_ADAPTER_NOT_IMPLEMENTED,
            evidence=(
                "the model's own compiled SQL is parsed, so the relations the "
                "expansion resolved to are real lineage; the macro that produced it "
                "is not modelled, so nothing can say which macro, or report a macro "
                "whose expansion depends on warehouse state at compile time. "
                "ParsedDbtResource.macro_dependency_count carries the count per "
                "model, so a macro-produced model is visibly bounded rather than "
                "silently counted as understood"
            ),
        ),
        DbtCoverageRow(
            aspect="project hooks (on-run-start / on-run-end)",
            state=CapabilityState.UNSUPPORTED.value,
            reason=REASON_ADAPTER_NOT_IMPLEMENTED,
            evidence=(
                "dbt compiles these into `operation` nodes, which are not in "
                "SUPPORTED_RESOURCE_TYPES and are skipped; their SQL is never "
                "parsed, so any relation they read or write is invisible to lineage. "
                "ParsedDbtArtifact.project_hook_count reports how many the manifest "
                "holds"
            ),
        ),
        DbtCoverageRow(
            aspect="model hooks (pre_hook / post_hook)",
            state=CapabilityState.UNSUPPORTED.value,
            reason=REASON_ADAPTER_NOT_IMPLEMENTED,
            evidence=(
                "a hook's SQL lives in the node's own config, not in its compiled "
                "code, and is never parsed -- a post-hook that writes another table "
                "produces no edge. ParsedDbtResource.pre_hook_count / "
                "post_hook_count report the counts so the gap is visible per model"
            ),
        ),
    ]
    return tuple(rows)


#: Per-engine discovery work this matrix reports as missing rather than closing.
#: Each is a row in the published matrix already; naming them together is what
#: makes the deferral honest instead of quiet.
_DECLARED_GAPS: Final[tuple[str, ...]] = (
    # Corrected 2026-09-18. Two entries here had stopped being true while this matrix
    # was served live: "a trigger's own body produces no lineage" (trigger bodies are
    # parsed by `procedure_lineage.parse_trigger_lineage`, and their reviewed edges are
    # read by impact, retrieval, description drafting and classification propagation)
    # and "discovered triggers and sequences are not persisted yet" (persistence,
    # reconciliation and receipt counting landed in ce98bee, proven live on PostgreSQL
    # and SQL Server). What is still missing on the trigger axis is stated instead.
    "An Oracle trigger's read from its firing row names no source table: "
    "`procedure_lineage` binds PostgreSQL's NEW / OLD and SQL Server's INSERTED / "
    "DELETED to the firing table, but records Oracle's `:NEW` / `:OLD` as "
    "UNRESOLVED_TRIGGER_SUBJECT rather than binding it, because sqlglot reads the "
    "leading colon as a bind placeholder.",
    "Databricks now reads view definitions and routine bodies, and declares no "
    "`grants` axis: Unity Catalog's privilege model is not the SQL grant model "
    "that axis records. Its dialect is also refused by both lineage parsers, so "
    "the definitions it now captures are inventoried and never parsed.",
    "PostgreSQL aggregate and window functions are discovered with their "
    "identity, signature, parameters and return type, and their definition is "
    "UNAVAILABLE: `pg_get_functiondef` refuses those prokinds, so PostgreSQL "
    "exposes no CREATE statement to capture.",
    "Discovery-selection pushdown: all six adapters take the schema scope into "
    "their own metadata queries. Oracle, Snowflake and BigQuery also take the "
    "`schema.object` patterns and object kinds, and Databricks the patterns, into "
    "the reads whose rows belong to one object; PostgreSQL and SQL Server push the "
    "schema scope only. No adapter narrows the inventories that establish which "
    "schemas exist -- a FULL run retires a schema it did not see -- so kinds and "
    "patterns are still applied to those after reading.",
    "The push-ingestion path records no invisible-object count, so a pushed estate "
    "has no visibility evidence of its own. (It does apply the discovery selection, "
    "since 2026-09-17; this entry used to say it did not.)",
    "There is no definition-history read route: "
    "metadata_routine_definition_version accumulates versions that no endpoint "
    "serves.",
    "`count_invisible_objects` answers on PostgreSQL only; every other adapter "
    "inherits None, which is reported as unknown visibility rather than as "
    "nothing hidden.",
    "Query history is declared False on all six adapters, including the two "
    "(Snowflake, BigQuery) whose method exists, because nothing consumes it.",
    # R11-FP07 (2026-09-18): was "Precise source mapping is unsupported". Statement
    # ranges now exist; what remains partial is stated instead.
    "Source mapping is statement-grain, not token-grain: a parsed fact carries the "
    "range of its statement in the stored, redacted body, and a fact no statement "
    "holds is NOT_LOCATED -- see the source-mapping record.",
    "No facet carries a tested engine-version or deployment-variant range; the "
    "matrix key names both as not recorded.",
)


def build_engine_capability_matrix(
    *, tests_root: Path | None = None
) -> EngineCapabilityMatrix:
    """Derive the whole matrix from live code.

    Pure apart from `inspect.getsource` over already-imported adapter modules
    and an optional read of the test tree, and deterministic given the
    installed code: calling it twice in one process returns equal matrices.
    """
    _require_every_registered_connector()
    root = _default_tests_root() if tests_root is None else tests_root
    degraded, total = _parser_degrades_on_routines()
    engines = tuple(
        _engine_row(definition, root) for definition in connector_registry.definitions
    )
    rows: list[ObjectKindRow] = []
    for definition in connector_registry.definitions:
        rows.extend(_kind_rows(definition, degraded=degraded, total=total))
    return EngineCapabilityMatrix(
        matrix_key=MATRIX_KEY,
        facets=FACETS,
        states=tuple(state.value for state in CapabilityState),
        engines=engines,
        rows=tuple(rows),
        source_mapping=_source_mapping_coverage(),
        dbt_coverage=_dbt_coverage(),
        parser_degradation_reasons=tuple(
            reason.value for reason in procedure_lineage.UnparsedReason
        ),
        declared_gaps=_DECLARED_GAPS,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_STATE_GLOSSARY: Final[tuple[tuple[str, str], ...]] = (
    (
        CapabilityState.SUPPORTED.value,
        "the code path exists and returns the whole fact. **Not** a claim that it "
        "has been exercised against a live instance -- that is the "
        "`live_validation` column, and the two are deliberately different facts.",
    ),
    (
        CapabilityState.PARTIAL.value,
        "implemented, and known to return only part of the fact. The honest answer "
        "wherever an object being inventoried does not mean every path through it "
        "is understood.",
    ),
    (
        CapabilityState.UNSUPPORTED.value,
        "the engine has the concept; Atlas has not implemented reading it.",
    ),
    (
        CapabilityState.NOT_APPLICABLE.value,
        "the engine has no such concept. A different answer from UNSUPPORTED, and "
        "the reason a UI must never offer an Oracle package on a source that has "
        "no packages.",
    ),
    (
        CapabilityState.NOT_SELECTED.value,
        "supported and applicable; this scan's own discovery selection left it out.",
    ),
    (
        CapabilityState.PERMISSION_DENIED.value,
        "the source refused this read for this login. Recorded per facet on the "
        "scan receipt rather than failing the run.",
    ),
    (
        CapabilityState.UNAVAILABLE.value,
        "supported, applicable, selected and permitted, and this read did not get "
        "it.",
    ),
    (
        CapabilityState.TRUNCATED.value,
        "part of the text arrived, so anything derived from it is incomplete by "
        "construction. Stored as a boolean beside the text and reported as this "
        "state; never written as a sentinel in place of the text.",
    ),
    (
        CapabilityState.UNRESOLVED.value,
        "the fact arrived but could not be tied to a named object. Stored as "
        "`source_resolved` and reported as this state, never inferred by "
        "string-comparing a stored name against a cosmetic sentinel.",
    ),
)


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def render_markdown(matrix: EngineCapabilityMatrix) -> str:
    lines: list[str] = [
        "# Engine capability matrix",
        "",
        "Generated by `scripts/generate_engine_capability_matrix.py`"
        " (`aida.engine_capability_matrix.build_engine_capability_matrix`). Every"
        " status below is derived from code at generation time -- the connector"
        " registry's own definitions, `ConnectorCapabilities`' own fields, method"
        " identity against the `Connector` base class, the lineage parsers' own"
        " dialect map and construct matrix, and a targeted scan of each adapter's"
        " source for the catalog objects a facet would have to read. Do not hand-edit"
        " this file; `--check` fails when it is out of date with the code.",
        "",
        "This file carries no generation timestamp on purpose, so `--check` can"
        " compare it byte for byte. `GET /v1/engines/capability-matrix` serves the"
        " same data live and stamps its own response.",
        "",
        "Review 2026-09-16 §5 asked for this matrix at six facets:"
        f" `{'`, `'.join(matrix.facets)}`. Its key is"
        f" `{' + '.join(matrix.matrix_key)}`.",
        "",
        "## What `SUPPORTED` does not mean",
        "",
        "The architecture can represent several dialects. That is not the same fact"
        " as verified support for every database and object kind, and this document"
        " keeps them in separate columns: a facet's state says a code path exists,"
        " `maturity` is the registry's own certification word, and"
        " `live_validation` says whether any test in this repository exercises that"
        " engine against a live instance. Dated implementation and verification"
        " evidence lives in"
        " [the capability register](../60-delivery/20-capability-register.md).",
        "",
        "## Engines",
        "",
    ]
    lines += _table(
        (
            "Engine",
            "Adapter",
            "Dialect",
            "Status",
            "Maturity",
            "Parser dialect",
            "live_validation",
        ),
        [
            (
                row.display_name,
                f"`{row.engine}` v{row.adapter_version}",
                f"`{row.dialect}`",
                row.implementation_status,
                row.maturity,
                "accepted" if row.parser_dialect_supported else "**refused**",
                row.live_validation,
            )
            for row in matrix.engines
        ],
    )
    lines += [
        "",
        "`live_validation` is `EXERCISED` when a live test in `tests/` names that"
        " connector type, `NOT_EXERCISED` when none does, and `UNKNOWN` when the"
        " matrix is built without the test tree (an installed package serving the"
        " live route).",
        "",
        "## Adapter flags",
        "",
        "Every field of `ConnectorCapabilities`, per engine. A `PLANNED` engine"
        " advertises none (INV-9), which is why its whole row reads UNSUPPORTED.",
        "",
    ]
    flag_names = tuple(field.name for field in dataclass_fields(ConnectorCapabilities))
    lines += _table(
        ("Engine", *(f"`{name}`" for name in flag_names)),
        [
            (
                row.engine,
                *(
                    "yes" if row.flags[name] == CapabilityState.SUPPORTED.value else "no"
                    for name in flag_names
                ),
            )
            for row in matrix.engines
        ],
    )
    lines += [
        "",
        "Optional `Connector` methods each adapter overrides (the base class"
        " declines honestly, so an override is the capability):",
        "",
    ]
    lines += [
        f"- `{row.engine}`: "
        + (", ".join(f"`{name}`" for name in row.overridden_methods) or "none")
        for row in matrix.engines
    ]
    lines += [
        "",
        "## Facets by engine and native object kind",
        "",
        "One row per engine and **native** object kind. An Oracle `PACKAGE`, a"
        " PostgreSQL materialized view and a SQL Server indexed view each keep their"
        " own row even where they share a `graph_category`, so a native identity is"
        " never lost to a shared category.",
        "",
    ]
    lines += _table(
        ("Engine", "Native object kind", "Graph category", *matrix.facets),
        [
            (
                row.engine,
                row.native_object_kind,
                row.graph_category,
                *(row.state(facet) for facet in matrix.facets),
            )
            for row in matrix.rows
        ],
    )
    lines += ["", "### States", ""]
    lines += [f"- `{state}` -- {description}" for state, description in _STATE_GLOSSARY]
    lines += [
        "",
        "### Why each cell says what it says",
        "",
        "Reason codes come from `aida.capability_states.CAPABILITY_REASON_CODES`, a"
        " closed vocabulary: a reason can never carry a source value (INV-6).",
        "",
        "Engines whose cell is identical are listed together, and the two"
        " self-explanatory classes are left out here -- a `NOT_APPLICABLE` cell is"
        " covered by the native-identity notes below, and every `PLANNED` engine's"
        " cell reads the same `ADAPTER_NOT_CERTIFIED` sentence. The complete,"
        " per-engine set is in `engine-capability-matrix.json`.",
        "",
    ]
    grouped: dict[tuple[str, str, str, str, str], list[str]] = {}
    for row in matrix.rows:
        for cell in row.cells:
            if cell.state == CapabilityState.NOT_APPLICABLE.value:
                continue
            if cell.reason == REASON_ADAPTER_NOT_CERTIFIED:
                continue
            key = (
                row.native_object_kind,
                cell.facet,
                cell.state,
                cell.reason,
                cell.evidence,
            )
            grouped.setdefault(key, []).append(row.engine)
    lines += _table(
        ("Native object kind", "Facet", "State", "Reason", "Engines", "Evidence"),
        [
            (
                kind,
                facet,
                state,
                f"`{reason}`" if reason else "",
                ", ".join(engines),
                evidence,
            )
            for (kind, facet, state, reason, evidence), engines in grouped.items()
        ],
    )
    lines += ["", "### Notes on native identity", ""]
    lines += [
        f"- **{kind}** -- {note}"
        for kind, note in dict(
            (row.native_object_kind, row.note) for row in matrix.rows if row.note
        ).items()
    ]
    lines += [
        "",
        "## Parsing degradation",
        "",
        "Every `PARTIAL` parsing cell degrades explicitly, with one of these named"
        " reasons on an `UNPARSED` marker edge"
        " (`aida.procedure_lineage.UnparsedReason`) -- never a silent drop:",
        "",
    ]
    lines += [f"- `{reason}`" for reason in matrix.parser_degradation_reasons]
    lines += [
        "",
        "Per-construct detail is in"
        " [the parser capability matrix](procedure-lineage-capability-matrix.md)."
        " Per-object completion is persisted on `routine_parse_coverage`, so"
        " \"was this routine fully understood?\" is a stored answer rather than"
        " something re-derived by hunting for `UNPARSED` edges.",
        "",
        "## Source mapping",
        "",
        f"- Granularity available: `{matrix.source_mapping.granularity}`",
        f"- State: `{matrix.source_mapping.state}`"
        f" (`{matrix.source_mapping.reason}`)",
        f"- Evidence: {matrix.source_mapping.evidence}",
        "",
        matrix.source_mapping.rationale,
        "",
        "## dbt macro and hook coverage",
        "",
        "Bounded coverage reporting, not resolution. Macro expansion is **not**"
        " resolved into lineage, and this section says so rather than letting a"
        " macro-produced model or a hooked project read as fully understood.",
        "",
    ]
    lines += _table(
        ("Aspect", "State", "Reason", "Evidence"),
        [
            (row.aspect, row.state, f"`{row.reason}`", row.evidence)
            for row in matrix.dbt_coverage
        ],
    )
    lines += [
        "",
        "## Declared gaps",
        "",
        "Named here so the deferral is published rather than merely decided. Each"
        " already appears as an `UNSUPPORTED` or `NOT_APPLICABLE` cell above.",
        "",
    ]
    lines += [f"- {gap}" for gap in matrix.declared_gaps]
    lines.append("")
    return "\n".join(lines)
