"""Row-shape-agnostic assembly of connector discovery results.

Connectors differ in how they *ask* a source for its inventory; they should not
differ in how they turn answers into the envelope's dataclasses. Everything here
takes plain row mappings and returns `connectors.base` values, so a connector is
a set of queries plus a call to `assemble_catalog`.

Envelope 1.1 (gap/02 N1) adds four axes -- view definitions, routines, source
descriptions and grants. They arrive through separate `apply_*` / `build_*`
helpers rather than through `build_table_map_from_column_rows`, because every
source exposes them in a different relation and several sources expose only some
of them. A connector that does not implement an axis simply does not call its
helper, and the axis is then absent rather than empty (INV-9).

Because every facet arrives through its own relation, every facet can be refused
on its own. `read_facet` below is where a connector says which facet one query
belongs to, so a refusal costs that facet instead of the run (R11-FP02).
"""

from collections.abc import Awaitable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Final

from aida.capability_states import (
    REASON_FACET_QUERY_FAILED,
    REASON_SOURCE_DENIED_READ,
    CapabilityState,
    is_permission_refusal,
)
from aida.connectors.base import (
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredConstraint,
    DiscoveredGrant,
    DiscoveredIndex,
    DiscoveredPartition,
    DiscoveredRoutine,
    DiscoveredRoutineParameter,
    DiscoveredSchema,
    DiscoveredTable,
    DiscoveredViewDefinition,
)


@dataclass(slots=True)
class _MutableTable:
    object_type: str
    columns: list[DiscoveredColumn] = field(default_factory=list)
    constraints: list[DiscoveredConstraint] = field(default_factory=list)
    indexes: list[DiscoveredIndex] = field(default_factory=list)
    partitions: list[DiscoveredPartition] = field(default_factory=list)
    source_description: str | None = None
    view_definition: DiscoveredViewDefinition | None = None


TableMap = dict[str, dict[str, _MutableTable]]

#: Schema-keyed routine and grant inventories, as `assemble_catalog` takes them.
RoutineMap = Mapping[str, Sequence[DiscoveredRoutine]]
GrantMap = Mapping[str, Sequence[DiscoveredGrant]]


# ---------------------------------------------------------------------------
# One facet's read, and what a refusal of it costs.
#
# R11-FP02 (with R11-FP01's selection reasoning). Discovery does not read a
# source once: it reads a roster, then constraints, then indexes, then
# comments, grants, view definitions and routine bodies -- each a separate
# statement against a separate catalog relation, each refusable on its own.
# Until this region existed a single refusal propagated out of
# `Connector.discover_streaming` and failed the entire run, so a login missing
# SELECT on one catalog relation cost a whole scan and left a receipt that said
# INTERRUPTED without saying why. One missing grant is one facet's worth of
# ignorance, not a lost estate.
#
# `read_facet` is the adoption point: a connector wraps one facet's own query
# in it and names the facet. A refusal is then recorded against that facet in
# the ambient `FacetReadScope`, the read yields no rows, and the run carries on
# with the facets this login does hold. The discovery activity drains the scope
# onto the receipt (`discovery_receipt.record_facet_outcome`), where
# PERMISSION_DENIED is a different answer from UNSUPPORTED ("this adapter does
# not collect it") and from a zero count ("the source has none").
#
# **Why the scope is ambient instead of an argument.** The connector contract
# (`connectors.base.Connector.discover_streaming`) takes no receipt and returns
# only catalogs, so reporting a per-facet outcome through it would change the
# signature every connector implements and every fake in the suite. A scope
# bound to the run for the duration of one discovery lets a connector adopt
# this one query at a time, with no interface change anywhere.
# ---------------------------------------------------------------------------

