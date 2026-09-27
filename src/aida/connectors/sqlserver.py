import asyncio
import copy
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import defusedxml.ElementTree as ET
import pytds

from aida.connectors.base import (
    ENTROPY_NOT_IMPLEMENTED,
    ColumnProfileSnapshot,
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
from aida.connectors.capability_certification import derive_capabilities
from aida.connectors.discovery import (
    FACET_CONSTRAINTS,
    FACET_GRANTS,
    FACET_INVENTORY,
    FACET_OBJECT_COMMENTS,
    FACET_ROUTINE_BODIES,
    FACET_SEQUENCES,
    FACET_TRIGGERS,
    FACET_VIEW_DEFINITIONS,
    append_grouped_foreign_key_rows,
    append_grouped_key_rows,
    apply_column_descriptions,
    apply_table_descriptions,
    apply_view_definitions,
    assemble_catalog,
    build_grants,
    build_routines,
    build_table_map_from_column_rows,
    read_facet,
)
from aida.connectors.schema_scope import SchemaScope, schema_scope, scoped_sqlserver_query
from aida.connectors.sql_execution import SqlExecutor
from aida.connectors.sqlserver_pool import borrow, pool_key

_SHOWPLAN_NS = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"
_EXCLUDED_SCHEMAS = ("sys", "INFORMATION_SCHEMA")

# --- envelope 1.1 (gap/02 N1) ------------------------------------------------
#
# Every definition below reads `sys.sql_modules.definition` rather than
# `INFORMATION_SCHEMA.ROUTINES.ROUTINE_DEFINITION` or `syscomments`. That is not
# a style preference: `ROUTINE_DEFINITION` is `nvarchar(4000)` and silently
# truncates every longer body, which is precisely the class of object -- a long
# ETL procedure -- whose text is worth parsing. `sys.sql_modules.definition` is
# `nvarchar(max)` and does not truncate, so this connector reports
# `truncated = false` because it is true, not because it did not check.
#
# A NULL definition means the module is encrypted (`WITH ENCRYPTION`) or is not
# visible to this principal. That is recorded as *unavailable with a reason*, so
# a downstream parser can tell it apart from a view with an empty body.

_VIEW_DEFINITION_SQL = """
    SELECT
        s.name AS table_schema,
        v.name AS table_name,
        m.definition AS definition,
        CAST(OBJECTPROPERTY(v.object_id, 'IsIndexed') AS int) AS is_materialized,
        CAST(OBJECTPROPERTY(v.object_id, 'IsUpdatable') AS int) AS is_updatable,
        CASE WHEN v.with_check_option = 1 THEN 'CASCADED' ELSE NULL END AS check_option,
        CASE
            WHEN m.definition IS NULL
            THEN 'sys.sql_modules returned no definition: the module is encrypted '
                 + 'or not visible to this principal'
            ELSE NULL
        END AS unavailable_reason
    FROM sys.views v
    JOIN sys.schemas s ON s.schema_id = v.schema_id
    LEFT JOIN sys.sql_modules m ON m.object_id = v.object_id
    WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
    ORDER BY s.name, v.name
"""

_ROUTINE_SQL = """
    SELECT
        s.name AS routine_schema,
        o.name AS routine_name,
        CAST(o.object_id AS varchar(30)) AS specific_name,
        CASE o.type WHEN 'P' THEN 'PROCEDURE' ELSE 'FUNCTION' END AS routine_type,
        CASE o.type
            WHEN 'FN' THEN 'SCALAR'
            WHEN 'IF' THEN 'INLINE_TABLE'
            WHEN 'TF' THEN 'MULTI_STATEMENT_TABLE'
        END AS native_subtype,
        'SQL' AS language,
        m.definition AS body,
        TYPE_NAME(ret.user_type_id) AS return_type,
        CAST(OBJECTPROPERTY(o.object_id, 'IsDeterministic') AS int) AS is_deterministic,
        CASE
            WHEN m.execute_as_principal_id IS NULL THEN 'INVOKER' ELSE 'DEFINER'
        END AS security_mode,
        CAST(ep.value AS nvarchar(max)) AS description,
        CASE
            WHEN m.definition IS NULL
            THEN 'sys.sql_modules returned no definition: the module is encrypted '
                 + 'or not visible to this principal'
            ELSE NULL
        END AS unavailable_reason
    FROM sys.objects o
    JOIN sys.schemas s ON s.schema_id = o.schema_id
    LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
    LEFT JOIN sys.parameters ret
      ON ret.object_id = o.object_id
     AND ret.parameter_id = 0
    LEFT JOIN sys.extended_properties ep
      ON ep.class = 1
     AND ep.major_id = o.object_id
     AND ep.minor_id = 0
     AND ep.name = 'MS_Description'
    WHERE o.type IN ('P', 'FN', 'IF', 'TF')
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
    ORDER BY s.name, o.name, o.object_id
"""

_ROUTINE_PARAMETER_SQL = """
    SELECT
        s.name AS routine_schema,
        CAST(p.object_id AS varchar(30)) AS specific_name,
        p.name AS parameter_name,
        p.parameter_id AS ordinal_position,
        CASE WHEN p.is_output = 1 THEN 'OUT' ELSE 'IN' END AS parameter_mode,
        TYPE_NAME(p.user_type_id) AS data_type,
        CASE
            WHEN p.has_default_value = 1
            THEN CONVERT(nvarchar(4000), p.default_value)
            ELSE NULL
        END AS parameter_default
    FROM sys.parameters p
    JOIN sys.objects o ON o.object_id = p.object_id
    JOIN sys.schemas s ON s.schema_id = o.schema_id
    WHERE o.type IN ('P', 'FN', 'IF', 'TF')
      AND p.parameter_id > 0
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
    ORDER BY s.name, p.object_id, p.parameter_id
"""

# R11-FP01: triggers.
#
# `sys.triggers` carries the trigger and its parent object; `sys.trigger_events`
# carries one row per event, which is why the three events arrive as MAX(CASE)
# flags over a grouped join rather than as three rows per trigger. The body
# comes from `sys.sql_modules.definition` for exactly the reason every other
# definition in this module does (see the header note): it is `nvarchar(max)`
# and does not truncate, where `syscomments` and INFORMATION_SCHEMA do.
#
# A NULL definition means the trigger is encrypted or invisible to this
# principal, recorded as unavailable with a reason -- never as a trigger with an
# empty body.
#
# `is_ms_shipped = 0` excludes system triggers. DDL and logon triggers are
# deliberately out of scope and their absence is a scope decision rather than an
# oversight: they have no parent table (`parent_id = 0`), so they are not a data
# path between two objects, which is the whole reason this axis exists. The
# `parent_class = 1` predicate is what excludes them, and it is spelled as a
# predicate rather than left to the join so this is visible.
# The events arrive through an `OUTER APPLY` rather than a `GROUP BY` over the
# joined `sys.trigger_events` rows, because `sys.sql_modules.definition` is
# `nvarchar(max)` and SQL Server refuses `MAX()` on that type -- grouping the
# join would force exactly that aggregate and fail at run time on every
# instance. Applying the aggregate to the event table alone keeps the body in
# the outer projection where no aggregate touches it.
#
# `timing` has no BEFORE branch because SQL Server has no BEFORE trigger: a DML
# trigger is AFTER or INSTEAD OF, full stop. `orientation` is the constant
# STATEMENT for the same kind of reason -- T-SQL has no `FOR EACH ROW`, so every
# DML trigger fires once per statement, and reporting it as a constant fact
# about the engine is honest where reporting NULL would read as "unknown".
_TRIGGER_SQL = """
    SELECT
        s.name AS trigger_schema,
        tr.name AS trigger_name,
        o.name AS table_name,
        s.name AS table_schema,
        CASE
            WHEN OBJECTPROPERTY(tr.object_id, 'ExecIsInsteadOfTrigger') = 1
            THEN 'INSTEAD OF'
            ELSE 'AFTER'
        END AS timing,
        'STATEMENT' AS orientation,
        ev.on_insert AS on_insert,
        ev.on_update AS on_update,
        ev.on_delete AS on_delete,
        CAST(0 AS int) AS on_truncate,
        CASE WHEN tr.is_disabled = 1 THEN 0 ELSE 1 END AS is_enabled,
        m.definition AS body,
        CASE
            WHEN m.definition IS NULL
            THEN 'sys.sql_modules returned no definition: the module is encrypted '
                 + 'or not visible to this principal'
            ELSE NULL
        END AS unavailable_reason
    FROM sys.triggers tr
    JOIN sys.objects o ON o.object_id = tr.parent_id
    JOIN sys.schemas s ON s.schema_id = o.schema_id
    LEFT JOIN sys.sql_modules m ON m.object_id = tr.object_id
    OUTER APPLY (
        SELECT
            MAX(CASE WHEN te.type_desc = 'INSERT' THEN 1 ELSE 0 END) AS on_insert,
            MAX(CASE WHEN te.type_desc = 'UPDATE' THEN 1 ELSE 0 END) AS on_update,
            MAX(CASE WHEN te.type_desc = 'DELETE' THEN 1 ELSE 0 END) AS on_delete
        FROM sys.trigger_events te
        WHERE te.object_id = tr.object_id
    ) ev
    WHERE tr.is_ms_shipped = 0
      AND tr.parent_class = 1
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
    ORDER BY s.name, o.name, tr.name
"""

# R11-FP01: sequences.
#
# `sys.sequences` also carries `current_value` and `last_used_value`, and
# neither is selected here: they are the value the next insert writes into a
# customer's row, which is source data rather than metadata (INV-6). Only the
# declaration is read.
#
# A SQL Server sequence is standalone -- there is no owning column to report,
# because an `IDENTITY` column is a column property and not a sequence object --
# so `owned_by_table` / `owned_by_column` are honestly absent rather than
# guessed from a default constraint's text.
_SEQUENCE_SQL = """
    SELECT
        s.name AS sequence_schema,
        sq.name AS sequence_name,
        TYPE_NAME(sq.user_type_id) AS data_type,
        CONVERT(nvarchar(64), sq.start_value) AS start_with,
        CONVERT(nvarchar(64), sq.increment) AS increment_by,
        CONVERT(nvarchar(64), sq.minimum_value) AS minimum_bound,
        CONVERT(nvarchar(64), sq.maximum_value) AS maximum_bound,
        CONVERT(nvarchar(64), sq.cache_size) AS cache_size,
        CASE WHEN sq.is_cycling = 1 THEN 1 ELSE 0 END AS cycles,
        CAST(ep.value AS nvarchar(max)) AS description
    FROM sys.sequences sq
    JOIN sys.schemas s ON s.schema_id = sq.schema_id
    LEFT JOIN sys.extended_properties ep
      ON ep.class = 1
     AND ep.major_id = sq.object_id
     AND ep.minor_id = 0
     AND ep.name = 'MS_Description'
    WHERE s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
    ORDER BY s.name, sq.name
"""

_TABLE_COMMENT_SQL = """
    SELECT
        s.name AS table_schema,
        o.name AS table_name,
        CAST(ep.value AS nvarchar(max)) AS description
    FROM sys.extended_properties ep
    JOIN sys.objects o ON o.object_id = ep.major_id
    JOIN sys.schemas s ON s.schema_id = o.schema_id
    WHERE ep.class = 1
      AND ep.minor_id = 0
      AND ep.name = 'MS_Description'
      AND o.type IN ('U', 'V')
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
    ORDER BY s.name, o.name
"""

_COLUMN_COMMENT_SQL = """
    SELECT
        s.name AS table_schema,
        o.name AS table_name,
        c.name AS column_name,
        CAST(ep.value AS nvarchar(max)) AS description
    FROM sys.extended_properties ep
    JOIN sys.objects o ON o.object_id = ep.major_id
    JOIN sys.schemas s ON s.schema_id = o.schema_id
    JOIN sys.columns c
      ON c.object_id = ep.major_id
     AND c.column_id = ep.minor_id
    WHERE ep.class = 1
      AND ep.minor_id > 0
      AND ep.name = 'MS_Description'
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
    ORDER BY s.name, o.name, c.column_id
"""

_SCHEMA_COMMENT_SQL = """
    SELECT
        s.name AS schema_name,
        CAST(ep.value AS nvarchar(max)) AS description
    FROM sys.extended_properties ep
    JOIN sys.schemas s ON s.schema_id = ep.major_id
    WHERE ep.class = 3
      AND ep.name = 'MS_Description'
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
    ORDER BY s.name
"""

_CATALOG_COMMENT_SQL = """
    SELECT TOP (1) CAST(ep.value AS nvarchar(max)) AS description
    FROM sys.extended_properties ep
    WHERE ep.class = 0
      AND ep.name = 'MS_Description'
"""

# Object-level GRANT and GRANT WITH GRANT OPTION only. DENY (state 'D') is
# deliberately excluded: this axis answers "who can already read this", and a
# DENY is not a privilege. Modelling revocation would need a second axis and a
# resolution rule, which nothing downstream consumes yet -- so it is left out
# rather than half-represented.
_GRANT_SQL = """
    SELECT
        s.name AS schema_name,
        g.name AS grantee,
        CASE
            WHEN g.type IN ('R', 'A') THEN 'ROLE' ELSE 'USER'
        END AS grantee_type,
        dp.permission_name AS privilege,
        CASE o.type
            WHEN 'V' THEN 'VIEW'
            WHEN 'P' THEN 'PROCEDURE'
            WHEN 'FN' THEN 'FUNCTION'
            WHEN 'IF' THEN 'FUNCTION'
            WHEN 'TF' THEN 'FUNCTION'
            ELSE 'TABLE'
        END AS object_type,
        o.name AS object_name,
        CASE WHEN dp.state = 'W' THEN 1 ELSE 0 END AS is_grantable
    FROM sys.database_permissions dp
    JOIN sys.database_principals g ON g.principal_id = dp.grantee_principal_id
    JOIN sys.objects o ON o.object_id = dp.major_id
    JOIN sys.schemas s ON s.schema_id = o.schema_id
    WHERE dp.class = 1
      AND dp.state IN ('G', 'W')
      AND s.name NOT IN ('sys', 'INFORMATION_SCHEMA')
    ORDER BY s.name, o.name, g.name, dp.permission_name
"""


def _quote_identifier(identifier: str) -> str:
    return "[" + identifier.replace("]", "]]") + "]"


@dataclass(frozen=True, slots=True)
class _ConnectionParams:
    host: str
    port: int
    database: str
    user: str
    password: str


def _parse_dsn(dsn: str) -> _ConnectionParams:
    """Parse an opaque resolved-secret value shaped as mssql://user:password@host:port/database.

    The credential_reference the API accepts is never a connection string; only the
    secret value it resolves to may be. This mirrors how PostgresConnector treats its
    resolved secret as a driver-ready DSN, adapted because pytds.connect() takes
    discrete host/port/database/user/password arguments rather than a URL.
    """
    parsed = urlsplit(dsn)
    if parsed.scheme not in {"mssql", "sqlserver"}:
        raise ValueError(
            "invalid SQL Server connection reference; expected "
            "mssql://user:password@host:port/database"
        )
    if not parsed.hostname or not parsed.username or parsed.password is None:
        raise ValueError("SQL Server connection reference is missing host, user, or password")
    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError("SQL Server connection reference must include a database name")
    return _ConnectionParams(
        host=parsed.hostname,
        port=parsed.port or 1433,
        database=database,
        user=unquote(parsed.username),
        password=unquote(parsed.password),
    )


def _extract_showplan_estimate(raw_xml: str) -> QueryEstimate:
    """Parse a SHOWPLAN_XML document into the connector-agnostic estimate contract."""
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise RuntimeError("source returned an invalid SHOWPLAN_XML document") from exc
    statement = root.find(f".//{_SHOWPLAN_NS}StmtSimple")
    if statement is None:
        statement = root.find(f".//{_SHOWPLAN_NS}StmtCond")
    if statement is None:
        raise RuntimeError("source returned a SHOWPLAN_XML document without a statement node")
    cost_text = statement.get("StatementSubTreeCost")
    if cost_text is None:
        raise RuntimeError("source returned a SHOWPLAN_XML statement without a subtree cost")
    try:
        total_cost = float(cost_text)
    except ValueError as exc:
        raise RuntimeError("source returned a non-numeric SHOWPLAN_XML subtree cost") from exc
    raw_rows = statement.get("StatementEstRows")
    estimated_rows: float | None
    if raw_rows is None:
        estimated_rows = None
    else:
        try:
            estimated_rows = float(raw_rows)
        except ValueError:
            estimated_rows = None
    return QueryEstimate(
        score=total_cost,
        kind="SHOWPLAN_XML",
        estimated_rows=estimated_rows,
        evidence={
            "Plan": {
                "Total Cost": total_cost,
                "Node Type": statement.get("StatementType", "SELECT"),
            },
            "dialect": "tsql",
            "estimated_rows": raw_rows,
        },
    )


# ---------------------------------------------------------------------------
# R11-FP02: one facet read's result, carried out of the driver thread.
#
# `read_facet` is a coroutine, and every read below happens inside one
# `asyncio.to_thread` hop because `pytds` is a synchronous driver that declares
# `threadsafety = 1` -- threads may share the module, not a connection. Giving
# each facet its own thread hop so it could be awaited individually would pass
# one connection between pool threads, which is exactly what that declaration
# says not to do. So the thread reads each facet and *captures* its failure
# instead of judging it, and the coroutine that owns the run replays each
# capture into `read_facet`, which classifies it, records it against the facet
# and decides whether it may be absorbed. The judgement stays in the one place
# it belongs (`connectors.discovery`), and nothing here second-guesses it.
#
# What this changes about failure order, deliberately: today the first failing
# query aborts the rest, and a captured failure lets the queries after it run
# before it surfaces. That costs a few doomed round trips on a source that has
# stopped answering, and buys the thing this feature is for -- a refusal of one
# facet no longer costs the facets read after it.
#
# **SQL Server's error-code reality.** `pytds` exceptions carry `msg_no`,
# `number`, `severity` and a TDS `state` byte, and no `sqlstate` field at all
# (verified against the installed driver). `number` is the server's own error
# number -- the `sys.messages.message_id` of the ERROR token, a structured field
# rather than text -- so since R11-FP02's follow-through
# `capability_states.is_permission_refusal` reads it
# (`SQLSERVER_PRIVILEGE_ERRORS`): a `DENY` on a catalog view answers 229, which
# classifies as PERMISSION_DENIED, and `read_facet` absorbs it like any other
# engine's refusal. Proven live in `tests/test_facet_refusal_sqlserver_live.py`.
# A failure with any other number is still UNAVAILABLE and still re-raised.
_CapturedRead = list[Any] | BaseException


@dataclass(frozen=True, slots=True)
class _CapturedReads:
    """Every discovery read of one run, in the order the thread made them.

    `catalog_name` is not a facet: a login that cannot ask `DB_NAME()` has no
    session to read anything else with, so that failure still leaves the thread
    immediately.
    """

    catalog_name: str
    columns: _CapturedRead
    keys: _CapturedRead
    foreign_keys: _CapturedRead
    view_definitions: _CapturedRead
    routines: _CapturedRead
    routine_parameters: _CapturedRead
    triggers: _CapturedRead
    sequences: _CapturedRead
    table_comments: _CapturedRead
    column_comments: _CapturedRead
    schema_comments: _CapturedRead
    catalog_comment: _CapturedRead
    grants: _CapturedRead


async def _captured(read: _CapturedRead) -> Sequence[Any]:
    """The rows a captured read returned, or the failure it captured, re-raised.

    The awaitable `read_facet` takes. Raising here rather than in the thread is
    the whole point: the exception reaches `read_facet` in the coroutine that
    owns the `FacetReadScope`, so it is classified and recorded exactly as a
    natively async driver's failure is (R11-FP02).
    """
    if isinstance(read, BaseException):
        raise read
    return read


class _StaleConnection(Exception):
    """An idle pooled connection the server had already closed (R11-MP24)."""


class SqlServerConnector(SqlExecutor):
    connector_type = "sqlserver"
    dialect = "tsql"
    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        constraints=True,
        indexes=False,
        partitions=False,
        explain=True,
        delegated_identity=False,
        approximate_statistics=True,
        # Envelope 1.1 (gap/02 N1). Each flag below is backed by a query in
        # `_read_facets_sync`, which is what INV-9 requires of a `True`:
        #   views            -> sys.views + sys.sql_modules.definition
        #   routines         -> sys.objects/sys.sql_modules + sys.parameters
        #   object_comments  -> sys.extended_properties, MS_Description
        #   grants           -> sys.database_permissions, class 1 (object)
        views=True,
        routines=True,
        object_comments=True,
        grants=True,
        # R11-FP01. Each backed by a query in `_read_facets_sync`:
        #   triggers  -> sys.triggers + sys.trigger_events + sys.sql_modules
        #   sequences -> sys.sequences
        triggers=True,
        sequences=True,
    )

    def __init__(
        self, dsn: str, *, command_timeout: float = 30.0, pooled_reads: bool = False
    ) -> None:
        self._params = _parse_dsn(dsn)
        self._command_timeout = command_timeout
        self._schema_scope = SchemaScope()
        self._pooled_reads = pooled_reads

    def with_pooled_reads(self) -> "SqlServerConnector":
        """R11-MP24: this source, with governed execution borrowing idle connections
        (`aida.connectors.sqlserver_pool`). The EXPLAIN gate never borrows."""
        pooled = copy.copy(self)
        pooled._pooled_reads = True
        return pooled

    @property
    def capabilities(self) -> ConnectorCapabilities:
        # INV-9: `DEFAULT_CAPABILITIES` is this connector's claim. What it advertises is
        # that claim narrowed to what its certification result supports
        # (`aida.connectors.capability_certification`); it can never exceed the claim.
        return derive_capabilities(self.connector_type, self.DEFAULT_CAPABILITIES)

    def _connect(self, *, timeout_seconds: float, autocommit: bool) -> Any:
        return pytds.connect(
            server=self._params.host,
            port=self._params.port,
            database=self._params.database,
            user=self._params.user,
            password=self._params.password,
            timeout=timeout_seconds,
            login_timeout=min(timeout_seconds, 15.0),
            as_dict=True,
            autocommit=autocommit,
            readonly=True,
        )

    async def test_connection(self) -> None:
        await asyncio.to_thread(self._test_connection_sync)

    def _test_connection_sync(self) -> None:
        connection = self._connect(timeout_seconds=self._command_timeout, autocommit=True)
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT 1")
                cursor.fetchall()
            finally:
                cursor.close()
        finally:
            connection.close()

    def scope_discovery(
        self,
        *,
        include_schemas: list[str],
        exclude_schemas: list[str],
        object_kinds: Sequence[str] = (),
        include_objects: Sequence[str] = (),
        exclude_objects: Sequence[str] = (),
    ) -> bool:
        # R11-FP01: this adapter pushes the schema scope only. The object kinds and
        # `schema.object` patterns are accepted so one call reaches every adapter, and are
        # left to `discovery_selection.apply_selection`, which runs on every batch anyway.
        self._schema_scope = schema_scope(include_schemas, exclude_schemas)
        return self._schema_scope.restricted

    def _scoped_execute(self, cursor: Any, sql: str) -> None:
        """A discovery query, restricted to the pushed-down schema scope if there is one."""
        scoped, parameters = scoped_sqlserver_query(sql, self._schema_scope)
        if parameters:
            cursor.execute(scoped, parameters)
        else:
            cursor.execute(scoped)

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        """R11-FP02: each facet's read replayed through `read_facet`, then assembled.

        The reads themselves all happened in one driver thread (see
        `_read_facets_sync` and the `_CapturedReads` comment above). They are
        replayed here in the order they were made, so the first failure is the
        one that surfaces, and so a refused facet -- which `read_facet` absorbs,
        returning no rows -- simply leaves its own `apply_*` / `build_*` call
        with nothing to attach.

        `triggers` and `sequences` are replayed *without* a facet: neither is in
        `DISCOVERY_FACETS`, so there is no name a `FacetReadScope` would accept
        or a receipt could publish, and naming one would turn a recoverable
        refusal into a `ValueError`. Their failures therefore still end the run,
        exactly as before this change (see the R11-FP02 remainder).
        """
        reads = await asyncio.to_thread(self._read_facets_sync)
        column_rows = await read_facet(FACET_INVENTORY, _captured(reads.columns))
        key_rows = await read_facet(FACET_CONSTRAINTS, _captured(reads.keys))
        foreign_key_rows = await read_facet(FACET_CONSTRAINTS, _captured(reads.foreign_keys))
        view_rows = await read_facet(FACET_VIEW_DEFINITIONS, _captured(reads.view_definitions))
        routine_rows = await read_facet(FACET_ROUTINE_BODIES, _captured(reads.routines))
        routine_parameter_rows = await read_facet(
            FACET_ROUTINE_BODIES, _captured(reads.routine_parameters)
        )
        trigger_rows = await read_facet(FACET_TRIGGERS, _captured(reads.triggers))
        sequence_rows = await read_facet(FACET_SEQUENCES, _captured(reads.sequences))
        table_description_rows = await read_facet(
            FACET_OBJECT_COMMENTS, _captured(reads.table_comments)
        )
        column_description_rows = await read_facet(
            FACET_OBJECT_COMMENTS, _captured(reads.column_comments)
        )
        schema_description_rows = await read_facet(
            FACET_OBJECT_COMMENTS, _captured(reads.schema_comments)
        )
        catalog_comment_rows = await read_facet(
            FACET_OBJECT_COMMENTS, _captured(reads.catalog_comment)
        )
        grant_rows = await read_facet(FACET_GRANTS, _captured(reads.grants))

        return _assemble_catalog(
            reads.catalog_name,
            list(column_rows),
            list(key_rows),
            list(foreign_key_rows),
            view_rows=list(view_rows),
            routine_rows=list(routine_rows),
            routine_parameter_rows=list(routine_parameter_rows),
            trigger_rows=list(trigger_rows),
            sequence_rows=list(sequence_rows),
            table_description_rows=list(table_description_rows),
            column_description_rows=list(column_description_rows),
            schema_description_rows=list(schema_description_rows),
            # One row, read as one row: `_assemble_catalog` takes the catalog
            # comment as a single row or None, and a refused read has none.
            catalog_description_row=catalog_comment_rows[0] if catalog_comment_rows else None,
            grant_rows=list(grant_rows),
        )

    def _facet_rows(self, cursor: Any, sql: str) -> _CapturedRead:
        """One facet's read, captured rather than judged (R11-FP02).

        Both halves are inside the guard, not just the `execute`: the driver
        decides which of the two raises, and a refusal that surfaced at fetch
        time would otherwise escape the capture and end the run.
        """
        try:
            self._scoped_execute(cursor, sql)
            rows: list[Any] = cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 -- replayed verbatim into `read_facet`
            return exc
        return rows

    def _read_facets_sync(self) -> _CapturedReads:
        connection = self._connect(timeout_seconds=self._command_timeout, autocommit=True)
        try:
            cursor = connection.cursor()
            try:
                self._scoped_execute(cursor, "SELECT DB_NAME() AS catalog_name")
                catalog_row = cursor.fetchone()
                catalog_name = str(catalog_row["catalog_name"]) if catalog_row else ""

                columns = self._facet_rows(
                    cursor,
                    """
                    SELECT
                        c.TABLE_SCHEMA AS table_schema,
                        c.TABLE_NAME AS table_name,
                        t.TABLE_TYPE AS table_type,
                        c.COLUMN_NAME AS column_name,
                        c.ORDINAL_POSITION AS ordinal_position,
                        c.DATA_TYPE AS data_type,
                        c.IS_NULLABLE AS is_nullable,
                        c.COLUMN_DEFAULT AS column_default
                    FROM INFORMATION_SCHEMA.COLUMNS c
                    JOIN INFORMATION_SCHEMA.TABLES t
                      ON t.TABLE_CATALOG = c.TABLE_CATALOG
                     AND t.TABLE_SCHEMA = c.TABLE_SCHEMA
                     AND t.TABLE_NAME = c.TABLE_NAME
                    WHERE c.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')
                    ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.ORDINAL_POSITION
                    """,
                )

                keys = self._facet_rows(
                    cursor,
                    """
                    SELECT
                        tc.TABLE_SCHEMA AS table_schema,
                        tc.TABLE_NAME AS table_name,
                        tc.CONSTRAINT_NAME AS constraint_name,
                        tc.CONSTRAINT_TYPE AS constraint_type,
                        kcu.COLUMN_NAME AS column_name,
                        kcu.ORDINAL_POSITION AS ordinal_position
                    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
                    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
                      ON kcu.CONSTRAINT_CATALOG = tc.CONSTRAINT_CATALOG
                     AND kcu.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
                     AND kcu.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
                    WHERE tc.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'UNIQUE')
                      AND tc.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')
                    ORDER BY tc.TABLE_SCHEMA, tc.TABLE_NAME, tc.CONSTRAINT_NAME,
                        kcu.ORDINAL_POSITION
                    """,
                )

                foreign_keys = self._facet_rows(
                    cursor,
                    """
                    SELECT
                        fk_tc.TABLE_SCHEMA AS table_schema,
                        fk_tc.TABLE_NAME AS table_name,
                        rc.CONSTRAINT_NAME AS constraint_name,
                        ref_tc.TABLE_SCHEMA AS referenced_schema,
                        ref_tc.TABLE_NAME AS referenced_table,
                        fk_kcu.COLUMN_NAME AS column_name,
                        ref_kcu.COLUMN_NAME AS referenced_column,
                        fk_kcu.ORDINAL_POSITION AS ordinal_position
                    FROM INFORMATION_SCHEMA.REFERENTIAL_CONSTRAINTS rc
                    JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS fk_tc
                      ON fk_tc.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
                     AND fk_tc.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA
                    JOIN INFORMATION_SCHEMA.TABLE_CONSTRAINTS ref_tc
                      ON ref_tc.CONSTRAINT_NAME = rc.UNIQUE_CONSTRAINT_NAME
                     AND ref_tc.CONSTRAINT_SCHEMA = rc.UNIQUE_CONSTRAINT_SCHEMA
                    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE fk_kcu
                      ON fk_kcu.CONSTRAINT_NAME = fk_tc.CONSTRAINT_NAME
                     AND fk_kcu.CONSTRAINT_SCHEMA = fk_tc.CONSTRAINT_SCHEMA
                    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE ref_kcu
                      ON ref_kcu.CONSTRAINT_NAME = ref_tc.CONSTRAINT_NAME
                     AND ref_kcu.CONSTRAINT_SCHEMA = ref_tc.CONSTRAINT_SCHEMA
                     AND ref_kcu.ORDINAL_POSITION = fk_kcu.ORDINAL_POSITION
                    WHERE fk_tc.TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA')
                    ORDER BY fk_tc.TABLE_SCHEMA, fk_tc.TABLE_NAME, rc.CONSTRAINT_NAME,
                        fk_kcu.ORDINAL_POSITION
                    """,
                )

                view_definitions = self._facet_rows(cursor, _VIEW_DEFINITION_SQL)
                routines = self._facet_rows(cursor, _ROUTINE_SQL)
                routine_parameters = self._facet_rows(cursor, _ROUTINE_PARAMETER_SQL)
                triggers = self._facet_rows(cursor, _TRIGGER_SQL)
                sequences = self._facet_rows(cursor, _SEQUENCE_SQL)
                table_comments = self._facet_rows(cursor, _TABLE_COMMENT_SQL)
                column_comments = self._facet_rows(cursor, _COLUMN_COMMENT_SQL)
                schema_comments = self._facet_rows(cursor, _SCHEMA_COMMENT_SQL)
                # The catalog comment is one row, read with the same `fetchall`
                # as the rest so one capture shape covers every read;
                # `discover()` takes the first row exactly as `fetchone` did.
                catalog_comment = self._facet_rows(cursor, _CATALOG_COMMENT_SQL)
                grants = self._facet_rows(cursor, _GRANT_SQL)
            finally:
                cursor.close()
        finally:
            connection.close()

        return _CapturedReads(
            catalog_name=catalog_name,
            columns=columns,
            keys=keys,
            foreign_keys=foreign_keys,
            view_definitions=view_definitions,
            routines=routines,
            routine_parameters=routine_parameters,
            triggers=triggers,
            sequences=sequences,
            table_comments=table_comments,
            column_comments=column_comments,
            schema_comments=schema_comments,
            catalog_comment=catalog_comment,
            grants=grants,
        )

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> QueryEstimate:
        return await asyncio.to_thread(self._estimate_read_query_sync, sql, timeout_seconds)

    def _estimate_read_query_sync(self, sql: str, timeout_seconds: int) -> QueryEstimate:
        connection = self._connect(timeout_seconds=timeout_seconds, autocommit=False)
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("SET SHOWPLAN_XML ON")
                cursor.execute(sql)
                row = cursor.fetchone()
                cursor.execute("SET SHOWPLAN_XML OFF")
                if row is None:
                    raise RuntimeError("source returned no SHOWPLAN_XML result")
                raw_xml = next(iter(row.values())) if isinstance(row, dict) else row[0]
                return _extract_showplan_estimate(str(raw_xml))
            finally:
                cursor.close()
                connection.rollback()
        finally:
            connection.close()

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        return await asyncio.to_thread(self._execute_read_query_sync, sql, timeout_seconds)

    def _execute_read_query_sync(self, sql: str, timeout_seconds: int) -> QueryResult:
        if self._pooled_reads:
            return self._execute_pooled_sync(sql, timeout_seconds)
        connection = self._connect(timeout_seconds=timeout_seconds, autocommit=False)
        try:
            return self._run_read(connection, sql)
        finally:
            connection.close()

    @staticmethod
    def _run_read(connection: Any, sql: str) -> QueryResult:
        cursor = connection.cursor()
        try:
            session_id = cursor.execute_scalar("SELECT @@SPID")
            cursor.execute(sql)
            rows = cursor.fetchall()
            return QueryResult(
                rows=tuple(dict(row) for row in rows),
                warehouse_query_id=f"sqlserver-spid:{session_id}",
            )
        finally:
            cursor.close()
            connection.rollback()

    def _execute_pooled_sync(self, sql: str, timeout_seconds: int) -> QueryResult:
        """R11-MP24: execution on a pooled connection. An idle connection the server has
        closed fails on `SELECT @@SPID`, before the statement runs, and is retried once on a
        fresh one; any other failure closes the connection and is raised."""
        params = self._params
        key = pool_key(
            params.host,
            params.port,
            params.database,
            params.user,
            params.password,
            timeout_seconds,
        )

        def connect() -> Any:
            return self._connect(timeout_seconds=timeout_seconds, autocommit=False)

        for attempt in range(2):
            try:
                # Raised out of the block, so `borrow` closes the connection, never returns it.
                with borrow(key, connect) as connection:
                    cursor = connection.cursor()
                    try:
                        try:
                            session_id = cursor.execute_scalar("SELECT @@SPID")
                        except (pytds.tds_base.ClosedConnectionError, OSError) as exc:
                            if attempt:
                                raise
                            raise _StaleConnection from exc
                        cursor.execute(sql)
                        rows = cursor.fetchall()
                        return QueryResult(
                            rows=tuple(dict(row) for row in rows),
                            warehouse_query_id=f"sqlserver-spid:{session_id}",
                        )
                    finally:
                        with suppress(Exception):
                            cursor.close()
            except _StaleConnection:
                continue
        raise AssertionError("unreachable")  # pragma: no cover

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
        return await asyncio.to_thread(
            self._profile_table_sync,
            schema_name,
            table_name,
            column_names,
            sample_rows,
            column_batch_size,
            timeout_seconds,
        )

    def _profile_table_sync(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        sample_rows: int,
        column_batch_size: int,
        timeout_seconds: int,
    ) -> TableProfileSnapshot:
        qualified_table = f"{_quote_identifier(schema_name)}.{_quote_identifier(table_name)}"
        connection = self._connect(timeout_seconds=timeout_seconds, autocommit=False)
        snapshots: list[ColumnProfileSnapshot] = []
        sampled_row_count = 0
        estimate: int | None = None
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(
                    """
                    SELECT SUM(p.rows) AS estimate
                    FROM sys.partitions p
                    JOIN sys.tables t ON t.object_id = p.object_id
                    JOIN sys.schemas s ON s.schema_id = t.schema_id
                    WHERE s.name = %s AND t.name = %s AND p.index_id IN (0, 1)
                    """,
                    (schema_name, table_name),
                )
                estimate_row = cursor.fetchone()
                if estimate_row is not None and estimate_row.get("estimate") is not None:
                    estimate = int(estimate_row["estimate"])

                for start in range(0, len(column_names), column_batch_size):
                    batch = column_names[start : start + column_batch_size]
                    selected = ", ".join(_quote_identifier(name) for name in batch)
                    expressions = ["COUNT(*) AS sampled_row_count"]
                    for position, name in enumerate(batch):
                        quoted = _quote_identifier(name)
                        text_form = f"LEN(CAST({quoted} AS NVARCHAR(MAX)))"
                        expressions.extend(
                            (
                                f"SUM(CASE WHEN {quoted} IS NULL THEN 1 ELSE 0 END) "
                                f"AS n_{position}",
                                f"COUNT({quoted}) AS nn_{position}",
                                f"COUNT(DISTINCT {quoted}) AS d_{position}",
                                f"MIN({text_form}) AS minl_{position}",
                                f"MAX({text_form}) AS maxl_{position}",
                            )
                        )
                        # R11-FP04. `LTRIM(RTRIM(...))` rather than `TRIM(...)`:
                        # TRIM is SQL Server 2017+, and this adapter supports
                        # older instances. Note that the blank and
                        # whitespace-only predicates compare the *text* form,
                        # not its LEN -- `LEN` ignores trailing spaces in T-SQL,
                        # so a LEN-based blank test would count '   ' as empty
                        # and the two findings would collapse into one.
                        cast_form = f"CAST({quoted} AS NVARCHAR(MAX))"
                        expressions.extend(
                            value_free_distribution_expressions(
                                position=position,
                                text_form=cast_form,
                                length_form=text_form,
                                trimmed_form=f"LTRIM(RTRIM({cast_form}))",
                            )
                        )
                    profile_sql = (
                        f"WITH bounded_sample AS (SELECT TOP ({int(sample_rows)}) {selected} "  # noqa: S608 -- identifiers are bracket-quoted and limits are validated integers
                        f"FROM {qualified_table}) SELECT {', '.join(expressions)} "
                        "FROM bounded_sample"
                    )
                    cursor.execute(profile_sql)
                    row = cursor.fetchone()
                    if row is None:
                        continue
                    sampled_row_count = max(sampled_row_count, int(row["sampled_row_count"]))
                    for position, name in enumerate(batch):
                        blank, whitespace, buckets = read_value_free_distribution(
                            position, row.get
                        )
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
                                facet_status=(ENTROPY_NOT_IMPLEMENTED,),
                            )
                        )
            finally:
                cursor.close()
                connection.rollback()
        finally:
            connection.close()
        return TableProfileSnapshot(
            row_count_estimate=(max(estimate, sampled_row_count) if estimate is not None else None),
            sampled_row_count=sampled_row_count,
            columns=tuple(snapshots),
            # R11-FP04: the `TOP (n)` above is the bound. `sys.partitions` is a
            # maintained estimate that can sit either side of the truth, so
            # comparing the two numbers -- which is what downstream used to do
            # -- is not the same question as "did the bound bite".
            observation_scope=bounded_scan_scope(
                sampled_row_count=sampled_row_count, sample_rows=sample_rows
            ),
        )


