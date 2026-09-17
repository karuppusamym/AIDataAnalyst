"""
Databricks Native SQL Warehouse Connector
==========================================

Implements the Atlas ``Connector`` ABC for Databricks (Unity Catalog) with strict
governance, fail-closed validation, EXPLAIN-COST-based query estimation, and
value-free metadata discovery.

CN-2b. Modeled directly on ``aida.connectors.snowflake`` -- the closest existing
adapter shape (a cloud warehouse reached over a DB-API driver, discovered through
INFORMATION_SCHEMA, with EXPLAIN-based cost estimation). This adapter has not been
exercised against a live Databricks workspace; see the registry notes for the same
"implemented, unverified live" honesty already carried by the Snowflake, Oracle and
BigQuery rows (Docs/20-modules/02-connectivity.md).

R11-FP01 closes two of the three "envelope 1.1" axes this adapter used to skip.
``views`` and ``routines`` are now read from Unity Catalog's own ANSI-shaped
``information_schema.views`` / ``.routines`` / ``.parameters``, through the same
best-effort pattern the foreign-key and comment queries already use: a workspace
or metastore version that does not expose a column, or a principal without
``USE SCHEMA`` on a dataset, shrinks the envelope and records the reason on the
catalog rather than failing discovery. That is what makes the flags claimable
without a live workspace -- the honest failure mode is built into the query
path, not assumed away. The previous text of this docstring argued the opposite
and it was right about the risk and wrong about the remedy: the remedy is a
refusal-shaped read, not an unimplemented axis.

``grants`` stays False. Unity Catalog's privilege model is not the SQL grant
model these three views describe -- ``TABLE_PRIVILEGES`` reports only what the
*current* principal was granted directly, not the estate's grants, so a read of
it would answer a different question from the one ``MetadataSourceGrant``
records and would look like a complete grant inventory while being one
principal's own row. That is a modelling decision to make with a live workspace,
and it stays unimplemented and honestly declared until then.

``triggers`` and ``sequences`` stay False too, and for a different reason
again: Unity Catalog has neither object. See
``DatabricksConnector.DEFAULT_CAPABILITIES``.

Table/column/schema/catalog *comments* are simple, single-valued, well-documented
INFORMATION_SCHEMA columns with no refusal-vs-empty ambiguity, so they are
implemented and ``object_comments`` is honestly set True.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from aida.connectors.base import (
    ENTROPY_NOT_IMPLEMENTED,
    ColumnProfileSnapshot,
    ConnectorCapabilities,
    DiscoveredCatalog,
    QueryEstimate,
    QueryResult,
    TableProfileSnapshot,
    bounded_scan_scope,
    read_value_free_distribution,
    rows_to_dicts,
    value_free_distribution_expressions,
)
from aida.connectors.discovery import (
    FACET_CONSTRAINTS,
    FACET_INVENTORY,
    FACET_OBJECT_COMMENTS,
    FACET_ROUTINE_BODIES,
    FACET_VIEW_DEFINITIONS,
    append_grouped_foreign_key_rows,
    append_grouped_key_rows,
    apply_view_definitions,
    assemble_catalog,
    build_routines,
    build_table_map_from_column_rows,
    read_facet,
)
from aida.connectors.sql_execution import SqlExecutor

_EXCLUDED_SCHEMA = "information_schema"

_CONSTRAINT_TYPE_MAP = {
    "PRIMARY KEY": "PRIMARY_KEY",
    "UNIQUE": "UNIQUE",
    "PRIMARY_KEY": "PRIMARY_KEY",
}

# Spark's `Statistics.toString` humanizes byte counts (`org.apache.spark.util.Utils
# .bytesToString`) rather than printing a raw integer, so EXPLAIN COST output has to
# be converted back rather than parsed as a number directly.
_BYTE_UNITS: dict[str, int] = {
    "B": 1,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "PIB": 1024**5,
    "EIB": 1024**6,
}


def _quote_identifier(identifier: str) -> str:
    """Databricks (Spark SQL) backtick-quote an identifier."""
    return "`" + identifier.replace("`", "``") + "`"


def _qualified_table(catalog: str, schema: str, table: str) -> str:
    """Format a fully-qualified 3-part Unity Catalog table identifier."""
    return f"{_quote_identifier(catalog)}.{_quote_identifier(schema)}.{_quote_identifier(table)}"


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class _DatabricksConnectionParams:
    server_hostname: str
    http_path: str
    access_token: str
    catalog: str | None = None
    schema: str | None = None


def _parse_dsn(dsn: str) -> _DatabricksConnectionParams:
    """Parse a Databricks connection reference from a JSON payload or DSN URI.

    Accepted formats:
    - JSON credential payload: ``{"server_hostname": "...", "http_path": "...",
      "access_token": "...", "catalog": "...", "schema": "..."}``
    - DSN URI: ``databricks://token:<access_token>@<server_hostname>/<catalog>/<schema>
      ?http_path=%2Fsql%2F1.0%2Fwarehouses%2F...``

    The URI form's ``token:<access_token>`` username/password split mirrors the
    literal username ``token`` Databricks' own JDBC/ODBC drivers expect for PAT
    auth -- the username is not itself a secret, only the password half is.
    ``http_path`` is carried as a query parameter (URL-encoded) rather than in the
    URI path because it is itself slash-delimited (``/sql/1.0/warehouses/<id>``)
    and would collide with the catalog/schema path segments.
    """
    raw = dsn.strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise ValueError(f"invalid Databricks credential JSON payload: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Databricks credential JSON payload must be a JSON object")
        server_hostname = data.get("server_hostname")
        http_path = data.get("http_path")
        access_token = data.get("access_token")
        if not server_hostname or not http_path or not access_token:
            raise ValueError(
                "Databricks credentials must include 'server_hostname', 'http_path', "
                "and 'access_token'"
            )
        return _DatabricksConnectionParams(
            server_hostname=str(server_hostname),
            http_path=str(http_path),
            access_token=str(access_token),
            catalog=str(data["catalog"]) if data.get("catalog") else None,
            schema=str(data["schema"]) if data.get("schema") else None,
        )

    parsed = urlsplit(raw)
    if parsed.scheme != "databricks":
        raise ValueError(
            "invalid Databricks connection reference; expected "
            "databricks://token:<access_token>@<server_hostname>/<catalog>/<schema>"
            "?http_path=<url-encoded http path>"
        )
    if not parsed.hostname or not parsed.password:
        raise ValueError(
            "Databricks connection reference is missing server_hostname or access_token"
        )

    query_params = parse_qs(parsed.query)
    http_path = query_params.get("http_path", [None])[0]
    if not http_path:
        raise ValueError("Databricks connection reference requires an 'http_path' query parameter")

    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    catalog = unquote(path_parts[0]) if len(path_parts) > 0 else None
    schema = unquote(path_parts[1]) if len(path_parts) > 1 else None

    return _DatabricksConnectionParams(
        server_hostname=parsed.hostname,
        http_path=unquote(http_path),
        access_token=unquote(parsed.password),
        catalog=catalog,
        schema=schema,
    )


def _extract_databricks_explain_cost(plan_text: str) -> QueryEstimate:
    """Extract a cost estimate from Databricks SQL ``EXPLAIN COST`` output.

    ``EXPLAIN COST`` prints a Spark plan annotated with zero or more
    ``Statistics(sizeInBytes=<humanized bytes>, rowCount=<n>)`` fragments, one per
    plan node with cost-based-optimizer statistics available (which requires the
    target tables to have been ``ANALYZE``d; an unanalyzed table simply carries no
    ``Statistics`` fragment at all). Fragments are nested bottom-up, so the largest
    values seen are taken as the estimate rather than summed -- summing every node's
    statistics would multiply-count the same rows as they flow up through the plan.

    A plan with no ``Statistics`` fragments (no CBO stats collected) falls back
    honestly to a floor estimate, exactly as the Snowflake adapter's EXPLAIN-JSON
    path falls back when the plan carries no usable numbers.
    """
    size_matches = re.findall(r"sizeInBytes=([\d.]+)\s*([KMGTPE]?i?B)\b", plan_text)
    row_matches = re.findall(r"rowCount=([\d,.]+)", plan_text)

    max_bytes = 0
    for value, unit in size_matches:
        multiplier = _BYTE_UNITS.get(unit.upper(), 1)
        max_bytes = max(max_bytes, int(float(value) * multiplier))

    max_rows = 0.0
    for value in row_matches:
        try:
            rows = float(value.replace(",", ""))
        except ValueError:
            continue
        max_rows = max(max_rows, rows)

    if max_bytes == 0 and max_rows == 0.0:
        return QueryEstimate(score=1.0, kind="DATABRICKS_EXPLAIN_FALLBACK")

    score = round(max(max_rows * 0.01 + (max_bytes / (1024 * 1024)), 1.0), 2)
    return QueryEstimate(
        score=score,
        kind="DATABRICKS_EXPLAIN_COST",
        estimated_rows=max_rows if max_rows > 0 else None,
        estimated_bytes=max_bytes if max_bytes > 0 else None,
        evidence={
            "statistics_fragments_found": len(size_matches),
            "row_count_fragments_found": len(row_matches),
        },
    )


# R11-FP01: the envelope 1.1 axes Unity Catalog genuinely exposes.
#
# Column lists are the ANSI INFORMATION_SCHEMA names Databricks documents, and
# are deliberately the same ones the BigQuery adapter reads from its own
# INFORMATION_SCHEMA -- the two warehouses implement the same standard views, so
# a second spelling here would be a second thing to keep right.
#
# `_VIEW_COLUMNS_NARROW` is the retry. `is_updatable` and `check_option` are in
# the standard's VIEWS shape and are the two columns a metastore version is
# most likely not to have; losing them must cost the *columns*, not the whole
# axis, so the primary query is retried without them before the axis is
# recorded as unavailable. Nothing else here is optional: a VIEWS relation
# without `view_definition` is not a VIEWS relation.
_VIEW_COLUMNS = "table_schema, table_name, view_definition, is_updatable, check_option"
_VIEW_COLUMNS_NARROW = "table_schema, table_name, view_definition"
_ROUTINE_COLUMNS = (
    "routine_schema, routine_name, specific_name, routine_type, data_type, "
    "routine_body, routine_definition, external_language, is_deterministic, "
    "security_type, comment"
)
_ROUTINE_PARAMETER_COLUMNS = (
    "specific_schema, specific_name, ordinal_position, parameter_mode, is_result, "
    "parameter_name, data_type, parameter_default"
)


def _view_definition_rows(view_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape `information_schema.views` rows for the shared `apply_view_definitions`.

    A NULL `view_definition` is left NULL rather than coerced to an empty
    string: `apply_view_definitions` records that as *unavailable with a reason*,
    which is the state a principal without `USE SCHEMA` on the view's own schema
    produces -- and which a downstream parser has to tell apart from a view whose
    body really is empty.
    """
    return [
        {
            "table_schema": row["table_schema"],
            "table_name": row["table_name"],
            "definition": row.get("view_definition"),
            "is_updatable": row.get("is_updatable"),
            "check_option": _optional_text(row.get("check_option")),
            "unavailable_reason": (
                None
                if row.get("view_definition") is not None
                else "information_schema.views returned no definition text: the view's "
                "schema is not readable by this principal"
            ),
        }
        for row in view_rows
    ]