#: The facets a *connector read* can be attributed to. `discovery_receipt`
#: publishes these same names (plus `object_visibility`, which is not a read of
#: the catalog but a question about it), and validates against this set, so a
#: misspelled facet is a loud error here rather than a refusal that silently
#: never reaches a receipt.
FACET_INVENTORY: Final = "inventory"
FACET_VIEW_DEFINITIONS: Final = "view_definitions"
FACET_ROUTINE_BODIES: Final = "routine_bodies"
FACET_CONSTRAINTS: Final = "constraints"
FACET_INDEXES: Final = "indexes"
FACET_PARTITIONS: Final = "partitions"
FACET_GRANTS: Final = "grants"
FACET_OBJECT_COMMENTS: Final = "object_comments"
#: R11-FP01: the two native-object axes. Defined here rather than in
#: `discovery_receipt` -- where they started -- so the facet a connector
#: attributes a refused read to and the facet the receipt publishes are the
#: same string by construction, which is what the other eight already get.
FACET_TRIGGERS: Final = "triggers"
FACET_SEQUENCES: Final = "sequences"

DISCOVERY_FACETS: Final[frozenset[str]] = frozenset(
    {
        FACET_INVENTORY,
        FACET_VIEW_DEFINITIONS,
        FACET_ROUTINE_BODIES,
        FACET_CONSTRAINTS,
        FACET_INDEXES,
        FACET_PARTITIONS,
        FACET_GRANTS,
        FACET_OBJECT_COMMENTS,
        FACET_TRIGGERS,
        FACET_SEQUENCES,
    }
)

#: Facets a refusal may not be absorbed for, because absorbing it would make
#: the run claim an estate it never saw.
#:
#: The inventory *is* the run: a refused roster yields no objects at all, and a
#: FULL run that completed with no objects reconciles every table it already
#: held as missing. `discovery_selection`'s own hazard note draws the line --
#: an object that was not *looked for* is not missing -- and a refused read is
#: on the not-looked-for side of it, which is why the outcome is still recorded
#: here before the exception goes on to fail the run. An INTERRUPTED receipt
#: naming the refusal is the honest outcome; a COMPLETE one over nothing is
#: not.
RETIREMENT_BEARING_FACETS: Final[frozenset[str]] = frozenset({FACET_INVENTORY})


def classify_read_failure(
    exc: BaseException, *, known_relations: bool = False
) -> tuple[CapabilityState, str]:
    """One failed facet read as a state and a reason code -- never as a message.

    INV-6 is the whole reason this is a function and not an f-string. A
    driver's permission error is spelled differently by every engine and
    routinely quotes the statement or the row that provoked it, so nothing
    derived from `str(exc)` may be persisted (the same rule
    `workflows.activities` applies to `analysis_run.error_message`, and the
    same one `connectors.base.FACET_REASON_CODES` gives for its own set). The
    judgement is made from the driver's own structured code -- SQLSTATE, or a
    vendor's numeric error code read as the field the driver reports it in
    (`capability_states.is_permission_refusal`) -- and what is written down is
    this pair of closed-vocabulary codes.

    `known_relations` is the caller vouching that every relation the failed
    statement names is one the engine always has (see
    `capability_states.HIDDEN_RELATION_ORACLE_ERRORS`), which is what lets a
    "does not exist or not authorized" code mean the second half.

    A failure no structured code identifies as a refusal classifies as
    UNAVAILABLE. That under-claims, which is the direction INV-9 requires: "we
    did not get it" is honest, while "the source refused you" would be a guess
    at the source's intent that sends an administrator off to grant access that
    may change nothing.
    """
    if is_permission_refusal(exc, known_relations=known_relations):
        return CapabilityState.PERMISSION_DENIED, REASON_SOURCE_DENIED_READ
    return CapabilityState.UNAVAILABLE, REASON_FACET_QUERY_FAILED


@dataclass(slots=True)
class FacetReadScope:
    """Per-facet read outcomes collected while one discovery run is in flight.

    A hand-off buffer, not the record: the receipt is the accumulator, and the
    activity drains this after every batch it commits so a facet refused during
    batch three is named by the receipt that batch three writes.
    """

    outcomes: dict[str, tuple[CapabilityState, str]] = field(default_factory=dict)

    def record(self, facet: str, *, state: CapabilityState, reason: str) -> None:
        """Record one facet's outcome; the first outcome for a facet wins.

        A refused facet is typically refused once per batch, and the first
        refusal is the one that describes the whole run's access to it. Keeping
        the first also means a later, vaguer failure of the same facet cannot
        downgrade a recorded PERMISSION_DENIED to UNAVAILABLE.
        """
        if facet not in DISCOVERY_FACETS:
            raise ValueError(f"unknown discovery facet: {facet}")
        self.outcomes.setdefault(facet, (state, reason))

    def drain(self) -> dict[str, tuple[CapabilityState, str]]:
        """Take what is recorded and clear it, so each outcome is written once."""
        drained = dict(self.outcomes)
        self.outcomes.clear()
        return drained


