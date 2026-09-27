"""R11-FP01 remainder: the selection pushed into Oracle, Snowflake, BigQuery and Databricks.

Before this, only PostgreSQL and SQL Server took a discovery selection into their own
metadata queries; the other four read their whole catalog and `apply_selection` dropped
what was out of scope afterwards. Each of the four now pushes the schema scope into every
read, and the `schema.object` patterns and object kinds into the reads whose rows belong
to one object (`aida.connectors.schema_scope`).

Two things must not change, and each engine is held to both here:

* **The result.** A pushed scan, after `apply_selection`, is exactly the unpushed scan
  after `apply_selection` -- same schemas, same objects, same children. Every engine is
  run twice against the same estate, once unscoped and once scoped, and compared.
* **Retirement safety.** A FULL run retires every existing schema it does not see, so a
  pushed scan may not lose a schema the unpushed one kept -- including one in scope whose
  every object is out of scope. That is why the inventories that establish a schema take
  the schema scope only, and `test_an_in_scope_schema_with_no_in_scope_object_is_still_seen`
  pins it on each engine.

**How the fakes answer.** Each engine's fake driver holds an estate per catalog relation
and answers a statement by *evaluating the scope predicates in it* against each row --
the `LIKE` patterns with their backslash escape, the kind lists, the `1 = 0` gate --
taking the values from the parameters the adapter bound, never from the statement text.
So a predicate on the wrong column, a parameter bound to the wrong placeholder, or a
pattern that narrows more than its glob would make the two scans differ. What a fake
cannot prove is each engine's own parser accepting the statement; no live Oracle,
Snowflake, BigQuery or Databricks instance exists here, and none is claimed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict
from typing import Any
from unittest.mock import patch

import pytest

from aida.connectors import bigquery, databricks, oracle, snowflake
from aida.connectors.base import Connector, DiscoveredCatalog
from aida.connectors.schema_scope import ScopeSql, discovery_scope
from aida.discovery_selection import DiscoverySelection, apply_selection

# ---------------------------------------------------------------------------
# Evaluating the predicates an adapter rendered, the way the source would.
# ---------------------------------------------------------------------------

_PLACEHOLDER = r"(?:\?|[:@]scope_\d+)"
_LIKE_TERM = re.compile(rf"LOWER\(([^()]*)\) LIKE ({_PLACEHOLDER})(?: ESCAPE '[^']*')?")
_CLAUSE = re.compile(
    rf" AND (?P<neg>NOT )?\((?P<likes>(?:LOWER\([^()]*\) LIKE {_PLACEHOLDER}"
    rf"(?: ESCAPE '[^']*')?(?: OR )?)+)\)"
    rf"| AND (?P<column>[\w.]+) IN \((?P<values>{_PLACEHOLDER}(?:, {_PLACEHOLDER})*)\)"
    rf"| AND (?P<never>1 = 0)"
)


def _like_matches(value: str, pattern: str) -> bool:
    """SQL `LIKE` with `\\` as the escape, implemented independently of the adapter."""
    regex: list[str] = []
    escaped = False
    for character in pattern:
        if escaped:
            regex.append(re.escape(character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "%":
            regex.append(".*")
        elif character == "_":
            regex.append(".")
        else:
            regex.append(re.escape(character))
    return re.fullmatch("".join(regex), value, re.DOTALL) is not None


class _Bindings:
    """The values an adapter bound, consumed in statement order for `?`."""

    def __init__(self, params: Any) -> None:
        self._named: Mapping[str, Any] = params if isinstance(params, Mapping) else {}
        self._positional: Iterator[Any] = iter(
            params if isinstance(params, list | tuple) else ()
        )

    def value(self, placeholder: str) -> Any:
        if placeholder == "?":
            return next(self._positional)
        return self._named[placeholder[1:]]


def _column(expression: str, sql: str, row: Mapping[str, Any]) -> str:
    """A column expression's value in `row`, through the statement's own `AS` aliases."""
    aliases = {
        source.lower(): alias.lower()
        for source, alias in re.findall(r"([\w.]+)\s+AS\s+(\w+)", sql, re.IGNORECASE)
    }
    key = aliases.get(expression.lower(), expression.split(".")[-1].lower())
    for name, value in row.items():
        if name.lower() == key:
            return str(value)
    raise KeyError(f"{expression} (as {key}) is not in the row: {sorted(row)}")


def _expression(expression: str, sql: str, row: Mapping[str, Any]) -> str:
    parts = expression.split(" || '.' || ")
    return ".".join(_column(part.strip(), sql, row) for part in parts)


def _admits(sql: str, params: Any, row: Mapping[str, Any]) -> bool:
    """Whether every scope clause in `sql` holds for `row`, with the bound values."""
    bindings = _Bindings(params)
    admitted = True
    for clause in _CLAUSE.finditer(sql):
        if clause.group("never"):
            admitted = False
        elif clause.group("likes") is not None:
            matched = False
            for expression, placeholder in _LIKE_TERM.findall(clause.group("likes")):
                pattern = bindings.value(placeholder)
                if _like_matches(_expression(expression, sql, row).lower(), pattern):
                    matched = True
            if matched == bool(clause.group("neg")):
                admitted = False
        else:
            values = {
                bindings.value(placeholder)
                for placeholder in re.findall(_PLACEHOLDER, clause.group("values"))
            }
            if _column(clause.group("column"), sql, row) not in values:
                admitted = False
    return admitted


class _Estate:
    """Rows per catalog relation, answered through `_admits`, with a read counter."""

    def __init__(self, relations: Sequence[tuple[str, list[dict[str, Any]]]]) -> None:
        self._relations = relations
        self.statements: list[str] = []
        self.rows_returned = 0

    def answer(self, sql: str, params: Any) -> list[dict[str, Any]]:
        self.statements.append(sql)
        for fragment, rows in self._relations:
            if fragment in sql:
                kept = [row for row in rows if _admits(sql, params, row)]
                self.rows_returned += len(kept)
                return kept
        return []


# ---------------------------------------------------------------------------
# One logical estate, rendered per engine.
#
#   SALES      FACT_ORDERS, DIM_CUSTOMER (tables), V_REVENUE (view),
#              PROC_LOAD (procedure), FN_TAX (function), constraints, comments, grants
#   SALES_TMP  TMP_STAGE (table)
#   HR         EMP (table); on Oracle also the package RISK_PKG and its members
#   FIN        LEDGER (table), V_LEDGER (view) -- no routine at all, so a selection of
#              routine kinds leaves it in scope with nothing in it
# ---------------------------------------------------------------------------

_TABLES = [
    ("SALES", "FACT_ORDERS", "TABLE", ["ID", "AMOUNT"]),
    ("SALES", "DIM_CUSTOMER", "TABLE", ["ID", "NAME"]),
    ("SALES", "V_REVENUE", "VIEW", ["ID", "TOTAL"]),
    ("SALES_TMP", "TMP_STAGE", "TABLE", ["ID"]),
    ("HR", "EMP", "TABLE", ["ID", "SALARY"]),
    ("FIN", "LEDGER", "TABLE", ["ID"]),
    ("FIN", "V_LEDGER", "VIEW", ["ID"]),
]
_ROUTINES = [
    ("SALES", "PROC_LOAD", "PROCEDURE"),
    ("SALES", "FN_TAX", "FUNCTION"),
]


def _column_rows(
    table_type: Callable[[str], str], *, upper: bool, extra: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    rows = []
    for schema, name, kind, columns in _TABLES:
        for position, column in enumerate(columns, start=1):
            row = {
                "table_schema": schema,
                "table_name": name,
                "table_type": table_type(kind),
                "column_name": column,
                "ordinal_position": position,
                "data_type": "NUMBER",
                "is_nullable": "NO",
                "column_default": None,
                **(extra or {}),
            }
            rows.append({key.upper(): value for key, value in row.items()} if upper else row)
    return rows


def _key_rows(*, upper: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keys = [
        {
            "table_schema": schema,
            "table_name": table,
            "constraint_name": f"PK_{table}",
            "constraint_type": "P" if upper else "PRIMARY KEY",
            "column_name": "ID",
            "ordinal_position": 1,
        }
        for schema, table, kind, _ in _TABLES
        if kind == "TABLE"
    ]
    foreign = [
        {
            "table_schema": "SALES",
            "table_name": "FACT_ORDERS",
            "constraint_name": "FK_ORDERS_CUSTOMER",
            "referenced_schema": "SALES",
            "referenced_table": "DIM_CUSTOMER",
            "column_name": "ID",
            "referenced_column": "ID",
            "ordinal_position": 1,
        }
    ]
    if upper:
        return (
            [{k.upper(): v for k, v in row.items()} for row in keys],
            [{k.upper(): v for k, v in row.items()} for row in foreign],
        )
    return keys, foreign


def _oracle_estate() -> _Estate:
    keys, foreign = _key_rows(upper=True)
    return _Estate(
        [
            (
                "FROM ALL_TAB_COLUMNS atc",
                _column_rows(lambda kind: "BASE TABLE" if kind == "TABLE" else kind, upper=True),
            ),
            ("ac.constraint_type IN ('P', 'U')", keys),
            ("ac.constraint_type = 'R'", foreign),
            (
                "FROM ALL_INDEXES ai",
                [
                    {
                        "TABLE_SCHEMA": schema,
                        "TABLE_NAME": table,
                        "INDEX_NAME": f"IX_{table}",
                        "INDEX_TYPE": "NORMAL",
                        "UNIQUENESS": "UNIQUE",
                        "COLUMN_NAME": "ID",
                        "ORDINAL_POSITION": 1,
                        "BACKING_CONSTRAINT_TYPE": "P",
                    }
                    for schema, table, kind, _ in _TABLES
                    if kind == "TABLE"
                ],
            ),
            (
                "FROM ALL_PART_TABLES",
                [{"TABLE_SCHEMA": "SALES", "TABLE_NAME": "FACT_ORDERS", "PARTITION_TYPE": "RANGE"}],
            ),
            (
                "FROM ALL_PART_KEY_COLUMNS",
                [
                    {
                        "TABLE_SCHEMA": "SALES",
                        "TABLE_NAME": "FACT_ORDERS",
                        "COLUMN_NAME": "ID",
                        "ORDINAL_POSITION": 1,
                    }
                ],
            ),
            (
                "FROM ALL_TAB_PARTITIONS",
                [
                    {
                        "TABLE_SCHEMA": "SALES",
                        "TABLE_NAME": "FACT_ORDERS",
                        "PARTITION_NAME": "P2026",
                        "ORDINAL_POSITION": 1,
                    }
                ],
            ),
            (
                "FROM ALL_VIEWS",
                [
                    {"OWNER": schema, "VIEW_NAME": name, "TEXT_LENGTH": 8, "TEXT": "SELECT 1"}
                    for schema, name, kind, _ in _TABLES
                    if kind == "VIEW"
                ],
            ),
            (
                "FROM ALL_MVIEWS",
                [
                    {"OWNER": "SALES", "MVIEW_NAME": "FACT_ORDERS", "QUERY_LEN": 8,
                     "QUERY": "SELECT 2"}
                ],
            ),
            (
                "LEFT JOIN ALL_PROCEDURES ap",
                [
                    {
                        "OWNER": schema,
                        "OBJECT_NAME": name,
                        "OBJECT_TYPE": kind,
                        "DETERMINISTIC": "NO",
                        "AUTHID": "DEFINER",
                    }
                    for schema, name, kind in [*_ROUTINES, ("HR", "RISK_PKG", "PACKAGE")]
                ],
            ),
            (
                "FROM ALL_SOURCE",
                [
                    {"OWNER": "SALES", "NAME": "PROC_LOAD", "TYPE": "PROCEDURE", "LINE": 1,
                     "TEXT": "PROCEDURE PROC_LOAD IS BEGIN NULL; END;"},
                    {"OWNER": "SALES", "NAME": "FN_TAX", "TYPE": "FUNCTION", "LINE": 1,
                     "TEXT": "FUNCTION FN_TAX RETURN NUMBER IS BEGIN RETURN 1; END;"},
                    {"OWNER": "HR", "NAME": "RISK_PKG", "TYPE": "PACKAGE", "LINE": 1,
                     "TEXT": "PACKAGE RISK_PKG IS FUNCTION SCORE RETURN NUMBER; END;"},
                    {"OWNER": "HR", "NAME": "RISK_PKG", "TYPE": "PACKAGE BODY", "LINE": 1,
                     "TEXT": "PACKAGE BODY RISK_PKG IS FUNCTION SCORE RETURN NUMBER IS "
                     "BEGIN RETURN 1; END; END;"},
                ],
            ),
            (
                "FROM ALL_ARGUMENTS",
                [
                    {"OWNER": "SALES", "OBJECT_NAME": "FN_TAX", "PACKAGE_NAME": None,
                     "SUBPROGRAM_ID": 1, "ARGUMENT_NAME": None, "POSITION": 0,
                     "DATA_TYPE": "NUMBER", "IN_OUT": "OUT"},
                    {"OWNER": "HR", "OBJECT_NAME": "SCORE", "PACKAGE_NAME": "RISK_PKG",
                     "SUBPROGRAM_ID": 1, "ARGUMENT_NAME": None, "POSITION": 0,
                     "DATA_TYPE": "NUMBER", "IN_OUT": "OUT"},
                ],
            ),
            (
                "procedure_name IS NOT NULL",
                [
                    {"OWNER": "HR", "OBJECT_NAME": "RISK_PKG", "PROCEDURE_NAME": "SCORE",
                     "SUBPROGRAM_ID": 1, "OVERLOAD": None},
                    {"OWNER": "HR", "OBJECT_NAME": "RISK_PKG", "PROCEDURE_NAME": "RECALC",
                     "SUBPROGRAM_ID": 2, "OVERLOAD": None},
                ],
            ),
            (
                "FROM ALL_TRIGGERS",
                [
                    {"OWNER": "SALES", "TRIGGER_NAME": "TRG_ORDERS_AUDIT", "TABLE_OWNER": "SALES",
                     "TABLE_NAME": "FACT_ORDERS", "TRIGGER_TYPE": "AFTER EACH ROW",
                     "TRIGGERING_EVENT": "INSERT", "STATUS": "ENABLED",
                     "BASE_OBJECT_TYPE": "TABLE", "TRIGGER_BODY": "BEGIN NULL; END;"}
                ],
            ),
            (
                "FROM ALL_SEQUENCES",
                [
                    {"SEQUENCE_OWNER": "SALES", "SEQUENCE_NAME": "SEQ_ORDERS", "MIN_VALUE": "1",
                     "MAX_VALUE": "999", "INCREMENT_BY": "1", "CYCLE_FLAG": "N",
                     "CACHE_SIZE": "20"}
                ],
            ),
            (
                "FROM ALL_TAB_COMMENTS",
                [
                    {"OWNER": schema, "TABLE_NAME": name, "COMMENTS": f"{name} comment"}
                    for schema, name, _kind, _ in _TABLES
                ],
            ),
            (
                "FROM ALL_COL_COMMENTS",
                [
                    {"OWNER": schema, "TABLE_NAME": name, "COLUMN_NAME": "ID",
                     "COMMENTS": "identifier"}
                    for schema, name, _kind, _ in _TABLES
                ],
            ),
            (
                "FROM ALL_TAB_PRIVS p",
                [
                    {"GRANTEE": "REPORTING", "TABLE_SCHEMA": "SALES", "TABLE_NAME": "FACT_ORDERS",
                     "PRIVILEGE": "SELECT", "GRANTABLE": "NO", "OBJECT_TYPE": "TABLE",
                     "GRANTEE_TYPE": "ROLE"},
                    {"GRANTEE": "REPORTING", "TABLE_SCHEMA": "HR", "TABLE_NAME": "RISK_PKG",
                     "PRIVILEGE": "EXECUTE", "GRANTABLE": "NO", "OBJECT_TYPE": "PACKAGE",
                     "GRANTEE_TYPE": "ROLE"},
                ],
            ),
        ]
    )


class _OracleCursor:
    def __init__(self, estate: _Estate) -> None:
        self._estate = estate
        self.description: Any = ()
        self._rows: list[tuple[Any, ...]] = []

    async def execute(self, sql: str, params: Any = None) -> None:
        if "SYS_CONTEXT" in sql:
            self.description, self._rows = (("CATALOG_NAME",),), [("BANK",)]
            return
        rows = self._estate.answer(sql, params)
        names = list(rows[0]) if rows else []
        self.description = tuple((name,) for name in names)
        self._rows = [tuple(row[name] for name in names) for row in rows]

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    async def __aenter__(self) -> _OracleCursor:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _OracleConnection:
    call_timeout = 0
    autocommit = False

    def __init__(self, cursor: _OracleCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _OracleCursor:
        return self._cursor

    async def close(self) -> None:
        return None


async def _oracle_scan(selection: DiscoverySelection | None) -> tuple[Any, _Estate, bool]:
    estate = _oracle_estate()
    connector = oracle.OracleConnector("oracle://u:p@h:1521/BANK")
    pushed = _scope(connector, selection)

    async def _connect(**_kwargs: Any) -> _OracleConnection:
        return _OracleConnection(_OracleCursor(estate))

    with patch.object(oracle.oracledb, "connect_async", _connect):
        return await connector.discover(), estate, pushed


# --- Snowflake ------------------------------------------------------------------------


class _SyncCursor:
    """A DB-API cursor over an estate; dict rows, as Snowflake's and Databricks' are used."""

    description: Any = None

    def __init__(self, answer: Callable[[str, Any], list[dict[str, Any]]]) -> None:
        self._answer = answer
        self._rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self._rows = self._answer(sql, params)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        return None


class _SyncConnection:
    def __init__(self, cursor: _SyncCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _SyncCursor:
        return self._cursor

    def close(self) -> None:
        return None


def _snowflake_estate() -> _Estate:
    keys, foreign = _key_rows(upper=False)
    materialized = ("SALES", "MV_DAILY", "MATERIALIZED VIEW", ["DAY"])
    columns = _column_rows(
        lambda kind: "BASE TABLE" if kind == "TABLE" else kind,
        upper=False,
        extra={"table_comment": None, "column_comment": None},
    ) + [
        {
            "table_schema": materialized[0],
            "table_name": materialized[1],
            "table_type": materialized[2],
            "column_name": "DAY",
            "ordinal_position": 1,
            "data_type": "DATE",
            "is_nullable": "NO",
            "column_default": None,
            "table_comment": None,
            "column_comment": None,
        }
    ]
    routine = {
        "routine_language": "SQL",
        "routine_definition": "SELECT 1",
        "argument_signature": "()",
        "data_type": "NUMBER",
        "is_secure": "NO",
        "comment": None,
    }
    return _Estate(
        [
            ("FROM information_schema.columns c", columns),
            ("tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')", keys),
            ("tc.constraint_type = 'FOREIGN KEY'", foreign),
            ("FROM information_schema.databases", [{"database_name": "BANK", "comment": None}]),
            (
                "FROM information_schema.schemata",
                [{"schema_name": s, "comment": f"{s} schema"} for s in ("SALES", "HR", "FIN")],
            ),
            (
                "FROM information_schema.views",
                [
                    {"table_schema": schema, "table_name": name, "view_definition": "SELECT 1",
                     "is_secure": "NO", "is_updatable": "NO", "check_option": "NONE"}
                    for schema, name, kind, _ in _TABLES
                    if kind == "VIEW"
                ],
            ),
            (
                "FROM information_schema.functions",
                [{"routine_schema": "SALES", "routine_name": "FN_TAX",
                  "routine_type": "FUNCTION", **routine}],
            ),
            (
                "FROM information_schema.procedures",
                [{"routine_schema": "SALES", "routine_name": "PROC_LOAD",
                  "routine_type": "PROCEDURE", **routine}],
            ),
            (
                "FROM information_schema.sequences",
                [{"sequence_schema": "SALES", "sequence_name": "SEQ_ORDERS",
                  "data_type": "NUMBER", "start_with": "1", "increment_by": "1",
                  "comment": None}],
            ),
        ]
    )


def _snowflake_answer(estate: _Estate) -> Callable[[str, Any], list[dict[str, Any]]]:
    def answer(sql: str, params: Any) -> list[dict[str, Any]]:
        ddl = re.search(r"GET_DDL\('VIEW', '([^']+)', TRUE\)", sql)
        if ddl:
            estate.statements.append(sql)
            estate.rows_returned += 1
            return [{"view_definition": f"create view {ddl.group(1)} as select 1"}]
        grants = re.search(r'SHOW GRANTS ON SCHEMA "[^"]+"\."([^"]+)"', sql)
        if grants:
            estate.statements.append(sql)
            estate.rows_returned += 2
            schema = grants.group(1)
            return [
                {"privilege": "USAGE", "granted_on": "SCHEMA", "name": f"BANK.{schema}",
                 "granted_to": "ROLE", "grantee_name": "ANALYST", "grant_option": "false"},
                {"privilege": "SELECT", "granted_on": "TABLE",
                 "name": f"BANK.{schema}.FACT_ORDERS", "granted_to": "ROLE",
                 "grantee_name": "ANALYST", "grant_option": "false"},
            ]
        return estate.answer(sql, params)

    return answer


async def _snowflake_scan(selection: DiscoverySelection | None) -> tuple[Any, _Estate, bool]:
    estate = _snowflake_estate()
    connector = snowflake.SnowflakeConnector("snowflake://u:p@acc/BANK/SALES")
    pushed = _scope(connector, selection)
    connection = _SyncConnection(_SyncCursor(_snowflake_answer(estate)))
    styles: list[str | None] = []

    def _get_connection(*, paramstyle: str | None = None) -> _SyncConnection:
        styles.append(paramstyle)
        return connection

    with patch.object(connector, "_get_connection", _get_connection):
        catalogs = await connector.discover()
    # Values are bound server-side, never interpolated by the client.
    assert styles == (["qmark"] if pushed else [None])
    return catalogs, estate, pushed


# --- BigQuery -------------------------------------------------------------------------


class _BigQueryRow:
    def __init__(self, row: Mapping[str, Any]) -> None:
        self._row = row

    def items(self) -> Any:
        return self._row.items()


class _BigQueryJob:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def result(self) -> list[_BigQueryRow]:
        return [_BigQueryRow(row) for row in self._rows]


class _BigQueryClient:
    def __init__(self, estate: _Estate) -> None:
        self._estate = estate

    def query(self, sql: str, job_config: Any = None, **_kwargs: Any) -> _BigQueryJob:
        params = {
            parameter.name: parameter.value
            for parameter in (getattr(job_config, "query_parameters", None) or [])
        }
        view = re.search(r"INFORMATION_SCHEMA\.([A-Z_]+)", sql)
        key = view.group(1) if view else ""
        # Dispatch on the exact view name, so COLUMNS never answers COLUMN_FIELD_PATHS.
        return _BigQueryJob(self._estate.answer(f"\x00{key}\x00" + sql, params))


def _bigquery_estate() -> _Estate:
    keys, _ = _key_rows(upper=False)
    lower = [
        {
            **row,
            "table_schema": row["table_schema"].lower(),
            "table_name": row["table_name"].lower(),
        }
        for row in _column_rows(lambda _kind: "BASE TABLE", upper=False)
    ]
    types = {
        (schema.lower(), name.lower()): ("VIEW" if kind == "VIEW" else "BASE TABLE")
        for schema, name, kind, _ in _TABLES
    }
    return _Estate(
        [
            ("\x00COLUMNS\x00", lower),
            (
                "\x00KEY_COLUMN_USAGE\x00",
                [
                    {**row, "table_schema": row["table_schema"].lower(),
                     "table_name": row["table_name"].lower()}
                    for row in keys
                ],
            ),
            (
                "\x00TABLES\x00",
                [
                    {"table_schema": schema, "table_name": name, "table_type": kind, "ddl": None}
                    for (schema, name), kind in types.items()
                ],
            ),
            (
                "\x00VIEWS\x00",
                [
                    {"table_schema": schema, "table_name": name,
                     "view_definition": "SELECT 1", "check_option": None}
                    for (schema, name), kind in types.items()
                    if kind == "VIEW"
                ],
            ),
            (
                "\x00ROUTINES\x00",
                [
                    {"routine_schema": schema.lower(), "routine_name": name.lower(),
                     "routine_type": kind, "data_type": "INT64", "routine_body": "SQL",
                     "routine_definition": "SELECT 1", "external_language": None,
                     "is_deterministic": None, "security_type": None}
                    for schema, name, kind in _ROUTINES
                ],
            ),
            (
                "\x00PARAMETERS\x00",
                [
                    {"specific_schema": "sales", "specific_name": "fn_tax",
                     "ordinal_position": 1, "parameter_mode": "IN", "is_result": "NO",
                     "parameter_name": "amount", "data_type": "INT64",
                     "parameter_default": None}
                ],
            ),
            (
                "\x00ROUTINE_OPTIONS\x00",
                [
                    {"routine_schema": "sales", "routine_name": "fn_tax",
                     "option_name": "description", "option_value": '"tax"'}
                ],
            ),
            (
                "\x00TABLE_OPTIONS\x00",
                [
                    {"table_schema": schema, "table_name": name,
                     "option_name": "description", "option_value": '"described"'}
                    for (schema, name) in types
                ],
            ),
            (
                "\x00SCHEMATA_OPTIONS\x00",
                [
                    {"schema_name": schema, "option_name": "description",
                     "option_value": '"dataset"'}
                    for schema in ("sales", "hr", "fin")
                ],
            ),
            (
                "\x00COLUMN_FIELD_PATHS\x00",
                [
                    {"table_schema": schema, "table_name": name, "column_name": "id",
                     "field_path": "id", "description": "identifier"}
                    for (schema, name) in types
                ],
            ),
        ]
    )


async def _bigquery_scan(selection: DiscoverySelection | None) -> tuple[Any, _Estate, bool]:
    estate = _bigquery_estate()
    connector = bigquery.BigQueryConnector(
        '{"auth_method": "workload_identity", "project_id": "bank-warehouse", "location": "US"}'
    )
    pushed = _scope(connector, selection)
    with patch.object(connector, "_get_client", lambda: _BigQueryClient(estate)):
        return await connector.discover(), estate, pushed


# --- Databricks -----------------------------------------------------------------------


def _databricks_estate() -> _Estate:
    keys, foreign = _key_rows(upper=False)
    lower = [
        {
            **row,
            "table_schema": row["table_schema"].lower(),
            "table_name": row["table_name"].lower(),
        }
        for row in _column_rows(
            lambda kind: "VIEW" if kind == "VIEW" else "MANAGED",
            upper=False,
            extra={"table_comment": None, "column_comment": None},
        )
    ]

    def _lowered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {**row, "table_schema": row["table_schema"].lower(),
             "table_name": row["table_name"].lower()}
            for row in rows
        ]

    return _Estate(
        [
            (".information_schema.columns c", lower),
            ("constraint_type IN ('PRIMARY KEY', 'UNIQUE')", _lowered(keys)),
            ("constraint_type = 'FOREIGN KEY'", _lowered(foreign)),
            (
                ".information_schema.schemata",
                [{"schema_name": s, "comment": f"{s} schema"} for s in ("sales", "hr", "fin")],
            ),
            (".information_schema.catalogs", [{"catalog_name": "main", "comment": "main"}]),
            (
                ".information_schema.views",
                [
                    {"table_schema": schema.lower(), "table_name": name.lower(),
                     "view_definition": "SELECT 1", "is_updatable": "NO",
                     "check_option": "NONE"}
                    for schema, name, kind, _ in _TABLES
                    if kind == "VIEW"
                ],
            ),
            (
                ".information_schema.routines",
                [
                    {"routine_schema": schema.lower(), "routine_name": name.lower(),
                     "specific_name": name.lower(), "routine_type": kind,
                     "data_type": "INT", "routine_body": "SQL",
                     "routine_definition": "SELECT 1", "external_language": None,
                     "is_deterministic": "YES", "security_type": "DEFINER",
                     "comment": None}
                    for schema, name, kind in _ROUTINES
                ],
            ),
            (
                ".information_schema.parameters",
                [
                    {"specific_schema": "sales", "specific_name": "fn_tax",
                     "ordinal_position": 1, "parameter_mode": "IN", "is_result": "NO",
                     "parameter_name": "amount", "data_type": "INT",
                     "parameter_default": None}
                ],
            ),
        ]
    )


async def _databricks_scan(selection: DiscoverySelection | None) -> tuple[Any, _Estate, bool]:
    estate = _databricks_estate()
    connector = databricks.DatabricksConnector(
        '{"server_hostname": "dbc.cloud.databricks.com", '
        '"http_path": "/sql/1.0/warehouses/abc", "access_token": "t", "catalog": "main"}'
    )
    pushed = _scope(connector, selection)
    connection = _SyncConnection(_SyncCursor(estate.answer))
    with patch.object(connector, "_get_connection", lambda: connection):
        return await connector.discover(), estate, pushed


# ---------------------------------------------------------------------------
# The comparison.
# ---------------------------------------------------------------------------


def _scope(connector: Connector, selection: DiscoverySelection | None) -> bool:
    if selection is None:
        return False
    return connector.scope_discovery(
        include_schemas=list(selection.include_schemas),
        exclude_schemas=list(selection.exclude_schemas),
        object_kinds=list(selection.object_kinds),
        include_objects=list(selection.include_objects),
        exclude_objects=list(selection.exclude_objects),
    )


def _canonical(value: Any) -> Any:
    """Order-free form of a catalog: every list sorted by its members' own canonical form."""
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return value


