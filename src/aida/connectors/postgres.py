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
    attach_native_objects,
    bounded_scan_scope,
    build_sequences,
    build_triggers,
    read_value_free_distribution,
    value_free_distribution_expressions,
)
from aida.connectors.discovery import (
    FACET_CONSTRAINTS,
    FACET_GRANTS,
    FACET_INDEXES,
    FACET_INVENTORY,
    FACET_OBJECT_COMMENTS,
    FACET_PARTITIONS,
    FACET_ROUTINE_BODIES,
    FACET_SEQUENCES,
    FACET_TRIGGERS,
    FACET_VIEW_DEFINITIONS,
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
    read_facet,
)
from aida.connectors.schema_scope import SchemaScope, schema_scope, scoped_postgres_query
from aida.connectors.sql_execution import SqlExecutor


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _entropy_expression(quoted_column: str, position: int) -> str:
    """R11-FP04: Shannon entropy of one column's frequency distribution, in bits.

    The whole point is what does *not* come back. The inner query groups by the
    column -- so PostgreSQL sees every value -- and projects only each group's
    share of the non-null rows; the outer aggregate collapses those shares to
    one float. No group key, no ordering, no mode, no exemplar crosses the
    wire, so this stays inside ADR-0014's value-free half and needs no
    `ProfilingExceptionPolicy` (which governs actual ranges and top values, a
    different query class that does return values).

    An uncorrelated scalar subquery over the caller's own `bounded_sample` CTE,
    not a second statement: the CTE is referenced more than once, so PostgreSQL
    materializes it and every facet in the profile describes the same rows. A
    second statement would re-run an unordered `LIMIT` and quietly mix two
    different samples into one profile row.

    `SUM(COUNT(*)) OVER ()` is the non-null total without a second scan.
    Entirely-null column: the grouped query returns no rows, the outer `SUM` is
    NULL, and the facet is honestly absent rather than 0.0 -- which would read
    as "one value repeated", a different fact.
    """
    share = "COUNT(*)::numeric / SUM(COUNT(*)) OVER () AS p"
    grouped = (
        f"SELECT {share} FROM bounded_sample "  # noqa: S608 -- identifier is ANSI-quoted above
        f"WHERE {quoted_column} IS NOT NULL GROUP BY {quoted_column}"
    )
    return (
        f"CAST((SELECT -SUM(f.p * LOG(2::numeric, f.p)) FROM ({grouped}) f) "  # noqa: S608 -- the only interpolation is an ANSI-quoted identifier and a generated integer alias
        f"AS double precision) AS en_{position}"
    )


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
# `pg_get_functiondef` raises on aggregate and window functions -- those two
# kinds are read by `_AGGREGATE_ROUTINE_SQL` below, which asks for no
# definition at all.
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

# R11-FP01: aggregate (`prokind = 'a'`) and window (`'w'`) functions.
#
# They were absent for a true reason that is not the same as "the object does
# not exist": `pg_get_functiondef` raises `ERROR: "x" is an aggregate function`
# on either kind, and `_ROUTINE_SQL` above calls it unconditionally, so widening
# that query's own `IN` list would have made every discovery run against a
# database with one aggregate fail outright. A `CASE` guard is not the fix
# either -- PostgreSQL does not guarantee that a `CASE` branch's function call
# is never evaluated for a non-matching row, so it would be a latent version-
# dependent failure. A second query that never asks for a definition cannot
# raise, which is why this is a query of its own rather than a widened `IN`.
#
# Identity, signature and parameters are all real and all discovered: the
# parameter query below covers all four prokinds. Only the *definition* is
# absent, and it arrives as `availability = UNAVAILABLE` with the reason, which
# is exactly what that column pair exists for -- an aggregate is now an object
# Atlas knows the name, signature and return type of and honestly says it
# cannot show the source of.
#
# `native_subtype` keeps the native identity beside the portable
# `routine_type`, the way SQL Server's SCALAR / INLINE_TABLE and BigQuery's
# SCALAR_FUNCTION already do (R11-FP03): an aggregate is a FUNCTION for every
# portable purpose and is still an aggregate.
_AGGREGATE_ROUTINE_SQL = """
    SELECT
        n.nspname AS routine_schema,
        p.proname AS routine_name,
        p.oid::text AS specific_name,
        'FUNCTION' AS routine_type,
        CASE p.prokind WHEN 'a' THEN 'AGGREGATE' ELSE 'WINDOW' END AS native_subtype,
        l.lanname AS language,
        NULL AS body,
        pg_get_function_result(p.oid) AS return_type,
        (p.provolatile <> 'v') AS is_deterministic,
        CASE WHEN p.prosecdef THEN 'DEFINER' ELSE 'INVOKER' END AS security_mode,
        obj_description(p.oid, 'pg_proc') AS description,
        -- `prokind` is PostgreSQL's `"char"`, and `'literal' || "char"` has no
        -- unique operator resolution (`operator is not unique: unknown || "char"`),
        -- so the cast is load-bearing rather than cosmetic.
        'pg_get_functiondef refuses prokind ' || p.prokind::text ||
            ': PostgreSQL exposes no CREATE statement for an aggregate or window function'
            AS unavailable_reason
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    JOIN pg_language l ON l.oid = p.prolang
    WHERE p.prokind IN ('a', 'w')
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
    WHERE p.prokind IN ('f', 'p', 'a', 'w')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY n.nspname, p.oid, arg.ordinality
"""