_ACTIVE_SCOPE: Final[ContextVar[FacetReadScope | None]] = ContextVar(
    "aida_discovery_facet_read_scope", default=None
)


@contextmanager
def facet_read_scope(scope: FacetReadScope | None = None) -> Iterator[FacetReadScope]:
    """Bind a `FacetReadScope` for the duration of one discovery run.

    Set by the caller that owns the receipt (`workflows.activities`). Outside
    such a scope `read_facet` absorbs nothing at all: a refusal nobody is
    recording must keep failing loudly rather than turn into an empty facet
    that no receipt explains.

    `scope` may be passed in so a caller can keep a reference that outlives the
    block. The discovery activity does exactly that: the refusal that *ends* a
    run is recorded while the exception is still on its way out, and the
    activity's own failure path -- which runs after this block has exited --
    needs it to write an INTERRUPTED receipt that says what it was refused.
    """
    scope = scope if scope is not None else FacetReadScope()
    token = _ACTIVE_SCOPE.set(scope)
    try:
        yield scope
    finally:
        _ACTIVE_SCOPE.reset(token)


def active_facet_read_scope() -> FacetReadScope | None:
    """The scope the current run is recording into, if any."""
    return _ACTIVE_SCOPE.get()


async def read_facet[T](
    facet: str, read: Awaitable[Sequence[T]], *, known_relations: bool = False
) -> Sequence[T]:
    """Await one facet's own query; a refusal of it costs that facet, not the run.

    Returns the rows the source gave. If the source *refuses* the read, the
    outcome is recorded against `facet` and no rows are returned, so the
    connector's own `apply_*` / `build_*` call for that facet simply has
    nothing to attach -- and the receipt, not the absence, is what says why.
    Downstream the two are never confused: the facet reads PERMISSION_DENIED
    rather than SUPPORTED-with-nothing (`discovery_receipt.as_json`), and the
    discovery activity keeps the objects of a refused facet out of the
    reconciliation pass (`workflows.activities.refused_facet_existing`), so a
    refused grants read never tombstones the grants an earlier run captured.

    Three deliberate non-absorptions:

    * **Anything that is not a refusal re-raises.** A timeout or a dropped
      connection is not one facet's problem: the next read will fail too, and
      absorbing it would let a FULL run reconcile against a source that had
      stopped answering. Only a refusal is per-facet and deterministic --
      asking again with the same login gets the same no.
    * **A refusal of a retirement-bearing facet re-raises** after being
      recorded (`RETIREMENT_BEARING_FACETS`).
    * **Outside a `facet_read_scope` nothing is absorbed**, because nothing
      would record it.

    `known_relations` is passed through to the classifier; see
    `classify_read_failure`.
    """
    rows, _refused = await read_optional_facet(facet, read, known_relations=known_relations)
    return rows


async def read_optional_facet[T](
    facet: str, read: Awaitable[Sequence[T]], *, known_relations: bool = False
) -> tuple[Sequence[T], bool]:
    """`read_facet`, plus whether an absorbed refusal is the reason no rows came back.

    Exactly `read_facet`'s rule -- the same classification, the same three
    non-absorptions -- and one extra fact in the return: `True` when the read
    was refused and absorbed. An adapter that renders a per-axis reason onto its
    own objects (Oracle's views and routine bodies) needs to tell "the source
    refused this" from "the source has none", and an empty sequence alone cannot.
    """
    if facet not in DISCOVERY_FACETS:
        raise ValueError(f"unknown discovery facet: {facet}")
    try:
        return await read, False
    except Exception as exc:
        state, reason = classify_read_failure(exc, known_relations=known_relations)
        scope = _ACTIVE_SCOPE.get()
        if scope is None:
            raise
        scope.record(facet, state=state, reason=reason)
        if state is not CapabilityState.PERMISSION_DENIED or facet in RETIREMENT_BEARING_FACETS:
            raise
        return (), True