def _kept(catalogs: tuple[DiscoveredCatalog, ...], selection: DiscoverySelection) -> Any:
    kept = apply_selection(catalogs, selection).catalogs
    return _canonical([asdict(catalog) for catalog in kept])


SCANS = {
    "oracle": _oracle_scan,
    "snowflake": _snowflake_scan,
    "bigquery": _bigquery_scan,
    "databricks": _databricks_scan,
}
#: Whether an engine pushes object kinds anywhere. Databricks does not: its `views`
#: relation has no type column to tell a view from a materialized view, and the roster
#: establishes schemas -- so a kinds-only selection is reported as nothing pushed there.
PUSHES_KINDS = {"oracle": True, "snowflake": True, "bigquery": True, "databricks": False}

SELECTIONS: dict[str, dict[str, Any]] = {
    "include schemas": {"include_schemas": ["sales*"]},
    "exclude schema": {"exclude_schemas": ["sales_tmp"]},
    "include objects": {"include_objects": ["sales.fact_*", "hr.*"]},
    "exclude objects": {"exclude_objects": ["sales.v_*", "hr.risk_pkg", "fin.ledger"]},
    "view kind": {"object_kinds": ["VIEW"]},
    "routine kinds": {"object_kinds": ["PROCEDURE", "FUNCTION"]},
    "package kind": {"object_kinds": ["PACKAGE"]},
    "everything at once": {
        "object_kinds": ["TABLE", "PROCEDURE", "VIEW"],
        "include_schemas": ["sales", "hr", "fin"],
        "exclude_objects": ["sales.dim_*"],
    },
    "literal underscore and percent": {
        "include_objects": ["sales.fact_orders", "sales.v_revenue", "fin.v%"],
    },
    "untranslatable globs": {
        "include_objects": ["sales.fact_[ab]*"],
        "exclude_objects": ["sales.v_[r]*"],
    },
    "injection-shaped pattern": {"include_objects": ["sales.x' OR '1'='1", "sales.fact_*"]},
}


