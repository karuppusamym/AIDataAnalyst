import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

import oracledb

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
    append_grouped_index_rows,
    append_grouped_key_rows,
    append_partition_rows,
    assemble_catalog,
    build_table_map_from_column_rows,
)

_EXCLUDED_SCHEMAS = (
    "SYS", "SYSTEM", "OUTLN", "XDB", "ORDS_METADATA", "ORDS_PUBLIC_USER",
    "APPQOSSYS", "DBSFWUSER", "DBSNMP", "GSMADMIN_INTERNAL", "MDSYS",
    "OLAPSYS", "ORDDATA", "ORDPLUGINS", "CTXSYS", "WMSYS", "GGSYS",
    "REMOTE_SCHEDULER_AGENT", "DVSYS", "DVF", "LBACSYS", "AUDSYS",
    "SYSBACKUP", "SYSKM", "SYSRAC", "SYSDG",
)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


# Oracle LOB and long-form types reject COUNT(DISTINCT ...) and TO_CHAR(...) directly;
# profiling falls back to honest placeholders instead of failing the whole batch.
_LOB_LIKE_TYPES = frozenset(
    {"BLOB", "CLOB", "NCLOB", "LONG", "LONG RAW", "BFILE", "XMLTYPE"}
)


def _profile_expressions(quoted_column: str, position: int, data_type: str) -> list[str]:
    """Build the per-column aggregate expressions used by bounded profiling.

    Standard scalar types get exact null/non-null counts, an approximate distinct
    count, and character-length bounds via TO_CHAR. LOB-like types only support the
    null/non-null counts; distinct-count and length expressions are replaced with
    honest static placeholders rather than raising or fabricating a value.
    """
    if data_type.upper() in _LOB_LIKE_TYPES:
        distinct_expression = f"CAST(0 AS NUMBER) AS d_{position}"
        min_length_expression = f"CAST(NULL AS NUMBER) AS minl_{position}"
        max_length_expression = f"CAST(NULL AS NUMBER) AS maxl_{position}"
    else:
        text_form = f"LENGTH(TO_CHAR({quoted_column}))"
        distinct_expression = f"COUNT(DISTINCT {quoted_column}) AS d_{position}"
        min_length_expression = f"MIN({text_form}) AS minl_{position}"
        max_length_expression = f"MAX({text_form}) AS maxl_{position}"
    return [
        f"SUM(CASE WHEN {quoted_column} IS NULL THEN 1 ELSE 0 END) AS n_{position}",
        f"COUNT({quoted_column}) AS nn_{position}",
        distinct_expression,
        min_length_expression,
        max_length_expression,
    ]


@dataclass(frozen=True, slots=True)
class _ConnectionParams:
    host: str
    port: int
    service_name: str
    user: str
    password: str


def _parse_dsn(dsn: str) -> _ConnectionParams:
    """Parse an opaque resolved-secret value shaped as oracle://user:password@host:port/service_name.

    The credential_reference the API accepts is never a connection string; only the
    secret value it resolves to may be. This mirrors how PostgresConnector and
    SqlServerConnector treat their resolved secrets as driver-ready values, adapted
    because python-oracledb's connect_async() takes an "easy connect" dsn string
    (host:port/service_name) rather than a full URL.
    """
    parsed = urlsplit(dsn)
    if parsed.scheme != "oracle":
        raise ValueError(
            "invalid Oracle connection reference; expected "
            "oracle://user:password@host:port/service_name"
        )
    if not parsed.hostname or not parsed.username or parsed.password is None:
        raise ValueError("Oracle connection reference is missing host, user, or password")
    service_name = parsed.path.lstrip("/")
    if not service_name:
        raise ValueError("Oracle connection reference must include a service name")
    return _ConnectionParams(
        host=parsed.hostname,
        port=parsed.port or 1521,
        service_name=service_name,
        user=unquote(parsed.username),
        password=unquote(parsed.password),
    )


def _schema_exclusion_clause(alias: str) -> str:
    quoted = ", ".join(f"'{name}'" for name in _EXCLUDED_SCHEMAS)
    return f"{alias} NOT IN ({quoted})"


