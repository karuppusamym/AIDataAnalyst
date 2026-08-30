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
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

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
        routines.setdefault(schema_name, []).append(
            DiscoveredRoutine(
                name=routine_name,
                routine_type=normalize_object_type(str(row["routine_type"])),
                language=_coerce_optional_str(row.get("language")),
                body_sql=body,
                parameters=tuple(grouped_parameters.get((schema_name, specific_name), ())),
                return_type=_coerce_optional_str(row.get("return_type")),
                is_deterministic=_coerce_optional_bool(row.get("is_deterministic")),
                security_mode=_coerce_optional_str(row.get("security_mode")),
                source_description=_coerce_optional_str(row.get("description")),
                truncated=bool(row.get("truncated", False)),
                unavailable_reason=reason,
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
