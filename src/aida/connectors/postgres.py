import json
from typing import Any

import asyncpg

from aida.connectors.base import (
    ColumnProfileSnapshot,
    Connector,
    ConnectorCapabilities,
    DiscoveredCatalog,
    QueryEstimate,
    QueryResult,
    TableProfileSnapshot,
)
from aida.connectors.discovery import (
    append_aggregated_constraint_rows,
    append_grouped_index_rows,
    append_partition_rows,
    assemble_catalog,
    build_table_map_from_column_rows,
)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class PostgresConnector(Connector):
    connector_type = "postgres"
    dialect = "postgres"
    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        constraints=True,
        indexes=True,
        partitions=True,
        explain=True,
        delegated_identity=False,
        approximate_statistics=True,
    )

    def __init__(self, dsn: str, *, command_timeout: float = 30.0) -> None:
        self._dsn = dsn
        self._command_timeout = command_timeout

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.DEFAULT_CAPABILITIES

    async def test_connection(self) -> None:
        connection = await asyncpg.connect(self._dsn, command_timeout=self._command_timeout)
        try:
            await connection.fetchval("SELECT 1")
        finally:
            await connection.close()

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        connection = await asyncpg.connect(self._dsn, command_timeout=self._command_timeout)
        try:
            catalog_name = await connection.fetchval("SELECT current_database()")
            rows = await connection.fetch(
                """
                SELECT
                    c.table_schema,
                    c.table_name,
                    t.table_type,
                    c.column_name,
                    c.ordinal_position,
                    c.data_type,
                    c.is_nullable,
                    c.column_default
                FROM information_schema.columns c
                JOIN information_schema.tables t
                  ON t.table_catalog = c.table_catalog
                 AND t.table_schema = c.table_schema
                 AND t.table_name = c.table_name
                WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY c.table_schema, c.table_name, c.ordinal_position
                """
            )
            constraint_rows = await connection.fetch(
                """
                SELECT
                    ns.nspname AS table_schema,
                    rel.relname AS table_name,
                    con.conname AS constraint_name,
                    CASE con.contype
                        WHEN 'p' THEN 'PRIMARY_KEY'
                        WHEN 'u' THEN 'UNIQUE'
                        WHEN 'f' THEN 'FOREIGN_KEY'
                    END AS constraint_type,
                    array_agg(att.attname ORDER BY local_key.ordinality) AS columns,
                    ref_ns.nspname AS referenced_schema,
                    ref_rel.relname AS referenced_table,
                    array_agg(ref_att.attname ORDER BY local_key.ordinality)
                        FILTER (WHERE ref_att.attname IS NOT NULL) AS referenced_columns
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                JOIN LATERAL unnest(con.conkey) WITH ORDINALITY
                    AS local_key(attnum, ordinality) ON TRUE
                JOIN pg_attribute att
                  ON att.attrelid = rel.oid
                 AND att.attnum = local_key.attnum
                LEFT JOIN pg_class ref_rel ON ref_rel.oid = con.confrelid
                LEFT JOIN pg_namespace ref_ns ON ref_ns.oid = ref_rel.relnamespace
                LEFT JOIN LATERAL unnest(con.confkey) WITH ORDINALITY
                    AS foreign_key(attnum, ordinality)
                  ON foreign_key.ordinality = local_key.ordinality
                LEFT JOIN pg_attribute ref_att
                  ON ref_att.attrelid = ref_rel.oid
                 AND ref_att.attnum = foreign_key.attnum
                WHERE con.contype IN ('p', 'u', 'f')
                  AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
                GROUP BY
                    ns.nspname,
                    rel.relname,
                    con.conname,
                    con.contype,
                    ref_ns.nspname,
                    ref_rel.relname
                ORDER BY ns.nspname, rel.relname, con.conname
                """
            )
            # pg_index/pg_am mirror the pg_constraint query above. Expression indexes
            # (indkey entries of 0) have no matching pg_attribute row and are silently
            # dropped by the join rather than reported with a placeholder column name.
            index_rows = await connection.fetch(
                """
                SELECT
                    ns.nspname AS table_schema,
                    rel.relname AS table_name,
                    ic.relname AS index_name,
                    am.amname AS index_type,
                    ix.indisunique AS is_unique,
                    ix.indisprimary AS is_primary,
                    att.attname AS column_name
                FROM pg_index ix
                JOIN pg_class rel ON rel.oid = ix.indrelid
                JOIN pg_class ic ON ic.oid = ix.indexrelid
                JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                JOIN pg_am am ON am.oid = ic.relam
                JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS cols(attnum, ordinality)
                    ON TRUE
                JOIN pg_attribute att
                  ON att.attrelid = rel.oid
                 AND att.attnum = cols.attnum
                WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY ns.nspname, rel.relname, ic.relname, cols.ordinality
                """
            )
            # Declarative partitioning: pg_inherits lists each partition's parent, and
            # pg_partitioned_table carries the parent's partitioning strategy and key.
            partition_key_rows = await connection.fetch(
                """
                SELECT
                    ns.nspname AS table_schema,
                    rel.relname AS table_name,
                    att.attname AS column_name,
                    key.ordinality AS ordinal_position
                FROM pg_partitioned_table part
                JOIN pg_class rel ON rel.oid = part.partrelid
                JOIN pg_namespace ns ON ns.oid = rel.relnamespace
                JOIN LATERAL unnest(part.partattrs) WITH ORDINALITY AS key(attnum, ordinality)
                    ON TRUE
                JOIN pg_attribute att
                  ON att.attrelid = rel.oid
                 AND att.attnum = key.attnum
                WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY ns.nspname, rel.relname, key.ordinality
                """
            )
            partition_rows = await connection.fetch(
                """
                SELECT
                    parent_ns.nspname AS table_schema,
                    parent.relname AS table_name,
                    child.relname AS partition_name,
                    CASE part.partstrat
                        WHEN 'r' THEN 'RANGE'
                        WHEN 'l' THEN 'LIST'
                        WHEN 'h' THEN 'HASH'
                    END AS partition_type,
                    pg_get_expr(child.relpartbound, child.oid) AS high_value,
                    inh.inhseqno AS ordinal_position
                FROM pg_inherits inh
                JOIN pg_class parent ON parent.oid = inh.inhparent
                JOIN pg_class child ON child.oid = inh.inhrelid
                JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
                JOIN pg_partitioned_table part ON part.partrelid = parent.oid
                WHERE parent_ns.nspname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY parent_ns.nspname, parent.relname, inh.inhseqno
                """
            )
        finally:
            await connection.close()

        tables = build_table_map_from_column_rows(rows)
        append_aggregated_constraint_rows(tables, constraint_rows)
        append_grouped_index_rows(tables, index_rows)

        partition_key_map: dict[tuple[str, str], list[str]] = {}
        for row in partition_key_rows:
            key = (str(row["table_schema"]), str(row["table_name"]))
            partition_key_map.setdefault(key, []).append(str(row["column_name"]))
        merged_partition_rows = [
            {
                "table_schema": str(row["table_schema"]),
                "table_name": str(row["table_name"]),
                "partition_name": str(row["partition_name"]),
                "partition_type": row["partition_type"],
                "high_value": row["high_value"],
                "ordinal_position": row["ordinal_position"],
                "key_columns": partition_key_map.get(
                    (str(row["table_schema"]), str(row["table_name"])), []
                ),
            }
            for row in partition_rows
        ]
        append_partition_rows(tables, merged_partition_rows)

        return assemble_catalog(str(catalog_name), tables)

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> QueryEstimate:
        connection = await asyncpg.connect(self._dsn, command_timeout=timeout_seconds)
        try:
            async with connection.transaction(readonly=True):
                await connection.execute(f"SET LOCAL statement_timeout = {timeout_seconds * 1000}")
                raw_plan = await connection.fetchval(f"EXPLAIN (FORMAT JSON) {sql}")
                parsed = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
                if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], dict):
                    raise RuntimeError("source returned an invalid EXPLAIN plan")
                return _extract_explain_estimate(parsed[0])
        finally:
            await connection.close()

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        connection = await asyncpg.connect(self._dsn, command_timeout=timeout_seconds)
        try:
            async with connection.transaction(readonly=True):
                await connection.execute(f"SET LOCAL statement_timeout = {timeout_seconds * 1000}")
                backend_id = await connection.fetchval("SELECT pg_backend_pid()")
                records = await connection.fetch(sql)
                return QueryResult(
                    rows=tuple(dict(record) for record in records),
                    warehouse_query_id=f"postgres-backend:{backend_id}",
                )
        finally:
            await connection.close()

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
        """Collect bounded statistics without returning or persisting source values."""
        if not column_names:
            return TableProfileSnapshot(None, 0, ())
        if sample_rows < 1 or column_batch_size < 1:
            raise ValueError("profiling limits must be positive")

        qualified_table = f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"
        connection = await asyncpg.connect(self._dsn, command_timeout=timeout_seconds)
        snapshots: list[ColumnProfileSnapshot] = []
        sampled_row_count = 0
        try:
            async with connection.transaction(readonly=True):
                await connection.execute(f"SET LOCAL statement_timeout = {timeout_seconds * 1000}")
                estimate = await connection.fetchval(
                    """
                    SELECT GREATEST(c.reltuples, 0)::bigint
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = $1 AND c.relname = $2
                    """,
                    schema_name,
                    table_name,
                )
                for start in range(0, len(column_names), column_batch_size):
                    batch = column_names[start : start + column_batch_size]
                    selected = ", ".join(_quote_identifier(name) for name in batch)
                    expressions = ["COUNT(*)::bigint AS sampled_row_count"]
                    for position, name in enumerate(batch):
                        quoted = _quote_identifier(name)
                        expressions.extend(
                            (
                                f"COUNT(*) FILTER (WHERE {quoted} IS NULL)::bigint AS n_{position}",
                                f"COUNT({quoted})::bigint AS nn_{position}",
                                f"COUNT(DISTINCT {quoted})::bigint AS d_{position}",
                                f"MIN(LENGTH({quoted}::text))::integer AS minl_{position}",
                                f"MAX(LENGTH({quoted}::text))::integer AS maxl_{position}",
                            )
                        )
                    profile_sql = (
                        f"WITH bounded_sample AS (SELECT {selected} FROM {qualified_table} "  # noqa: S608 -- identifiers are ANSI-quoted and limits are validated integers
                        f"LIMIT {int(sample_rows)}) SELECT {', '.join(expressions)} "
                        "FROM bounded_sample"
                    )
                    row = await connection.fetchrow(profile_sql)
                    if row is None:
                        continue
                    sampled_row_count = max(sampled_row_count, int(row["sampled_row_count"]))
                    for position, name in enumerate(batch):
                        snapshots.append(
                            ColumnProfileSnapshot(
                                name=name,
                                null_count=int(row[f"n_{position}"]),
                                non_null_count=int(row[f"nn_{position}"]),
                                approximate_distinct_count=int(row[f"d_{position}"]),
                                min_length=row[f"minl_{position}"],
                                max_length=row[f"maxl_{position}"],
                            )
                        )
        finally:
            await connection.close()
        return TableProfileSnapshot(
            row_count_estimate=(
                max(int(estimate), sampled_row_count) if estimate is not None else None
            ),
            sampled_row_count=sampled_row_count,
            columns=tuple(snapshots),
        )


def _extract_explain_estimate(raw_plan: dict[str, Any]) -> QueryEstimate:
    plan_body = raw_plan.get("Plan")
    if not isinstance(plan_body, dict):
        raise RuntimeError("source returned an invalid EXPLAIN plan body")
    raw_cost = plan_body.get("Total Cost")
    if raw_cost is None:
        raise RuntimeError("source returned an EXPLAIN plan without total cost")
    try:
        total_cost = float(raw_cost)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("source returned a non-numeric EXPLAIN total cost") from exc
    raw_rows = plan_body.get("Plan Rows")
    estimated_rows: float | None
    if raw_rows is None:
        estimated_rows = None
    else:
        try:
            estimated_rows = float(raw_rows)
        except (TypeError, ValueError):
            estimated_rows = None
    return QueryEstimate(
        score=total_cost,
        kind="EXPLAIN_COST",
        estimated_rows=estimated_rows,
        evidence=raw_plan,
    )