class OracleConnector(Connector):
    connector_type = "oracle"
    dialect = "oracle"
    DEFAULT_CAPABILITIES = ConnectorCapabilities(
        constraints=True,
        indexes=True,
        partitions=True,
        explain=False,
        delegated_identity=False,
        approximate_statistics=True,
    )

    def __init__(self, dsn: str, *, command_timeout: float = 30.0) -> None:
        self._params = _parse_dsn(dsn)
        self._command_timeout = command_timeout

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.DEFAULT_CAPABILITIES

    async def _connect(self, *, timeout_seconds: float) -> Any:
        connection = await oracledb.connect_async(
            user=self._params.user,
            password=self._params.password,
            dsn=f"{self._params.host}:{self._params.port}/{self._params.service_name}",
            tcp_connect_timeout=min(timeout_seconds, 15.0),
        )
        connection.call_timeout = int(timeout_seconds * 1000)
        connection.autocommit = False
        return connection

    async def test_connection(self) -> None:
        connection = await self._connect(timeout_seconds=self._command_timeout)
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1 FROM DUAL")
                await cursor.fetchall()
        finally:
            await connection.close()

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        connection = await self._connect(timeout_seconds=self._command_timeout)
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT SYS_CONTEXT('USERENV', 'DB_NAME') AS catalog_name FROM DUAL"
                )
                catalog_row = await cursor.fetchone()
                catalog_name = str(catalog_row[0]) if catalog_row else ""

                columns_query = f"""
                    SELECT
                        atc.owner AS table_schema,
                        atc.table_name AS table_name,
                        CASE WHEN ao.object_type = 'TABLE' THEN 'BASE TABLE'
                             ELSE ao.object_type END AS table_type,
                        atc.column_name AS column_name,
                        atc.column_id AS ordinal_position,
                        atc.data_type AS data_type,
                        atc.nullable AS is_nullable,
                        atc.data_default AS column_default
                    FROM ALL_TAB_COLUMNS atc
                    JOIN ALL_OBJECTS ao
                      ON ao.owner = atc.owner
                     AND ao.object_name = atc.table_name
                     AND ao.object_type IN ('TABLE', 'VIEW')
                    WHERE {_schema_exclusion_clause("atc.owner")}
                    ORDER BY atc.owner, atc.table_name, atc.column_id
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(columns_query)
                column_rows = [
                    {
                        "table_schema": row["TABLE_SCHEMA"],
                        "table_name": row["TABLE_NAME"],
                        "table_type": row["TABLE_TYPE"],
                        "column_name": row["COLUMN_NAME"],
                        "ordinal_position": row["ORDINAL_POSITION"],
                        "data_type": row["DATA_TYPE"],
                        "is_nullable": row["IS_NULLABLE"],
                        "column_default": row["COLUMN_DEFAULT"],
                    }
                    for row in _rows_as_dicts(cursor.description, await cursor.fetchall())
                ]

                keys_query = f"""
                    SELECT
                        ac.owner AS table_schema,
                        ac.table_name AS table_name,
                        ac.constraint_name AS constraint_name,
                        ac.constraint_type AS constraint_type,
                        acc.column_name AS column_name,
                        acc.position AS ordinal_position
                    FROM ALL_CONSTRAINTS ac
                    JOIN ALL_CONS_COLUMNS acc
                      ON acc.owner = ac.owner AND acc.constraint_name = ac.constraint_name
                    WHERE ac.constraint_type IN ('P', 'U')
                      AND {_schema_exclusion_clause("ac.owner")}
                    ORDER BY ac.owner, ac.table_name, ac.constraint_name, acc.position
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(keys_query)
                key_rows = [
                    {
                        "table_schema": row["TABLE_SCHEMA"],
                        "table_name": row["TABLE_NAME"],
                        "constraint_name": row["CONSTRAINT_NAME"],
                        "constraint_type": row["CONSTRAINT_TYPE"],
                        "column_name": row["COLUMN_NAME"],
                    }
                    for row in _rows_as_dicts(cursor.description, await cursor.fetchall())
                ]

                foreign_keys_query = f"""
                    SELECT
                        ac.owner AS table_schema,
                        ac.table_name AS table_name,
                        ac.constraint_name AS constraint_name,
                        r_ac.owner AS referenced_schema,
                        r_ac.table_name AS referenced_table,
                        acc.column_name AS column_name,
                        r_acc.column_name AS referenced_column,
                        acc.position AS ordinal_position
                    FROM ALL_CONSTRAINTS ac
                    JOIN ALL_CONS_COLUMNS acc
                      ON acc.owner = ac.owner AND acc.constraint_name = ac.constraint_name
                    JOIN ALL_CONSTRAINTS r_ac
                      ON r_ac.owner = ac.r_owner AND r_ac.constraint_name = ac.r_constraint_name
                    JOIN ALL_CONS_COLUMNS r_acc
                      ON r_acc.owner = r_ac.owner
                     AND r_acc.constraint_name = r_ac.constraint_name
                     AND r_acc.position = acc.position
                    WHERE ac.constraint_type = 'R'
                      AND {_schema_exclusion_clause("ac.owner")}
                    ORDER BY ac.owner, ac.table_name, ac.constraint_name, acc.position
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(foreign_keys_query)
                foreign_key_rows = [
                    {
                        "table_schema": row["TABLE_SCHEMA"],
                        "table_name": row["TABLE_NAME"],
                        "constraint_name": row["CONSTRAINT_NAME"],
                        "referenced_schema": row["REFERENCED_SCHEMA"],
                        "referenced_table": row["REFERENCED_TABLE"],
                        "column_name": row["COLUMN_NAME"],
                        "referenced_column": row["REFERENCED_COLUMN"],
                    }
                    for row in _rows_as_dicts(cursor.description, await cursor.fetchall())
                ]

                # ALL_INDEXES/ALL_IND_COLUMNS mirror ALL_CONSTRAINTS/ALL_CONS_COLUMNS above.
                # Whether an index backs a PRIMARY KEY constraint is surfaced via a LEFT JOIN
                # against ALL_CONSTRAINTS on the same (owner, index_name) rather than a second
                # round trip.
                indexes_query = f"""
                    SELECT
                        ai.owner AS table_schema,
                        ai.table_name AS table_name,
                        ai.index_name AS index_name,
                        ai.index_type AS index_type,
                        ai.uniqueness AS uniqueness,
                        aic.column_name AS column_name,
                        aic.column_position AS ordinal_position,
                        ac.constraint_type AS backing_constraint_type
                    FROM ALL_INDEXES ai
                    JOIN ALL_IND_COLUMNS aic
                      ON aic.index_owner = ai.owner
                     AND aic.index_name = ai.index_name
                     AND aic.table_name = ai.table_name
                    LEFT JOIN ALL_CONSTRAINTS ac
                      ON ac.owner = ai.owner
                     AND ac.constraint_name = ai.index_name
                     AND ac.constraint_type = 'P'
                    WHERE {_schema_exclusion_clause("ai.owner")}
                    ORDER BY ai.owner, ai.table_name, ai.index_name, aic.column_position
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(indexes_query)
                index_rows = [
                    {
                        "table_schema": row["TABLE_SCHEMA"],
                        "table_name": row["TABLE_NAME"],
                        "index_name": row["INDEX_NAME"],
                        "index_type": row["INDEX_TYPE"],
                        "is_unique": str(row["UNIQUENESS"]).upper() == "UNIQUE",
                        "is_primary": row["BACKING_CONSTRAINT_TYPE"] == "P",
                        "column_name": row["COLUMN_NAME"],
                    }
                    for row in _rows_as_dicts(cursor.description, await cursor.fetchall())
                ]

                partition_type_query = f"""
                    SELECT owner AS table_schema, table_name AS table_name,
                           partitioning_type AS partition_type
                    FROM ALL_PART_TABLES
                    WHERE {_schema_exclusion_clause("owner")}
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(partition_type_query)
                partition_types = {
                    (row["TABLE_SCHEMA"], row["TABLE_NAME"]): row["PARTITION_TYPE"]
                    for row in _rows_as_dicts(cursor.description, await cursor.fetchall())
                }

                partition_key_query = f"""
                    SELECT owner AS table_schema, name AS table_name,
                           column_name AS column_name, column_position AS ordinal_position
                    FROM ALL_PART_KEY_COLUMNS
                    WHERE object_type = 'TABLE' AND {_schema_exclusion_clause("owner")}
                    ORDER BY owner, name, column_position
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(partition_key_query)
                partition_key_columns: dict[tuple[str, str], list[str]] = {}
                for row in _rows_as_dicts(cursor.description, await cursor.fetchall()):
                    key = (row["TABLE_SCHEMA"], row["TABLE_NAME"])
                    partition_key_columns.setdefault(key, []).append(row["COLUMN_NAME"])

                # HIGH_VALUE is a LONG column; fetching it reliably needs an output-type
                # handler the async oracledb driver does not expose the same way as the
                # sync client, so partitions are extracted without a high_value bound
                # rather than risk a truncated or failed fetch. Bounds can be added once
                # a certified handling path exists (tracked alongside CN-1a/CN-1b).
                partitions_query = f"""
                    SELECT
                        table_owner AS table_schema,
                        table_name AS table_name,
                        partition_name AS partition_name,
                        partition_position AS ordinal_position
                    FROM ALL_TAB_PARTITIONS
                    WHERE {_schema_exclusion_clause("table_owner")}
                    ORDER BY table_owner, table_name, partition_position
                    """  # noqa: S608 -- schema exclusion list is a static hardcoded tuple, not user input
                await cursor.execute(partitions_query)
                partition_rows = []
                for row in _rows_as_dicts(cursor.description, await cursor.fetchall()):
                    schema_name = row["TABLE_SCHEMA"]
                    table_name = row["TABLE_NAME"]
                    partition_rows.append(
                        {
                            "table_schema": schema_name,
                            "table_name": table_name,
                            "partition_name": row["PARTITION_NAME"],
                            "ordinal_position": row["ORDINAL_POSITION"],
                            "partition_type": partition_types.get(
                                (schema_name, table_name), "UNKNOWN"
                            ),
                            "key_columns": partition_key_columns.get(
                                (schema_name, table_name), []
                            ),
                        }
                    )
        finally:
            await connection.close()

        return _assemble_catalog(
            catalog_name, column_rows, key_rows, foreign_key_rows, index_rows, partition_rows
        )

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> QueryEstimate:
        connection = await self._connect(timeout_seconds=timeout_seconds)
        statement_id = uuid.uuid4().hex[:28]
        try:
            async with connection.cursor() as cursor:
                try:
                    await cursor.execute(
                        f"EXPLAIN PLAN SET STATEMENT_ID = '{statement_id}' FOR {sql}"
                    )
                    await cursor.execute(
                        "SELECT cost, cardinality FROM plan_table "
                        "WHERE statement_id = :1 AND id = 0",
                        [statement_id],
                    )
                    row = await cursor.fetchone()
                    if row is None or row[0] is None:
                        raise RuntimeError(
                            "source returned an EXPLAIN PLAN without a total cost"
                        )
                    total_cost = float(row[0])
                    estimated_rows = float(row[1]) if row[1] is not None else None
                    return QueryEstimate(
                        score=total_cost,
                        kind="EXPLAIN_PLAN_COST",
                        estimated_rows=estimated_rows,
                        evidence={
                            "statement_id": statement_id,
                            "cost": total_cost,
                            "cardinality": estimated_rows,
                        },
                    )
                finally:
                    await cursor.execute(
                        "DELETE FROM plan_table WHERE statement_id = :1", [statement_id]
                    )
        finally:
            await connection.rollback()
            await connection.close()

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        connection = await self._connect(timeout_seconds=timeout_seconds)
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT SYS_CONTEXT('USERENV', 'SID') FROM DUAL")
                session_row = await cursor.fetchone()
                session_id = (
                    str(session_row[0]) if session_row is not None else uuid.uuid4().hex[:12]
                )
                await cursor.execute(sql)
                rows = _rows_as_dicts(cursor.description, await cursor.fetchall())
                return QueryResult(
                    rows=tuple(rows),
                    warehouse_query_id=f"oracle-sid:{session_id}",
                )
        finally:
            await connection.rollback()
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
        connection = await self._connect(timeout_seconds=timeout_seconds)
        snapshots: list[ColumnProfileSnapshot] = []
        sampled_row_count = 0
        estimate: int | None = None
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "SELECT num_rows FROM ALL_TABLES WHERE owner = :1 AND table_name = :2",
                    [schema_name, table_name],
                )
                estimate_row = await cursor.fetchone()
                if estimate_row is not None and estimate_row[0] is not None:
                    estimate = int(estimate_row[0])

                await cursor.execute(
                    "SELECT column_name, data_type FROM ALL_TAB_COLUMNS "
                    "WHERE owner = :1 AND table_name = :2",
                    [schema_name, table_name],
                )
                data_types = {
                    str(row[0]): str(row[1]) for row in await cursor.fetchall()
                }

                for start in range(0, len(column_names), column_batch_size):
                    batch = column_names[start : start + column_batch_size]
                    selected = ", ".join(_quote_identifier(name) for name in batch)
                    expressions = ["COUNT(*) AS sampled_row_count"]
                    for position, name in enumerate(batch):
                        quoted = _quote_identifier(name)
                        data_type = data_types.get(name, "")
                        expressions.extend(_profile_expressions(quoted, position, data_type))
                    profile_sql = (
                        f"WITH bounded_sample AS (SELECT {selected} FROM {qualified_table} "  # noqa: S608 -- identifiers are double-quoted and limits are validated integers
                        f"FETCH FIRST {int(sample_rows)} ROWS ONLY) "
                        f"SELECT {', '.join(expressions)} FROM bounded_sample"
                    )
                    await cursor.execute(profile_sql)
                    row = await cursor.fetchone()
                    if row is None:
                        continue
                    row_dict = _rows_as_dicts(cursor.description, [row])[0]
                    sampled_row_count = max(
                        sampled_row_count, int(row_dict["SAMPLED_ROW_COUNT"])
                    )
                    for position, name in enumerate(batch):
                        snapshots.append(
                            ColumnProfileSnapshot(
                                name=name,
                                null_count=int(row_dict[f"N_{position}"]),
                                non_null_count=int(row_dict[f"NN_{position}"]),
                                approximate_distinct_count=int(row_dict[f"D_{position}"]),
                                min_length=row_dict[f"MINL_{position}"],
                                max_length=row_dict[f"MAXL_{position}"],
                            )
                        )
        finally:
            await connection.rollback()
            await connection.close()
        return TableProfileSnapshot(
            row_count_estimate=(max(estimate, sampled_row_count) if estimate is not None else None),
            sampled_row_count=sampled_row_count,
            columns=tuple(snapshots),
        )