# R11-FP01: triggers.
#
# `pg_trigger.tgtype` is a bitmask and is the only place the timing, the
# orientation and the event set live; `information_schema.triggers` spells them
# out but emits one row per event, is permission-filtered to tables the role
# owns, and omits the action function entirely -- so the catalog is both more
# complete and cheaper here.
#
# The bits, from PostgreSQL's own `catalog/pg_trigger.h`: 1 ROW, 2 BEFORE,
# 4 INSERT, 8 DELETE, 16 UPDATE, 32 TRUNCATE, 64 INSTEAD. INSTEAD is tested
# first because an INSTEAD OF trigger has neither the BEFORE bit nor an AFTER
# bit -- reading the absence of bit 2 as AFTER without that test would report
# every INSTEAD OF trigger on a view as AFTER.
#
# `NOT tgisinternal` excludes the triggers PostgreSQL creates to enforce foreign
# keys and deferred constraints. Those are already discovered as constraints
# (`pg_constraint`, above), and reporting them again as triggers would double
# every referential integrity rule in the estate and present an implementation
# detail as user code.
#
# A PostgreSQL trigger has no body: `tgfoid` names a function, and that
# function's own body is already captured by `_ROUTINE_SQL`. So `body` is NULL
# here and `unavailable_reason` says exactly that, with `action_routine`
# carrying the qualified name that joins the two. `pg_get_triggerdef` is
# deliberately not called: it returns the whole `CREATE TRIGGER` statement,
# which re-states facts this row already has as columns, and its `WHEN` clause
# would put an unredacted SQL expression -- literals and all -- into the
# envelope (INV-6).
_TRIGGER_SQL = """
    SELECT
        n.nspname AS trigger_schema,
        t.tgname AS trigger_name,
        c.relname AS table_name,
        n.nspname AS table_schema,
        CASE
            WHEN (t.tgtype::int & 64) <> 0 THEN 'INSTEAD OF'
            WHEN (t.tgtype::int & 2) <> 0 THEN 'BEFORE'
            ELSE 'AFTER'
        END AS timing,
        CASE WHEN (t.tgtype::int & 1) <> 0 THEN 'ROW' ELSE 'STATEMENT' END AS orientation,
        (t.tgtype::int & 4) <> 0 AS on_insert,
        (t.tgtype::int & 16) <> 0 AS on_update,
        (t.tgtype::int & 8) <> 0 AS on_delete,
        (t.tgtype::int & 32) <> 0 AS on_truncate,
        (t.tgenabled <> 'D') AS is_enabled,
        fn.nspname || '.' || p.proname AS action_routine,
        NULL AS body,
        'a PostgreSQL trigger has no body of its own: it executes the function '
            || 'named in action_routine, whose own body is captured on the routine axis'
            AS unavailable_reason
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_proc p ON p.oid = t.tgfoid
    JOIN pg_namespace fn ON fn.oid = p.pronamespace
    WHERE NOT t.tgisinternal
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY n.nspname, c.relname, t.tgname
"""