def _routine_rows(routine_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape `information_schema.routines` rows for the shared `build_routines`.

    `external_language` is preferred over `routine_body` for `language` because
    it names what a Python UDF actually is, where `routine_body` answers the
    standard's own SQL/EXTERNAL dichotomy and would report every Python UDF as
    `EXTERNAL`.
    """
    return [
        {
            "routine_schema": row["routine_schema"],
            "routine_name": row["routine_name"],
            "specific_name": row.get("specific_name") or row["routine_name"],
            "routine_type": row.get("routine_type") or "FUNCTION",
            "language": _optional_text(row.get("external_language"))
            or _optional_text(row.get("routine_body")),
            "body": row.get("routine_definition"),
            "return_type": _optional_text(row.get("data_type")),
            "is_deterministic": row.get("is_deterministic"),
            "security_mode": _optional_text(row.get("security_type")),
            "description": _optional_text(row.get("comment")),
            "unavailable_reason": (
                None
                if row.get("routine_definition") is not None
                else "information_schema.routines returned no definition text: the "
                "routine's schema is not readable by this principal, or the routine "
                "body is held outside the metastore"
            ),
        }
        for row in routine_rows
    ]


def _routine_parameter_rows(parameter_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape `information_schema.parameters` rows for the shared `build_routines`.

    `is_result` rows are the function's *return*, not a parameter, and are
    dropped -- the same distinction Oracle's `POSITION = 0` carries.
    """
    return [
        {
            "routine_schema": row["specific_schema"],
            "specific_name": row["specific_name"],
            "parameter_name": _optional_text(row.get("parameter_name")),
            "ordinal_position": row["ordinal_position"],
            "parameter_mode": _optional_text(row.get("parameter_mode")) or "IN",
            "data_type": row.get("data_type") or "",
            "parameter_default": _optional_text(row.get("parameter_default")),
        }
        for row in parameter_rows
        if not _truthy(row.get("is_result"))
    ]


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().upper() in {"YES", "Y", "TRUE", "T", "1"}


# ---------------------------------------------------------------------------
# R11-FP02: reads captured in the driver thread, judged in the coroutine.
#
# `read_facet` is a coroutine and `databricks-sql-connector` is a synchronous
# driver that declares `threadsafety = 1` -- threads may share the module, not
# a connection -- so every read happens inside one `asyncio.to_thread` hop.
# The thread captures each read's failure instead of judging it, and the
# coroutine replays the captures into `read_facet`, which classifies them,
# records them against their facet and decides what may be absorbed. The
# judgement stays in `connectors.discovery`, where all six adapters share it.
#
# Two replay modes, matching the two failure contracts this adapter already
# had -- adoption changes neither:
#
# * The column roster and the primary-key read have always failed the run, so
#   they are replayed bare. The roster goes under `FACET_INVENTORY`, which
#   `RETIREMENT_BEARING_FACETS` holds, so its refusal is recorded and then
#   still ends the run: a FULL run completing over zero objects would retire
#   the estate.
# * The foreign-key, comment and envelope reads have always degraded to no
#   rows (`_collect`, and the two `try`/`except` comment reads), so those are
#   replayed with the re-raise suppressed. The refusal now reaches the
#   receipt; what the run does is unchanged.
#
# **Databricks' SQLSTATE reality, and it is the interesting one.** The driver's
# `ServerOperationError` has no `sqlstate` attribute, so
# `capability_states.is_permission_refusal` returns False and a refusal here
# classifies as UNAVAILABLE / FACET_QUERY_FAILED. But the SQLSTATE *is* there:
# the installed 4.4.0 driver carries it in `exc.context["sqlState"]` (verified
# against the class), and Unity Catalog reports an insufficient-privilege error
# as `42501`. Reading it would mean teaching the shared classifier a second
# place to look, which is a change to `capability_states` and not to an
# adapter, so this adapter under-claims for now and the finding is recorded as
# an R11-FP02 remainder rather than patched around here.
# ---------------------------------------------------------------------------
_CapturedRead = Sequence[Mapping[str, Any]] | BaseException


@dataclass(frozen=True, slots=True)
class _CapturedReads:
    """One run's discovery reads, as the driver thread hands them back.

    The three envelope axes are plain row lists rather than captures: `_collect`
    absorbs their failures where it renders their reason, and hands the
    exception over in `refusals` for `discover()` to replay.
    """

    catalog_name: str
    columns: _CapturedRead
    primary_keys: _CapturedRead = ()
    foreign_keys: _CapturedRead = ()
    schema_comments: _CapturedRead = ()
    catalog_comments: _CapturedRead = ()
    view_rows: Sequence[Mapping[str, Any]] = ()
    routine_rows: Sequence[Mapping[str, Any]] = ()
    routine_parameter_rows: Sequence[Mapping[str, Any]] = ()
    unavailable: tuple[tuple[str, str], ...] = ()
    refusals: tuple[tuple[str, BaseException], ...] = ()


async def _captured(read: _CapturedRead) -> Sequence[Mapping[str, Any]]:
    """The rows a captured read returned, or the failure it captured, re-raised.

    The awaitable `read_facet` takes. Raising here rather than in the thread is
    the point: the exception reaches `read_facet` inside the coroutine that owns
    the `FacetReadScope`, so it is classified and recorded there.
    """
    if isinstance(read, BaseException):
        raise read
    return read


async def _read_absorbed(facet: str, read: _CapturedRead) -> list[dict[str, Any]]:
    """A read this adapter has always absorbed, now also classified and recorded.

    `read_facet` judges the failure and records it against `facet`; the re-raise
    it may make is suppressed here, because this read's pre-R11-FP02 contract
    was to shrink the envelope rather than fail the run -- the contract this
    adapter's own module docstring calls "a refusal-shaped read". Narrowing it
    is a separate decision, and while this driver reports no SQLSTATE that
    `read_facet` can see, a bare replay would narrow it for every failure at
    once.
    """
    with suppress(Exception):
        return [dict(row) for row in await read_facet(facet, _captured(read))]
    return []


async def _record_refused_axes(refusals: Sequence[tuple[str, BaseException]]) -> None:
    """Classify and record each already-absorbed axis failure against its facet.

    Same reasoning as `_read_absorbed`, for the axes whose rows `_collect` has
    already turned into an empty list and a reason string.
    """
    for facet, failure in refusals:
        with suppress(Exception):
            await read_facet(facet, _captured(failure))


def _assemble_databricks_catalog(
    catalog_name: str,
    column_rows: list[dict[str, Any]],
    pk_rows: list[dict[str, Any]],
    fk_rows: list[dict[str, Any]],
    schema_rows: list[dict[str, Any]],
    catalog_rows: list[dict[str, Any]],
    *,
    view_rows: list[dict[str, Any]] | None = None,
    routine_rows: list[dict[str, Any]] | None = None,
    routine_parameter_rows: list[dict[str, Any]] | None = None,
    unavailable: tuple[tuple[str, str], ...] = (),
) -> tuple[DiscoveredCatalog, ...]:
    """Assemble the catalog graph and fold table/column/schema/catalog comments onto it.

    The R11-FP01 row sets default to `None` so a caller that collected only the
    1.0 axes produces an envelope where the others are genuinely absent rather
    than empty, which is the same contract the Oracle, Snowflake and BigQuery
    assemblers carry.
    """
    table_map = build_table_map_from_column_rows(column_rows)
    append_grouped_key_rows(table_map, pk_rows, constraint_type_map=_CONSTRAINT_TYPE_MAP)
    append_grouped_foreign_key_rows(table_map, fk_rows)
    apply_view_definitions(table_map, _view_definition_rows(view_rows or []))
    catalogs = assemble_catalog(
        catalog_name,
        table_map,
        routines=build_routines(
            _routine_rows(routine_rows or []),
            _routine_parameter_rows(routine_parameter_rows or []),
        ),
    )

    table_comments = {
        (str(row["table_schema"]), str(row["table_name"])): _optional_text(row.get("table_comment"))
        for row in column_rows
    }
    column_comments = {
        (str(row["table_schema"]), str(row["table_name"]), str(row["column_name"])): _optional_text(
            row.get("column_comment")
        )
        for row in column_rows
    }
    schema_comments = {
        str(row["schema_name"]): _optional_text(row.get("comment")) for row in schema_rows
    }
    catalog_comment = next(
        (
            _optional_text(row.get("comment"))
            for row in catalog_rows
            if str(row.get("catalog_name")) == catalog_name
        ),
        None,
    )

    rebuilt: list[DiscoveredCatalog] = []
    for catalog in catalogs:
        schemas = []
        for schema in catalog.schemas:
            tables = []
            for table in schema.tables:
                key = (schema.name, table.name)
                columns = tuple(
                    replace(
                        column,
                        source_description=column_comments.get(
                            (schema.name, table.name, column.name)
                        ),
                    )
                    for column in table.columns
                )
                tables.append(
                    replace(
                        table,
                        columns=columns,
                        source_description=table_comments.get(key),
                    )
                )
            schemas.append(
                replace(
                    schema,
                    tables=tuple(tables),
                    source_description=schema_comments.get(schema.name),
                )
            )
        attributes = (
            {**catalog.attributes, "envelope_v11_unavailable": dict(unavailable)}
            if unavailable
            else catalog.attributes
        )
        rebuilt.append(
            replace(
                catalog,
                schemas=tuple(schemas),
                source_description=catalog_comment,
                attributes=attributes,
            )
        )
    return tuple(rebuilt)


class DatabricksConnector(SqlExecutor):
    """Databricks native connector conforming to the Atlas Connector protocol.

    Reaches a Databricks SQL warehouse (or all-purpose cluster exposing the SQL
    endpoint) over ``databricks-sql-connector``, the same DB-API driver Databricks
    itself ships for first-party SQL connectivity. Discovery reads Unity Catalog's
    per-catalog ``information_schema`` -- the ANSI-standard-shaped metadata views
    Unity Catalog exposes for ``catalogs``, ``schemata``, ``tables``, ``columns``,
    ``table_constraints``, ``key_column_usage``, ``constraint_column_usage`` and
    ``referential_constraints``.
    """

    connector_type = "databricks"
    dialect = "databricks"
    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        catalogs=True,
        schemas=True,
        constraints=True,
        indexes=False,
        partitions=False,
        explain=True,
        # INV-9 (tracker AT-D3, 2026-09-01). Advertised `True` while nothing in the
        # platform consumes it -- there is no `get_query_history()` on any connector,
        # this one included. Advertising a capability we do not implement is the exact
        # failure this invariant exists to prevent, and under-claiming is the correct
        # direction to fail. Returns to `True` when AT-12 (query-history mining)
        # certifies it. Mirrors the same fix already landed for Snowflake's copy of
        # this flag.
        query_history=False,
        # PAT-only auth for now (see `_parse_dsn`); no delegated/workload identity path.
        delegated_identity=False,
        approximate_statistics=True,
        # R11-FP01. Each flag below is backed by a query in `discover()`, which
        # is what INV-9 requires of a `True`:
        #   views           -> information_schema.views.view_definition
        #   routines        -> information_schema.routines / .parameters
        #   object_comments -> the comment columns on the queries above
        views=True,
        routines=True,
        object_comments=True,
        # Not implemented, and not the same kind of gap as each other -- see the
        # module docstring. `grants` is an unimplemented axis on an engine that
        # has the concept in a different shape; `triggers` and `sequences` are
        # NOT_APPLICABLE, because Unity Catalog has neither object:
        #
        #   * no trigger of any kind. There is no `CREATE TRIGGER`; a Delta
        #     Live Tables pipeline or a job is scheduled or streamed, not fired
        #     by a DML statement against a table.
        #   * no sequence. `GENERATED ALWAYS AS IDENTITY` and a generated column
        #     are properties *of a Delta table's column* -- there is no separate
        #     object with an increment, bounds, a cache and a cycle flag, and
        #     nothing to inventory under its own name.
        #
        # Both stay False (INV-9's default), which is what makes
        # `discovery_selection` answer NOT_APPLICABLE for them rather than
        # UNSUPPORTED; the engine facts themselves live in
        # `discovery_selection._NO_TRIGGER_KIND` / `_NO_SEQUENCE_KIND`.
        grants=False,
        triggers=False,
        sequences=False,
    )

    def __init__(self, dsn: str, *, command_timeout: float = 60.0) -> None:
        self._dsn = dsn
        self._params = _parse_dsn(dsn)
        self._command_timeout = command_timeout

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.DEFAULT_CAPABILITIES

    def _get_connection(self) -> Any:
        """Create a Databricks SQL DBAPI connection using databricks-sql-connector."""
        try:
            import databricks.sql as databricks_sql
        except ImportError as exc:
            raise RuntimeError(
                "databricks-sql-connector package is required for native Databricks "
                "connectivity. Install with: pip install databricks-sql-connector"
            ) from exc

        kwargs: dict[str, Any] = {
            "server_hostname": self._params.server_hostname,
            "http_path": self._params.http_path,
            "access_token": self._params.access_token,
            "_socket_timeout": self._command_timeout,
        }
        if self._params.catalog:
            kwargs["catalog"] = self._params.catalog
        if self._params.schema:
            kwargs["schema"] = self._params.schema

        return databricks_sql.connect(**kwargs)

    async def test_connection(self) -> None:
        """Verify warehouse connectivity and PAT authentication."""

        def _sync_test() -> None:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    cur.execute("SELECT current_catalog(), current_user()")
                    cur.fetchone()
                finally:
                    cur.close()
            finally:
                conn.close()

        await asyncio.to_thread(_sync_test)

    @staticmethod
    def _capture(cur: Any, sql: str) -> _CapturedRead:
        """One read, captured rather than judged (R11-FP02).

        Both halves are inside the guard, not just the `execute`: the driver
        decides which of the two raises, and a refusal that surfaced at fetch
        time would otherwise escape the capture -- the same reasoning `_collect`
        below already carries for the envelope axes.
        """
        try:
            cur.execute(sql)
            return rows_to_dicts(cur, cur.fetchall())
        except Exception as exc:  # noqa: BLE001 -- replayed verbatim into `read_facet`
            return exc

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        """Discover Unity Catalog catalogs, schemas, tables, columns, and constraints.

        R11-FP02: the reads happen in one driver thread and are replayed here
        through `read_facet` in the order they were made -- see the
        `_CapturedReads` comment above for the two replay modes and why they
        differ. The assembly is a pure function of the rows, so it moved out of
        the thread with them and produces exactly what it produced before.
        """
        reads = await asyncio.to_thread(self._read_facets_sync)
        column_rows = [
            dict(row) for row in await read_facet(FACET_INVENTORY, _captured(reads.columns))
        ]
        pk_rows = [
            dict(row) for row in await read_facet(FACET_CONSTRAINTS, _captured(reads.primary_keys))
        ]
        fk_rows = await _read_absorbed(FACET_CONSTRAINTS, reads.foreign_keys)
        schema_rows = await _read_absorbed(FACET_OBJECT_COMMENTS, reads.schema_comments)
        catalog_rows = await _read_absorbed(FACET_OBJECT_COMMENTS, reads.catalog_comments)
        await _record_refused_axes(reads.refusals)

        return _assemble_databricks_catalog(
            reads.catalog_name,
            column_rows,
            pk_rows,
            fk_rows,
            schema_rows,
            catalog_rows,
            view_rows=[dict(row) for row in reads.view_rows],
            routine_rows=[dict(row) for row in reads.routine_rows],
            routine_parameter_rows=[dict(row) for row in reads.routine_parameter_rows],
            unavailable=reads.unavailable,
        )

    def _read_facets_sync(self) -> _CapturedReads:
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            try:
                if self._params.catalog:
                    catalog_name = self._params.catalog
                else:
                    cur.execute("SELECT current_catalog()")
                    row = cur.fetchone()
                    catalog_name = str(row[0]) if row and row[0] else "hive_metastore"

                quoted_catalog = _quote_identifier(catalog_name)

                # Columns and tables. INFORMATION_SCHEMA.COLUMNS carries no table
                # type or comment of its own, so the table type/comment come from
                # a join against INFORMATION_SCHEMA.TABLES (same shape as the
                # Snowflake adapter's discovery query).
                columns = self._capture(
                    cur,
                    f"""
                        SELECT
                            c.table_schema,
                            c.table_name,
                            t.table_type,
                            c.column_name,
                            c.ordinal_position,
                            c.data_type,
                            c.is_nullable,
                            c.column_default,
                            t.comment AS table_comment,
                            c.comment AS column_comment
                        FROM {quoted_catalog}.information_schema.columns c
                        JOIN {quoted_catalog}.information_schema.tables t
                          ON t.table_catalog = c.table_catalog
                         AND t.table_schema = c.table_schema
                         AND t.table_name = c.table_name
                        WHERE c.table_schema <> '{_EXCLUDED_SCHEMA}'
                        ORDER BY c.table_schema, c.table_name, c.ordinal_position
                        """,  # noqa: S608 -- catalog identifier is backtick-quoted, not interpolated as a literal
                )

                # Primary keys and unique constraints. Unity Catalog PK/UNIQUE
                # constraints are informational (not enforced), but the metadata
                # is real and is exposed through the same ANSI-shaped views
                # PostgreSQL and Snowflake use.
                primary_keys = self._capture(
                    cur,
                    f"""
                        SELECT
                            tc.table_schema,
                            tc.table_name,
                            tc.constraint_name,
                            tc.constraint_type,
                            kcu.column_name,
                            kcu.ordinal_position
                        FROM {quoted_catalog}.information_schema.table_constraints tc
                        JOIN {quoted_catalog}.information_schema.key_column_usage kcu
                          ON kcu.constraint_catalog = tc.constraint_catalog
                         AND kcu.constraint_schema = tc.constraint_schema
                         AND kcu.constraint_name = tc.constraint_name
                        WHERE tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
                          AND tc.table_schema <> '{_EXCLUDED_SCHEMA}'
                        ORDER BY tc.table_schema, tc.table_name,
                            tc.constraint_name, kcu.ordinal_position
                        """,  # noqa: S608
                )

                # Foreign keys. Best-effort: Unity Catalog FK support (and the
                # REFERENTIAL_CONSTRAINTS / CONSTRAINT_COLUMN_USAGE views that
                # expose it) is a comparatively newer surface than PK/UNIQUE, so a
                # workspace or metastore version that does not have it yet must not
                # fail discovery -- it degrades to "no foreign keys observed"
                # rather than to a thrown exception, matching how the BigQuery
                # adapter treats its own optional key query. R11-FP02: that
                # degradation is unchanged, and `discover()` now replays the
                # failure through `read_facet` so the receipt carries it too.
                foreign_keys = self._capture(
                    cur,
                    f"""
                            SELECT
                                tc.table_schema,
                                tc.table_name,
                                tc.constraint_name,
                                kcu.column_name,
                                ccu.table_schema AS referenced_schema,
                                ccu.table_name AS referenced_table,
                                ccu.column_name AS referenced_column,
                                kcu.ordinal_position
                            FROM {quoted_catalog}.information_schema.table_constraints tc
                            JOIN {quoted_catalog}.information_schema.referential_constraints rc
                              ON rc.constraint_catalog = tc.constraint_catalog
                             AND rc.constraint_schema = tc.constraint_schema
                             AND rc.constraint_name = tc.constraint_name
                            JOIN {quoted_catalog}.information_schema.key_column_usage kcu
                              ON kcu.constraint_catalog = tc.constraint_catalog
                             AND kcu.constraint_schema = tc.constraint_schema
                             AND kcu.constraint_name = tc.constraint_name
                            JOIN {quoted_catalog}.information_schema.constraint_column_usage ccu
                              ON ccu.constraint_catalog = rc.unique_constraint_catalog
                             AND ccu.constraint_schema = rc.unique_constraint_schema
                             AND ccu.constraint_name = rc.unique_constraint_name
                            WHERE tc.constraint_type = 'FOREIGN KEY'
                              AND tc.table_schema <> '{_EXCLUDED_SCHEMA}'
                            ORDER BY tc.table_schema, tc.table_name,
                                tc.constraint_name, kcu.ordinal_position
                            """,  # noqa: S608
                )

                # Schema and catalog comments. Best-effort for the same reason as
                # foreign keys: a permission or version gap here must shrink the
                # envelope, not fail discovery outright.
                schema_comments = self._capture(
                    cur,
                    f"""
                            SELECT schema_name, comment
                            FROM {quoted_catalog}.information_schema.schemata
                            WHERE schema_name <> '{_EXCLUDED_SCHEMA}'
                            """,  # noqa: S608
                )

                catalog_comments = self._capture(
                    cur,
                    f"""
                            SELECT catalog_name, comment
                            FROM {quoted_catalog}.information_schema.catalogs
                            """,  # noqa: S608
                )

                # R11-FP01: the two axes this adapter used to skip. Read
                # through `_collect`, so a metastore version without a view
                # and a principal without `USE SCHEMA` both shrink the
                # envelope with a recorded reason instead of failing the run
                # or, worse, reading as "this catalog has no views".
                unavailable: list[tuple[str, str]] = []
                refusals: list[tuple[str, BaseException]] = []

                def _collect(axis: str, sql: str, *, facet: str) -> list[dict[str, Any]]:
                    # Both halves are inside the guard, not just the
                    # `execute`: the driver decides which of the two raises,
                    # and a refusal that surfaced at fetch time would
                    # otherwise fail the whole run -- the same shape the
                    # foreign-key and comment reads above already have.
                    try:
                        cur.execute(sql)
                        return rows_to_dicts(cur, cur.fetchall())
                    except Exception as exc:
                        unavailable.append((axis, f"{type(exc).__name__}: {exc}"))
                        # R11-FP02: captured, not judged -- `read_facet` is a
                        # coroutine and this runs inside the driver thread.
                        refusals.append((facet, exc))
                        return []

                def _view_query(columns: str) -> str:
                    return f"""
                            SELECT {columns}
                            FROM {quoted_catalog}.information_schema.views
                            WHERE table_schema <> '{_EXCLUDED_SCHEMA}'
                            ORDER BY table_schema, table_name
                            """  # noqa: S608

                view_rows = _collect(
                    "views", _view_query(_VIEW_COLUMNS), facet=FACET_VIEW_DEFINITIONS
                )
                if not view_rows and unavailable and unavailable[-1][0] == "views":
                    # The two optional ANSI columns are the likely cause;
                    # retry without them rather than lose the whole axis.
                    # A second failure keeps both reasons, so the log says
                    # the narrow read failed too and the axis is genuinely
                    # unavailable rather than merely column-shy.
                    view_rows = _collect(
                        "views",
                        _view_query(_VIEW_COLUMNS_NARROW),
                        facet=FACET_VIEW_DEFINITIONS,
                    )

                routine_rows = _collect(
                    "routines",
                    f"""
                        SELECT {_ROUTINE_COLUMNS}
                        FROM {quoted_catalog}.information_schema.routines
                        WHERE routine_schema <> '{_EXCLUDED_SCHEMA}'
                        ORDER BY routine_schema, routine_name, specific_name
                        """,  # noqa: S608
                    facet=FACET_ROUTINE_BODIES,
                )
                routine_parameter_rows = _collect(
                    "parameters",
                    f"""
                        SELECT {_ROUTINE_PARAMETER_COLUMNS}
                        FROM {quoted_catalog}.information_schema.parameters
                        WHERE specific_schema <> '{_EXCLUDED_SCHEMA}'
                        ORDER BY specific_schema, specific_name, ordinal_position
                        """,  # noqa: S608
                    facet=FACET_ROUTINE_BODIES,
                )
            finally:
                cur.close()
        finally:
            conn.close()

        return _CapturedReads(
            catalog_name=catalog_name,
            columns=columns,
            primary_keys=primary_keys,
            foreign_keys=foreign_keys,
            schema_comments=schema_comments,
            catalog_comments=catalog_comments,
            view_rows=view_rows,
            routine_rows=routine_rows,
            routine_parameter_rows=routine_parameter_rows,
            unavailable=tuple(unavailable),
            refusals=tuple(refusals),
        )

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int = 30) -> QueryEstimate:
        """Run EXPLAIN COST and extract row/byte estimates from the Spark plan text."""

        def _sync_estimate() -> QueryEstimate:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(f"EXPLAIN COST {sql}")
                    rows = cur.fetchall()
                    plan_text = "\n".join(str(row[0]) for row in rows if row)
                    return _extract_databricks_explain_cost(plan_text)
                finally:
                    cur.close()
            finally:
                conn.close()

        return await asyncio.to_thread(_sync_estimate)

    async def profile_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        *,
        sample_rows: int = 1000,
        column_batch_size: int = 20,
        timeout_seconds: int = 30,
    ) -> TableProfileSnapshot:
        """Compute bounded statistical metrics on the target table.

        Every batch reads from the same ``LIMIT``-bounded CTE rather than issuing a
        fresh sample per column, so a multi-column profile stays a small, fixed
        number of round trips instead of one per column (the shape the BigQuery
        adapter uses; Snowflake profiles one column per round trip instead, since
        Snowflake's ``APPROX_COUNT_DISTINCT`` is cheap per-column but Spark's
        planner benefits more from batching several aggregates over one shared scan).
        """
        if not column_names:
            return TableProfileSnapshot(row_count_estimate=None, sampled_row_count=0, columns=())
        if sample_rows < 1 or column_batch_size < 1:
            raise ValueError("profiling limits must be positive")

        def _sync_profile() -> TableProfileSnapshot:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    catalog = self._params.catalog or "hive_metastore"
                    target = _qualified_table(catalog, schema_name, table_name)

                    cur.execute(f"SELECT COUNT(*) FROM {target}")  # noqa: S608
                    row = cur.fetchone()
                    row_count = int(row[0]) if row and row[0] is not None else 0

                    column_snapshots: list[ColumnProfileSnapshot] = []
                    sampled_row_count = 0
                    for start in range(0, len(column_names), column_batch_size):
                        batch = column_names[start : start + column_batch_size]
                        selected = ", ".join(_quote_identifier(c) for c in batch)
                        expressions = ["COUNT(*) AS sampled_row_count"]
                        for position, col in enumerate(batch):
                            quoted_col = _quote_identifier(col)
                            text_form = f"CAST({quoted_col} AS STRING)"
                            expressions.extend(
                                [
                                    f"COUNT(*) - COUNT({quoted_col}) AS n_{position}",
                                    f"COUNT({quoted_col}) AS nn_{position}",
                                    f"APPROX_COUNT_DISTINCT({quoted_col}) AS d_{position}",
                                    f"MIN(LENGTH({text_form})) AS minl_{position}",
                                    f"MAX(LENGTH({text_form})) AS maxl_{position}",
                                ]
                            )
                            # R11-FP04: value-free distribution shape on the
                            # same shared scan this method already batches
                            # onto, so it costs no extra round trip.
                            expressions.extend(
                                value_free_distribution_expressions(
                                    position=position,
                                    text_form=text_form,
                                    length_form=f"LENGTH({text_form})",
                                    trimmed_form=f"TRIM({text_form})",
                                )
                            )
                        cur.execute(
                            f"""
                            WITH bounded_sample AS (
                                SELECT {selected} FROM {target} LIMIT {int(sample_rows)}
                            )
                            SELECT {", ".join(expressions)} FROM bounded_sample
                            """  # noqa: S608 -- identifiers are backtick-quoted; sample_rows is a validated int
                        )
                        stats_rows = rows_to_dicts(cur, cur.fetchall())
                        stats = stats_rows[0] if stats_rows else {}
                        sampled_row_count = max(
                            sampled_row_count, int(stats.get("sampled_row_count") or 0)
                        )
                        for position, col in enumerate(batch):
                            blank, whitespace, buckets = read_value_free_distribution(
                                position, stats.get
                            )
                            column_snapshots.append(
                                ColumnProfileSnapshot(
                                    name=col,
                                    null_count=int(stats.get(f"n_{position}") or 0),
                                    non_null_count=int(stats.get(f"nn_{position}") or 0),
                                    approximate_distinct_count=int(stats.get(f"d_{position}") or 0),
                                    min_length=stats.get(f"minl_{position}"),
                                    max_length=stats.get(f"maxl_{position}"),
                                    blank_count=blank,
                                    whitespace_only_count=whitespace,
                                    length_bucket_counts=buckets,
                                    facet_status=(ENTROPY_NOT_IMPLEMENTED,),
                                )
                            )

                    # R11-FP04: `row_count` is a real `SELECT COUNT(*)` over the
                    # whole table, so it stays -- but whether the *profile* saw
                    # all of it is decided by the `LIMIT` above, not by
                    # comparing the two numbers.
                    return TableProfileSnapshot(
                        row_count_estimate=row_count,
                        sampled_row_count=sampled_row_count,
                        columns=tuple(column_snapshots),
                        observation_scope=bounded_scan_scope(
                            sampled_row_count=sampled_row_count, sample_rows=sample_rows
                        ),
                    )
                finally:
                    cur.close()
            finally:
                conn.close()

        return await asyncio.to_thread(_sync_profile)

    async def execute_read_query(
        self,
        sql: str,
        *,
        timeout_seconds: int = 30,
        max_rows: int = 1000,
    ) -> QueryResult:
        """Execute a read-only query bounded by max_rows, capturing the warehouse query ID."""

        def _sync_execute() -> QueryResult:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql)
                    col_names = [desc[0] for desc in cur.description] if cur.description else []
                    rows_raw = cur.fetchmany(max_rows)
                    rows = tuple(dict(zip(col_names, row, strict=False)) for row in rows_raw)
                    query_id = getattr(cur, "query_id", None)
                    return QueryResult(rows=rows, warehouse_query_id=query_id)
                finally:
                    cur.close()
            finally:
                conn.close()

        return await asyncio.to_thread(_sync_execute)
