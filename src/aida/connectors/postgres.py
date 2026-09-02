import json
from collections.abc import AsyncIterator
from typing import Any

import asyncpg

from aida.connectors.base import (
    ColumnProfileSnapshot,
    ColumnValueProfileSnapshot,
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
# CN-3. `information_schema.tables`/`.columns` never list materialized views
# (relkind 'm') -- that is a documented Postgres limitation of the SQL-standard
# information_schema, not a version difference -- so a materialized view was
# never entering `tables` via the primary column query below, and
# `apply_view_definitions` (`_lookup_table` returning None) was silently
# dropping its columns *and* its view_definition even though `_VIEW_DEFINITION_SQL`
# below reads it and DEFAULT_CAPABILITIES.views's own docstring claims coverage
# of relkind 'v' *and* 'm'. Found by building a real live fixture with a
# materialized view (tests/test_postgres_version_fixtures.py) -- every existing
# unit test drives `build_table_map_from_column_rows` directly with hand-built
# rows, so this gap was invisible to all of them. Reconstructed from
# `pg_attribute`/`pg_attrdef` in the same row shape `build_table_map_from_column_rows`
# expects, so the existing assembly pipeline needs no changes -- only this query
# and the two lines in `discover()` that merge its rows in.
_MATERIALIZED_VIEW_COLUMN_SQL = """
    SELECT
        n.nspname AS table_schema,
        c.relname AS table_name,
        'MATERIALIZED VIEW' AS table_type,
        a.attname AS column_name,
        a.attnum AS ordinal_position,
        format_type(a.atttypid, a.atttypmod) AS data_type,
        CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable,
        pg_get_expr(ad.adbin, ad.adrelid) AS column_default
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a
      ON a.attrelid = c.oid
     AND a.attnum > 0
     AND NOT a.attisdropped
    LEFT JOIN pg_attrdef ad
      ON ad.adrelid = c.oid
     AND ad.adnum = a.attnum
    WHERE c.relkind = 'm'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY n.nspname, c.relname, a.attnum
"""

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

# CT-3/CN-8. Not an envelope 1.1 axis (cost-estimation-only, see DiscoveredIndex),
# grouped like the constraint query above via pg_index/pg_am. Expression indexes
# (indkey entries of 0) have no matching pg_attribute row and are silently
# dropped by the join rather than reported with a placeholder column name.
_INDEX_SQL = """
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
    JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS cols(attnum, ordinality) ON TRUE
    JOIN pg_attribute att
      ON att.attrelid = rel.oid
     AND att.attnum = cols.attnum
    WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY ns.nspname, rel.relname, ic.relname, cols.ordinality
"""

# Declarative partitioning: pg_partitioned_table carries the parent's
# partitioning strategy and key; pg_inherits lists each partition's parent.
_PARTITION_KEY_SQL = """
    SELECT
        ns.nspname AS table_schema,
        rel.relname AS table_name,
        att.attname AS column_name,
        key.ordinality AS ordinal_position
    FROM pg_partitioned_table part
    JOIN pg_class rel ON rel.oid = part.partrelid
    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
    JOIN LATERAL unnest(part.partattrs) WITH ORDINALITY AS key(attnum, ordinality) ON TRUE
    JOIN pg_attribute att
      ON att.attrelid = rel.oid
     AND att.attnum = key.attnum
    WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY ns.nspname, rel.relname, key.ordinality
"""

_PARTITION_SQL = """
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


# CN-3/PR-5. Streaming-discovery batch queries (`PostgresConnector.discover_streaming`).
# Each mirrors the unscoped query of the same axis above exactly, with one added
# predicate that restricts it to the (schema, table) pairs in the current page --
# see `_batch_predicate` for the shape asyncpg needs to bind two parallel arrays
# as a single filter. Kept as separate constants rather than building the filter
# into the original queries so `discover()` (still used by anything that wants
# the unscoped, single-shot path) is untouched byte-for-byte.


def _batch_predicate(schema_column: str, name_column: str) -> str:
    # noqa: S608 -- `schema_column`/`name_column` are always one of the hardcoded
    # qualified-identifier literals passed at each call site below (e.g.
    # "c.table_schema"), never source-derived text; the actual filter values
    # (schema/table names) are bound separately via asyncpg positional
    # parameters ($1/$2), never interpolated into the SQL text.
    return (
        f"AND ({schema_column}, {name_column}) IN "  # noqa: S608
        "(SELECT * FROM unnest($1::text[], $2::text[]) AS _batch(schema_name, table_name))"
    )


# Lightweight roster of every ordinary table/view (information_schema never lists
# materialized views -- see `_MATERIALIZED_VIEW_COLUMN_SQL`'s comment above -- so
# `_MATERIALIZED_VIEW_ROSTER_SQL` below covers that gap the same way the
# unscoped path does). One row per table, not per column, so this alone is cheap
# even at 100K tables; it exists only to compute page boundaries before any
# per-axis query runs.
_TABLE_ROSTER_SQL = """
    SELECT t.table_schema, t.table_name, t.table_type
    FROM information_schema.tables t
    WHERE t.table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY t.table_schema, t.table_name
"""

_MATERIALIZED_VIEW_ROSTER_SQL = """
    SELECT n.nspname AS table_schema, c.relname AS table_name,
           'MATERIALIZED VIEW' AS table_type
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind = 'm'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY n.nspname, c.relname
"""

_COLUMN_BATCH_SQL = (
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
      {predicate}
    ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""
).format(
    predicate=_batch_predicate("c.table_schema", "c.table_name")
)  # noqa: S608 -- static, hardcoded identifier columns only; the actual
# filter values are bound via asyncpg positional parameters ($1/$2) in
# `discover_streaming`, never interpolated into this SQL text.


_MATERIALIZED_VIEW_COLUMN_BATCH_SQL = (
    """
    SELECT
        n.nspname AS table_schema,
        c.relname AS table_name,
        'MATERIALIZED VIEW' AS table_type,
        a.attname AS column_name,
        a.attnum AS ordinal_position,
        format_type(a.atttypid, a.atttypmod) AS data_type,
        CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable,
        pg_get_expr(ad.adbin, ad.adrelid) AS column_default
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a
      ON a.attrelid = c.oid
     AND a.attnum > 0
     AND NOT a.attisdropped
    LEFT JOIN pg_attrdef ad
      ON ad.adrelid = c.oid
     AND ad.adnum = a.attnum
    WHERE c.relkind = 'm'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      {predicate}
    ORDER BY n.nspname, c.relname, a.attnum
"""
).format(
    predicate=_batch_predicate("n.nspname", "c.relname")
)  # noqa: S608 -- static, hardcoded identifier columns only; the actual
# filter values are bound via asyncpg positional parameters ($1/$2) in
# `discover_streaming`, never interpolated into this SQL text.


_CONSTRAINT_BATCH_SQL = (
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
      {predicate}
    GROUP BY
        ns.nspname,
        rel.relname,
        con.conname,
        con.contype,
        ref_ns.nspname,
        ref_rel.relname
    ORDER BY ns.nspname, rel.relname, con.conname
"""
).format(
    predicate=_batch_predicate("ns.nspname", "rel.relname")
)  # noqa: S608 -- static, hardcoded identifier columns only; the actual
# filter values are bound via asyncpg positional parameters ($1/$2) in
# `discover_streaming`, never interpolated into this SQL text.