# R11-FP01: sequences.
#
# Read from `pg_sequence`, not from `pg_sequences`: the latter is a view that
# carries `last_value`, and `last_value` is the value the next insert writes
# into a customer's row -- source data, not metadata (INV-6). `pg_sequence` has
# the declaration and nothing else, so the value cannot be read here even by
# accident. That is why this query exists in this shape rather than as a
# `SELECT * FROM pg_sequences`.
#
# The `pg_depend` join is the reason a sequence is part of the footprint rather
# than a loose object: it names the table and column whose default expression
# reads this sequence. `deptype` 'a' is the `serial` case (the sequence is owned
# by the column and dropped with it) and 'i' is an `IDENTITY` column's internal
# dependency. A standalone `CREATE SEQUENCE` matches neither and honestly
# reports no owner.
_SEQUENCE_SQL = """
    SELECT
        n.nspname AS sequence_schema,
        c.relname AS sequence_name,
        format_type(s.seqtypid, NULL) AS data_type,
        s.seqstart::text AS start_with,
        s.seqincrement::text AS increment_by,
        s.seqmin::text AS minimum_bound,
        s.seqmax::text AS maximum_bound,
        s.seqcache::text AS cache_size,
        s.seqcycle AS cycles,
        owner.relname AS owned_by_table,
        att.attname AS owned_by_column,
        obj_description(c.oid, 'pg_class') AS description
    FROM pg_sequence s
    JOIN pg_class c ON c.oid = s.seqrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_depend d
      ON d.classid = 'pg_class'::regclass
     AND d.objid = c.oid
     AND d.refclassid = 'pg_class'::regclass
     AND d.deptype IN ('a', 'i')
    LEFT JOIN pg_class owner ON owner.oid = d.refobjid
    LEFT JOIN pg_attribute att
      ON att.attrelid = d.refobjid
     AND att.attnum = d.refobjsubid
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY n.nspname, c.relname
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


# R11-FP01. A trigger is keyed by the table it fires on, so it pages with the
# table roster exactly as columns and constraints do -- unlike routines and
# sequences, which belong to a schema and are fetched once up front. A 100K-
# table estate can hold more triggers than tables (audit estates routinely have
# three per table), which is precisely the shape `discover_streaming` exists for.
_TRIGGER_BATCH_SQL = (
    """
    SELECT
        n.nspname AS trigger_schema,
        t.tgname AS trigger_name,
        c.relname AS table_name,
        n.nspname AS table_schema,
        CASE
            WHEN (t.tgtype::int & 64) <> 0 THEN 'INSTEAD OF'
            WHEN (t.tgtype::int & 2) <> 0 THEN 'BEFORE'
            ELSE 'AFTER'
        END AS timing,
        CASE WHEN (t.tgtype::int & 1) <> 0 THEN 'ROW' ELSE 'STATEMENT' END AS orientation,
        (t.tgtype::int & 4) <> 0 AS on_insert,
        (t.tgtype::int & 16) <> 0 AS on_update,
        (t.tgtype::int & 8) <> 0 AS on_delete,
        (t.tgtype::int & 32) <> 0 AS on_truncate,
        (t.tgenabled <> 'D') AS is_enabled,
        fn.nspname || '.' || p.proname AS action_routine,
        NULL AS body,
        'a PostgreSQL trigger has no body of its own: it executes the function '
            || 'named in action_routine, whose own body is captured on the routine axis'
            AS unavailable_reason
    FROM pg_trigger t
    JOIN pg_class c ON c.oid = t.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_proc p ON p.oid = t.tgfoid
    JOIN pg_namespace fn ON fn.oid = p.pronamespace
    WHERE NOT t.tgisinternal
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      {predicate}
    ORDER BY n.nspname, c.relname, t.tgname