@pytest.mark.parametrize("engine", sorted(SCANS))
@pytest.mark.parametrize("name", sorted(SELECTIONS))
async def test_a_pushed_scan_returns_exactly_what_the_unpushed_scan_returns(
    engine: str, name: str
) -> None:
    selection = DiscoverySelection.model_validate(SELECTIONS[name])
    unpushed, full_estate, _ = await SCANS[engine](None)
    pushed, scoped_estate, was_pushed = await SCANS[engine](selection)

    assert _kept(pushed, selection) == _kept(unpushed, selection)

    translatable = discovery_scope(
        include_schemas=selection.include_schemas,
        exclude_schemas=selection.exclude_schemas,
        object_kinds=selection.object_kinds,
        include_objects=selection.include_objects,
        exclude_objects=selection.exclude_objects,
    )
    assert was_pushed is translatable.narrows(kinds=PUSHES_KINDS[engine])
    if not was_pushed:
        # Nothing expressible to push: the same statements, the same rows.
        assert scoped_estate.rows_returned == full_estate.rows_returned


@pytest.mark.parametrize("engine", sorted(SCANS))
@pytest.mark.parametrize("name", ["include schemas", "exclude schema", "include objects"])
async def test_a_pushed_scan_reads_less_than_the_unpushed_scan(engine: str, name: str) -> None:
    """The point of the whole exercise: the source is asked for less."""
    selection = DiscoverySelection.model_validate(SELECTIONS[name])
    _, full_estate, _ = await SCANS[engine](None)
    _, scoped_estate, was_pushed = await SCANS[engine](selection)

    assert was_pushed
    assert scoped_estate.rows_returned < full_estate.rows_returned