def build_table_map_from_column_rows(column_rows: Sequence[Mapping[str, Any]]) -> TableMap:
    tables: TableMap = {}
    for row in column_rows:
        schema_name = str(row["table_schema"])
        table_name = str(row["table_name"])
        schema_tables = tables.setdefault(schema_name, {})
        table = schema_tables.setdefault(
            table_name,
            _MutableTable(object_type=normalize_object_type(str(row["table_type"]))),
        )
        table.columns.append(
            DiscoveredColumn(
                name=str(row["column_name"]),
                ordinal_position=int(row["ordinal_position"]),
                physical_type=str(row["data_type"]),
                nullable=_is_nullable(row["is_nullable"]),
                default_expression=_coerce_optional_str(row.get("column_default")),
            )
        )
    return tables


def append_aggregated_constraint_rows(
    tables: TableMap, constraint_rows: Sequence[Mapping[str, Any]]
) -> None:
    for row in constraint_rows:
        table = _lookup_table(tables, row["table_schema"], row["table_name"])
        if table is None:
            continue
        table.constraints.append(
            DiscoveredConstraint(
                name=str(row["constraint_name"]),
                constraint_type=normalize_constraint_type(str(row["constraint_type"])),
                columns=_tuple_of_strings(row.get("columns")),
                referenced_schema=_coerce_optional_str(row.get("referenced_schema")),
                referenced_table=_coerce_optional_str(row.get("referenced_table")),
                referenced_columns=_tuple_of_strings(row.get("referenced_columns")),
            )
        )


def append_grouped_key_rows(
    tables: TableMap,
    key_rows: Sequence[Mapping[str, Any]],
    *,
    constraint_type_map: Mapping[str, str],
) -> None:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in key_rows:
        key = (str(row["table_schema"]), str(row["table_name"]), str(row["constraint_name"]))
        entry = grouped.setdefault(
            key,
            {"constraint_type": str(row["constraint_type"]), "columns": []},
        )
        entry["columns"].append(str(row["column_name"]))
    for (schema_name, table_name, constraint_name), entry in grouped.items():
        table = tables.get(schema_name, {}).get(table_name)
        if table is None:
            continue
        raw_type = str(entry["constraint_type"])
        table.constraints.append(
            DiscoveredConstraint(
                name=constraint_name,
                constraint_type=constraint_type_map.get(
                    raw_type, normalize_constraint_type(raw_type)
                ),
                columns=tuple(entry["columns"]),
            )
        )


def append_grouped_foreign_key_rows(
    tables: TableMap, foreign_key_rows: Sequence[Mapping[str, Any]]
) -> None:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in foreign_key_rows:
        key = (str(row["table_schema"]), str(row["table_name"]), str(row["constraint_name"]))
        entry = grouped.setdefault(
            key,
            {
                "referenced_schema": _coerce_optional_str(row.get("referenced_schema")),
                "referenced_table": _coerce_optional_str(row.get("referenced_table")),
                "columns": [],
                "referenced_columns": [],
            },
        )
        entry["columns"].append(str(row["column_name"]))
        entry["referenced_columns"].append(str(row["referenced_column"]))
    for (schema_name, table_name, constraint_name), entry in grouped.items():
        table = tables.get(schema_name, {}).get(table_name)
        if table is None:
            continue
        table.constraints.append(
            DiscoveredConstraint(
                name=constraint_name,
                constraint_type="FOREIGN_KEY",
                columns=tuple(entry["columns"]),
                referenced_schema=entry["referenced_schema"],
                referenced_table=entry["referenced_table"],
                referenced_columns=tuple(entry["referenced_columns"]),
            )
        )


