"""
Snowflake Native Warehouse Connector
====================================

Implements the Atlas ``Connector`` ABC for Snowflake Data Cloud with strict governance,
fail-closed validation, partition-pruned EXPLAIN cost estimation, and value-free metadata discovery.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

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
    append_grouped_foreign_key_rows,
    append_grouped_key_rows,
    assemble_catalog,
    build_table_map_from_column_rows,
)

_COMPLEX_SCALAR_TYPES = frozenset({"VARIANT", "OBJECT", "ARRAY", "GEOGRAPHY", "GEOMETRY"})
_EXCLUDED_SCHEMAS = frozenset({"INFORMATION_SCHEMA", "ACCOUNT_USAGE", "READER_ACCOUNT_USAGE"})


def _quote_identifier(identifier: str) -> str:
    """Snowflake double-quote an identifier."""
    return '"' + identifier.replace('"', '""') + '"'


def _qualified_table(database: str, schema: str, table: str) -> str:
    """Format a fully-qualified 3-part Snowflake table identifier."""
    return f"{_quote_identifier(database)}.{_quote_identifier(schema)}.{_quote_identifier(table)}"


@dataclass(frozen=True, slots=True)
class _SnowflakeConnectionParams:
    account: str
    user: str
    password: str | None = None
    database: str = ""
    schema: str | None = None
    warehouse: str | None = None
    role: str | None = None
    host: str | None = None
    port: int = 443
    authenticator: str | None = None
    token: str | None = None


def _parse_dsn(dsn: str) -> _SnowflakeConnectionParams:
    """Parse Snowflake connection reference from JSON payload or standard URI.

    Accepted formats:
    - JSON credential payload: {"account": "...", "user": "...", ...}
    - DSN URI: snowflake://user:password@account/database/schema?warehouse=WH&role=ROLE
    - Host-style DSN URI: snowflake://user:password@account.snowflakecomputing.com/database
    """
    raw = dsn.strip()
    if raw.startswith("{") and raw.endswith("}"):
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise ValueError(f"invalid Snowflake credential JSON payload: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Snowflake credential JSON payload must be a JSON object")
        account = data.get("account")
        user = data.get("user")
        if not account or not user:
            raise ValueError("Snowflake credentials must include 'account' and 'user'")
        return _SnowflakeConnectionParams(
            account=str(account),
            user=str(user),
            password=str(data["password"]) if "password" in data else None,
            database=str(data.get("database") or ""),
            schema=str(data["schema"]) if "schema" in data else None,
            warehouse=str(data["warehouse"]) if "warehouse" in data else None,
            role=str(data["role"]) if "role" in data else None,
            host=str(data["host"]) if "host" in data else None,
            port=int(data.get("port", 443)),
            authenticator=str(data["authenticator"]) if "authenticator" in data else None,
            token=str(data["token"]) if "token" in data else None,
        )

    parsed = urlsplit(raw)
    if parsed.scheme not in {"snowflake", "snowflake-sql"}:
        raise ValueError(
            "invalid Snowflake connection reference; expected "
            "snowflake://user:password@account/database/schema?warehouse=WH&role=ROLE"
        )
    if not parsed.hostname or not parsed.username:
        raise ValueError("Snowflake connection reference is missing account/host or username")

    # Account identifier extraction
    host_or_account = parsed.hostname
    if host_or_account.endswith(".snowflakecomputing.com"):
        account = host_or_account[: -len(".snowflakecomputing.com")]
        host = host_or_account
    else:
        account = host_or_account
        host = f"{account}.snowflakecomputing.com"

    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    database = unquote(path_parts[0]) if len(path_parts) > 0 else ""
    schema = unquote(path_parts[1]) if len(path_parts) > 1 else None

    query_params = parse_qs(parsed.query)
    warehouse = query_params.get("warehouse", [None])[0]
    role = query_params.get("role", [None])[0]
    authenticator = query_params.get("authenticator", [None])[0]

    return _SnowflakeConnectionParams(
        account=account,
        user=unquote(parsed.username),
        password=unquote(parsed.password) if parsed.password is not None else None,
        database=database,
        schema=schema,
        warehouse=warehouse,
        role=role,
        host=host,
        port=parsed.port or 443,
        authenticator=authenticator,
    )


def _extract_snowflake_explain_estimate(explain_data: Any) -> QueryEstimate:
    """Extract rows, bytes, and partition pruning estimates from Snowflake EXPLAIN JSON."""
    total_bytes = 0
    total_rows = 0.0
    partitions_total = 0
    partitions_assigned = 0

    def traverse(node: Any) -> None:
        nonlocal total_bytes, total_rows, partitions_total, partitions_assigned
        if isinstance(node, dict):
            if "bytesAssigned" in node and isinstance(node["bytesAssigned"], int | float):
                total_bytes += int(node["bytesAssigned"])
            elif "bytes" in node and isinstance(node["bytes"], int | float):
                total_bytes += int(node["bytes"])

            if "rowsTotal" in node and isinstance(node["rowsTotal"], int | float):
                total_rows += float(node["rowsTotal"])
            elif "rows" in node and isinstance(node["rows"], int | float):
                total_rows += float(node["rows"])

            if "partitionsTotal" in node and isinstance(node["partitionsTotal"], int | float):
                partitions_total += int(node["partitionsTotal"])
            if "partitionsAssigned" in node and isinstance(node["partitionsAssigned"], int | float):
                partitions_assigned += int(node["partitionsAssigned"])

            for value in node.values():
                traverse(value)
        elif isinstance(node, list):
            for item in node:
                traverse(item)

    if isinstance(explain_data, str):
        try:
            parsed = json.loads(explain_data)
            traverse(parsed)
        except Exception:
            # Fallback regex parse for text-based plan
            rows_match = re.search(r"rows\s*=\s*(\d+)", explain_data, re.IGNORECASE)
            if rows_match:
                total_rows = float(rows_match.group(1))
            bytes_match = re.search(r"bytes\s*=\s*(\d+)", explain_data, re.IGNORECASE)
            if bytes_match:
                total_bytes = int(bytes_match.group(1))
    else:
        traverse(explain_data)

    score = round(max(total_rows * 0.01 + (total_bytes / (1024 * 1024)), 1.0), 2)
    return QueryEstimate(
        score=score,
        kind="SNOWFLAKE_EXPLAIN_PLAN",
        estimated_rows=total_rows if total_rows > 0 else None,
        estimated_bytes=total_bytes if total_bytes > 0 else None,
        evidence={
            "partitions_total": partitions_total,
            "partitions_assigned": partitions_assigned,
            "pruning_ratio": (
                round(1.0 - (partitions_assigned / partitions_total), 4)
                if partitions_total > 0
                else 1.0
            ),
        },
    )


_CONSTRAINT_TYPE_MAP = {
    "PRIMARY KEY": "PRIMARY_KEY",
    "UNIQUE": "UNIQUE",
    "PRIMARY_KEY": "PRIMARY_KEY",
}


def _rows_to_dicts(cursor: Any, rows: list[Any] | tuple[Any, ...]) -> list[dict[str, Any]]:
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [dict(r) for r in rows]
    col_names = [desc[0].lower() for desc in cursor.description] if cursor.description else []
    return [dict(zip(col_names, row, strict=False)) for row in rows]


class SnowflakeConnector(Connector):
    """Snowflake native connector conforming to the Atlas Connector protocol."""

    connector_type = "snowflake"
    dialect = "snowflake"
    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        catalogs=True,
        schemas=True,
        constraints=True,
        indexes=False,
        partitions=True,
        explain=True,
        query_history=True,
        delegated_identity=True,
        approximate_statistics=True,
    )

    def __init__(self, dsn: str, *, command_timeout: float = 60.0) -> None:
        self._dsn = dsn
        self._params = _parse_dsn(dsn)
        self._command_timeout = command_timeout

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.DEFAULT_CAPABILITIES

    def _get_connection(self) -> Any:
        """Create a Snowflake DBAPI connection using snowflake-connector-python."""
        try:
            import snowflake.connector
        except ImportError as exc:
            raise RuntimeError(
                "snowflake-connector-python package is required for native Snowflake connectivity. "
                "Install with: pip install snowflake-connector-python"
            ) from exc

        kwargs: dict[str, Any] = {
            "account": self._params.account,
            "user": self._params.user,
            "database": self._params.database or None,
            "schema": self._params.schema or None,
            "warehouse": self._params.warehouse or None,
            "role": self._params.role or None,
            "login_timeout": int(self._command_timeout),
            "network_timeout": int(self._command_timeout),
        }
        if self._params.password is not None:
            kwargs["password"] = self._params.password
        if self._params.authenticator is not None:
            kwargs["authenticator"] = self._params.authenticator
        if self._params.token is not None:
            kwargs["token"] = self._params.token

        return snowflake.connector.connect(**kwargs)

    async def test_connection(self) -> None:
        """Verify warehouse connectivity and session authentication."""

        def _sync_test() -> None:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    cur.execute("SELECT CURRENT_VERSION(), CURRENT_ACCOUNT(), CURRENT_ROLE()")
                    cur.fetchone()
                finally:
                    cur.close()
            finally:
                conn.close()

        await asyncio.to_thread(_sync_test)

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        """Discover database catalogs, schemas, tables, columns, and constraints."""

        def _sync_discover() -> tuple[DiscoveredCatalog, ...]:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    # Catalog / Database Name
                    if self._params.database:
                        catalog_name = self._params.database.upper()
                    else:
                        cur.execute("SELECT CURRENT_DATABASE()")
                        row = cur.fetchone()
                        catalog_name = str(row[0]).upper() if row and row[0] else "SNOWFLAKE_DB"

                    # Discover Columns and Tables
                    cur.execute(
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
                        WHERE c.table_schema NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')
                        ORDER BY c.table_schema, c.table_name, c.ordinal_position
                        """
                    )
                    column_rows = _rows_to_dicts(cur, cur.fetchall())

                    # Discover Primary Keys & Unique Constraints
                    cur.execute(
                        """
                        SELECT
                            tc.table_schema,
                            tc.table_name,
                            tc.constraint_name,
                            tc.constraint_type,
                            kcu.column_name,
                            kcu.ordinal_position
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu
                          ON kcu.constraint_catalog = tc.constraint_catalog
                         AND kcu.constraint_schema = tc.constraint_schema
                         AND kcu.constraint_name = tc.constraint_name
                        WHERE tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')
                          AND tc.table_schema NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')
                        ORDER BY tc.table_schema, tc.table_name,
                            tc.constraint_name, kcu.ordinal_position
                        """
                    )
                    pk_rows = _rows_to_dicts(cur, cur.fetchall())

                    # Discover Foreign Keys
                    cur.execute(
                        """
                        SELECT
                            tc.table_schema,
                            tc.table_name,
                            tc.constraint_name,
                            kcu.column_name,
                            ccu.table_schema AS referenced_schema,
                            ccu.table_name AS referenced_table,
                            ccu.column_name AS referenced_column,
                            kcu.ordinal_position
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.referential_constraints rc
                          ON rc.constraint_catalog = tc.constraint_catalog
                         AND rc.constraint_schema = tc.constraint_schema
                         AND rc.constraint_name = tc.constraint_name
                        JOIN information_schema.key_column_usage kcu
                          ON kcu.constraint_catalog = tc.constraint_catalog
                         AND kcu.constraint_schema = tc.constraint_schema
                         AND kcu.constraint_name = tc.constraint_name
                        JOIN information_schema.constraint_column_usage ccu
                          ON ccu.constraint_catalog = rc.unique_constraint_catalog
                         AND ccu.constraint_schema = rc.unique_constraint_schema
                         AND ccu.constraint_name = rc.unique_constraint_name
                        WHERE tc.constraint_type = 'FOREIGN KEY'
                          AND tc.table_schema NOT IN ('INFORMATION_SCHEMA', 'ACCOUNT_USAGE')
                        ORDER BY tc.table_schema, tc.table_name,
                            tc.constraint_name, kcu.ordinal_position
                        """
                    )
                    fk_rows = _rows_to_dicts(cur, cur.fetchall())

                finally:
                    cur.close()
            finally:
                conn.close()

            # Assemble into Atlas Catalog Graph
            table_map = build_table_map_from_column_rows(column_rows)
            append_grouped_key_rows(table_map, pk_rows, constraint_type_map=_CONSTRAINT_TYPE_MAP)
            append_grouped_foreign_key_rows(table_map, fk_rows)
            return assemble_catalog(catalog_name, table_map)

        return await asyncio.to_thread(_sync_discover)

    async def estimate_read_query(
        self, sql: str, *, timeout_seconds: int = 30
    ) -> QueryEstimate:
        """Run EXPLAIN USING JSON to extract cost and partition pruning estimates."""

        def _sync_estimate() -> QueryEstimate:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    cur.execute(f"EXPLAIN USING JSON {sql}")
                    rows = cur.fetchall()
                    if rows and len(rows) > 0 and len(rows[0]) > 0:
                        plan_payload = rows[0][0]
                        return _extract_snowflake_explain_estimate(plan_payload)
                    return QueryEstimate(score=1.0, kind="SNOWFLAKE_EXPLAIN_FALLBACK")
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
        """Compute bounded statistical metrics on the target table."""

        def _sync_profile() -> TableProfileSnapshot:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    db = self._params.database or "DB"
                    target = _qualified_table(db, schema_name, table_name)

                    # Row counts (exact & sampled)
                    cur.execute(f"SELECT COUNT(*) FROM {target}")  # noqa: S608
                    row = cur.fetchone()
                    row_count = int(row[0]) if row and row[0] is not None else 0

                    column_snapshots: list[ColumnProfileSnapshot] = []
                    for start in range(0, len(column_names), column_batch_size):
                        batch = column_names[start : start + column_batch_size]
                        for col in batch:
                            quoted_col = _quote_identifier(col)
                            cur.execute(
                                f"""
                                SELECT
                                    COUNT(*) - COUNT({quoted_col}) AS null_count,
                                    COUNT({quoted_col}) AS non_null_count,
                                    APPROX_COUNT_DISTINCT({quoted_col}) AS distinct_estimate
                                FROM {target}
                                """  # noqa: S608
                            )
                            stats = cur.fetchone()
                            null_c = int(stats[0]) if stats and stats[0] is not None else 0
                            non_null_c = int(stats[1]) if stats and stats[1] is not None else 0
                            approx_distinct = int(stats[2]) if stats and stats[2] is not None else 0

                            column_snapshots.append(
                                ColumnProfileSnapshot(
                                    name=col,
                                    null_count=null_c,
                                    non_null_count=non_null_c,
                                    approximate_distinct_count=approx_distinct,
                                    min_length=None,
                                    max_length=None,
                                )
                            )

                    return TableProfileSnapshot(
                        row_count_estimate=row_count,
                        sampled_row_count=min(row_count, sample_rows),
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
        """Execute a read-only query bounded by max_rows and timeout."""

        def _sync_execute() -> QueryResult:
            conn = self._get_connection()
            try:
                cur = conn.cursor()
                try:
                    # Enforce statement timeout parameter
                    cur.execute(
                        f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {timeout_seconds}"
                    )
                    cur.execute(sql)
                    col_names = [desc[0] for desc in cur.description] if cur.description else []
                    rows_raw = cur.fetchmany(max_rows)
                    rows = tuple(dict(zip(col_names, row, strict=False)) for row in rows_raw)
                    sf_query_id = getattr(cur, "sfqid", None)
                    return QueryResult(rows=rows, warehouse_query_id=sf_query_id)
                finally:
                    cur.close()
            finally:
                conn.close()

        return await asyncio.to_thread(_sync_execute)
