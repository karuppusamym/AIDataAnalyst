import json
from typing import Any

import asyncpg

from aida.connectors.base import (
    ColumnProfileSnapshot,
    ConnectorCapabilities,
    DiscoveredCatalog,
    QueryEstimate,
    QueryResult,
    TableProfileSnapshot,
)
from aida.connectors.discovery import (
    append_aggregated_constraint_rows,
    apply_column_descriptions,
    apply_table_descriptions,
    apply_view_definitions,
    assemble_catalog,
    build_grants,
    build_routines,
    build_table_map_from_column_rows,
)
from aida.connectors.sql_execution import SqlExecutor


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


# Envelope 1.1 (gap/02 N1). `pg_get_viewdef` returns the complete reconstructed
# definition -- PostgreSQL never truncates it -- so `truncated` is left false
# here rather than guessed. A view whose definition this principal may not read
# yields NULL, which `apply_view_definitions` records as *unavailable* rather
# than as an empty view.
_VIEW_DEFINITION_SQL = """
    SELECT
        n.nspname AS table_schema,
        c.relname AS table_name,
        pg_get_viewdef(c.oid, true) AS definition,
        (c.relkind = 'm') AS is_materialized,
        v.is_updatable AS is_updatable,
        v.check_option AS check_option
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN information_schema.views v
      ON v.table_schema = n.nspname
     AND v.table_name = c.relname
    WHERE c.relkind IN ('v', 'm')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY n.nspname, c.relname
"""

# `p.oid` is the overload discriminator: PostgreSQL allows two routines to share
# a schema and a name, so the name alone is not an identity and the parameter
# join has to be on the oid. Restricted to prokind 'f'/'p' because
# `pg_get_functiondef` raises on aggregate and window functions.
_ROUTINE_SQL = """
    SELECT
        n.nspname AS routine_schema,
        p.proname AS routine_name,
        p.oid::text AS specific_name,
        CASE p.prokind WHEN 'p' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS routine_type,
        l.lanname AS language,
        pg_get_functiondef(p.oid) AS body,
        pg_get_function_result(p.oid) AS return_type,
        (p.provolatile <> 'v') AS is_deterministic,
        CASE WHEN p.prosecdef THEN 'DEFINER' ELSE 'INVOKER' END AS security_mode,
        obj_description(p.oid, 'pg_proc') AS description
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_language l ON l.oid = p.prolang
    WHERE p.prokind IN ('f', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY n.nspname, p.proname, p.oid
"""

_ROUTINE_PARAMETER_SQL = """
    SELECT
        n.nspname AS routine_schema,
        p.oid::text AS specific_name,
        p.proargnames[arg.ordinality] AS parameter_name,
        arg.ordinality::int AS ordinal_position,
        CASE COALESCE(p.proargmodes[arg.ordinality], 'i')
            WHEN 'i' THEN 'IN'
            WHEN 'o' THEN 'OUT'
            WHEN 'b' THEN 'INOUT'
            WHEN 'v' THEN 'VARIADIC'
            WHEN 't' THEN 'TABLE'
            ELSE 'IN'
        END AS parameter_mode,
        format_type(arg.type_oid, NULL) AS data_type
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN LATERAL unnest(COALESCE(p.proallargtypes, p.proargtypes::oid[]))
        WITH ORDINALITY AS arg(type_oid, ordinality) ON TRUE
    WHERE p.prokind IN ('f', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY n.nspname, p.oid, arg.ordinality
"""

_TABLE_COMMENT_SQL = """
    SELECT
        n.nspname AS table_schema,
        c.relname AS table_name,
        obj_description(c.oid, 'pg_class') AS description
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'v', 'm', 'f', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND obj_description(c.oid, 'pg_class') IS NOT NULL
    ORDER BY n.nspname, c.relname
"""

_COLUMN_COMMENT_SQL = """
    SELECT
        n.nspname AS table_schema,
        c.relname AS table_name,
        a.attname AS column_name,
        col_description(c.oid, a.attnum) AS description
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a
      ON a.attrelid = c.oid
     AND a.attnum > 0
     AND NOT a.attisdropped
    WHERE c.relkind IN ('r', 'v', 'm', 'f', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND col_description(c.oid, a.attnum) IS NOT NULL
    ORDER BY n.nspname, c.relname, a.attnum
"""

_SCHEMA_COMMENT_SQL = """
    SELECT
        n.nspname AS schema_name,
        obj_description(n.oid, 'pg_namespace') AS description
    FROM pg_namespace n
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND obj_description(n.oid, 'pg_namespace') IS NOT NULL
    ORDER BY n.nspname
"""

_CATALOG_COMMENT_SQL = """
    SELECT shobj_description(d.oid, 'pg_database')
    FROM pg_database d
    WHERE d.datname = current_database()
"""

# `role_table_grants` is the privileges visible to the connecting role, which is
# the honest scope: a metadata reader is not a superuser, and reporting only what
# it can see is preferable to failing the whole discovery on a permission error.
# PostgreSQL has one principal kind, so `grantee_type` is always ROLE.
_GRANT_SQL = """
    SELECT
        g.table_schema AS schema_name,
        g.grantee AS grantee,
        'ROLE' AS grantee_type,
        g.privilege_type AS privilege,
        'TABLE' AS object_type,
        g.table_name AS object_name,
        g.is_grantable AS is_grantable
    FROM information_schema.role_table_grants g
    WHERE g.table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY g.table_schema, g.table_name, g.grantee, g.privilege_type
"""


class PostgresConnector(SqlExecutor):
    connector_type = "postgres"
    dialect = "postgres"
    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        constraints=True,
        indexes=False,
        partitions=False,
        explain=True,
        delegated_identity=False,
        approximate_statistics=True,
        # Envelope 1.1 (gap/02 N1). Each flag below is backed by a query in
        # `discover()`, which is what INV-9 requires of a `True`:
        #   views            -> pg_get_viewdef over pg_class relkind in ('v','m')
        #   routines         -> pg_proc / pg_get_functiondef plus a parameter query
        #   object_comments  -> shobj_description / obj_description / col_description
        #   grants           -> information_schema.role_table_grants
        views=True,
        routines=True,
        object_comments=True,
        grants=True,
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
            view_rows = await connection.fetch(_VIEW_DEFINITION_SQL)
            routine_rows = await connection.fetch(_ROUTINE_SQL)
            routine_parameter_rows = await connection.fetch(_ROUTINE_PARAMETER_SQL)
            table_description_rows = await connection.fetch(_TABLE_COMMENT_SQL)
            column_description_rows = await connection.fetch(_COLUMN_COMMENT_SQL)
            schema_description_rows = await connection.fetch(_SCHEMA_COMMENT_SQL)
            catalog_description = await connection.fetchval(_CATALOG_COMMENT_SQL)
            grant_rows = await connection.fetch(_GRANT_SQL)
        finally:
            await connection.close()

        tables = build_table_map_from_column_rows(rows)
        append_aggregated_constraint_rows(tables, constraint_rows)
        apply_table_descriptions(tables, table_description_rows)
        apply_column_descriptions(tables, column_description_rows)
        apply_view_definitions(tables, view_rows)
        return assemble_catalog(
            str(catalog_name),
            tables,
            routines=build_routines(routine_rows, routine_parameter_rows),
            grants=build_grants(grant_rows),
            schema_descriptions={
                str(row["schema_name"]): str(row["description"])
                for row in schema_description_rows
            },
            catalog_description=(
                None if catalog_description is None else str(catalog_description)
            ),
        )

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