def append_grouped_index_rows(tables: TableMap, index_rows: Sequence[Mapping[str, Any]]) -> None:
    """Group flat (table, index, column) rows into one ``DiscoveredIndex`` per index.

    Callers must order rows by the index's own column position so the grouped
    ``columns`` tuple preserves index-key order (mirrors ``append_grouped_key_rows``).
    """
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in index_rows:
        key = (str(row["table_schema"]), str(row["table_name"]), str(row["index_name"]))
        entry = grouped.setdefault(
            key,
            {
                "index_type": _coerce_optional_str(row.get("index_type")) or "UNKNOWN",
                "is_unique": _is_truthy(row.get("is_unique")),
                "is_primary": _is_truthy(row.get("is_primary")),
                "columns": [],
            },
        )
        entry["columns"].append(str(row["column_name"]))
    for (schema_name, table_name, index_name), entry in grouped.items():
        table = tables.get(schema_name, {}).get(table_name)
        if table is None:
            continue
        table.indexes.append(
            DiscoveredIndex(
                name=index_name,
                index_type=str(entry["index_type"]),
                columns=tuple(entry["columns"]),
                is_unique=bool(entry["is_unique"]),
                is_primary=bool(entry["is_primary"]),
            )
        )


def append_partition_rows(tables: TableMap, partition_rows: Sequence[Mapping[str, Any]]) -> None:
    """Attach one ``DiscoveredPartition`` per row (one row already means one partition).

    Unlike indexes and constraints, a partition's key columns are a property of
    the parent table's partitioning scheme, not of the individual partition, so
    callers are expected to have already merged the shared ``key_columns`` list
    onto every partition row for a given table before calling this.
    """
    for row in partition_rows:
        table = _lookup_table(tables, row["table_schema"], row["table_name"])
        if table is None:
            continue
        table.partitions.append(
            DiscoveredPartition(
                name=str(row["partition_name"]),
                partition_type=_coerce_optional_str(row.get("partition_type")) or "UNKNOWN",
                ordinal_position=int(row.get("ordinal_position") or 0),
                key_columns=_tuple_of_strings(row.get("key_columns")),
                high_value=_coerce_optional_str(row.get("high_value")),
            )
        )


def assemble_catalog(
    catalog_name: str,
    tables: TableMap,
    *,
    routines: RoutineMap | None = None,
    grants: GrantMap | None = None,
    schema_descriptions: Mapping[str, str] | None = None,
    catalog_description: str | None = None,
) -> tuple[DiscoveredCatalog, ...]:
    """Assemble the envelope tree, including any 1.1 axes the caller collected.

    Schema names are the union of every axis, not just of `tables`: a schema that
    holds only stored procedures is a real schema, and dropping it would make the
    routine inventory silently incomplete for exactly the estates -- procedural
    ones -- where it matters most.
    """
    routines = routines or {}
    grants = grants or {}
    schema_descriptions = schema_descriptions or {}
    schema_names = list(tables)
    for name in (*routines, *grants, *schema_descriptions):
        if name not in tables:
            schema_names.append(name)

    schemas: list[DiscoveredSchema] = []
    for schema_name in schema_names:
        raw_tables = tables.get(schema_name, {})
        discovered_tables = [
            DiscoveredTable(
                name=table_name,
                object_type=raw_table.object_type,
                columns=tuple(raw_table.columns),
                constraints=tuple(raw_table.constraints),
                indexes=tuple(raw_table.indexes),
                partitions=tuple(raw_table.partitions),
                source_description=raw_table.source_description,
                view_definition=raw_table.view_definition,
            )
            for table_name, raw_table in raw_tables.items()
        ]
        schemas.append(
            DiscoveredSchema(
                name=schema_name,
                tables=tuple(discovered_tables),
                routines=tuple(routines.get(schema_name, ())),
                grants=tuple(grants.get(schema_name, ())),
                source_description=schema_descriptions.get(schema_name),
            )
        )
    return (
        DiscoveredCatalog(
            name=catalog_name,
            schemas=tuple(schemas),
            source_description=catalog_description,
        ),
    )


# --- envelope 1.1 axes ------------------------------------------------------


