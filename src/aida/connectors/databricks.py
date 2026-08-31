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

Scope deliberately excludes the "envelope 1.1" axes (view text, routine bodies,
grants) that the Snowflake and BigQuery adapters carry: Unity Catalog exposes the
ANSI-shaped views those axes would read (``VIEWS``, ``ROUTINES``, ``PARAMETERS``,
``TABLE_PRIVILEGES``/``SCHEMA_PRIVILEGES``/``CATALOG_PRIVILEGES``), but without a
live workspace to verify column shapes and refusal modes against, claiming those
capabilities here would be exactly the kind of overclaim INV-9 exists to prevent.
Table/column/schema/catalog *comments* are simple, single-valued, well-documented
INFORMATION_SCHEMA columns with no refusal-vs-empty ambiguity, so they are
implemented and ``object_comments`` is honestly set True; ``views``, ``routines``
and ``grants`` stay False until a certified adapter closes CN-2a-equivalent work
for Databricks with real verification.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from aida.connectors.base import (
    ColumnProfileSnapshot,
    ConnectorCapabilities,
    DiscoveredCatalog,
    QueryEstimate,
    QueryResult,
    TableProfileSnapshot,
)
from aida.connectors.discovery import (
    append_grouped_foreign_key_rows,
    append_grouped_key_rows,
    assemble_catalog,
    build_table_map_from_column_rows,
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


def _rows_to_dicts(cursor: Any, rows: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [dict(r) for r in rows]
    col_names = [desc[0].lower() for desc in cursor.description] if cursor.description else []
    return [dict(zip(col_names, row, strict=False)) for row in rows]


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


def _assemble_databricks_catalog(
    catalog_name: str,
    column_rows: list[dict[str, Any]],
    pk_rows: list[dict[str, Any]],
    fk_rows: list[dict[str, Any]],
    schema_rows: list[dict[str, Any]],
    catalog_rows: list[dict[str, Any]],
) -> tuple[DiscoveredCatalog, ...]:
    """Assemble the catalog graph and fold table/column/schema/catalog comments onto it."""
    table_map = build_table_map_from_column_rows(column_rows)
    append_grouped_key_rows(table_map, pk_rows, constraint_type_map=_CONSTRAINT_TYPE_MAP)
    append_grouped_foreign_key_rows(table_map, fk_rows)
    catalogs = assemble_catalog(catalog_name, table_map)

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
        rebuilt.append(
            replace(catalog, schemas=tuple(schemas), source_description=catalog_comment)
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
        query_history=True,
        # PAT-only auth for now (see `_parse_dsn`); no delegated/workload identity path.
        delegated_identity=False,
        approximate_statistics=True,
        # Comments are simple, unambiguous INFORMATION_SCHEMA columns with no
        # refusal-vs-empty distinction to model, so they are implemented and
        # honestly claimed. Views/routines/grants are not (see module docstring).
        views=False,
        routines=False,
        object_comments=True,
        grants=False,
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

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        """Discover Unity Catalog catalogs, schemas, tables, columns, and constraints."""

        def _sync_discover() -> tuple[DiscoveredCatalog, ...]:
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
                    cur.execute(
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
                        """  # noqa: S608 -- catalog identifier is backtick-quoted, not interpolated as a literal
                    )
                    column_rows = _rows_to_dicts(cur, cur.fetchall())

                    # Primary keys and unique constraints. Unity Catalog PK/UNIQUE
                    # constraints are informational (not enforced), but the metadata
                    # is real and is exposed through the same ANSI-shaped views
                    # PostgreSQL and Snowflake use.
                    cur.execute(
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
                        """  # noqa: S608
                    )
                    pk_rows = _rows_to_dicts(cur, cur.fetchall())

                    # Foreign keys. Best-effort: Unity Catalog FK support (and the
                    # REFERENTIAL_CONSTRAINTS / CONSTRAINT_COLUMN_USAGE views that
                    # expose it) is a comparatively newer surface than PK/UNIQUE, so a
                    # workspace or metastore version that does not have it yet must not
                    # fail discovery -- it degrades to "no foreign keys observed"
                    # rather than to a thrown exception, matching how the BigQuery
                    # adapter treats its own optional key query.
                    try:
                        cur.execute(
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
                            """  # noqa: S608
                        )
                        fk_rows = _rows_to_dicts(cur, cur.fetchall())
                    except Exception:
                        fk_rows = []

                    # Schema and catalog comments. Best-effort for the same reason as
                    # foreign keys: a permission or version gap here must shrink the
                    # envelope, not fail discovery outright.
                    try:
                        cur.execute(
                            f"""
                            SELECT schema_name, comment
                            FROM {quoted_catalog}.information_schema.schemata
                            WHERE schema_name <> '{_EXCLUDED_SCHEMA}'
                            """  # noqa: S608
                        )
                        schema_rows = _rows_to_dicts(cur, cur.fetchall())
                    except Exception:
                        schema_rows = []

                    try:
                        cur.execute(
                            f"""
                            SELECT catalog_name, comment
                            FROM {quoted_catalog}.information_schema.catalogs
                            """  # noqa: S608
                        )
                        catalog_rows = _rows_to_dicts(cur, cur.fetchall())
                    except Exception:
                        catalog_rows = []
                finally:
                    cur.close()
            finally:
                conn.close()

            return _assemble_databricks_catalog(
                catalog_name, column_rows, pk_rows, fk_rows, schema_rows, catalog_rows
            )

        return await asyncio.to_thread(_sync_discover)

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
                            expressions.extend(
                                [
                                    f"COUNT(*) - COUNT({quoted_col}) AS n_{position}",
                                    f"COUNT({quoted_col}) AS nn_{position}",
                                    f"APPROX_COUNT_DISTINCT({quoted_col}) AS d_{position}",
                                    f"MIN(LENGTH(CAST({quoted_col} AS STRING))) AS minl_{position}",
                                    f"MAX(LENGTH(CAST({quoted_col} AS STRING))) AS maxl_{position}",
                                ]
                            )
                        cur.execute(
                            f"""
                            WITH bounded_sample AS (
                                SELECT {selected} FROM {target} LIMIT {int(sample_rows)}
                            )
                            SELECT {", ".join(expressions)} FROM bounded_sample
                            """  # noqa: S608 -- identifiers are backtick-quoted; sample_rows is a validated int
                        )
                        stats_rows = _rows_to_dicts(cur, cur.fetchall())
                        stats = stats_rows[0] if stats_rows else {}
                        sampled_row_count = max(
                            sampled_row_count, int(stats.get("sampled_row_count") or 0)
                        )
                        for position, col in enumerate(batch):
                            column_snapshots.append(
                                ColumnProfileSnapshot(
                                    name=col,
                                    null_count=int(stats.get(f"n_{position}") or 0),
                                    non_null_count=int(stats.get(f"nn_{position}") or 0),
                                    approximate_distinct_count=int(stats.get(f"d_{position}") or 0),
                                    min_length=stats.get(f"minl_{position}"),
                                    max_length=stats.get(f"maxl_{position}"),
                                )
                            )

                    return TableProfileSnapshot(
                        row_count_estimate=row_count,
                        sampled_row_count=sampled_row_count,
                        columns=tuple(column_snapshots),
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
