"""R11-FP01: which objects a datasource's discovery takes in, and what it may retire.

A selection narrows discovery by native object kind, schema and qualified object name. It is
applied to what the connector returns, before anything is persisted, so it governs every
registered connector identically. Pushing it into each source's own metadata queries is a
later optimisation; correctness does not depend on it.

**The hazard a selection must not create.** A FULL run retires every existing object it did
not see (`workflows.activities._deprecate_missing`,
`ingestion.deprecate_missing_envelope_extensions`). An object outside the selection was not
*looked for*, so it is not missing: the discovery activity counts existing out-of-scope
objects as seen before that pass runs (`workflows.activities.out_of_scope_existing`).
Narrowing a selection stops maintaining an object; it never retires one.

**Matching** is case-insensitive `fnmatch` -- identifier case differs by engine (PostgreSQL
folds to lower case, Oracle and Snowflake to upper), and a scope written as `sales` must name
the same schema on both. Schema patterns match the schema name; object patterns match
`schema.object`. Excludes win over includes. An empty selection is unrestricted and has no
fingerprint, so a datasource that never set one behaves exactly as before.

**The preview is a count over the last completed scan**, labelled as such (`basis`). It cannot
see an object the source holds but Atlas has never discovered, and it does not dial the source:
a live preview would need credentials, a network path and a bounded source query, which is a
separate decision.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.connectors.base import DiscoveredCatalog, DiscoveredGrant, DiscoveredRoutine
from aida.connectors.registry import connector_registry
from aida.envelope_models import MetadataRoutine, MetadataSequence, MetadataTrigger
from aida.models import DataSource, MetadataCatalog, MetadataSchema, MetadataTable

ObjectKind = Literal[
    "TABLE",
    "VIEW",
    "MATERIALIZED_VIEW",
    "PROCEDURE",
    "FUNCTION",
    "PACKAGE",
    "TRIGGER",
    "SEQUENCE",
]
OBJECT_KINDS: tuple[ObjectKind, ...] = (
    "TABLE",
    "VIEW",
    "MATERIALIZED_VIEW",
    "PROCEDURE",
    "FUNCTION",
    # R11-FP03: an Oracle package is its own kind, never a function in disguise.
    "PACKAGE",
    # R11-FP01: a trigger is its own kind for the same reason -- it is not
    # called but fires, on a table, for an event, at a time -- and a sequence is
    # its own kind because it holds no rows and is read by somebody else's
    # default expression. Both are selectable so that a deployment that does not
    # want them reads NOT_SELECTED (below) rather than UNSUPPORTED: "the scan
    # excluded it" is neither a missing feature nor a missing concept.
    "TRIGGER",
    "SEQUENCE",
)
#: Review 2026-09-16 §5 widened this from SUPPORTED / UNSUPPORTED /
#: NOT_APPLICABLE onto the shared vocabulary in `aida.capability_states`, so a
#: kind this scan's own selection leaves out reads `NOT_SELECTED` instead of
#: claiming the support the adapter would have had. Every previously-valid
#: value still means exactly what it did: this is an enum *widening*, which the
#: OpenAPI gate classifies as non-breaking for a response field, and no
#: existing caller sees a value change for a selection it was already sending.
#:
#: Only the states a *capability read* can honestly answer are listed.
#: `PERMISSION_DENIED`, `UNAVAILABLE` and `TRUNCATED` are outcomes of one read
#: with one login, not properties of the adapter, and belong on the discovery
#: receipt; `UNRESOLVED` is a property of a parsed fact.
CapabilityStatus = Literal[
    "SUPPORTED", "PARTIAL", "UNSUPPORTED", "NOT_APPLICABLE", "NOT_SELECTED"
]

MAX_PATTERNS = 100
#: Upper bound on the catalog rows one preview reads; beyond it the counts are partial and
#: the response says so.
PREVIEW_OBJECT_LIMIT = 200_000

Pattern = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

#: Connectors that report a materialized view as its own object kind. SQL Server has no such
#: kind -- an indexed view is reported as a VIEW with `is_materialized` -- and the Oracle and
#: Databricks adapters do not discover one as a distinct kind today.
_MATERIALIZED_VIEW_CONNECTORS = frozenset({"postgres", "snowflake", "bigquery"})
_NO_MATERIALIZED_VIEW_KIND = frozenset({"sqlserver"})
_CONTAINER_GRANT_TYPES = frozenset({"SCHEMA", "DATABASE", "CATALOG"})
_ROUTINE_GRANT_TYPES = frozenset({"PROCEDURE", "FUNCTION", "PACKAGE"})
#: Engines with a package object. Everywhere else a PACKAGE kind is NOT_APPLICABLE.
_PACKAGE_CONNECTORS = frozenset({"oracle"})

# R11-FP01: which engines have a trigger or a sequence *at all*, which is the
# fact that separates NOT_APPLICABLE from UNSUPPORTED. Engine knowledge, not a
# fact in this repository, and deliberately listed as the negative set: a
# connector absent from both sets has the concept and the flag answers whether
# the adapter reads it, so a seventh adapter arrives as UNSUPPORTED (honest)
# rather than as NOT_APPLICABLE (a claim about its SQL nobody made).
#
# Each engine gets its own answer rather than one blanket verdict:
#   Snowflake -- has sequences (CREATE SEQUENCE, INFORMATION_SCHEMA.SEQUENCES);
#                has no trigger object of any kind. Streams plus tasks are how
#                the same intent is expressed, and they are neither triggers nor
#                in scope here.
#   BigQuery   -- has neither. There is no CREATE TRIGGER, and nothing named a
#                sequence: GENERATE_UUID / GENERATE_ARRAY are functions.
#   Databricks -- has neither. Unity Catalog has no trigger and no sequence;
#                Delta's generated columns and `IDENTITY` are column properties
#                of the table, not a separate object with its own increment.
_NO_TRIGGER_KIND = frozenset({"snowflake", "bigquery", "databricks"})
_NO_SEQUENCE_KIND = frozenset({"bigquery", "databricks"})
#: Engines whose trigger carries its own text (Oracle `ALL_TRIGGERS.TRIGGER_BODY`,
#: SQL Server `sys.sql_modules.definition`). PostgreSQL's does not: the action is
#: `EXECUTE FUNCTION f()` and `f`'s body arrives on the routine axis, so its
#: definition facet is PARTIAL rather than SUPPORTED -- see `kind_capabilities`.
_TRIGGER_BODY_CONNECTORS = frozenset({"oracle", "sqlserver"})


class DiscoverySelection(BaseModel):
    """What discovery takes in. Every list empty means unrestricted."""

    model_config = ConfigDict(extra="forbid")

    object_kinds: list[ObjectKind] = Field(
        default_factory=list,
        max_length=len(OBJECT_KINDS),
        description="Kinds to discover; empty discovers every kind the connector reports.",
    )
    include_schemas: list[Pattern] = Field(default_factory=list, max_length=MAX_PATTERNS)
    exclude_schemas: list[Pattern] = Field(default_factory=list, max_length=MAX_PATTERNS)
    include_objects: list[Pattern] = Field(
        default_factory=list,
        max_length=MAX_PATTERNS,
        description="`schema.object` patterns, for example `sales.fact_*`.",
    )
    exclude_objects: list[Pattern] = Field(default_factory=list, max_length=MAX_PATTERNS)

    @field_validator(
        "object_kinds", "include_schemas", "exclude_schemas", "include_objects", "exclude_objects"
    )
    @classmethod
    def _without_duplicates(cls, values: list[str]) -> list[str]:
        unique: dict[str, str] = {}
        for value in values:
            if any(ord(character) < 32 for character in value):
                raise ValueError("a pattern may not contain control characters")
            unique.setdefault(value.lower(), value)
        return list(unique.values())

    @property
    def restricted(self) -> bool:
        return bool(
            self.object_kinds
            or self.include_schemas
            or self.exclude_schemas
            or self.include_objects
            or self.exclude_objects
        )

    def fingerprint(self) -> str | None:
        """Stable over order and case, so a preview and the run it precedes can be matched."""
        if not self.restricted:
            return None
        canonical = {
            "object_kinds": sorted(self.object_kinds),
            "include_schemas": sorted(value.lower() for value in self.include_schemas),
            "exclude_schemas": sorted(value.lower() for value in self.exclude_schemas),
            "include_objects": sorted(value.lower() for value in self.include_objects),
            "exclude_objects": sorted(value.lower() for value in self.exclude_objects),
        }
        payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def schema_in_scope(self, schema: str) -> bool:
        name = schema.lower()
        if self.include_schemas and not _matches(name, self.include_schemas):
            return False
        return not _matches(name, self.exclude_schemas)

    def object_in_scope(self, schema: str, name: str, kind: str) -> bool:
        if not self.schema_in_scope(schema):
            return False
        if self.object_kinds and kind not in self.object_kinds:
            return False
        qualified = f"{schema}.{name}".lower()
        if self.include_objects and not _matches(qualified, self.include_objects):
            return False
        return not _matches(qualified, self.exclude_objects)


def _matches(value: str, patterns: Iterable[str]) -> bool:
    return any(fnmatchcase(value, pattern.lower()) for pattern in patterns)


def selection_for(datasource: DataSource) -> DiscoverySelection:
    return DiscoverySelection.model_validate(datasource.discovery_selection or {})


def table_kind(object_type: str) -> str:
    """`MetadataTable.object_type` as a selection kind; every table-like type is TABLE."""
    normalized = object_type.strip().replace(" ", "_").upper()
    return normalized if normalized in {"VIEW", "MATERIALIZED_VIEW"} else "TABLE"


def routine_kind(routine_type: str) -> str:
    """PROCEDURE, FUNCTION or PACKAGE. A native function subtype -- BigQuery's
    `SCALAR_FUNCTION`, `TABLE_FUNCTION` -- is a FUNCTION (R11-FP03): kept as its raw name it
    matched no selectable kind, so any restricted selection silently dropped it. Anything
    else keeps its own name and is excluded rather than miscounted."""
    normalized = routine_type.strip().replace(" ", "_").upper()
    if normalized.endswith("_FUNCTION"):
        return "FUNCTION"
    return normalized


def routine_in_scope(
    selection: DiscoverySelection,
    schema: str,
    name: str,
    routine_type: str,
    package_name: str | None = None,
) -> bool:
    """R11-FP03: whether a routine is in scope -- an Oracle package member by its package.

    **The contract.** A member subprogram of an Oracle package enters and leaves the scan
    with its package: it is judged as kind PACKAGE under the name `schema.package`, never by
    its own name or kind. A selection that includes the package -- by `schema.object`
    pattern or by the PACKAGE kind -- reaches every member; one that excludes the package
    excludes every member; and no pattern selects a member apart from its package.

    **Why the package and not the member.** A member has nothing of its own a scan could
    maintain separately: its source is the package's (`oracle._append_package_members`
    records it absent with that reason), EXECUTE is granted on the package, and lineage
    parses the package body as one. Scoped by its own name, `include_objects=["hr.risk_pkg"]`
    kept the package and silently dropped every member -- the selection reached the
    container and none of its contents -- and `object_kinds=["PACKAGE"]` did the same.

    A standalone routine (`package_name` empty) is judged by its own name and kind exactly
    as before. See `apply_selection` for the one half of this contract not yet wired.
    """
    if package_name:
        return selection.object_in_scope(schema, package_name, "PACKAGE")
    return selection.object_in_scope(schema, name, routine_kind(routine_type))


def _routine_kept(
    selection: DiscoverySelection,
    schema: str,
    name: str,
    routine_type: str,
    package_name: str | None,
) -> bool:
    """The routine rule `apply_selection` and the preview apply today.

    `routine_in_scope`, widened by the member's *own* rule -- and the widening is the
    retirement half of the contract, deliberately not closed here. A FULL run retires
    every existing routine it did not see unless `workflows.activities.
    out_of_scope_existing` counts it as out of scope, and that function still judges a
    member by its own name and kind (`selection.object_in_scope(schema, name, kind)`).
    So this rule may only ever drop a routine that one also counts as out of scope:
    admitting more is safe (the member is simply seen), dropping more is not -- with
    `exclude_objects=["hr.risk_pkg"]` the member `hr.score` would be left out of the scan
    by this rule and judged *in* scope by that one, and tombstoned. The include half of
    the contract (a package reaches its members) is therefore live; the exclude half (an
    excluded package takes its members with it) waits for `out_of_scope_existing` to call
    `routine_in_scope`, at which point the `or` below is deleted in the same change.
    `tests/test_package_member_selection.py` pins the safety property either way.
    """
    return routine_in_scope(selection, schema, name, routine_type, package_name)


def _package_of(routine: DiscoveredRoutine) -> str | None:
    """The package a discovered routine is a member of, from the reserved attribute."""
    package = routine.attributes.get("package_name")
    return str(package) if package else None


def grant_in_scope(
    selection: DiscoverySelection, schema: str, object_type: str, object_name: str
) -> bool:
    """A grant follows the object it is on; a schema-level grant follows its schema."""
    normalized = object_type.strip().replace(" ", "_").upper()
    if normalized in _CONTAINER_GRANT_TYPES:
        return selection.schema_in_scope(schema)
    # R11-FP03: a grant on a package follows the package -- read as a table kind, it was
    # scoped as TABLE and kept or dropped with the tables.
    kind = routine_kind(normalized) if normalized in _ROUTINE_GRANT_TYPES else table_kind(
        normalized
    )
    return selection.object_in_scope(schema, object_name, kind)


def _grant_in_scope(selection: DiscoverySelection, schema: str, grant: DiscoveredGrant) -> bool:
    return grant_in_scope(
        selection, grant.schema_name or schema, grant.object_type, grant.object_name
    )


@dataclass(frozen=True, slots=True)
class SelectionOutcome:
    catalogs: tuple[DiscoveredCatalog, ...]
    #: Objects the source returned that the selection left out, by kind (`SCHEMA` included).
    excluded: dict[str, int]

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded.values())


def apply_selection(
    catalogs: tuple[DiscoveredCatalog, ...], selection: DiscoverySelection
) -> SelectionOutcome:
    """Drop what the selection does not cover, before anything is persisted."""
    if not selection.restricted:
        return SelectionOutcome(catalogs, {})
    excluded: Counter[str] = Counter()
    kept_catalogs = []
    for catalog in catalogs:
        kept_schemas = []
        for schema in catalog.schemas:
            if not selection.schema_in_scope(schema.name):
                excluded["SCHEMA"] += 1
                excluded.update(table_kind(table.object_type) for table in schema.tables)
                excluded.update(routine_kind(routine.routine_type) for routine in schema.routines)
                # R11-FP01: counted by kind like everything else, so an
                # excluded schema's triggers and sequences appear in the
                # receipt's excluded tally rather than vanishing. Counted
                # through `update` rather than `+= len(...)`: on a `Counter`
                # the latter creates a zero entry, and a kind reported as
                # "0 excluded" reads as a kind that was looked at.
                excluded.update({"TRIGGER": len(schema.triggers)} if schema.triggers else {})
                excluded.update({"SEQUENCE": len(schema.sequences)} if schema.sequences else {})
                continue
            tables = []
            for table in schema.tables:
                kind = table_kind(table.object_type)
                if selection.object_in_scope(schema.name, table.name, kind):
                    tables.append(table)
                else:
                    excluded[kind] += 1
            routines = []
            for routine in schema.routines:
                kind = routine_kind(routine.routine_type)
                # R11-FP03: a package member follows its package in (see `_routine_kept`
                # for the half of the contract that is not wired yet, and why).
                if _routine_kept(
                    selection, schema.name, routine.name, routine.routine_type, _package_of(routine)
                ):
                    routines.append(routine)
                else:
                    excluded[kind] += 1
            # R11-FP01: a trigger is scoped by its own qualified name, not by
            # its firing table's. A selection that excludes `staging.*` must
            # stop maintaining the triggers *in* `staging`; whether one of them
            # fires on an in-scope table is a lineage fact, and using it as the
            # scope would silently re-admit an object the operator excluded.
            triggers = []
            for trigger in schema.triggers:
                if selection.object_in_scope(schema.name, trigger.name, "TRIGGER"):
                    triggers.append(trigger)
                else:
                    excluded["TRIGGER"] += 1
            sequences = []
            for sequence in schema.sequences:
                if selection.object_in_scope(schema.name, sequence.name, "SEQUENCE"):
                    sequences.append(sequence)
                else:
                    excluded["SEQUENCE"] += 1
            grants = tuple(
                grant for grant in schema.grants if _grant_in_scope(selection, schema.name, grant)
            )
            kept_schemas.append(
                replace(
                    schema,
                    tables=tuple(tables),
                    routines=tuple(routines),
                    triggers=tuple(triggers),
                    sequences=tuple(sequences),
                    grants=grants,
                )
            )
        kept_catalogs.append(replace(catalog, schemas=tuple(kept_schemas)))
    return SelectionOutcome(tuple(kept_catalogs), dict(excluded))


class ObjectKindCapabilityRead(BaseModel):
    kind: ObjectKind
    inventory: CapabilityStatus
    definition: CapabilityStatus


class DiscoverySelectionRead(BaseModel):
    datasource_id: UUID
    selection: DiscoverySelection
    restricted: bool
    fingerprint: str | None
    capabilities: list[ObjectKindCapabilityRead]
    capability_source: Literal["CONNECTION_TEST", "CONNECTOR_DEFAULT"]


class SelectionCountRead(BaseModel):
    kind: str
    in_scope: int
    excluded: int


class DiscoverySelectionPreviewRead(BaseModel):
    datasource_id: UUID
    restricted: bool
    fingerprint: str | None
    basis: Literal["LAST_SCAN"] = "LAST_SCAN"
    schemas: SelectionCountRead
    kinds: list[SelectionCountRead]
    unmatched_include_patterns: list[str]
    truncated: bool
    capabilities: list[ObjectKindCapabilityRead]
    capability_source: Literal["CONNECTION_TEST", "CONNECTOR_DEFAULT"]


def _capabilities_of(datasource: DataSource) -> tuple[dict[str, Any], str]:
    if datasource.capabilities:
        return datasource.capabilities, "CONNECTION_TEST"
    try:
        return connector_registry.definition(datasource.connector_type).capabilities, (
            "CONNECTOR_DEFAULT"
        )
    except (KeyError, ValueError):
        return {}, "CONNECTOR_DEFAULT"


def kind_capabilities(
    connector_type: str,
    capabilities: dict[str, Any],
    selection: DiscoverySelection | None = None,
) -> list[ObjectKindCapabilityRead]:
    """Per kind, whether the connector inventories it and captures its definition.

    Derived from the connector's own capability flags, which are honest by INV-9 (a flag is
    False until the axis is implemented); `NOT_APPLICABLE` is a native concept the engine
    does not have, which is a different answer from `UNSUPPORTED`.

    Review 2026-09-16 §5: when `selection` restricts the object kinds, a kind it leaves out
    reads `NOT_SELECTED` rather than the support the adapter would otherwise have had. That
    is the third distinct answer the design target asks for -- "the scan intentionally
    excluded it" is neither a missing feature nor a missing concept, and a panel that
    showed SUPPORTED for a kind the next run will not read would be telling the truth
    about the adapter and the wrong thing about this source.

    The state is *masked*, never overwritten: a kind that is NOT_APPLICABLE or UNSUPPORTED
    stays so, because an engine without packages does not gain one by being excluded, and
    excluding an axis the adapter cannot read is not what kept it out.
    """
    views: CapabilityStatus = "SUPPORTED" if capabilities.get("views") else "UNSUPPORTED"
    routines: CapabilityStatus = "SUPPORTED" if capabilities.get("routines") else "UNSUPPORTED"
    materialized: CapabilityStatus = (
        "SUPPORTED" if connector_type in _MATERIALIZED_VIEW_CONNECTORS
        else "NOT_APPLICABLE" if connector_type in _NO_MATERIALIZED_VIEW_KIND
        else "UNSUPPORTED"
    )
    reads = [
        ObjectKindCapabilityRead(kind="TABLE", inventory="SUPPORTED", definition="NOT_APPLICABLE"),
        ObjectKindCapabilityRead(kind="VIEW", inventory="SUPPORTED", definition=views),
        ObjectKindCapabilityRead(
            kind="MATERIALIZED_VIEW",
            inventory=materialized,
            definition=views if materialized == "SUPPORTED" else materialized,
        ),
        ObjectKindCapabilityRead(kind="PROCEDURE", inventory=routines, definition=routines),
        ObjectKindCapabilityRead(kind="FUNCTION", inventory=routines, definition=routines),
        ObjectKindCapabilityRead(
            kind="PACKAGE",
            inventory=routines if connector_type in _PACKAGE_CONNECTORS else "NOT_APPLICABLE",
            definition=routines if connector_type in _PACKAGE_CONNECTORS else "NOT_APPLICABLE",
        ),
        # R11-FP01. `triggers` and `sequences` are the adapter's own flags, so
        # INV-9 holds -- neither is True until the adapter reads the kind -- and
        # the engine's own lack of the concept outranks the flag, because an
        # engine without triggers does not gain one by an adapter implementing
        # the axis.
        ObjectKindCapabilityRead(
            kind="TRIGGER",
            inventory=_trigger_inventory(connector_type, capabilities),
            definition=_trigger_definition(connector_type, capabilities),
        ),
        ObjectKindCapabilityRead(
            kind="SEQUENCE",
            inventory=_sequence_inventory(connector_type, capabilities),
            # A sequence has no defining text to retrieve. Its declaration --
            # increment, bounds, cache, cycle -- *is* the inventory, exactly as
            # a base relation's columns are the fact rather than a stored
            # `CREATE TABLE` statement, so this facet is NOT_APPLICABLE on every
            # engine (including the two that have no sequence at all, where it
            # is NOT_APPLICABLE for the stronger reason).
            definition="NOT_APPLICABLE",
        ),
    ]
    if selection is None or not selection.object_kinds:
        return reads
    excluded = {kind for kind in OBJECT_KINDS if kind not in selection.object_kinds}
    return [
        _masked_for_selection(read) if read.kind in excluded else read for read in reads
    ]


def _trigger_inventory(connector_type: str, capabilities: dict[str, Any]) -> CapabilityStatus:
    if connector_type in _NO_TRIGGER_KIND:
        return "NOT_APPLICABLE"
    return "SUPPORTED" if capabilities.get("triggers") else "UNSUPPORTED"


def _trigger_definition(connector_type: str, capabilities: dict[str, Any]) -> CapabilityStatus:
    """Whether the trigger's own code text is retrievable, per engine.

    PostgreSQL is `PARTIAL` and it is not a shortfall in the adapter: a
    PostgreSQL trigger has no body. `CREATE TRIGGER ... EXECUTE FUNCTION f()`
    names a function whose body is captured on the routine axis, and the adapter
    records that name (`DiscoveredTrigger.action_routine`) so the code is
    reachable -- but there is no trigger-local text to return, and the `WHEN`
    condition is not captured either. `PARTIAL` is what the vocabulary has for
    "implemented, and known to return only part of the fact"; `SUPPORTED` would
    claim a body that does not exist and `UNSUPPORTED` would blame the adapter
    for the engine's design.
    """
    inventory = _trigger_inventory(connector_type, capabilities)
    if inventory != "SUPPORTED":
        return inventory
    return "SUPPORTED" if connector_type in _TRIGGER_BODY_CONNECTORS else "PARTIAL"


def _sequence_inventory(connector_type: str, capabilities: dict[str, Any]) -> CapabilityStatus:
    if connector_type in _NO_SEQUENCE_KIND:
        return "NOT_APPLICABLE"
    return "SUPPORTED" if capabilities.get("sequences") else "UNSUPPORTED"


def _masked_for_selection(read: ObjectKindCapabilityRead) -> ObjectKindCapabilityRead:
    """`read` with each facet the selection kept out marked `NOT_SELECTED`.

    A facet already answering NOT_APPLICABLE or UNSUPPORTED keeps its answer: the reason it
    is not being read is the engine or the adapter, not the selection, and overwriting it
    would lose the more specific fact.
    """
    state: CapabilityStatus = "NOT_SELECTED"
    maskable: frozenset[str] = frozenset({"SUPPORTED", "PARTIAL"})
    return ObjectKindCapabilityRead(
        kind=read.kind,
        inventory=state if read.inventory in maskable else read.inventory,
        definition=state if read.definition in maskable else read.definition,
    )


def selection_read(datasource: DataSource) -> DiscoverySelectionRead:
    selection = selection_for(datasource)
    capabilities, source = _capabilities_of(datasource)
    return DiscoverySelectionRead(
        datasource_id=datasource.id,
        selection=selection,
        restricted=selection.restricted,
        fingerprint=selection.fingerprint(),
        capabilities=kind_capabilities(datasource.connector_type, capabilities, selection),
        capability_source=source,
    )


async def preview_selection(
    session: AsyncSession, datasource: DataSource, selection: DiscoverySelection
) -> DiscoverySelectionPreviewRead:
    """Counts of what `selection` would keep and leave out, over the last completed scan."""
    schema_names = (
        await session.scalars(
            select(MetadataSchema.name)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataCatalog.datasource_id == datasource.id, MetadataSchema.status == "ACTIVE"
            )
            .limit(PREVIEW_OBJECT_LIMIT + 1)
        )
    ).all()
    tables = (
        await session.execute(
            select(MetadataSchema.name, MetadataTable.name, MetadataTable.object_type)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(MetadataTable.datasource_id == datasource.id, MetadataTable.status == "ACTIVE")
            .limit(PREVIEW_OBJECT_LIMIT + 1)
        )
    ).all()
    routines = (
        await session.execute(
            select(
                MetadataSchema.name,
                MetadataRoutine.name,
                MetadataRoutine.routine_type,
                # R11-FP03: a member is previewed by the rule the run applies to it.
                MetadataRoutine.package_name,
            )
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .where(
                MetadataRoutine.datasource_id == datasource.id, MetadataRoutine.status == "ACTIVE"
            )
            .limit(PREVIEW_OBJECT_LIMIT + 1)
        )
    ).all()
    # R11-FP01: the two new kinds are counted from their own tables, with
    # `organization_id` restated beside `datasource_id` in each predicate
    # (INV-5) exactly as the envelope queries above do. A source scanned before
    # these axes existed returns nothing here, which reads as 0 in scope and 0
    # excluded -- the honest answer for a kind the last scan never looked for.
    triggers = (
        await session.execute(
            select(MetadataSchema.name, MetadataTrigger.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTrigger.schema_id)
            .where(
                MetadataTrigger.organization_id == datasource.organization_id,
                MetadataTrigger.datasource_id == datasource.id,
                MetadataTrigger.status == "ACTIVE",
            )
            .limit(PREVIEW_OBJECT_LIMIT + 1)
        )
    ).all()
    sequences = (
        await session.execute(
            select(MetadataSchema.name, MetadataSequence.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataSequence.schema_id)
            .where(
                MetadataSequence.organization_id == datasource.organization_id,
                MetadataSequence.datasource_id == datasource.id,
                MetadataSequence.status == "ACTIVE",
            )
            .limit(PREVIEW_OBJECT_LIMIT + 1)
        )
    ).all()
    truncated = any(
        len(rows) > PREVIEW_OBJECT_LIMIT
        for rows in (schema_names, tables, routines, triggers, sequences)
    )

    matched_patterns: set[str] = set()
    schema_count = SelectionCountRead(kind="SCHEMA", in_scope=0, excluded=0)
    for name in schema_names[:PREVIEW_OBJECT_LIMIT]:
        matched_patterns.update(
            pattern
            for pattern in selection.include_schemas
            if fnmatchcase(name.lower(), pattern.lower())
        )
        if selection.schema_in_scope(name):
            schema_count.in_scope += 1
        else:
            schema_count.excluded += 1

    counts: dict[str, SelectionCountRead] = {
        kind: SelectionCountRead(kind=kind, in_scope=0, excluded=0) for kind in OBJECT_KINDS
    }
    # The fourth element is the package a routine is a member of ("" for a standalone
    # routine, None for every other kind), so a member is counted by the same rule the
    # run applies to it (`_routine_kept`).
    objects: list[tuple[str, str, str, str | None]] = [
        (schema, name, table_kind(object_type), None)
        for schema, name, object_type in tables[:PREVIEW_OBJECT_LIMIT]
    ] + [
        (schema, name, routine_kind(routine_type), package or "")
        for schema, name, routine_type, package in routines[:PREVIEW_OBJECT_LIMIT]
    ] + [
        (schema, name, "TRIGGER", None) for schema, name in triggers[:PREVIEW_OBJECT_LIMIT]
    ] + [
        (schema, name, "SEQUENCE", None) for schema, name in sequences[:PREVIEW_OBJECT_LIMIT]
    ]
    for schema, name, kind, package in objects:
        qualified = f"{schema}.{name}".lower()
        matched_patterns.update(
            pattern
            for pattern in selection.include_objects
            if fnmatchcase(qualified, pattern.lower())
        )
        count = counts.setdefault(kind, SelectionCountRead(kind=kind, in_scope=0, excluded=0))
        in_scope = (
            selection.object_in_scope(schema, name, kind)
            if package is None
            else _routine_kept(selection, schema, name, kind, package or None)
        )
        if in_scope:
            count.in_scope += 1
        else:
            count.excluded += 1

    capabilities, source = _capabilities_of(datasource)
    return DiscoverySelectionPreviewRead(
        datasource_id=datasource.id,
        restricted=selection.restricted,
        fingerprint=selection.fingerprint(),
        schemas=schema_count,
        kinds=list(counts.values()),
        unmatched_include_patterns=[
            pattern
            for pattern in [*selection.include_schemas, *selection.include_objects]
            if pattern not in matched_patterns
        ],
        truncated=truncated,
        # The previewed selection, not the stored one: the panel is asking what
        # *this* selection would do, so a kind it drops must read NOT_SELECTED
        # here even while the source's current selection still reads it.
        capabilities=kind_capabilities(datasource.connector_type, capabilities, selection),
        capability_source=source,
    )