"""
).format(
    predicate=_batch_predicate("n.nspname", "c.relname")
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



# R11-FP02: what this login may not see. `pg_class` is readable by every role, so the
# unfiltered estate can be counted even where `information_schema` hides most of it.
#
# Deliberately only the kinds whose roster *is* permission-filtered. `_TABLE_ROSTER_SQL` reads
# `information_schema.tables`, so a table or view this role holds no privilege on never reaches
# discovery -- that is what is counted here, as the exact negation of that view's own visibility
# rule. Materialized views and routines are read from `pg_class`/`pg_proc` directly
# (`_MATERIALIZED_VIEW_ROSTER_SQL`, `_ROUTINE_SQL`), which every role may read, so none of them
# is ever hidden from discovery and counting the ones this role cannot SELECT or EXECUTE would
# report a gap that does not exist. R11-FP01's two new axes are the same case: `pg_trigger` and
# `pg_sequence` are readable by every role, so no trigger and no sequence is hidden from this
# connector, and neither kind is counted here.
_INVISIBLE_TABLE_SQL = """
    -- `relkind` is PostgreSQL's `"char"`, which asyncpg hands back as bytes; ::text keeps
    -- the lookup below reading the letter the catalog means.
    SELECT c.relkind::text AS relkind, COUNT(*) AS invisible
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'v', 'f')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg\\_toast%' ESCAPE '\\'
      AND n.nspname NOT LIKE 'pg\\_temp%' ESCAPE '\\'
      AND NOT (
          pg_has_role(c.relowner, 'USAGE')
          OR has_table_privilege(
              c.oid, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'
          )
          OR has_any_column_privilege(c.oid, 'SELECT, INSERT, UPDATE, REFERENCES')
      )
    GROUP BY c.relkind