def _rows_as_dicts(description: Any, rows: list[Any]) -> list[dict[str, Any]]:
    columns = [column[0] for column in description]
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _assemble_catalog(
    catalog_name: str,
    column_rows: list[dict[str, Any]],
    key_rows: list[dict[str, Any]],
    foreign_key_rows: list[dict[str, Any]],
    index_rows: list[dict[str, Any]] | None = None,
    partition_rows: list[dict[str, Any]] | None = None,
) -> tuple[DiscoveredCatalog, ...]:
    """Assemble a catalog from already-normalized, lowercase-keyed discovery rows.

    Callers (namely ``discover()``) are responsible for translating Oracle's
    uppercase-folded column names into the lowercase keys the shared
    ``aida.connectors.discovery`` helpers expect; this function performs no
    case remapping itself so its contract matches every other caller of those
    helpers. ``index_rows`` and ``partition_rows`` are optional so existing
    callers that only care about columns and constraints are unaffected.
    """
    tables = build_table_map_from_column_rows(column_rows)
    append_grouped_key_rows(
        tables,
        key_rows,
        constraint_type_map={"P": "PRIMARY_KEY", "U": "UNIQUE"},
    )
    append_grouped_foreign_key_rows(tables, foreign_key_rows)
    if index_rows:
        append_grouped_index_rows(tables, index_rows)
    if partition_rows:
        append_partition_rows(tables, partition_rows)
    return assemble_catalog(str(catalog_name), tables)