_VIEW_DEFINITION_BATCH_SQL = (
    """
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
      {predicate}
    ORDER BY n.nspname, c.relname
"""
).format(
    predicate=_batch_predicate("n.nspname", "c.relname")
)  # noqa: S608 -- static, hardcoded identifier columns only; the actual
# filter values are bound via asyncpg positional parameters ($1/$2) in
# `discover_streaming`, never interpolated into this SQL text.


_TABLE_COMMENT_BATCH_SQL = (
    """
    SELECT
        n.nspname AS table_schema,
        c.relname AS table_name,
        obj_description(c.oid, 'pg_class') AS description
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'v', 'm', 'f', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND obj_description(c.oid, 'pg_class') IS NOT NULL
      {predicate}
    ORDER BY n.nspname, c.relname
"""
).format(
    predicate=_batch_predicate("n.nspname", "c.relname")
)  # noqa: S608 -- static, hardcoded identifier columns only; the actual
# filter values are bound via asyncpg positional parameters ($1/$2) in
# `discover_streaming`, never interpolated into this SQL text.


_COLUMN_COMMENT_BATCH_SQL = (
    """
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
      {predicate}
    ORDER BY n.nspname, c.relname, a.attnum
"""
).format(
    predicate=_batch_predicate("n.nspname", "c.relname")
)  # noqa: S608 -- static, hardcoded identifier columns only; the actual
# filter values are bound via asyncpg positional parameters ($1/$2) in
# `discover_streaming`, never interpolated into this SQL text.