@pytest.mark.parametrize("engine", [e for e, pushes in PUSHES_KINDS.items() if pushes])
async def test_a_kinds_only_selection_reads_less_where_the_engine_pushes_kinds(
    engine: str,
) -> None:
    """TABLE only: no view text is read (Oracle ALL_VIEWS, Snowflake and BigQuery VIEWS),
    and on Oracle no routine source either -- while the roster, which establishes
    schemas, is read in full."""
    selection = DiscoverySelection.model_validate({"object_kinds": ["TABLE"]})
    _, full_estate, _ = await SCANS[engine](None)
    _, scoped_estate, was_pushed = await SCANS[engine](selection)

    assert was_pushed
    assert scoped_estate.rows_returned < full_estate.rows_returned


async def test_databricks_reports_a_kinds_only_selection_as_not_pushed() -> None:
    """`selection_pushed_down` may not claim a push that did not happen."""
    selection = DiscoverySelection.model_validate({"object_kinds": ["TABLE"]})
    _, full_estate, _ = await _databricks_scan(None)
    _, scoped_estate, was_pushed = await _databricks_scan(selection)

    assert was_pushed is False
    assert scoped_estate.rows_returned == full_estate.rows_returned


@pytest.mark.parametrize("engine", sorted(SCANS))
async def test_no_pattern_value_ever_reaches_the_statement_text(engine: str) -> None:
    """The injection surface: every operator-supplied value is a bound parameter."""
    selection = DiscoverySelection.model_validate(
        {
            "include_schemas": ["sales'; DROP TABLE x; --", "sales*"],
            "exclude_schemas": ["tmp' OR '1'='1"],
            "include_objects": ["sales.fact_*", "sales.\"q\"--"],
            "exclude_objects": ["sales.v_) OR (1=1"],
        }
    )
    _, estate, was_pushed = await SCANS[engine](selection)

    assert was_pushed
    statements = "\n".join(estate.statements)
    for fragment in ("DROP TABLE", "'1'='1", '"q"', "1=1", "fact\\_", "sales%"):
        assert fragment not in statements, fragment