def apply_table_descriptions(
    tables: TableMap, description_rows: Sequence[Mapping[str, Any]]
) -> None:
    """Attach source-side table comments. Rows: table_schema, table_name, description."""
    for row in description_rows:
        table = _lookup_table(tables, row["table_schema"], row["table_name"])
        if table is None:
            continue
        description = _coerce_optional_str(row.get("description"))
        if description is not None:
            table.source_description = description


def apply_column_descriptions(
    tables: TableMap, description_rows: Sequence[Mapping[str, Any]]
) -> None:
    """Attach source-side column comments.

    Rows: table_schema, table_name, column_name, description. `DiscoveredColumn`
    is frozen, so the matching column is replaced in place rather than mutated.
    """
    for row in description_rows:
        table = _lookup_table(tables, row["table_schema"], row["table_name"])
        if table is None:
            continue
        description = _coerce_optional_str(row.get("description"))
        if description is None:
            continue
        column_name = str(row["column_name"])
        for index, column in enumerate(table.columns):
            if column.name == column_name:
                table.columns[index] = replace(column, source_description=description)
                break


def apply_view_definitions(
    tables: TableMap, view_rows: Sequence[Mapping[str, Any]]
) -> None:
    """Attach view definitions.

    Rows: table_schema, table_name, definition, and optionally is_materialized,
    is_updatable, check_option, truncated, unavailable_reason.

    A row whose `definition` is NULL is recorded as *unavailable*, not as an
    empty view: the source refused, and a downstream parser has to be able to
    tell that apart from a view whose body really is empty. `unavailable_reason`
    defaults to a generic statement rather than to NULL so the state is never
    silently reasonless.
    """
    for row in view_rows:
        table = _lookup_table(tables, row["table_schema"], row["table_name"])
        if table is None:
            continue
        definition = _coerce_optional_str(row.get("definition"))
        reason = _coerce_optional_str(row.get("unavailable_reason"))
        if definition is None:
            reason = reason or "source returned no definition text for this view"
        else:
            reason = None
        table.view_definition = DiscoveredViewDefinition(
            definition_sql=definition,
            is_materialized=bool(row.get("is_materialized", False)),
            is_updatable=_coerce_optional_bool(row.get("is_updatable")),
            check_option=_coerce_optional_str(row.get("check_option")),
            truncated=bool(row.get("truncated", False)),
            unavailable_reason=reason,
        )


def view_definition_row(
    table_schema: str, table_name: str, definition: DiscoveredViewDefinition
) -> dict[str, Any]:
    """Adapt an already-built ``DiscoveredViewDefinition`` into an `apply_view_definitions` row.

    Oracle, Snowflake and BigQuery each build the definition locally -- LONG-column
    quirks, secure-view NULLs, GET_DDL fallbacks are genuinely per-dialect -- but all
    three then only need to *attach* it, which is exactly what `apply_view_definitions`
    already does. This is the round trip that lets them use it instead of re-walking
    and rebuilding the catalog tree by hand to do the same attachment themselves.
    """
    return {
        "table_schema": table_schema,
        "table_name": table_name,
        "definition": definition.definition_sql,
        "is_materialized": definition.is_materialized,
        "is_updatable": definition.is_updatable,
        "check_option": definition.check_option,
        "truncated": definition.truncated,
        "unavailable_reason": definition.unavailable_reason,
    }