_INDEX_BATCH_SQL = (
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
    JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS cols(attnum, ordinality) ON TRUE
    JOIN pg_attribute att
      ON att.attrelid = rel.oid
     AND att.attnum = cols.attnum
    WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
      {predicate}
    ORDER BY ns.nspname, rel.relname, ic.relname, cols.ordinality
"""
).format(
    predicate=_batch_predicate("ns.nspname", "rel.relname")
)  # noqa: S608 -- static, hardcoded identifier columns only; the actual
# filter values are bound via asyncpg positional parameters ($1/$2) in
# `discover_streaming`, never interpolated into this SQL text.


_PARTITION_KEY_BATCH_SQL = (
    """
    SELECT
        ns.nspname AS table_schema,
        rel.relname AS table_name,
        att.attname AS column_name,
        key.ordinality AS ordinal_position
    FROM pg_partitioned_table part
    JOIN pg_class rel ON rel.oid = part.partrelid
    JOIN pg_namespace ns ON ns.oid = rel.relnamespace
    JOIN LATERAL unnest(part.partattrs) WITH ORDINALITY AS key(attnum, ordinality) ON TRUE
    JOIN pg_attribute att
      ON att.attrelid = rel.oid
     AND att.attnum = key.attnum
    WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
      {predicate}
    ORDER BY ns.nspname, rel.relname, key.ordinality
"""
).format(
    predicate=_batch_predicate("ns.nspname", "rel.relname")
)  # noqa: S608 -- static, hardcoded identifier columns only; the actual
# filter values are bound via asyncpg positional parameters ($1/$2) in
# `discover_streaming`, never interpolated into this SQL text.


_PARTITION_BATCH_SQL = (
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
      {predicate}
    ORDER BY parent_ns.nspname, parent.relname, inh.inhseqno
"""
).format(
    predicate=_batch_predicate("parent_ns.nspname", "parent.relname")
)  # noqa: S608 -- static, hardcoded identifier columns only; the actual
# filter values are bound via asyncpg positional parameters ($1/$2) in
# `discover_streaming`, never interpolated into this SQL text.


_GRANT_BATCH_SQL = (
    """
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
      {predicate}
    ORDER BY g.table_schema, g.table_name, g.grantee, g.privilege_type
"""
).format(
    predicate=_batch_predicate("g.table_schema", "g.table_name")
)  # noqa: S608 -- static, hardcoded identifier columns only; the actual
# filter values are bound via asyncpg positional parameters ($1/$2) in
# `discover_streaming`, never interpolated into this SQL text.



