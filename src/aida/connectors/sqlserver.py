import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import defusedxml.ElementTree as ET
import pytds

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

_SHOWPLAN_NS = "{http://schemas.microsoft.com/sqlserver/2004/07/showplan}"
_EXCLUDED_SCHEMAS = ("sys", "INFORMATION_SCHEMA")


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


class SqlServerConnector(Connector):
    connector_type = "sqlserver"
    dialect = "tsql"
    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        constraints=True,
        indexes=False,
        partitions=False,
        explain=True,
        delegated_identity=False,
        approximate_statistics=True,
    )

    def __init__(self, dsn: str, *, command_timeout: float = 30.0) -> None:
        self._params = _parse_dsn(dsn)
        self._command_timeout = command_timeout

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.DEFAULT_CAPABILITIES

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

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        return await asyncio.to_thread(self._discover_sync)

    def _discover_sync(self) -> tuple[DiscoveredCatalog, ...]:
        connection = self._connect(timeout_seconds=self._command_timeout, autocommit=True)
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT DB_NAME() AS catalog_name")
                catalog_row = cursor.fetchone()
                catalog_name = str(catalog_row["catalog_name"]) if catalog_row else ""

                cursor.execute(
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
                    """
                )
                column_rows = cursor.fetchall()

                cursor.execute(
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
                    """
                )
                key_rows = cursor.fetchall()

                cursor.execute(
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
                    """
                )
                foreign_key_rows = cursor.fetchall()
            finally:
                cursor.close()
        finally:
            connection.close()

        return _assemble_catalog(catalog_name, column_rows, key_rows, foreign_key_rows)

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
        connection = self._connect(timeout_seconds=timeout_seconds, autocommit=False)
        try:
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
        finally:
            connection.close()

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
                cursor.close()
                connection.rollback()
        finally:
            connection.close()
        return TableProfileSnapshot(
            row_count_estimate=(max(estimate, sampled_row_count) if estimate is not None else None),
            sampled_row_count=sampled_row_count,
            columns=tuple(snapshots),
        )


def _assemble_catalog(
    catalog_name: str,
    column_rows: list[dict[str, Any]],
    key_rows: list[dict[str, Any]],
    foreign_key_rows: list[dict[str, Any]],
) -> tuple[DiscoveredCatalog, ...]:
    tables = build_table_map_from_column_rows(column_rows)
    append_grouped_key_rows(
        tables,
        key_rows,
        constraint_type_map={"PRIMARY KEY": "PRIMARY_KEY", "UNIQUE": "UNIQUE"},
    )
    append_grouped_foreign_key_rows(tables, foreign_key_rows)
    return assemble_catalog(str(catalog_name), tables)