def build_routines(
    routine_rows: Sequence[Mapping[str, Any]],
    parameter_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, list[DiscoveredRoutine]]:
    """Group routines by schema, attaching parameters ordered by position.

    Routine rows: routine_schema, routine_name, routine_type, and optionally
    language, body, return_type, is_deterministic, security_mode, description,
    truncated, unavailable_reason, specific_name.

    Parameter rows: routine_schema, specific_name, parameter_name,
    ordinal_position, parameter_mode, data_type, parameter_default.

    `specific_name` is the overload-discriminating identifier every source that
    supports overloading provides (`information_schema.routines.specific_name`,
    `sys.objects.object_id`). It falls back to the routine name where a source
    has no overloads, so a connector need not invent one.
    """
    grouped_parameters: dict[tuple[str, str], list[DiscoveredRoutineParameter]] = {}
    for row in parameter_rows:
        key = (str(row["routine_schema"]), str(row["specific_name"]))
        grouped_parameters.setdefault(key, []).append(
            DiscoveredRoutineParameter(
                name=_coerce_optional_str(row.get("parameter_name")),
                ordinal_position=int(row["ordinal_position"]),
                mode=str(row.get("parameter_mode") or "IN").strip().upper(),
                physical_type=str(row["data_type"]),
                default_expression=_coerce_optional_str(row.get("parameter_default")),
            )
        )
    for parameters in grouped_parameters.values():
        parameters.sort(key=lambda parameter: parameter.ordinal_position)

    routines: dict[str, list[DiscoveredRoutine]] = {}
    for row in routine_rows:
        schema_name = str(row["routine_schema"])
        routine_name = str(row["routine_name"])
        specific_name = str(row.get("specific_name") or routine_name)
        body = _coerce_optional_str(row.get("body"))
        reason = _coerce_optional_str(row.get("unavailable_reason"))
        if body is None:
            reason = reason or "source returned no routine body"
        else:
            reason = None
        # R11-FP03: a native function kind (BigQuery's SCALAR_FUNCTION) is a FUNCTION with a
        # subtype, and a connector's own finer kind (SQL Server's INLINE_TABLE) is kept beside
        # it. Carried in `attributes` so a routine without one fingerprints as it always did.
        routine_type = normalize_object_type(str(row["routine_type"]))
        subtype = _coerce_optional_str(row.get("native_subtype"))
        if routine_type.endswith("_FUNCTION"):
            subtype = subtype or routine_type
            routine_type = "FUNCTION"
        attributes: dict[str, Any] = {"native_subtype": subtype} if subtype else {}
        routines.setdefault(schema_name, []).append(
            DiscoveredRoutine(
                name=routine_name,
                routine_type=routine_type,
                language=_coerce_optional_str(row.get("language")),
                body_sql=body,
                parameters=tuple(grouped_parameters.get((schema_name, specific_name), ())),
                return_type=_coerce_optional_str(row.get("return_type")),
                is_deterministic=_coerce_optional_bool(row.get("is_deterministic")),
                security_mode=_coerce_optional_str(row.get("security_mode")),
                source_description=_coerce_optional_str(row.get("description")),
                truncated=bool(row.get("truncated", False)),
                unavailable_reason=reason,
                attributes=attributes,
            )
        )
    return routines


def build_grants(grant_rows: Sequence[Mapping[str, Any]]) -> dict[str, list[DiscoveredGrant]]:
    """Group source-side privileges by the schema that holds the object.

    Rows: schema_name, grantee, privilege, and optionally grantee_type,
    object_type, object_name, is_grantable.
    """
    grants: dict[str, list[DiscoveredGrant]] = {}
    for row in grant_rows:
        schema_name = str(row["schema_name"])
        grants.setdefault(schema_name, []).append(
            DiscoveredGrant(
                grantee=str(row["grantee"]),
                grantee_type=str(row.get("grantee_type") or "ROLE").strip().upper(),
                privilege=str(row["privilege"]).strip().upper(),
                object_type=normalize_object_type(str(row.get("object_type") or "TABLE")),
                object_name=str(row.get("object_name") or ""),
                schema_name=schema_name,
                is_grantable=_is_truthy(row.get("is_grantable")),
            )
        )
    return grants


def normalize_constraint_type(value: str) -> str:
    normalized = value.strip().replace(" ", "_").upper()
    if normalized == "PRIMARY":
        return "PRIMARY_KEY"
    if normalized == "FOREIGN":
        return "FOREIGN_KEY"
    return normalized


def normalize_object_type(value: str) -> str:
    return value.strip().replace(" ", "_").upper()


def _coerce_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _coerce_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return _is_truthy(value)


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().upper() in {"YES", "Y", "TRUE", "T", "1"}


def _is_nullable(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().upper() in {"YES", "Y", "TRUE", "1"}


def _lookup_table(
    tables: TableMap, schema_name: object, table_name: object
) -> _MutableTable | None:
    return tables.get(str(schema_name), {}).get(str(table_name))


def _tuple_of_strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)