class PostgresConnector(SqlExecutor):
    connector_type = "postgres"
    dialect = "postgres"
    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        constraints=True,
        # CT-3/CN-8: indexes -> pg_index/pg_am; partitions -> pg_partitioned_table
        # + pg_inherits. See `_INDEX_SQL`/`_PARTITION_SQL` below.
        indexes=True,
        partitions=True,
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
        # PR-2: the only connector today with a real `profile_column_values`
        # implementation below -- every other connector stays honestly
        # unsupported (default False) rather than simulating this capability.
        value_range_profiling=True,
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
            materialized_view_column_rows = await connection.fetch(
                _MATERIALIZED_VIEW_COLUMN_SQL
            )
            view_rows = await connection.fetch(_VIEW_DEFINITION_SQL)
            routine_rows = await connection.fetch(_ROUTINE_SQL)
            routine_parameter_rows = await connection.fetch(_ROUTINE_PARAMETER_SQL)
            table_description_rows = await connection.fetch(_TABLE_COMMENT_SQL)
            column_description_rows = await connection.fetch(_COLUMN_COMMENT_SQL)
            schema_description_rows = await connection.fetch(_SCHEMA_COMMENT_SQL)
            catalog_description = await connection.fetchval(_CATALOG_COMMENT_SQL)
            grant_rows = await connection.fetch(_GRANT_SQL)
            index_rows = await connection.fetch(_INDEX_SQL)
            partition_key_rows = await connection.fetch(_PARTITION_KEY_SQL)
            partition_rows = await connection.fetch(_PARTITION_SQL)
        finally:
            await connection.close()

        # CN-3: materialized-view rows are appended, not merged separately -- they
        # share the exact row shape `information_schema.columns` rows have, so one
        # call to `build_table_map_from_column_rows` populates both. See
        # `_MATERIALIZED_VIEW_COLUMN_SQL` above for why this is necessary at all.
        tables = build_table_map_from_column_rows([*rows, *materialized_view_column_rows])
        append_aggregated_constraint_rows(tables, constraint_rows)
        apply_table_descriptions(tables, table_description_rows)
        apply_column_descriptions(tables, column_description_rows)
        apply_view_definitions(tables, view_rows)
        append_grouped_index_rows(tables, index_rows)

        # A partition key is a property of the parent table's partitioning
        # scheme, not of the individual partition, so it is merged onto every
        # partition row for that table before `append_partition_rows` groups them.
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

    async def discover_streaming(
        self, *, batch_size: int = 500
    ) -> AsyncIterator[tuple[DiscoveredCatalog, ...]]:
        """CN-3/PR-5. The real fix for the 100K-table timeout: pages through the
        source's table roster in bounded batches and scopes every per-axis query
        (columns, constraints, views, indexes, partitions, comments, grants) to
        just that batch's tables, yielding one `DiscoveredCatalog` per page
        instead of building the whole source's inventory in memory before
        returning anything.

        `discover()` above is unchanged and still the right call for a caller
        that wants the unscoped, single-shot result (e.g. a small source, or a
        one-off connectivity check) -- this is an additional path, not a
        replacement, matching the default `Connector.discover_streaming` every
        other connector still gets (base.py).

        Catalog-level axes that are not table-scoped -- routines (schema+routine
        keyed, not table-keyed), schema comments, and the single catalog
        comment -- are cheap relative to the per-table axes even at 100K tables
        (a source has orders of magnitude fewer routines and schemas than
        tables), so they are fetched once, up front, and attached to the
        *first* yielded batch only; `assemble_catalog` unions schema names
        across `tables`/`routines`/`grants`/`schema_descriptions`, so a schema
        that holds only routines still appears even though its routines were
        attached on batch one, not on the batch containing its tables.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        connection = await asyncpg.connect(self._dsn, command_timeout=self._command_timeout)
        try:
            catalog_name = str(await connection.fetchval("SELECT current_database()"))

            # One row per table (not per column), so this roster scan is cheap
            # even at 100K tables -- it exists only to compute page boundaries
            # before any of the heavier per-axis queries below ever runs.
            roster_rows = await connection.fetch(_TABLE_ROSTER_SQL)
            materialized_roster_rows = await connection.fetch(_MATERIALIZED_VIEW_ROSTER_SQL)
            roster = sorted(
                {
                    (str(row["table_schema"]), str(row["table_name"]))
                    for row in (*roster_rows, *materialized_roster_rows)
                }
            )
            if not roster:
                yield (DiscoveredCatalog(name=catalog_name, schemas=()),)
                return

            routine_rows = await connection.fetch(_ROUTINE_SQL)
            routine_parameter_rows = await connection.fetch(_ROUTINE_PARAMETER_SQL)
            schema_description_rows = await connection.fetch(_SCHEMA_COMMENT_SQL)
            catalog_description = await connection.fetchval(_CATALOG_COMMENT_SQL)
            routines = build_routines(routine_rows, routine_parameter_rows)
            schema_descriptions = {
                str(row["schema_name"]): str(row["description"])
                for row in schema_description_rows
            }
            catalog_description_str = (
                None if catalog_description is None else str(catalog_description)
            )

            for start in range(0, len(roster), batch_size):
                page = roster[start : start + batch_size]
                schemas_arr = [schema for schema, _name in page]
                names_arr = [name for _schema, name in page]

                column_rows = await connection.fetch(_COLUMN_BATCH_SQL, schemas_arr, names_arr)
                materialized_view_column_rows = await connection.fetch(
                    _MATERIALIZED_VIEW_COLUMN_BATCH_SQL, schemas_arr, names_arr
                )
                constraint_rows = await connection.fetch(
                    _CONSTRAINT_BATCH_SQL, schemas_arr, names_arr
                )
                view_rows = await connection.fetch(
                    _VIEW_DEFINITION_BATCH_SQL, schemas_arr, names_arr
                )
                table_description_rows = await connection.fetch(
                    _TABLE_COMMENT_BATCH_SQL, schemas_arr, names_arr
                )
                column_description_rows = await connection.fetch(
                    _COLUMN_COMMENT_BATCH_SQL, schemas_arr, names_arr
                )
                grant_rows = await connection.fetch(_GRANT_BATCH_SQL, schemas_arr, names_arr)
                index_rows = await connection.fetch(_INDEX_BATCH_SQL, schemas_arr, names_arr)
                partition_key_rows = await connection.fetch(
                    _PARTITION_KEY_BATCH_SQL, schemas_arr, names_arr
                )
                partition_rows = await connection.fetch(
                    _PARTITION_BATCH_SQL, schemas_arr, names_arr
                )

                # CN-3: materialized-view rows are appended, not merged separately,
                # exactly as in `discover()` above -- see `_MATERIALIZED_VIEW_COLUMN_SQL`'s
                # comment for why.
                tables = build_table_map_from_column_rows(
                    [*column_rows, *materialized_view_column_rows]
                )
                append_aggregated_constraint_rows(tables, constraint_rows)
                apply_table_descriptions(tables, table_description_rows)
                apply_column_descriptions(tables, column_description_rows)
                apply_view_definitions(tables, view_rows)
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

                is_first_batch = start == 0
                yield assemble_catalog(
                    catalog_name,
                    tables,
                    routines=routines if is_first_batch else None,
                    grants=build_grants(grant_rows),
                    schema_descriptions=schema_descriptions if is_first_batch else None,
                    catalog_description=(
                        catalog_description_str if is_first_batch else None
                    ),
                )
        finally:
            await connection.close()

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
        """PR-2: the one connector with a real value-bearing implementation.

        Callers (`profile_table_task`) are responsible for only invoking this
        for columns whose classification has an APPROVED, unrevoked
        `ProfilingExceptionPolicy` -- this method has no policy awareness of
        its own and, per ADR-0014, is never on the path `profile_table` uses.
        """
        if not column_names:
            return ()
        if sample_rows < 1 or top_n < 1:
            raise ValueError("profiling limits must be positive")
        qualified_table = f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"
        connection = await asyncpg.connect(self._dsn, command_timeout=timeout_seconds)
        snapshots: list[ColumnValueProfileSnapshot] = []
        try:
            async with connection.transaction(readonly=True):
                await connection.execute(f"SET LOCAL statement_timeout = {timeout_seconds * 1000}")
                for name in column_names:
                    quoted = _quote_identifier(name)
                    bounded_sample = (
                        f"SELECT {quoted} AS v FROM {qualified_table} "  # noqa: S608 -- identifiers are ANSI-quoted; limits are validated integers
                        f"LIMIT {int(sample_rows)}"
                    )
                    try:
                        # A nested transaction here is a SAVEPOINT (asyncpg's
                        # behaviour for a transaction opened inside another): a
                        # column whose type has no total order (e.g. json) raises
                        # below and is rolled back to the savepoint alone, rather
                        # than aborting the outer read-only transaction and
                        # poisoning every column queried after it.
                        async with connection.transaction():
                            range_row = await connection.fetchrow(
                                f"SELECT MIN(v::text) AS min_v, MAX(v::text) AS max_v "  # noqa: S608 -- identifiers are ANSI-quoted; limits are validated integers
                                f"FROM ({bounded_sample}) AS bounded_sample"
                            )
                            top_rows = await connection.fetch(
                                f"SELECT v::text AS value, COUNT(*) AS cnt "  # noqa: S608 -- identifiers are ANSI-quoted; limits are validated integers
                                f"FROM ({bounded_sample}) AS bounded_sample "
                                "WHERE v IS NOT NULL GROUP BY v "
                                f"ORDER BY COUNT(*) DESC, v LIMIT {int(top_n)}"
                            )
                    except asyncpg.PostgresError:
                        snapshots.append(
                            ColumnValueProfileSnapshot(name=name, min_value=None, max_value=None)
                        )
                        continue
                    snapshots.append(
                        ColumnValueProfileSnapshot(
                            name=name,
                            min_value=None if range_row is None else range_row["min_v"],
                            max_value=None if range_row is None else range_row["max_v"],
                            top_values=tuple(
                                (str(row["value"]), int(row["cnt"])) for row in top_rows
                            ),
                        )
                    )
        finally:
            await connection.close()
        return tuple(snapshots)


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
