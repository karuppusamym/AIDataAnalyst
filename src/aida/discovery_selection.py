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

from aida.connectors.base import DiscoveredCatalog, DiscoveredGrant
from aida.connectors.registry import connector_registry
from aida.envelope_models import MetadataRoutine
from aida.models import DataSource, MetadataCatalog, MetadataSchema, MetadataTable

ObjectKind = Literal["TABLE", "VIEW", "MATERIALIZED_VIEW", "PROCEDURE", "FUNCTION"]
OBJECT_KINDS: tuple[ObjectKind, ...] = (
    "TABLE",
    "VIEW",
    "MATERIALIZED_VIEW",
    "PROCEDURE",
    "FUNCTION",
)
CapabilityStatus = Literal["SUPPORTED", "UNSUPPORTED", "NOT_APPLICABLE"]

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
    """PROCEDURE or FUNCTION; any other native type (an Oracle PACKAGE) keeps its own name,
    so a selection that lists kinds excludes it rather than miscounting it as a function."""
    return routine_type.strip().upper()


def grant_in_scope(
    selection: DiscoverySelection, schema: str, object_type: str, object_name: str
) -> bool:
    """A grant follows the object it is on; a schema-level grant follows its schema."""
    normalized = object_type.strip().replace(" ", "_").upper()
    if normalized in _CONTAINER_GRANT_TYPES:
        return selection.schema_in_scope(schema)
    kind = routine_kind(normalized) if normalized in {"PROCEDURE", "FUNCTION"} else table_kind(
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
                if selection.object_in_scope(schema.name, routine.name, kind):
                    routines.append(routine)
                else:
                    excluded[kind] += 1
            grants = tuple(
                grant for grant in schema.grants if _grant_in_scope(selection, schema.name, grant)
            )
            kept_schemas.append(
                replace(schema, tables=tuple(tables), routines=tuple(routines), grants=grants)
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
    connector_type: str, capabilities: dict[str, Any]
) -> list[ObjectKindCapabilityRead]:
    """Per kind, whether the connector inventories it and captures its definition.

    Derived from the connector's own capability flags, which are honest by INV-9 (a flag is
    False until the axis is implemented); `NOT_APPLICABLE` is a native concept the engine
    does not have, which is a different answer from `UNSUPPORTED`.
    """
    views: CapabilityStatus = "SUPPORTED" if capabilities.get("views") else "UNSUPPORTED"
    routines: CapabilityStatus = "SUPPORTED" if capabilities.get("routines") else "UNSUPPORTED"
    materialized: CapabilityStatus = (
        "SUPPORTED" if connector_type in _MATERIALIZED_VIEW_CONNECTORS
        else "NOT_APPLICABLE" if connector_type in _NO_MATERIALIZED_VIEW_KIND
        else "UNSUPPORTED"
    )
    return [
        ObjectKindCapabilityRead(kind="TABLE", inventory="SUPPORTED", definition="NOT_APPLICABLE"),
        ObjectKindCapabilityRead(kind="VIEW", inventory="SUPPORTED", definition=views),
        ObjectKindCapabilityRead(
            kind="MATERIALIZED_VIEW",
            inventory=materialized,
            definition=views if materialized == "SUPPORTED" else materialized,
        ),
        ObjectKindCapabilityRead(kind="PROCEDURE", inventory=routines, definition=routines),
        ObjectKindCapabilityRead(kind="FUNCTION", inventory=routines, definition=routines),
    ]


def selection_read(datasource: DataSource) -> DiscoverySelectionRead:
    selection = selection_for(datasource)
    capabilities, source = _capabilities_of(datasource)
    return DiscoverySelectionRead(
        datasource_id=datasource.id,
        selection=selection,
        restricted=selection.restricted,
        fingerprint=selection.fingerprint(),
        capabilities=kind_capabilities(datasource.connector_type, capabilities),
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
            select(MetadataSchema.name, MetadataRoutine.name, MetadataRoutine.routine_type)
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .where(
                MetadataRoutine.datasource_id == datasource.id, MetadataRoutine.status == "ACTIVE"
            )
            .limit(PREVIEW_OBJECT_LIMIT + 1)
        )
    ).all()
    truncated = any(
        len(rows) > PREVIEW_OBJECT_LIMIT for rows in (schema_names, tables, routines)
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
    objects = [
        (schema, name, table_kind(object_type))
        for schema, name, object_type in tables[:PREVIEW_OBJECT_LIMIT]
    ] + [
        (schema, name, routine_kind(routine_type))
        for schema, name, routine_type in routines[:PREVIEW_OBJECT_LIMIT]
    ]
    for schema, name, kind in objects:
        qualified = f"{schema}.{name}".lower()
        matched_patterns.update(
            pattern
            for pattern in selection.include_objects
            if fnmatchcase(qualified, pattern.lower())
        )
        count = counts.setdefault(kind, SelectionCountRead(kind=kind, in_scope=0, excluded=0))
        if selection.object_in_scope(schema, name, kind):
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
        capabilities=kind_capabilities(datasource.connector_type, capabilities),
        capability_source=source,
    )