@pytest.mark.parametrize("engine", sorted(SCANS))
async def test_an_in_scope_schema_with_no_in_scope_object_is_still_seen(engine: str) -> None:
    """The retirement hazard, named. FIN holds a table and a view and no routine; a
    selection of routine kinds keeps FIN in scope with nothing in it. A FULL run retires
    every schema it did not see, so the pushed scan must still return FIN -- which is
    why the roster and the other schema-establishing inventories take the schema scope
    only. Pushing the kinds into the roster would drop FIN here, and tombstone it."""
    selection = DiscoverySelection.model_validate(
        {"object_kinds": ["PROCEDURE", "FUNCTION"], "exclude_objects": ["sales.tmp_*"]}
    )
    pushed, _, was_pushed = await SCANS[engine](selection)

    assert was_pushed
    kept = apply_selection(pushed, selection).catalogs
    fin = [s for c in kept for s in c.schemas if s.name.lower() == "fin"]
    assert fin, "an in-scope schema vanished from the pushed scan"
    assert fin[0].tables == ()


def test_each_engine_binds_in_its_own_syntax() -> None:
    """The escape and placeholder spellings are engine facts no fake can check, so they
    are pinned here against what each engine documents (`schema_scope._LIKE_ESCAPE`)."""
    scope = discovery_scope(include_schemas=["s_1"], exclude_schemas=[])
    rendered = {dialect: ScopeSql(scope, dialect) for dialect in SCANS}  # type: ignore[arg-type]
    assert rendered["oracle"].schema("owner") == " AND (LOWER(owner) LIKE :scope_0 ESCAPE '\\')"
    assert rendered["snowflake"].schema("s") == " AND (LOWER(s) LIKE ? ESCAPE '\\\\')"
    assert rendered["bigquery"].schema("s") == " AND (LOWER(s) LIKE @scope_0)"
    assert rendered["databricks"].schema("s") == " AND (LOWER(s) LIKE :scope_0)"
    assert rendered["oracle"].named == {"scope_0": "s\\_1"}
    assert rendered["snowflake"].positional == ["s\\_1"]