def _assemble_catalog(
    catalog_name: str,
    column_rows: list[dict[str, Any]],
    key_rows: list[dict[str, Any]],
    foreign_key_rows: list[dict[str, Any]],
    *,
    view_rows: list[dict[str, Any]] | None = None,
    routine_rows: list[dict[str, Any]] | None = None,
    routine_parameter_rows: list[dict[str, Any]] | None = None,
    trigger_rows: list[dict[str, Any]] | None = None,
    sequence_rows: list[dict[str, Any]] | None = None,
    table_description_rows: list[dict[str, Any]] | None = None,
    column_description_rows: list[dict[str, Any]] | None = None,
    schema_description_rows: list[dict[str, Any]] | None = None,
    catalog_description_row: dict[str, Any] | None = None,
    grant_rows: list[dict[str, Any]] | None = None,
) -> tuple[DiscoveredCatalog, ...]:
    """Assemble the envelope from raw row sets.

    The 1.1 row sets default to `None` so the existing 1.0 assembly tests keep
    calling this with four positional arguments, and so a caller that collected
    only some axes produces an envelope where the others are genuinely absent.
    """
    tables = build_table_map_from_column_rows(column_rows)
    append_grouped_key_rows(
        tables,
        key_rows,
        constraint_type_map={"PRIMARY KEY": "PRIMARY_KEY", "UNIQUE": "UNIQUE"},
    )
    append_grouped_foreign_key_rows(tables, foreign_key_rows)
    apply_table_descriptions(tables, table_description_rows or [])
    apply_column_descriptions(tables, column_description_rows or [])
    apply_view_definitions(tables, view_rows or [])
    catalog_description = None
    if catalog_description_row is not None:
        raw_description = catalog_description_row.get("description")
        if raw_description is not None:
            catalog_description = str(raw_description)
    # R11-FP01: attached after assembly rather than passed in, because
    # `assemble_catalog` takes no parameter for these two axes -- see
    # `connectors.base.attach_native_objects` for why the helper lives there.
    return attach_native_objects(
        assemble_catalog(
            str(catalog_name),
            tables,
            routines=build_routines(routine_rows or [], routine_parameter_rows or []),
            grants=build_grants(grant_rows or []),
            schema_descriptions={
                str(row["schema_name"]): str(row["description"])
                for row in (schema_description_rows or [])
                if row.get("description") is not None
            },
            catalog_description=catalog_description,
        ),
        triggers=build_triggers(trigger_rows or []),
        sequences=build_sequences(sequence_rows or []),
    )