"""

#: `pg_class.relkind` as a selection kind. A foreign table is a table Atlas reads as one.
_RELKIND_TO_KIND = {"r": "TABLE", "p": "TABLE", "f": "TABLE", "v": "VIEW"}


# ---------------------------------------------------------------------------
# R11-FP02: which facet each discovery query belongs to.
#
# Every read below goes through `connectors.discovery.read_facet` under the
# facet it answers, so a login that may not read one catalog relation loses
# that relation rather than the run. asyncpg is the driver this is provable
# with: `asyncpg.PostgresError.sqlstate` is the SQLSTATE the server sent, so
# `capability_states.is_permission_refusal` sees `42501` for an
# insufficient-privilege refusal and `classify_read_failure` returns
# PERMISSION_DENIED rather than the under-claiming UNAVAILABLE the drivers
# without a SQLSTATE field have to settle for
# (`tests/test_facet_refusal_live.py` pins it against a real revoke).
#
# Two reads here are deliberately *not* wrapped:
#
# * **The roster and the column queries are the inventory**, and they are
#   wrapped as `FACET_INVENTORY`, which is not the same thing as being made
#   optional: `RETIREMENT_BEARING_FACETS` holds that facet, so `read_facet`
#   records the refusal and then lets it fail the run exactly as it did before.
#   A FULL run that completed over zero objects would retire the estate. What
#   the wrap buys is the receipt entry -- the INTERRUPTED receipt now names the
#   read that ended the run instead of saying only that something did.
#
# * **The trigger and sequence queries have no facet to be attributed to.**
#   `DISCOVERY_FACETS` (and `discovery_receipt.RECEIPT_FACETS` with it) names
#   eight facets, and neither triggers nor sequences is one of them, so there
#   is no name `FacetReadScope.record` would accept and none that a receipt
#   could publish. Inventing one here would raise `unknown discovery facet` at
#   the moment of the refusal -- turning a recoverable refusal into a crash --
#   so `_TRIGGER_SQL`, `_TRIGGER_BATCH_SQL` and `_SEQUENCE_SQL` keep failing the
#   run until the facet vocabulary gains them (see the R11-FP02 remainder).
# ---------------------------------------------------------------------------


async def _fetch_scalar_rows(connection: Any, sql: str) -> list[Any]:
    """One scalar metadata read, shaped as rows so `read_facet` can carry it.

    R11-FP02: `read_facet` takes a read that returns a row sequence, because
    every other discovery query returns one. The catalog comment
    (`_CATALOG_COMMENT_SQL`) is the single discovery read that is one value, and
    it belongs to `object_comments` like the three comment queries beside it --
    so it is shaped as a one-row list here rather than left as the one comment
    read whose refusal still costs the whole run.
    """
    return [await connection.fetchval(sql)]


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
        # R11-FP01. Each backed by a query in `discover()`, which is what INV-9
        # requires of a `True`:
        #   triggers  -> pg_trigger (+ pg_proc for the action function)
        #   sequences -> pg_sequence (+ pg_depend for the owning column)
        triggers=True,
        sequences=True,
        # PR-2: the only connector today with a real `profile_column_values`
        # implementation below -- every other connector stays honestly
        # unsupported (default False) rather than simulating this capability.
        value_range_profiling=True,
        # R11-FP04: `_entropy_expression` below is the query behind this flag,
        # and INV-9 is what requires the flag to be backed by one. Postgres
        # only: `log(numeric, numeric)` plus an aggregate under a window is the
        # combination that makes the whole facet one more expression on the
        # bounded scan rather than a round trip per column, and it is the
        # engine whose profiling path this repo can actually exercise. Every
        # other connector reports ENTROPY UNSUPPORTED per column instead of
        # returning None and letting a reader read "constant" into it.
        distribution_entropy_profiling=True,
    )

    def __init__(self, dsn: str, *, command_timeout: float = 30.0) -> None:
        self._dsn = dsn
        self._command_timeout = command_timeout
        self._schema_scope = SchemaScope()

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.DEFAULT_CAPABILITIES

    async def test_connection(self) -> None:
        connection = await asyncpg.connect(self._dsn, command_timeout=self._command_timeout)
        try:
            await connection.fetchval("SELECT 1")
        finally:
            await connection.close()

    def scope_discovery(self, *, include_schemas: list[str], exclude_schemas: list[str]) -> bool:
        self._schema_scope = schema_scope(include_schemas, exclude_schemas)
        return self._schema_scope.restricted

    async def _fetch(self, connection: Any, sql: str, *arguments: Any) -> list[Any]:
        """A discovery query, restricted to the pushed-down schema scope if there is one."""
        scoped, bound = scoped_postgres_query(sql, self._schema_scope, arguments)
        return list(await connection.fetch(scoped, *bound))

    async def count_invisible_objects(self) -> dict[str, int] | None:
        """R11-FP02: objects in the pushed-down scope this login holds no privilege on.

        Read from `pg_class`, which every role may read, so the answer covers the whole
        database rather than the part `information_schema` returns. Tables and views only --
        see `_INVISIBLE_TABLE_SQL` for why a materialized view or a routine is never hidden
        from this connector. The schema scope applies here exactly as it does to every other
        query, so a schema the selection excludes is not counted as hidden: it was not asked
        for, which is a different fact.
        """
        connection = await asyncpg.connect(self._dsn, command_timeout=self._command_timeout)
        try:
            counts: dict[str, int] = {}
            for relkind, invisible in await self._fetch(connection, _INVISIBLE_TABLE_SQL):
                kind = _RELKIND_TO_KIND.get(relkind)
                if kind is not None and invisible:
                    counts[kind] = counts.get(kind, 0) + int(invisible)
            return counts
        finally:
            await connection.close()

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        connection = await asyncpg.connect(self._dsn, command_timeout=self._command_timeout)
        try:
            catalog_name = await connection.fetchval("SELECT current_database()")
            rows = await read_facet(
                FACET_INVENTORY,
                self._fetch(
                    connection,
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
                    """,
                ),
            )
            constraint_rows = await read_facet(
                FACET_CONSTRAINTS,
                self._fetch(
                    connection,
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
                    """,
                ),
            )
            materialized_view_column_rows = await read_facet(
                FACET_INVENTORY,
                self._fetch(connection, _MATERIALIZED_VIEW_COLUMN_SQL),
            )
            view_rows = await read_facet(
                FACET_VIEW_DEFINITIONS, self._fetch(connection, _VIEW_DEFINITION_SQL)
            )
            routine_rows = await read_facet(
                FACET_ROUTINE_BODIES, self._fetch(connection, _ROUTINE_SQL)
            )
            # R11-FP01: aggregates and window functions, whose definition
            # `pg_get_functiondef` refuses -- see `_AGGREGATE_ROUTINE_SQL`.
            aggregate_routine_rows = await read_facet(
                FACET_ROUTINE_BODIES, self._fetch(connection, _AGGREGATE_ROUTINE_SQL)
            )
            routine_parameter_rows = await read_facet(
                FACET_ROUTINE_BODIES, self._fetch(connection, _ROUTINE_PARAMETER_SQL)
            )
            trigger_rows = await read_facet(
                FACET_TRIGGERS, self._fetch(connection, _TRIGGER_SQL)
            )
            sequence_rows = await read_facet(
                FACET_SEQUENCES, self._fetch(connection, _SEQUENCE_SQL)
            )
            table_description_rows = await read_facet(
                FACET_OBJECT_COMMENTS, self._fetch(connection, _TABLE_COMMENT_SQL)
            )
            column_description_rows = await read_facet(
                FACET_OBJECT_COMMENTS, self._fetch(connection, _COLUMN_COMMENT_SQL)
            )
            schema_description_rows = await read_facet(
                FACET_OBJECT_COMMENTS, self._fetch(connection, _SCHEMA_COMMENT_SQL)
            )
            catalog_comment_rows = await read_facet(
                FACET_OBJECT_COMMENTS, _fetch_scalar_rows(connection, _CATALOG_COMMENT_SQL)
            )
            catalog_description = catalog_comment_rows[0] if catalog_comment_rows else None
            grant_rows = await read_facet(FACET_GRANTS, self._fetch(connection, _GRANT_SQL))
            index_rows = await read_facet(FACET_INDEXES, self._fetch(connection, _INDEX_SQL))
            partition_key_rows = await read_facet(
                FACET_PARTITIONS, self._fetch(connection, _PARTITION_KEY_SQL)
            )
            partition_rows = await read_facet(
                FACET_PARTITIONS, self._fetch(connection, _PARTITION_SQL)
            )
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

        # R11-FP01: the two new axes are attached to the assembled tree rather
        # than passed into `assemble_catalog`, which takes no parameter for
        # them -- see `attach_native_objects` for why the helper lives in
        # `connectors.base` this cycle.
        return attach_native_objects(
            assemble_catalog(
                str(catalog_name),
                tables,
                routines=build_routines(
                    [*routine_rows, *aggregate_routine_rows], routine_parameter_rows
                ),
                grants=build_grants(grant_rows),
                schema_descriptions={
                    str(row["schema_name"]): str(row["description"])
                    for row in schema_description_rows
                },
                catalog_description=(
                    None if catalog_description is None else str(catalog_description)
                ),
            ),
            triggers=build_triggers(trigger_rows),
            sequences=build_sequences(sequence_rows),
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
            # R11-FP02: the roster *is* the inventory, so it is read under
            # `FACET_INVENTORY` -- recorded on refusal and then still allowed to fail
            # the run, because `RETIREMENT_BEARING_FACETS` holds that facet. See the
            # facet-attribution comment above `_fetch_scalar_rows`.
            roster_rows = await read_facet(
                FACET_INVENTORY, self._fetch(connection, _TABLE_ROSTER_SQL)
            )
            materialized_roster_rows = await read_facet(
                FACET_INVENTORY, self._fetch(connection, _MATERIALIZED_VIEW_ROSTER_SQL)
            )
            roster = sorted(
                {
                    (str(row["table_schema"]), str(row["table_name"]))
                    for row in (*roster_rows, *materialized_roster_rows)
                }
            )
            if not roster:
                yield (DiscoveredCatalog(name=catalog_name, schemas=()),)
                return

            routine_rows = await read_facet(
                FACET_ROUTINE_BODIES, self._fetch(connection, _ROUTINE_SQL)
            )
            aggregate_routine_rows = await read_facet(
                FACET_ROUTINE_BODIES, self._fetch(connection, _AGGREGATE_ROUTINE_SQL)
            )
            routine_parameter_rows = await read_facet(
                FACET_ROUTINE_BODIES, self._fetch(connection, _ROUTINE_PARAMETER_SQL)
            )
            # R11-FP01: a sequence belongs to a schema, not to a table, so it
            # joins routines and schema comments in the up-front, attach-to-the-
            # first-batch group. Triggers do not: see `_TRIGGER_BATCH_SQL`.
            sequence_rows = await read_facet(
                FACET_SEQUENCES, self._fetch(connection, _SEQUENCE_SQL)
            )
            schema_description_rows = await read_facet(
                FACET_OBJECT_COMMENTS, self._fetch(connection, _SCHEMA_COMMENT_SQL)
            )
            catalog_comment_rows = await read_facet(
                FACET_OBJECT_COMMENTS, _fetch_scalar_rows(connection, _CATALOG_COMMENT_SQL)
            )
            catalog_description = catalog_comment_rows[0] if catalog_comment_rows else None
            routines = build_routines(
                [*routine_rows, *aggregate_routine_rows], routine_parameter_rows
            )
            sequences = build_sequences(sequence_rows)
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

                column_rows = await read_facet(
                    FACET_INVENTORY,
                    self._fetch(connection, _COLUMN_BATCH_SQL, schemas_arr, names_arr),
                )
                materialized_view_column_rows = await read_facet(
                    FACET_INVENTORY,
                    self._fetch(
                        connection,
                        _MATERIALIZED_VIEW_COLUMN_BATCH_SQL, schemas_arr, names_arr
                    ),
                )
                constraint_rows = await read_facet(
                    FACET_CONSTRAINTS,
                    self._fetch(
                        connection,
                        _CONSTRAINT_BATCH_SQL, schemas_arr, names_arr
                    ),
                )
                view_rows = await read_facet(
                    FACET_VIEW_DEFINITIONS,
                    self._fetch(
                        connection,
                        _VIEW_DEFINITION_BATCH_SQL, schemas_arr, names_arr
                    ),
                )
                table_description_rows = await read_facet(
                    FACET_OBJECT_COMMENTS,
                    self._fetch(
                        connection,
                        _TABLE_COMMENT_BATCH_SQL, schemas_arr, names_arr
                    ),
                )
                column_description_rows = await read_facet(
                    FACET_OBJECT_COMMENTS,
                    self._fetch(
                        connection,
                        _COLUMN_COMMENT_BATCH_SQL, schemas_arr, names_arr
                    ),
                )
                trigger_rows = await read_facet(
                    FACET_TRIGGERS,
                    self._fetch(connection, _TRIGGER_BATCH_SQL, schemas_arr, names_arr),
                )
                grant_rows = await read_facet(
                    FACET_GRANTS,
                    self._fetch(connection, _GRANT_BATCH_SQL, schemas_arr, names_arr),
                )
                index_rows = await read_facet(
                    FACET_INDEXES,
                    self._fetch(connection, _INDEX_BATCH_SQL, schemas_arr, names_arr),
                )
                partition_key_rows = await read_facet(
                    FACET_PARTITIONS,
                    self._fetch(
                        connection,
                        _PARTITION_KEY_BATCH_SQL, schemas_arr, names_arr
                    ),
                )
                partition_rows = await read_facet(
                    FACET_PARTITIONS,
                    self._fetch(
                        connection,
                        _PARTITION_BATCH_SQL, schemas_arr, names_arr
                    ),
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
                yield attach_native_objects(
                    assemble_catalog(
                        catalog_name,
                        tables,
                        routines=routines if is_first_batch else None,
                        grants=build_grants(grant_rows),
                        schema_descriptions=schema_descriptions if is_first_batch else None,
                        catalog_description=(
                            catalog_description_str if is_first_batch else None
                        ),
                    ),
                    triggers=build_triggers(trigger_rows),
                    sequences=sequences if is_first_batch else None,
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
                        # R11-FP04: value-free distribution shape, on the same
                        # bounded scan -- counts per code-defined length bucket
                        # plus blank/whitespace-only counts. No boundary, mode
                        # or exemplar is read back (ADR-0014).
                        expressions.extend(
                            value_free_distribution_expressions(
                                position=position,
                                text_form=f"{quoted}::text",
                                length_form=f"LENGTH({quoted}::text)",
                                trimmed_form=f"BTRIM({quoted}::text)",
                            )
                        )
                        expressions.append(_entropy_expression(quoted, position))
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
                        blank, whitespace, buckets = read_value_free_distribution(
                            position, row.__getitem__
                        )
                        entropy = row[f"en_{position}"]
                        snapshots.append(
                            ColumnProfileSnapshot(
                                name=name,
                                null_count=int(row[f"n_{position}"]),
                                non_null_count=int(row[f"nn_{position}"]),
                                approximate_distinct_count=int(row[f"d_{position}"]),
                                min_length=row[f"minl_{position}"],
                                max_length=row[f"maxl_{position}"],
                                blank_count=blank,
                                whitespace_only_count=whitespace,
                                length_bucket_counts=buckets,
                                frequency_entropy_bits=(
                                    None if entropy is None else float(entropy)
                                ),
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
            # R11-FP04: the `LIMIT` above is the bound, so the scope follows
            # from whether it bit -- never from comparing the sample with
            # `pg_class.reltuples`, which is an estimate that can sit either
            # side of the truth.
            observation_scope=bounded_scan_scope(
                sampled_row_count=sampled_row_count, sample_rows=sample_rows
            ),
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
