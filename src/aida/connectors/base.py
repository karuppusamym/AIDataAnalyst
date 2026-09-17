from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any


def rows_to_dicts(cursor: Any, rows: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    """Map DBAPI rows to dicts keyed by lower-cased column name.

    Shared by the Snowflake and Databricks connectors, which held
    byte-identical private copies (`Docs/review-2026-09-05/REVIEW.md` R07).
    Both warehouses return upper-cased identifiers from their metadata views
    while the rest of this system keys on lower case, so the folding is the
    contract rather than a convenience -- a second copy that stopped folding
    would silently return rows every caller reads as empty.

    Some drivers already yield mappings; those are copied through untouched.
    `strict=False` on the zip is deliberate: a driver whose description and
    row width disagree should lose the surplus column rather than abort a
    discovery run mid-catalog.
    """
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [dict(r) for r in rows]
    col_names = [desc[0].lower() for desc in cursor.description] if cursor.description else []
    return [dict(zip(col_names, row, strict=False)) for row in rows]


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    catalogs: bool = True
    schemas: bool = True
    constraints: bool = False
    indexes: bool = False
    partitions: bool = False
    explain: bool = False
    query_history: bool = False
    delegated_identity: bool = False
    approximate_statistics: bool = False
    # Envelope 1.1 (gap/02 N1). Default False so a connector that has not
    # implemented an axis keeps reporting honestly (INV-9) without any edit.
    views: bool = False
    routines: bool = False
    object_comments: bool = False
    grants: bool = False
    # R11-FP01: two more native object kinds, on exactly the same
    # every-axis-defaults-to-False convention as the four envelope 1.1 flags
    # above (INV-9) -- an adapter that has not implemented the axis keeps
    # reporting honestly with no edit, and `discovery_selection` turns the flag
    # into SUPPORTED / UNSUPPORTED / NOT_APPLICABLE per engine.
    #
    # Two flags rather than one "other native objects" flag, because the six
    # engines disagree *per kind* and a single flag could only be honest on the
    # engines that happen to have both: Snowflake has sequences and no trigger
    # object at all, BigQuery has neither (its `GENERATE_UUID`/`GENERATE_ARRAY`
    # are functions, not sequences), Databricks has neither. Collapsing them
    # would make a Snowflake source read UNSUPPORTED for triggers, which is the
    # NOT_APPLICABLE-shown-as-UNSUPPORTED confusion the capability vocabulary
    # exists to end.
    triggers: bool = False
    sequences: bool = False
    # PR-2 (ADR-0014 exception path). Value-free statistics (row estimates,
    # null rates, distinct estimates, lengths) are always computed by
    # `profile_table` regardless of this flag. Actual ranges/top-values are a
    # different, much more sensitive query class -- reading real column
    # contents rather than shapes -- so a connector must opt in explicitly by
    # overriding `Connector.profile_column_values` AND setting this True.
    # Default False so every connector that has not implemented it keeps
    # reporting honestly (fail-closed, matching the `views`/`routines`/etc.
    # convention above) rather than silently claiming support it lacks.
    value_range_profiling: bool = False
    # R11-FP04. The Shannon entropy of a column's *frequency distribution* --
    # how evenly the rows spread over the distinct values. Deliberately a
    # separate flag from `value_range_profiling` above and NOT an extension of
    # it: entropy needs a `GROUP BY` over the column, but the group keys never
    # leave the source. The engine returns one float and no value, no bucket
    # edge and no exemplar, so it stays inside ADR-0014's value-free half and
    # needs no `ProfilingExceptionPolicy`. The flag exists because the query
    # shape is a per-column aggregate rather than one more expression on the
    # shared scan, so an engine that has not implemented it must report the
    # facet UNSUPPORTED (see `ProfileFacetStatus`) rather than return None and
    # let a reader guess whether the column is simply constant.
    distribution_entropy_profiling: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveredColumn:
    name: str
    ordinal_position: int
    physical_type: str
    nullable: bool
    default_expression: str | None = None
    source_description: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredConstraint:
    name: str
    constraint_type: str
    columns: tuple[str, ...]
    referenced_schema: str | None = None
    referenced_table: str | None = None
    referenced_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveredIndex:
    """CT-3/CN-8. Cost-estimation-only inventory, deliberately not part of the
    envelope 1.1 axes: nothing in lineage or semantic meaning reads an index, so
    it carries none of that axis's unavailable-reason machinery and is grouped
    like a constraint instead.
    """

    name: str
    index_type: str
    columns: tuple[str, ...]
    is_unique: bool = False
    is_primary: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveredPartition:
    """CT-3/CN-8. See `DiscoveredIndex` for why this is not an envelope 1.1 axis."""

    name: str
    partition_type: str
    ordinal_position: int
    key_columns: tuple[str, ...] = ()
    high_value: str | None = None


@dataclass(frozen=True, slots=True)
class QueryLogEntry:
    """CN-9. One row of a warehouse's own query history, exactly as a
    connector's `get_query_history()` surfaces it -- the connector-side
    counterpart to `aida.query_history_miner.WarehouseQueryLogEntry` (that
    module's own docstring calls this shape out by name as what a connector
    implementation "only has to produce"). Kept as its own type here rather
    than importing the miner's dataclass: `aida.connectors` is a lower layer
    than the modules that mine query history (module 02 vs. 05/07/12), and a
    connector must not depend upward on a feature module to describe what it
    itself returns. The two are structurally identical by construction; the
    call site that wires a connector's output into
    `mine_and_land_query_history_candidates` does the one-line mapping.

    `sql_text` is the query's own literal SQL text, deliberately including
    any literal values it contains -- INV-6 is not enforced by scrubbing it
    here. It is enforced by what happens to it next: this type is never
    itself a persisted model, `get_query_history()` returns it only in
    memory, and nothing downstream may write `sql_text` to any table --
    only a `query_id` reference and a value-free `QueryStructure` derived by
    parsing past every literal (`extract_query_structure`) may land in
    platform state. See CN-9's tracker row for the gap-proof test this
    invariant still needs once a connector implements this method.
    """

    query_id: str
    sql_text: str
    executed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredViewDefinition:
    """The text a view is defined by, and how much of it the source would give.

    Envelope 1.1 (gap/02 N1). This is the input to view-DDL lineage parsing
    (N2), which is the largest single lineage-coverage win available, so the
    envelope carries the definition verbatim and records honestly when it could
    not: a truncated or unavailable definition must never look like an empty
    one. `definition_sql is None` with a populated `unavailable_reason` is a
    first-class state, not an error.
    """

    definition_sql: str | None
    is_materialized: bool = False
    is_updatable: bool | None = None
    check_option: str | None = None
    truncated: bool = False
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredRoutineParameter:
    name: str | None
    ordinal_position: int
    mode: str
    physical_type: str
    default_expression: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredRoutine:
    """A stored procedure or function, with its body when the source exposes it.

    Envelope 1.1 (gap/02 N1/N3/N12). `body_sql` is what procedure-body parsing
    consumes, and what a read-only proof for procedure-to-tool generation is
    proved against. Same honesty rule as views: unavailable and empty are
    different, and `unavailable_reason` says which.
    """

    name: str
    routine_type: str
    language: str | None = None
    body_sql: str | None = None
    parameters: tuple[DiscoveredRoutineParameter, ...] = ()
    return_type: str | None = None
    is_deterministic: bool | None = None
    security_mode: str | None = None
    source_description: str | None = None
    truncated: bool = False
    unavailable_reason: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# R11-FP01: triggers and sequences, modelled as themselves.
#
# The review's rule is that a native kind keeps its native identity even where
# it shares a graph category with another ("an Oracle package, PostgreSQL
# materialized view and SQL Server indexed view must retain their native
# identity"). Neither of the two types below is therefore a `DiscoveredRoutine`
# or a `DiscoveredTable` with a flag on it:
#
# * A trigger is not a routine. It is not called; it *fires*, on a named table,
#   for a named event, at a named time, and the thing it runs may be its own
#   body (Oracle, SQL Server) or a separately-declared function (PostgreSQL).
#   None of those four facts has anywhere to live on `DiscoveredRoutine`, and a
#   trigger squeezed into one would report a firing table as a parameter or
#   lose it entirely.
# * A sequence is not a table. It holds no rows and has no columns; it has an
#   increment, bounds, a cache and a cycle flag, and it is *read* by a default
#   expression on somebody else's column. `DiscoveredTable` can express none of
#   that, and a sequence reported as a table would be offered for profiling.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DiscoveredTrigger:
    """A trigger, the table it fires on, and the code it runs.

    **Why this is worth discovering at all.** A trigger that writes another
    table is a data path the footprint cannot otherwise see: the write has no
    view definition, no routine call site and no dbt model behind it, so to
    every lineage surface the destination table simply changes by itself.
    `table_name` is the half of that path this pass captures -- the object whose
    modification runs the code -- and `action_routine` names the code where the
    engine keeps it somewhere else.

    **`body_sql` is redacted and value-free exactly as a routine body is.** A
    trigger body is SQL, SQL carries source values in its literals (INV-6), and
    a trigger body reaches model context by the same paths a procedure body
    does, so it is the same injection surface and is never quoted anywhere.
    `body_sql is None` with a populated `unavailable_reason` is a first-class
    state, as on `DiscoveredRoutine`: PostgreSQL genuinely has no trigger body
    to give -- the action is `EXECUTE FUNCTION f()` and `f`'s body arrives on
    the routine axis -- and that is a different fact from a body the source
    refused.

    A `WHEN` condition is deliberately not a field of its own. It is a SQL
    expression, so it would need the whole redaction/fingerprint/screening
    apparatus the body already carries to be stored safely, for one predicate;
    where the engine returns the condition as part of the trigger's own text it
    is already inside `body_sql` and redacted with it.
    """

    name: str
    #: The table whose modification fires this trigger.
    table_name: str
    #: The firing table's schema, when the engine allows it to differ from the
    #: trigger's own (Oracle). `None` means "the schema this trigger is in",
    #: which is the only possibility on PostgreSQL and SQL Server.
    table_schema: str | None = None
    #: BEFORE / AFTER / INSTEAD_OF / COMPOUND. Never inferred: an engine that
    #: does not say leaves it empty.
    timing: str = ""
    #: INSERT / UPDATE / DELETE / TRUNCATE, in engine order. A tuple because
    #: one trigger can fire on several events and every engine allows it.
    events: tuple[str, ...] = ()
    #: ROW or STATEMENT, where the engine distinguishes them.
    orientation: str | None = None
    is_enabled: bool | None = None
    #: The qualified name of the function this trigger runs, where the engine
    #: keeps the code outside the trigger (PostgreSQL's `tgfoid`). `None` on an
    #: engine whose trigger carries its own body.
    action_routine: str | None = None
    body_sql: str | None = None
    truncated: bool = False
    unavailable_reason: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredSequence:
    """A sequence generator, as its own declaration.

    **What is deliberately absent: the sequence's current position.** Every
    engine exposes it (`pg_sequences.last_value`, `ALL_SEQUENCES.LAST_NUMBER`,
    `sys.sequences.current_value`) and it is *source data*, not metadata -- it
    is the value the next insert will write into a customer's row, and it
    changes on every insert. Reading it would also advance nothing but would
    put a live business value into the control plane, which INV-6 forbids. It
    is not read, not carried here and not persisted.

    Every numeric declaration parameter is a string. Oracle permits a 28-digit
    `MAXVALUE` and PostgreSQL a `bigint` one; a single Python/SQL integer column
    wide enough for both does not exist, and a declaration is compared and
    displayed rather than arithmetic, so text is the honest carrier. The field
    names say `_bound` rather than `min_value`/`max_value` on purpose: these are
    limits written in a `CREATE SEQUENCE` statement, and a field spelled like a
    column value invites exactly the confusion INV-6's naming ratchet exists to
    catch.
    """

    name: str
    data_type: str | None = None
    start_with: str | None = None
    increment_by: str | None = None
    minimum_bound: str | None = None
    maximum_bound: str | None = None
    cache_size: str | None = None
    cycles: bool | None = None
    #: The table and column whose default expression reads this sequence, where
    #: the engine records the dependency (PostgreSQL `serial`/`IDENTITY`). This
    #: is the edge that makes a sequence part of the footprint rather than a
    #: loose object: it says which column's values this generator produces.
    owned_by_table: str | None = None
    owned_by_column: str | None = None
    source_description: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredGrant:
    """One privilege held by one grantee on one object.

    Envelope 1.1 (gap/02 N1). Source-side grants are evidence about the estate,
    never an authority in this platform: nothing here grants anything, and the
    policy engine does not read it to make a decision. It exists so that "who
    can already see this" is answerable, and so a workspace source binding can
    be reviewed against what the source itself permits.
    """

    grantee: str
    grantee_type: str
    privilege: str
    object_type: str
    object_name: str
    schema_name: str | None = None
    is_grantable: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveredTable:
    name: str
    object_type: str
    columns: tuple[DiscoveredColumn, ...]
    constraints: tuple[DiscoveredConstraint, ...] = ()
    indexes: tuple[DiscoveredIndex, ...] = ()
    partitions: tuple[DiscoveredPartition, ...] = ()
    source_description: str | None = None
    view_definition: DiscoveredViewDefinition | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredSchema:
    name: str
    tables: tuple[DiscoveredTable, ...]
    routines: tuple[DiscoveredRoutine, ...] = ()
    grants: tuple[DiscoveredGrant, ...] = ()
    #: R11-FP01. Defaulted to empty so an adapter that reads neither axis is
    #: unchanged, and so "this engine has no triggers" and "this scan read none"
    #: stay distinguishable through `capabilities.triggers` rather than through
    #: an empty tuple.
    triggers: tuple[DiscoveredTrigger, ...] = ()
    sequences: tuple[DiscoveredSequence, ...] = ()
    source_description: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredCatalog:
    name: str
    schemas: tuple[DiscoveredSchema, ...]
    source_description: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# R11-FP01: row-shape assembly for the two new axes.
#
# These belong beside `aida.connectors.discovery`'s `build_routines` /
# `build_grants`, which is where every other row-shape-agnostic builder lives,
# and they are here instead for one reason worth recording rather than leaving
# as a puzzle: `discovery.py` is owned by the push-ingestion workstream this
# cycle and is not mine to edit. They are byte-for-byte the shape that module's
# helpers take -- schema-keyed maps built from plain row mappings -- so moving
# them there later is a cut and a paste, and `attach_native_objects` below
# exists precisely so `assemble_catalog` needs no new parameter to be given
# them.
# --------------------------------------------------------------------------

#: Schema-keyed trigger and sequence inventories, as `attach_native_objects` takes them.
TriggerMap = Mapping[str, Sequence[DiscoveredTrigger]]
SequenceMap = Mapping[str, Sequence[DiscoveredSequence]]


def _optional_row_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_row_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in {"YES", "Y", "TRUE", "T", "1", "ENABLED"}


def build_triggers(
    trigger_rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[DiscoveredTrigger]]:
    """Group triggers by the schema that holds them.

    Rows: `trigger_schema`, `trigger_name`, `table_name`, and optionally
    `table_schema`, `timing`, `orientation`, `is_enabled`, `action_routine`,
    `body`, `truncated`, `unavailable_reason`, plus the four event flags
    `on_insert` / `on_update` / `on_delete` / `on_truncate`, or an `events`
    sequence where the engine returns them as one string.

    The event flags are read rather than an `events` list built per adapter,
    because three of the four engines with triggers expose the events as
    separate booleans or bits (PostgreSQL `tgtype`, SQL Server
    `sys.trigger_events`) and only Oracle hands over a phrase. Normalising here
    keeps the order stable across engines, which a set would not.

    A row whose `body` is NULL is recorded as *unavailable with a reason*, never
    as a trigger with an empty body -- the same rule `build_routines` applies,
    and for the same downstream reason: a parser has to tell "there is no text"
    from "we were not given the text".
    """
    triggers: dict[str, list[DiscoveredTrigger]] = {}
    for row in trigger_rows:
        schema_name = str(row["trigger_schema"])
        events = _trigger_events(row)
        body = _optional_row_text(row.get("body"))
        reason = _optional_row_text(row.get("unavailable_reason"))
        if body is None:
            reason = reason or "source returned no trigger body"
        else:
            reason = None
        triggers.setdefault(schema_name, []).append(
            DiscoveredTrigger(
                name=str(row["trigger_name"]),
                table_name=str(row["table_name"]),
                table_schema=_optional_row_text(row.get("table_schema")),
                timing=(_optional_row_text(row.get("timing")) or "").replace(" ", "_").upper(),
                events=events,
                orientation=_optional_row_text(row.get("orientation")),
                is_enabled=_optional_row_bool(row.get("is_enabled")),
                action_routine=_optional_row_text(row.get("action_routine")),
                body_sql=body,
                truncated=bool(row.get("truncated", False)),
                unavailable_reason=reason,
            )
        )
    return triggers


#: The DML events a trigger may fire on, in the order every engine lists them.
TRIGGER_EVENTS: tuple[str, ...] = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


def _trigger_events(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The events one trigger row fires on, from flags or from a phrase.

    Oracle's `ALL_TRIGGERS.TRIGGERING_EVENT` is a phrase (`INSERT OR UPDATE`),
    so it is matched against the closed `TRIGGER_EVENTS` vocabulary rather than
    split on `OR` and stored as whatever words came back: a phrase this function
    does not recognise contributes no event, which reads as "the events are not
    known" instead of putting engine prose into platform state.
    """
    declared = row.get("events")
    if declared is not None:
        phrase = (
            " ".join(str(item) for item in declared)
            if isinstance(declared, list | tuple)
            else str(declared)
        ).upper()
        return tuple(event for event in TRIGGER_EVENTS if event in phrase)
    flags = {
        "INSERT": row.get("on_insert"),
        "UPDATE": row.get("on_update"),
        "DELETE": row.get("on_delete"),
        "TRUNCATE": row.get("on_truncate"),
    }
    return tuple(event for event in TRIGGER_EVENTS if _optional_row_bool(flags[event]))


def build_sequences(
    sequence_rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[DiscoveredSequence]]:
    """Group sequences by schema.

    Rows: `sequence_schema`, `sequence_name`, and optionally `data_type`,
    `start_with`, `increment_by`, `minimum_bound`, `maximum_bound`,
    `cache_size`, `cycles`, `owned_by_table`, `owned_by_column`, `description`.

    Every numeric parameter is coerced to text -- see `DiscoveredSequence` for
    why -- and a row carrying the sequence's *current position* under any name
    is not read here at all: this function only ever looks at the keys above.
    """
    sequences: dict[str, list[DiscoveredSequence]] = {}
    for row in sequence_rows:
        schema_name = str(row["sequence_schema"])
        sequences.setdefault(schema_name, []).append(
            DiscoveredSequence(
                name=str(row["sequence_name"]),
                data_type=_optional_row_text(row.get("data_type")),
                start_with=_optional_row_text(row.get("start_with")),
                increment_by=_optional_row_text(row.get("increment_by")),
                minimum_bound=_optional_row_text(row.get("minimum_bound")),
                maximum_bound=_optional_row_text(row.get("maximum_bound")),
                cache_size=_optional_row_text(row.get("cache_size")),
                cycles=_optional_row_bool(row.get("cycles")),
                owned_by_table=_optional_row_text(row.get("owned_by_table")),
                owned_by_column=_optional_row_text(row.get("owned_by_column")),
                source_description=_optional_row_text(row.get("description")),
            )
        )
    return sequences


def attach_native_objects(
    catalogs: tuple[DiscoveredCatalog, ...],
    *,
    triggers: TriggerMap | None = None,
    sequences: SequenceMap | None = None,
) -> tuple[DiscoveredCatalog, ...]:
    """`catalogs` with each schema's triggers and sequences attached.

    A `replace` pass over an already-assembled tree, the same shape the Oracle,
    Snowflake, BigQuery and Databricks adapters already use to fold their own
    per-dialect facts onto one (`envelope_v11_unavailable`, Databricks'
    comments). It is a pass rather than two more `assemble_catalog` parameters
    for the ownership reason recorded above this section.

    A schema that holds *only* triggers or sequences is added, exactly as
    `assemble_catalog` unions a routine-only schema in: a schema whose entire
    content is an audit trigger is a real schema, and dropping it would make the
    inventory silently incomplete for the estates where the axis matters most.
    """
    if not triggers and not sequences:
        return catalogs
    triggers = triggers or {}
    sequences = sequences or {}
    attached: list[DiscoveredCatalog] = []
    for index, catalog in enumerate(catalogs):
        seen = {schema.name for schema in catalog.schemas}
        schemas = [
            replace(
                schema,
                triggers=tuple(triggers.get(schema.name, ())),
                sequences=tuple(sequences.get(schema.name, ())),
            )
            for schema in catalog.schemas
        ]
        # Only the first catalog gains the schemas nothing else mentioned:
        # every adapter that reads these axes returns exactly one catalog, and
        # adding an unseen schema to each of several would duplicate it.
        if index == 0:
            for name in (*triggers, *sequences):
                if name in seen:
                    continue
                seen.add(name)
                schemas.append(
                    DiscoveredSchema(
                        name=name,
                        tables=(),
                        triggers=tuple(triggers.get(name, ())),
                        sequences=tuple(sequences.get(name, ())),
                    )
                )
        attached.append(replace(catalog, schemas=tuple(schemas)))
    return tuple(attached)


@dataclass(frozen=True, slots=True)
class QueryResult:
    rows: tuple[dict[str, Any], ...]
    warehouse_query_id: str | None


@dataclass(frozen=True, slots=True)
class QueryEstimate:
    score: float
    kind: str
    estimated_rows: float | None = None
    estimated_bytes: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# R11-FP04: observation scope, per-facet honesty, and value-free distribution
# shape.
#
# Three separate problems, solved here because all three are the connector's
# own knowledge and nothing above this layer can reconstruct them:
#
# 1. *How much of the table did this profile see?* Every consumer of a profile
#    statistic needs it (`relationship_validation.ProfileBounds`, which decides
#    whether a join's uniqueness evidence is sample-bounded, is the one that
#    actually acts on it). It used to be re-derived downstream by comparing
#    `sampled_row_count` with `row_count_estimate`, which is only a proxy: an
#    engine that reports the sample as the estimate looks like a full scan, and
#    an engine that scans fully but reports a nominal sample size looks
#    sampled. Only the connector knows whether it issued a bound, so it now
#    says so.
# 2. *Which facets could this engine produce for this column, and if not, why
#    not?* Following `DiscoveredViewDefinition`/`DiscoveredRoutine`'s
#    `unavailable_reason` convention above: unavailable and empty are different
#    states, and a reader must be able to tell "this engine cannot compute
#    lengths" from "this type has no text form" from "the source refused".
# 3. *Distribution shape without values.* ADR-0014/INV-6: a count is a
#    statistic about values, a bucket edge or an exemplar is a value. So the
#    bucket boundaries live here, in code, versioned by
#    `LENGTH_BUCKET_SCHEME`, and only the per-bucket counts are ever returned
#    or persisted.
# --------------------------------------------------------------------------

#: The profile aggregated every row of the table.
OBSERVATION_SCOPE_FULL = "FULL"
#: The profile aggregated a bounded subset; every statistic is sample-bounded.
OBSERVATION_SCOPE_SAMPLE = "SAMPLE"
#: Nothing was read (no columns, or the engine could not say), so no claim is made.
OBSERVATION_SCOPE_UNKNOWN = "UNKNOWN"

OBSERVATION_SCOPES = frozenset(
    {OBSERVATION_SCOPE_FULL, OBSERVATION_SCOPE_SAMPLE, OBSERVATION_SCOPE_UNKNOWN}
)


def bounded_scan_scope(*, sampled_row_count: int, sample_rows: int) -> str:
    """The observation scope of a profile whose scan carried a row bound.

    A bound that never bit means the aggregate did see the whole table, so
    `sampled_row_count < sample_rows` -- including the empty table, which is
    fully observed at zero rows -- is honestly FULL. A bound that filled up
    proves nothing about what lies past it: the table may have exactly
    `sample_rows` rows or ten million, and SAMPLE is the only claim the
    connector can support. Never the other way around, which is the R11-FP04
    defect this replaces.
    """
    if sample_rows < 1:
        return OBSERVATION_SCOPE_UNKNOWN
    if sampled_row_count < sample_rows:
        return OBSERVATION_SCOPE_FULL
    return OBSERVATION_SCOPE_SAMPLE


#: Facets a caller may ask about per column. Named so an engine can be honest
#: about one while producing another.
PROFILE_FACET_DISTINCT = "DISTINCT"
PROFILE_FACET_LENGTH = "LENGTH"
PROFILE_FACET_BLANKS = "BLANKS"
PROFILE_FACET_LENGTH_DISTRIBUTION = "LENGTH_DISTRIBUTION"
PROFILE_FACET_ENTROPY = "ENTROPY"
PROFILE_FACET_PATTERN_CLASS = "PATTERN_CLASS"
PROFILE_FACET_UNITS = "UNITS"

PROFILE_FACETS = frozenset(
    {
        PROFILE_FACET_DISTINCT,
        PROFILE_FACET_LENGTH,
        PROFILE_FACET_BLANKS,
        PROFILE_FACET_LENGTH_DISTRIBUTION,
        PROFILE_FACET_ENTROPY,
        PROFILE_FACET_PATTERN_CLASS,
        PROFILE_FACET_UNITS,
    }
)

#: The engine has no way to express this facet at all.
FACET_UNSUPPORTED = "UNSUPPORTED"
#: The engine could express it, but it means nothing for this column's type.
FACET_NOT_APPLICABLE = "NOT_APPLICABLE"
#: The source refused the read. Distinct from UNSUPPORTED: asking again with
#: different credentials could succeed.
FACET_PERMISSION_DENIED = "PERMISSION_DENIED"
#: Supported and applicable, but this run did not get it (timeout, transient error).
FACET_UNAVAILABLE = "UNAVAILABLE"

FACET_STATUSES = frozenset(
    {FACET_UNSUPPORTED, FACET_NOT_APPLICABLE, FACET_PERMISSION_DENIED, FACET_UNAVAILABLE}
)

#: Why a facet is missing, as a closed vocabulary rather than free text.
#:
#: INV-6, and the reason this is an enumeration instead of a `str` message:
#: the honest-sounding implementation of an unavailable reason is to pass the
#: driver's own error through, and a source driver's error text routinely
#: quotes the offending row (`workflows.activities` carries the same rule for
#: `analysis_run.error_message`). A code cannot carry a value. Anything not in
#: this set is replaced with `FACET_REASON_UNRECORDED` at persist time.
FACET_REASON_ENGINE_LACKS_FACET = "ENGINE_LACKS_FACET"
FACET_REASON_TYPE_HAS_NO_TEXT_FORM = "TYPE_HAS_NO_TEXT_FORM"
FACET_REASON_TYPE_IS_REPEATED = "TYPE_IS_REPEATED"
FACET_REASON_SOURCE_DENIED_READ = "SOURCE_DENIED_READ"
FACET_REASON_FACET_QUERY_FAILED = "FACET_QUERY_FAILED"
#: The engine could express it; this platform does not ask for it yet. Kept
#: distinct from ENGINE_LACKS_FACET so "we have not built it" is never read as
#: "your warehouse cannot do it".
FACET_REASON_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
#: The facet's only honest form would itself be a value (ADR-0014). A unit
#: inferred from data is a data-inferred string, not a statistic about one.
FACET_REASON_WOULD_CARRY_A_VALUE = "WOULD_CARRY_A_VALUE"
#: A reason arrived that is not in this vocabulary; the reason is dropped, the
#: fact that the facet is missing is kept.
FACET_REASON_UNRECORDED = "UNRECORDED"

FACET_REASON_CODES = frozenset(
    {
        FACET_REASON_ENGINE_LACKS_FACET,
        FACET_REASON_TYPE_HAS_NO_TEXT_FORM,
        FACET_REASON_TYPE_IS_REPEATED,
        FACET_REASON_SOURCE_DENIED_READ,
        FACET_REASON_FACET_QUERY_FAILED,
        FACET_REASON_NOT_IMPLEMENTED,
        FACET_REASON_WOULD_CARRY_A_VALUE,
        FACET_REASON_UNRECORDED,
    }
)


@dataclass(frozen=True, slots=True)
class ProfileFacetStatus:
    """One facet this engine could *not* produce for one column, and why.

    Only absent facets are reported: a facet with a populated statistic needs
    no status, and listing every present one would make the common case the
    verbose one. All three fields are closed vocabularies (`PROFILE_FACETS`,
    `FACET_STATUSES`, `FACET_REASON_CODES`) so nothing here can carry a source
    value (INV-6).
    """

    facet: str
    status: str
    reason_code: str


#: The FP-04 facets the value-free half does not compute at all, with the
#: reason each one is absent.
#:
#: Platform-wide and engine-independent, so these are *not* written onto every
#: `ColumnProfile` row -- a constant repeated once per column would be storage
#: spent on a fact that never varies. The read surface serves them alongside
#: each column's own per-engine statuses, so "Atlas did not compute pattern
#: evidence" and "this engine cannot compute lengths" reach a reader the same
#: way and are still distinguishable.
#:
#: Units are listed as NOT_APPLICABLE rather than unimplemented on purpose: a
#: unit inferred from the data is a data-inferred string, which ADR-0014 makes
#: a value. The value-free half can never carry one, however it is built --
#: only an owner-declared unit (FP-08's semantics, a different surface) can.
UNCOMPUTED_FACET_STATUS: tuple[ProfileFacetStatus, ...] = (
    ProfileFacetStatus(
        PROFILE_FACET_PATTERN_CLASS, FACET_UNSUPPORTED, FACET_REASON_NOT_IMPLEMENTED
    ),
    ProfileFacetStatus(PROFILE_FACET_UNITS, FACET_NOT_APPLICABLE, FACET_REASON_WOULD_CARRY_A_VALUE),
)


#: Length buckets, as `(low, high_inclusive_or_None)` over the column's text
#: length. R11-FP04/INV-6: the boundaries are code, versioned by the scheme
#: name below, and only the counts are returned and persisted -- a persisted
#: bucket edge is a value, and a reader that needs the edges reads them from
#: the scheme.
#:
#: The empty string is deliberately not a bucket: it is `blank_count`, which a
#: reader wants separately from "short".
LENGTH_BUCKET_SCHEME = "length-buckets-v1"
LENGTH_BUCKET_BOUNDS: tuple[tuple[int, int | None], ...] = (
    (1, 8),
    (9, 32),
    (33, 128),
    (129, 1024),
    (1025, None),
)


def value_free_distribution_expressions(
    *, position: int, text_form: str, length_form: str, trimmed_form: str
) -> list[str]:
    """The count-only distribution aggregates, in SQL every engine speaks.

    R11-FP04. Rides on whichever bounded scan the caller has already opened --
    these are more aggregates over the same rows, never a second sample, so a
    profile's counts and its distribution always describe the same observation.

    `SUM(CASE WHEN ...)` rather than `COUNT(*) FILTER (WHERE ...)`: the filter
    clause is the nicer spelling and two of the six engines do not have it, and
    a shared generator that is correct everywhere is worth more here than a
    per-dialect one that drifts.

    The three expressions are the engine's own, and are exactly the ones its
    existing min/max-length aggregates are already built from: `text_form` is
    the column rendered as text, `length_form` that text's length,
    `trimmed_form` that text with surrounding whitespace removed. Passing them
    in rather than composing them here is what keeps this helper from having a
    dialect table of its own.
    """
    expressions = [
        f"SUM(CASE WHEN {text_form} = '' THEN 1 ELSE 0 END) AS bl_{position}",
        f"SUM(CASE WHEN {text_form} <> '' AND {trimmed_form} = '' THEN 1 ELSE 0 END) "
        f"AS ws_{position}",
    ]
    for index, (low, high) in enumerate(LENGTH_BUCKET_BOUNDS):
        predicate = (
            f"{length_form} >= {low}"
            if high is None
            else f"{length_form} >= {low} AND {length_form} <= {high}"
        )
        expressions.append(f"SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) AS lb_{position}_{index}")
    return expressions


def null_distribution_expressions(*, position: int, null_literal: str) -> list[str]:
    """Typed NULL placeholders in place of `value_free_distribution_expressions`.

    For a column whose type has no text form at all -- an ARRAY, a STRUCT, a
    LOB -- where `CAST(col AS STRING)` is an error rather than a value. The
    aliases still have to exist or the batch's whole row shape changes per
    column, and this is the same "honest static placeholder" the existing
    length/distinct expressions already use for those types. `None` comes back
    from `read_value_free_distribution`, and the connector pairs it with a
    NOT_APPLICABLE `ProfileFacetStatus` so the absence has a reason.
    """
    aliases = [f"bl_{position}", f"ws_{position}"]
    aliases.extend(f"lb_{position}_{index}" for index in range(len(LENGTH_BUCKET_BOUNDS)))
    return [f"{null_literal} AS {alias}" for alias in aliases]


#: Entropy, as every engine but PostgreSQL reports it. UNSUPPORTED rather than
#: a silent None: `frequency_entropy_bits is None` also means "the column is
#: entirely null", and the two must not be the same answer.
ENTROPY_NOT_IMPLEMENTED = ProfileFacetStatus(
    PROFILE_FACET_ENTROPY, FACET_UNSUPPORTED, FACET_REASON_NOT_IMPLEMENTED
)


def text_facets_not_applicable(reason_code: str) -> tuple[ProfileFacetStatus, ...]:
    """Every facet that needs the column rendered as text, marked not applicable.

    One helper because the three engines with a placeholder branch (BigQuery's
    REPEATED and complex-scalar types, Oracle's LOBs) each withdraw exactly the
    same set, and a per-connector list would drift the moment a facet is added.
    """
    return tuple(
        ProfileFacetStatus(facet, FACET_NOT_APPLICABLE, reason_code)
        for facet in (
            PROFILE_FACET_LENGTH,
            PROFILE_FACET_BLANKS,
            PROFILE_FACET_LENGTH_DISTRIBUTION,
        )
    )


def read_value_free_distribution(
    position: int, read: Any
) -> tuple[int | None, int | None, tuple[int, ...] | None]:
    """Read back what `value_free_distribution_expressions` computed.

    `read(alias)` is the caller's own row accessor, because the six engines
    disagree about the case of a returned alias and about whether a missing one
    raises or answers None. Any alias that comes back None (or absent) makes
    the whole facet absent rather than zero: a zero count is a claim that the
    engine looked and found none, which is not what a missing column means.
    """

    def _count(alias: str) -> int | None:
        try:
            raw = read(alias)
        except (KeyError, IndexError):
            return None
        return None if raw is None else int(raw)

    blank = _count(f"bl_{position}")
    whitespace = _count(f"ws_{position}")
    buckets: list[int] = []
    for index in range(len(LENGTH_BUCKET_BOUNDS)):
        value = _count(f"lb_{position}_{index}")
        if value is None:
            return blank, whitespace, None
        buckets.append(value)
    return blank, whitespace, tuple(buckets)


@dataclass(frozen=True, slots=True)
class ColumnProfileSnapshot:
    name: str
    null_count: int
    non_null_count: int
    approximate_distinct_count: int
    min_length: int | None
    max_length: int | None
    # R11-FP04, all value-free and all optional so a connector that has not
    # implemented a facet keeps compiling and keeps reporting honestly (the
    # `views`/`routines` convention on `ConnectorCapabilities`). `None` means
    # "no claim"; `facet_status` says which of the four kinds of no-claim it
    # is. Note what is *not* here and must never be: a bucket edge, a mode, an
    # exemplar or a pattern literal (ADR-0014; `tests/test_inv6_value_freedom.py`
    # asserts it positively rather than by naming convention alone).
    blank_count: int | None = None
    whitespace_only_count: int | None = None
    length_bucket_counts: tuple[int, ...] | None = None
    frequency_entropy_bits: float | None = None
    facet_status: tuple[ProfileFacetStatus, ...] = ()


@dataclass(frozen=True, slots=True)
class TableProfileSnapshot:
    row_count_estimate: int | None
    sampled_row_count: int
    columns: tuple[ColumnProfileSnapshot, ...]
    # R11-FP04. Defaulted to UNKNOWN rather than required: an in-tree double or
    # a connector that has not been taught to say makes no claim, which is the
    # honest reading, instead of inheriting whichever of FULL/SAMPLE happened
    # to be the constructor's first argument.
    observation_scope: str = OBSERVATION_SCOPE_UNKNOWN


@dataclass(frozen=True, slots=True)
class ColumnValueProfileSnapshot:
    """PR-2: the value-bearing counterpart to `ColumnProfileSnapshot`.

    Only produced when a policy-approved classification-specific exception
    (`ProfilingExceptionPolicy`) is APPROVED for the column's classification
    *and* the connector's `capabilities.value_range_profiling` is True --
    everywhere else the platform only ever computes `ColumnProfileSnapshot`
    (ADR-0014). Every field here is real source data and is persisted only
    into a `ColumnValueProfileArtifact` with a retention/expiry pinned at
    capture time, never onto the value-free `ColumnProfile` row.
    """

    name: str
    min_value: str | None
    max_value: str | None
    # (value, count) pairs, most frequent first, bounded to the caller's `top_n`.
    top_values: tuple[tuple[str, int], ...] = ()


class ConnectorValueProfilingUnsupported(NotImplementedError):
    """Raised by the default `Connector.profile_column_values` implementation.

    A connector that has not implemented the value-bearing query path fails
    closed with this rather than silently returning an empty/simulated
    result -- callers must treat "unsupported" and "captured nothing" as
    distinguishable outcomes.
    """


class ConnectorQueryHistoryUnsupported(NotImplementedError):
    """Raised by the default `Connector.get_query_history` implementation.

    CN-9 / INV-9: a connector that has not implemented real warehouse
    query-history extraction fails closed with this rather than silently
    returning an empty sequence -- callers (and `capabilities.query_history`,
    which must independently be `True` before this is even attempted) must
    be able to tell "unsupported" apart from "the warehouse logged nothing
    in this window."
    """


class Connector(ABC):
    """Source access with structured arguments only.

    Deliberately has no SQL-accepting member: the `estimate_read_query` /
    `execute_read_query` pair lives on `aida.connectors.sql_execution.SqlExecutor`
    so that INV-2 (one execution choke point) is enforced by the type system and
    the import graph rather than by convention. See that module for the argument.
    """

    connector_type: str
    dialect: str

    @property
    @abstractmethod
    def capabilities(self) -> ConnectorCapabilities:
        raise NotImplementedError

    @abstractmethod
    async def test_connection(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        raise NotImplementedError

    def scope_discovery(self, *, include_schemas: list[str], exclude_schemas: list[str]) -> bool:
        """R11-FP01: take a selection's schema scope into the source's own metadata queries.

        Returns whether this connector does. The default does not, and correctness never
        depends on it: the selection is applied to whatever `discover` returns, before anything
        is persisted (`aida.discovery_selection.apply_selection`). A connector that overrides
        this reads less; it never reads differently (`aida.connectors.schema_scope`).
        """
        return False

    async def count_invisible_objects(self) -> dict[str, int] | None:
        """R11-FP02: how many objects in scope this login may not see, by kind -- or `None`.

        A source's metadata is itself permission-filtered. PostgreSQL's `information_schema`
        shows a role only what it holds some privilege on, so a run that read 40 tables of a
        400-table schema returns exactly what a complete run of a 40-table schema returns:
        every count in the receipt is right, and the estate it describes is a third of the
        real one. Where a connector can ask the source the unfiltered question -- for
        PostgreSQL, `pg_class` is readable by every role -- it answers here.

        `None` means the source cannot be asked, which is *not* zero: the receipt records it
        as UNKNOWN rather than as nothing hidden. Value-free, and deliberately counts only:
        the name of an object this login may not read is not ours to publish.
        """
        return None

    async def discover_streaming(
        self, *, batch_size: int = 500
    ) -> AsyncIterator[tuple[DiscoveredCatalog, ...]]:
        """CN-3/PR-5. Discovery as a sequence of bounded batches instead of one
        all-at-once return.

        Deliberately NOT `@abstractmethod`: a 100K-table source timing out
        `discover()` before it can return anything (and, downstream, before
        anything can be persisted -- see `discover_datasource`) is a real
        `PostgresConnector`-scale problem today; the other five connectors have
        no comparable scale harness exercising them, so forcing each to grow a
        real streaming implementation now would be unproven, unmotivated churn
        against passing connectors. `PostgresConnector` is the only override;
        every other connector inherits this default, which just wraps the
        existing `discover()` as a single batch -- zero behaviour change, zero
        risk to their existing tests. A caller that wants incremental
        persistence/heartbeating (`discover_datasource`) can drive any
        connector through this uniformly; `batch_size` is a hint a connector is
        free to ignore, as this default does.
        """
        yield await self.discover()

    @abstractmethod
    async def profile_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        *,
        sample_rows: int,
        column_batch_size: int,
        timeout_seconds: int,
    ) -> TableProfileSnapshot:
        raise NotImplementedError

    async def profile_column_values(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        *,
        sample_rows: int,
        top_n: int,
        timeout_seconds: int,
    ) -> tuple[ColumnValueProfileSnapshot, ...]:
        """PR-2: read actual ranges/top-values for `column_names`.

        Deliberately NOT `@abstractmethod` -- unlike `profile_table`, no
        connector is required to implement this. The default fails closed
        rather than every other connector subclass needing a no-op override:
        a connector that has not implemented the real value-bearing query
        must never silently claim support it lacks (INV-9-style honesty).
        Callers must gate a call here behind both an APPROVED, unrevoked
        `ProfilingExceptionPolicy` for the column's classification AND
        `self.capabilities.value_range_profiling` -- this method does not
        itself know about policy state.
        """
        raise ConnectorValueProfilingUnsupported(
            f"{type(self).__name__} does not support value-range profiling"
        )

    async def get_query_history(
        self,
        *,
        since: datetime,
        limit: int = 5_000,
        timeout_seconds: int = 30,
    ) -> tuple[QueryLogEntry, ...]:
        """CN-9: read this warehouse's own record of queries it has run,
        bounded to `limit` rows no older than `since`.

        Deliberately NOT `@abstractmethod`, the same shape as
        `profile_column_values`: most connectors have not implemented this
        yet, and the default must fail closed rather than every connector
        subclass needing a no-op override. Callers must gate a call here
        behind `self.capabilities.query_history` -- per module 02 §11 that
        flag is derived from a certification result, never hand-declared,
        so it stays `False` (INV-9) until a real end-to-end mining run has
        been proven against a live account, not merely until this method
        has been overridden.

        Returns entries in memory only. A connector implementation must
        read only the query's own SQL text and timing -- never the rows or
        bytes that query itself returned -- and a caller must never persist
        `entry.sql_text` verbatim to any table (INV-6); only a `query_id`
        reference and structure derived by parsing past every literal may
        land in platform state, exactly as `aida.query_history_miner`
        already does for every candidate it produces.
        """
        raise ConnectorQueryHistoryUnsupported(
            f"{type(self).__name__} does not support query-history extraction"
        )
