from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aida.connectors.base import (
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredConstraint,
    DiscoveredIndex,
    DiscoveredPartition,
    DiscoveredSchema,
    DiscoveredTable,
)


@dataclass(slots=True)
class _MutableTable:
    object_type: str
    columns: list[DiscoveredColumn] = field(default_factory=list)
    constraints: list[DiscoveredConstraint] = field(default_factory=list)
    indexes: list[DiscoveredIndex] = field(default_factory=list)
    partitions: list[DiscoveredPartition] = field(default_factory=list)


TableMap = dict[str, dict[str, _MutableTable]]


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
                "is_unique": bool(row.get("is_unique", False)),
                "is_primary": bool(row.get("is_primary", False)),
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


def assemble_catalog(catalog_name: str, tables: TableMap) -> tuple[DiscoveredCatalog, ...]:
    schemas: list[DiscoveredSchema] = []
    for schema_name, raw_tables in tables.items():
        discovered_tables = [
            DiscoveredTable(
                name=table_name,
                object_type=raw_table.object_type,
                columns=tuple(raw_table.columns),
                constraints=tuple(raw_table.constraints),
                indexes=tuple(raw_table.indexes),
                partitions=tuple(raw_table.partitions),
            )
            for table_name, raw_table in raw_tables.items()
        ]
        schemas.append(DiscoveredSchema(name=schema_name, tables=tuple(discovered_tables)))
    return (DiscoveredCatalog(name=catalog_name, schemas=tuple(schemas)),)


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
