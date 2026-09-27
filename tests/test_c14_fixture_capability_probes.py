"""R11-C14: FIXTURE-tier capability evidence for Oracle, Snowflake, BigQuery and Databricks.

No instance of these four engines exists in this environment (tracker R11-B5), so the most
their capability flags can be certified at is the FIXTURE tier: *the connector's own code
works, driven through its own driver double*. That is real evidence about the connector's logic
(rows in, envelope out) and **none at all about the engine** -- a double cannot tell you that
BigQuery accepts the SQL, that Oracle grants a bank-scoped role the PLAN_TABLE write, or that a
plan format is what the engine really emits. The certification result labels every row it
produces here `FIXTURE` and `verify_result` refuses to let one be labelled `LIVE`.

**Where each cell's evidence comes from.** One of four things, decided per (connector, flag):

* `EXISTING_EVIDENCE` -- a test that already drives the connector's real public method through
  its driver double and asserts the flag's data. Named by pytest node id; the runner executes
  it, so it certifies only while it passes.
* `FIXTURE_PROBES` -- a probe in this file, written where the four connector-by-connector maps
  found no existing test that exercised the flag through the public method: BigQuery and
  Snowflake `explain`, every Oracle axis (its tests asserted positive per-axis data only on
  helpers, never through `discover()`), the approximate-statistics counts, Snowflake's
  delegated-identity pass-through, and the *absence* of the axes a connector does not claim.
* `NOT_APPLICABLE` -- the engine has no such object, taken from `aida.discovery_selection`'s
  own engine facts rather than restated here.
* `UNPROBED` -- not claimed, and no double can exercise "there is no such mechanism".

**A probe observes.** Like the live ones (`test_c14_live_capability_probes.py`), each returns
`WORKS` or `ABSENT`, and fails only when the connector answered wrongly. Whether that certifies
anything is decided later by the claim, never by the probe.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from aida.connectors.base import (
    ConnectorQueryHistoryUnsupported,
    ConnectorValueProfilingUnsupported,
    DiscoveredCatalog,
    DiscoveredSchema,
    DiscoveredTable,
    ProfileFacetStatus,
    TableProfileSnapshot,
)
from aida.connectors.bigquery import BigQueryConnector
from aida.connectors.capability_certification import CAPABILITY_FLAGS
from aida.connectors.databricks import DatabricksConnector
from aida.connectors.oracle import OracleConnector
from aida.connectors.snowflake import SnowflakeConnector
from aida.discovery_selection import _NO_SEQUENCE_KIND, _NO_TRIGGER_KIND
from tests.test_connectors_bigquery import _bigquery_discover
from tests.test_connectors_profile_facets import (
    _profile_bigquery,
    _profile_databricks,
    _profile_oracle,
    _profile_snowflake,
)
from tests.test_discovery_pushdown_warehouses import (
    _bigquery_scan,
    _databricks_scan,
    _oracle_scan,
    _snowflake_scan,
)

FIXTURE_CONNECTORS = ("oracle", "snowflake", "bigquery", "databricks")
FIXTURE_MODULE = "tests/test_c14_fixture_capability_probes.py"

WORKS = "WORKS"
ABSENT = "ABSENT"

_TESTS = Path(__file__).parent


@dataclass(frozen=True, slots=True)
class Observation:
    verdict: str
    evidence: str


@dataclass(frozen=True, slots=True)
class FixtureProbe:
    connector: str
    flag: str
    what: str
    run: Callable[[], Awaitable[Observation]]

    @property
    def node_id(self) -> str:
        return f"{FIXTURE_MODULE}::test_fixture_probe[{self.connector}-{self.flag}]"


@dataclass(frozen=True, slots=True)
class ExistingEvidence:
    """Existing tests that drive the real public method through a driver double."""

    what: str
    tests: tuple[str, ...]
    #: What a pass of those tests means. `ABSENT` when they assert that the connector
    #: deliberately returns nothing for the axis (BigQuery's declined grants).
    verdict: str = WORKS


FIXTURE_PROBES: dict[tuple[str, str], FixtureProbe] = {}


def fixture_probe(
    connector: str, flag: str, what: str
) -> Callable[[Callable[[], Awaitable[Observation]]], Callable[[], Awaitable[Observation]]]:
    def register(run: Callable[[], Awaitable[Observation]]) -> Callable[[], Awaitable[Observation]]:
        assert (connector, flag) not in FIXTURE_PROBES, f"duplicate probe {connector}.{flag}"
        FIXTURE_PROBES[(connector, flag)] = FixtureProbe(connector, flag, what, run)
        return run

    return register


def _works(evidence: str) -> Observation:
    return Observation(WORKS, evidence)


def _absent(evidence: str) -> Observation:
    return Observation(ABSENT, evidence)


def _schema(catalogs: tuple[DiscoveredCatalog, ...], name: str) -> DiscoveredSchema:
    for catalog in catalogs:
        for schema in catalog.schemas:
            if schema.name == name:
                return schema
    raise AssertionError(
        f"schema {name!r} missing: {[s.name for c in catalogs for s in c.schemas]}"
    )


def _table(schema: DiscoveredSchema, name: str) -> DiscoveredTable:
    for table in schema.tables:
        if table.name == name:
            return table
    raise AssertionError(f"table {name!r} missing: {[t.name for t in schema.tables]}")


def _all_tables(catalogs: tuple[DiscoveredCatalog, ...]) -> list[DiscoveredTable]:
    return [t for c in catalogs for s in c.schemas for t in s.tables]


def _entropy_status(statuses: tuple[ProfileFacetStatus, ...]) -> str | None:
    for status in statuses:
        if status.facet == "ENTROPY":
            return f"{status.status}/{status.reason_code}"
    return None


# --- absence probes that need no driver double ------------------------------------------------

_DSNS = {
    "oracle": "oracle://user:pass@host:1521/service",
    "snowflake": "snowflake://user:pass@account/db/schema?warehouse=wh",
    "bigquery": json.dumps(
        {"auth_method": "workload_identity", "project_id": "bank-warehouse", "location": "US"}
    ),
    "databricks": json.dumps(
        {
            "server_hostname": "dbc-test.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/test123",
            "access_token": "dapi_test_token",
        }
    ),
}
_CLASSES: dict[str, Any] = {
    "oracle": OracleConnector,
    "snowflake": SnowflakeConnector,
    "bigquery": BigQueryConnector,
    "databricks": DatabricksConnector,
}


def _value_range_probe(connector: str) -> None:
    @fixture_probe(
        connector,
        "value_range_profiling",
        "profile_column_values() is called to see whether the connector returns ranges and values",
    )
    async def probe() -> Observation:
        instance = _CLASSES[connector](_DSNS[connector])
        try:
            await instance.profile_column_values(
                "s", "t", ("c",), sample_rows=10, top_n=2, timeout_seconds=5
            )
        except ConnectorValueProfilingUnsupported:
            return _absent(
                "profile_column_values() fails closed with ConnectorValueProfilingUnsupported"
            )
        return _works("profile_column_values() returned value ranges")


def _query_history_absence_probe(connector: str) -> None:
    @fixture_probe(
        connector,
        "query_history",
        "get_query_history() is called to see whether the connector reads warehouse history",
    )
    async def probe() -> Observation:
        instance = _CLASSES[connector](_DSNS[connector])
        try:
            await instance.get_query_history(
                since=datetime.now(UTC) - timedelta(days=1), limit=1, timeout_seconds=5
            )
        except ConnectorQueryHistoryUnsupported:
            return _absent("get_query_history() fails closed with ConnectorQueryHistoryUnsupported")
        return _works("get_query_history() returned entries")


for _connector in FIXTURE_CONNECTORS:
    _value_range_probe(_connector)
for _connector in ("oracle", "databricks"):
    _query_history_absence_probe(_connector)


def _entropy_probe(
    connector: str, profile: Callable[[], Awaitable[tuple[TableProfileSnapshot, Any]]]
) -> None:
    @fixture_probe(
        connector,
        "distribution_entropy_profiling",
        "profile_table() is asked for a column's entropy through the driver double",
    )
    async def probe() -> Observation:
        snapshot, _cursor = await profile()
        column = snapshot.columns[0]
        if column.frequency_entropy_bits is not None:
            return _works(f"entropy {column.frequency_entropy_bits:.4f} bits")
        status = _entropy_status(column.facet_status)
        assert status is not None, "no entropy and no entropy status: an unexplained gap"
        return _absent(f"frequency_entropy_bits is None and the facet status is {status}")


_entropy_probe("oracle", lambda: _profile_oracle(sample_rows=1000, sampled=1000))
_entropy_probe("snowflake", lambda: _profile_snowflake(sample_rows=1000, row_count=1000))
_entropy_probe("bigquery", lambda: _profile_bigquery(sample_rows=1000, sampled=1000))
_entropy_probe("databricks", lambda: _profile_databricks(sample_rows=1000, sampled=1000))


def _approximate_statistics_probe(
    connector: str,
    profile: Callable[[], Awaitable[tuple[TableProfileSnapshot, Any]]],
    fragment: str,
) -> None:
    @fixture_probe(
        connector,
        "approximate_statistics",
        "profile_table() through the driver double returns null, non-null and distinct counts",
    )
    async def probe() -> Observation:
        snapshot, driver = await profile()
        column = snapshot.columns[0]
        # The shared `_answer` double reports 10 nulls, 990 non-nulls and 900 distinct values.
        assert (column.null_count, column.non_null_count) == (10, 990), column
        assert column.approximate_distinct_count == 900, column
        statements = [s.upper() for s in getattr(driver, "statements", [])]
        assert any(fragment in s for s in statements), f"no {fragment} in the statements sent"
        return _works(
            f"10 null, 990 non-null, 900 distinct read back from the aggregate; "
            f"the statement sent uses {fragment}"
        )


_approximate_statistics_probe(
    "oracle", lambda: _profile_oracle(sample_rows=1000, sampled=1000), "COUNT(DISTINCT"
)
_approximate_statistics_probe(
    "snowflake",
    lambda: _profile_snowflake(sample_rows=1000, row_count=5_000_000),
    "APPROX_COUNT_DISTINCT",
)
_approximate_statistics_probe(
    "bigquery", lambda: _profile_bigquery(sample_rows=1000, sampled=1000), "APPROX_COUNT_DISTINCT"
)


# --- Oracle: every axis, through discover() -----------------------------------------------------


@fixture_probe("oracle", "catalogs", "discover() returns the database as its catalog")
async def _oracle_catalogs() -> Observation:
    catalogs, _estate, _pushed = await _oracle_scan(None)
    assert [c.name for c in catalogs] == ["BANK"], [c.name for c in catalogs]
    return _works("discover() returned one catalog, named for the database")


@fixture_probe("oracle", "schemas", "discover() returns each owner as a schema, with its objects")
async def _oracle_schemas() -> Observation:
    catalogs, _estate, _pushed = await _oracle_scan(None)
    names = {s.name for c in catalogs for s in c.schemas}
    assert {"SALES", "SALES_TMP", "HR", "FIN"} <= names, names
    assert {t.name for t in _schema(catalogs, "SALES").tables} >= {"FACT_ORDERS", "DIM_CUSTOMER"}
    return _works(
        "SALES, SALES_TMP, HR and FIN came back; SALES holds FACT_ORDERS and DIM_CUSTOMER"
    )


@fixture_probe(
    "oracle", "constraints", "discover() returns PRIMARY KEY and FOREIGN KEY constraints"
)
async def _oracle_constraints() -> Observation:
    catalogs, _estate, _pushed = await _oracle_scan(None)
    table = _table(_schema(catalogs, "SALES"), "FACT_ORDERS")
    by_type = {c.constraint_type: c for c in table.constraints}
    assert set(by_type) == {"PRIMARY_KEY", "FOREIGN_KEY"}, table.constraints
    assert by_type["PRIMARY_KEY"].columns == ("ID",), by_type["PRIMARY_KEY"]
    assert by_type["FOREIGN_KEY"].referenced_table == "DIM_CUSTOMER", by_type["FOREIGN_KEY"]
    assert by_type["FOREIGN_KEY"].referenced_columns == ("ID",), by_type["FOREIGN_KEY"]
    return _works("FACT_ORDERS: PRIMARY_KEY(ID) and FOREIGN_KEY(ID) -> DIM_CUSTOMER(ID)")


@fixture_probe(
    "oracle", "indexes", "discover() returns the index rows with their columns and flags"
)
async def _oracle_indexes() -> Observation:
    catalogs, _estate, _pushed = await _oracle_scan(None)
    table = _table(_schema(catalogs, "SALES"), "FACT_ORDERS")
    assert [(i.name, i.columns, i.is_unique, i.is_primary) for i in table.indexes] == [
        ("IX_FACT_ORDERS", ("ID",), True, True)
    ], table.indexes
    return _works("FACT_ORDERS: IX_FACT_ORDERS on (ID), unique and primary")


@fixture_probe("oracle", "partitions", "discover() returns partition name, type and key columns")
async def _oracle_partitions() -> Observation:
    catalogs, _estate, _pushed = await _oracle_scan(None)
    table = _table(_schema(catalogs, "SALES"), "FACT_ORDERS")
    assert [(p.name, p.partition_type, p.key_columns) for p in table.partitions] == [
        ("P2026", "RANGE", ("ID",))
    ], table.partitions
    return _works("FACT_ORDERS: partition P2026, RANGE, key (ID); bounds are deliberately not read")


@fixture_probe("oracle", "views", "discover() returns view and materialized-view definition text")
async def _oracle_views() -> Observation:
    catalogs, _estate, _pushed = await _oracle_scan(None)
    sales = _schema(catalogs, "SALES")
    view = _table(sales, "V_REVENUE").view_definition
    mview = _table(sales, "FACT_ORDERS").view_definition
    assert view is not None and view.definition_sql == "SELECT 1" and not view.is_materialized
    assert mview is not None and mview.definition_sql == "SELECT 2" and mview.is_materialized
    return _works("V_REVENUE definition came back; the materialized view came back flagged")


@fixture_probe("oracle", "routines", "discover() returns procedures, functions and package members")
async def _oracle_routines() -> Observation:
    catalogs, _estate, _pushed = await _oracle_scan(None)
    sales = {r.name: r for r in _schema(catalogs, "SALES").routines}
    assert sales["PROC_LOAD"].routine_type == "PROCEDURE" and sales["PROC_LOAD"].body_sql
    assert sales["FN_TAX"].routine_type == "FUNCTION" and sales["FN_TAX"].return_type == "NUMBER"
    hr = {r.name: r for r in _schema(catalogs, "HR").routines}
    assert hr["RISK_PKG"].routine_type == "PACKAGE", hr["RISK_PKG"]
    assert {"SCORE", "RECALC"} <= set(hr), sorted(hr)
    return _works("PROC_LOAD, FN_TAX, package RISK_PKG and its members SCORE and RECALC came back")


@fixture_probe("oracle", "object_comments", "discover() returns table and column comments")
async def _oracle_comments() -> Observation:
    catalogs, _estate, _pushed = await _oracle_scan(None)
    table = _table(_schema(catalogs, "SALES"), "FACT_ORDERS")
    assert table.source_description == "FACT_ORDERS comment", table.source_description
    assert table.columns[0].source_description == "identifier", table.columns[0]
    return _works("FACT_ORDERS table comment and its first column's comment came back")


@fixture_probe(
    "oracle", "grants", "discover() returns object privileges with grantee and privilege"
)
async def _oracle_grants() -> Observation:
    catalogs, _estate, _pushed = await _oracle_scan(None)
    sales = [(g.grantee, g.privilege, g.object_name) for g in _schema(catalogs, "SALES").grants]
    hr = [(g.grantee, g.privilege, g.object_name) for g in _schema(catalogs, "HR").grants]
    assert sales == [("REPORTING", "SELECT", "FACT_ORDERS")], sales
    assert hr == [("REPORTING", "EXECUTE", "RISK_PKG")], hr
    return _works("SELECT on FACT_ORDERS and EXECUTE on RISK_PKG, both to REPORTING, came back")


@fixture_probe("oracle", "triggers", "discover() returns a trigger with table, timing and event")
async def _oracle_triggers() -> Observation:
    catalogs, _estate, _pushed = await _oracle_scan(None)
    triggers = _schema(catalogs, "SALES").triggers
    assert [(t.name, t.table_name, t.timing, t.orientation, t.events) for t in triggers] == [
        ("TRG_ORDERS_AUDIT", "FACT_ORDERS", "AFTER", "ROW", ("INSERT",))
    ], triggers
    assert triggers[0].body_sql, "the trigger body did not come back"
    return _works("TRG_ORDERS_AUDIT on FACT_ORDERS: AFTER ROW INSERT, body present")


@fixture_probe(
    "oracle", "sequences", "discover() returns a sequence declaration and never its position"
)
async def _oracle_sequences() -> Observation:
    catalogs, estate, _pushed = await _oracle_scan(None)
    sequences = _schema(catalogs, "SALES").sequences
    assert [(s.name, s.increment_by, s.cache_size, s.cycles) for s in sequences] == [
        ("SEQ_ORDERS", "1", "20", False)
    ], sequences
    assert not any("last_number" in sql.lower() for sql in estate.statements), (
        "a statement read the sequence's current position"
    )
    return _works("SEQ_ORDERS increment 1, cache 20; no statement read LAST_NUMBER")


class _PlanCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.binds: list[object] = []
        self._row: tuple[Any, ...] | None = None

    async def __aenter__(self) -> _PlanCursor:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)
        self.binds.append(params)
        if sql.startswith("SELECT cost"):
            self._row = (123.0, 4500)

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


@fixture_probe(
    "oracle",
    "explain",
    "estimate_read_query() runs EXPLAIN PLAN through a driver double and returns its cost",
)
async def _oracle_explain() -> Observation:
    connector = OracleConnector(_DSNS["oracle"])
    cursor = _PlanCursor()
    state = {"rolled_back": False, "closed": False}

    async def _rollback() -> None:
        state["rolled_back"] = True

    async def _close() -> None:
        state["closed"] = True

    connection = MagicMock()
    connection.cursor.return_value = cursor
    connection.rollback = _rollback
    connection.close = _close

    async def _connect(*, timeout_seconds: float) -> Any:
        return connection

    with patch.object(connector, "_connect", _connect):
        estimate = await connector.estimate_read_query("SELECT 1 FROM dual", timeout_seconds=30)
    assert (estimate.kind, estimate.score, estimate.estimated_rows) == (
        "EXPLAIN_PLAN_COST",
        123.0,
        4500.0,
    ), estimate
    assert any(s.startswith("EXPLAIN PLAN") for s in cursor.statements), cursor.statements
    assert any(s.startswith("DELETE FROM plan_table") for s in cursor.statements)
    # The double answers whatever it is asked, so check the question: the cost must be read
    # from the plan's root node (id = 0) of *this* statement's plan, and that plan must be
    # cleaned up by the same id. A wrong predicate or bind would otherwise read another
    # statement's plan (or none) and still pass.
    explain_sql = next(s for s in cursor.statements if s.startswith("EXPLAIN PLAN"))
    statement_id = explain_sql.split("STATEMENT_ID = '")[1].split("'")[0]
    select_at = next(i for i, s in enumerate(cursor.statements) if s.startswith("SELECT cost"))
    assert "statement_id = :1" in cursor.statements[select_at], cursor.statements[select_at]
    assert "id = 0" in cursor.statements[select_at], cursor.statements[select_at]
    assert cursor.binds[select_at] == [statement_id], cursor.binds[select_at]
    delete_at = next(i for i, s in enumerate(cursor.statements) if s.startswith("DELETE FROM"))
    assert cursor.binds[delete_at] == [statement_id], cursor.binds[delete_at]
    assert state == {"rolled_back": True, "closed": True}, state
    return _works(
        "EXPLAIN PLAN cost 123, rows 4500 read back; the PLAN_TABLE cleanup and rollback ran. "
        "Code path only: whether a least-privilege role may write PLAN_TABLE is unproven (R11-B5)"
    )


# --- Snowflake ----------------------------------------------------------------------------------


@fixture_probe(
    "snowflake",
    "explain",
    "estimate_read_query() runs EXPLAIN USING JSON through a driver double and parses the plan",
)
async def _snowflake_explain() -> Observation:
    connector = SnowflakeConnector(_DSNS["snowflake"])
    plan = json.dumps(
        {
            "GlobalStats": {
                "bytesAssigned": 10485760,
                "rowsTotal": 50000,
                "partitionsTotal": 100,
                "partitionsAssigned": 15,
            }
        }
    )
    cursor = MagicMock()
    cursor.fetchall.return_value = [(plan,)]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    with patch.object(connector, "_get_connection", return_value=connection):
        estimate = await connector.estimate_read_query("SELECT 1", timeout_seconds=30)
    sent = cursor.execute.call_args[0][0]
    assert sent == "EXPLAIN USING JSON SELECT 1", sent
    assert estimate.kind == "SNOWFLAKE_EXPLAIN_PLAN", estimate
    assert (estimate.estimated_rows, estimate.estimated_bytes) == (50000.0, 10485760), estimate
    assert estimate.evidence["pruning_ratio"] == 0.85, estimate.evidence
    return _works(
        "EXPLAIN USING JSON sent; plan parsed to 50000 rows, 10 MiB, 85% partition pruning. "
        "An unparseable plan falls back to score 1.0, the cheapest, which this does not exercise"
    )


@fixture_probe(
    "snowflake",
    "delegated_identity",
    "a DSN carrying authenticator and token reaches the driver connect() as such, with no password",
)
async def _snowflake_delegated_identity() -> Observation:
    connector = SnowflakeConnector(
        json.dumps(
            {
                "account": "acct",
                "user": "delegate@example.invalid",
                "authenticator": "oauth",
                "token": "opaque-token-value",
            }
        )
    )
    with patch("snowflake.connector.connect") as connect:
        connector._get_connection()  # noqa: SLF001 -- the driver call is the thing under test
    kwargs = connect.call_args.kwargs
    assert kwargs["authenticator"] == "oauth", kwargs.keys()
    assert kwargs["token"] == "opaque-token-value", kwargs.keys()  # noqa: S105
    assert "password" not in kwargs, kwargs.keys()
    return _works(
        "authenticator=oauth and the token reached connect(), and no password was sent. "
        "Pass-through only: the URI form of the DSN drops `token`, and nothing here supplies a "
        "per-user token"
    )


@fixture_probe("snowflake", "indexes", "discover() is asked for indexes across the whole estate")
async def _snowflake_indexes() -> Observation:
    catalogs, _estate, _pushed = await _snowflake_scan(None)
    tables = _all_tables(catalogs)
    assert tables, "the estate discovered no tables at all"
    if any(t.indexes for t in tables):
        return _works("discover() returned index rows")
    return _absent(
        f"discover() returned no index for any of {len(tables)} tables: no index read exists"
    )


@fixture_probe(
    "snowflake", "partitions", "discover() is asked for partitions across the whole estate"
)
async def _snowflake_partitions() -> Observation:
    catalogs, _estate, _pushed = await _snowflake_scan(None)
    tables = _all_tables(catalogs)
    assert tables, "the estate discovered no tables at all"
    if any(t.partitions for t in tables):
        return _works("discover() returned partition rows")
    return _absent(
        f"discover() returned no partition for any of {len(tables)} tables: the adapter has no "
        "partition read; only EXPLAIN's pruning counters mention partitions"
    )


# --- BigQuery -----------------------------------------------------------------------------------


@fixture_probe("bigquery", "catalogs", "discover() returns the project as its catalog")
async def _bigquery_catalogs() -> Observation:
    catalogs = await _bigquery_discover()
    assert catalogs[0].name == "bank-warehouse", [c.name for c in catalogs]
    return _works("discover() returned one catalog, named for the project")


@fixture_probe("bigquery", "schemas", "discover() returns each dataset as a schema with its tables")
async def _bigquery_schemas() -> Observation:
    catalogs = await _bigquery_discover()
    retail = _schema(catalogs, "retail")
    assert {t.name for t in retail.tables} >= {"customer", "active_customer"}, retail.tables
    return _works("dataset retail came back as a schema holding customer and active_customer")


@fixture_probe(
    "bigquery", "constraints", "discover() returns the primary-key constraint from the keys view"
)
async def _bigquery_constraints() -> Observation:
    catalogs = await _bigquery_discover(
        KEY_COLUMN_USAGE=[
            {
                "table_schema": "retail",
                "table_name": "customer",
                "constraint_name": "customer.pk$",
                "constraint_type": "PRIMARY KEY",
                "column_name": "customer_id",
                "ordinal_position": 1,
            }
        ]
    )
    table = _table(_schema(catalogs, "retail"), "customer")
    assert [(c.constraint_type, c.columns) for c in table.constraints] == [
        ("PRIMARY_KEY", ("customer_id",))
    ], table.constraints
    return _works(
        "customer: PRIMARY_KEY(customer_id) came back from the fixture's KEY_COLUMN_USAGE rows. "
        "Foreign keys are honestly omitted. Fixture only: whether the real view exposes the "
        "`constraint_type` column this query filters on is not something a double can show"
    )


@fixture_probe(
    "bigquery",
    "explain",
    "estimate_read_query() issues a dry-run job through a client double and returns the bytes",
)
async def _bigquery_explain() -> Observation:
    connector = BigQueryConnector(_DSNS["bigquery"])
    job = MagicMock()
    job.total_bytes_processed = 123456789
    client = MagicMock()
    client.query.return_value = job
    with patch.object(connector, "_get_client", return_value=client):
        estimate = await connector.estimate_read_query("SELECT 1", timeout_seconds=30)
    config = client.query.call_args.kwargs["job_config"]
    assert config.dry_run is True and config.use_query_cache is False, config
    assert client.query.call_args.kwargs["timeout"] == 30
    assert (estimate.kind, estimate.estimated_bytes, estimate.score) == (
        "BIGQUERY_DRY_RUN_BYTES",
        123456789,
        123456789.0,
    ), estimate
    return _works(
        "a dry-run job with the query cache off was issued; 123456789 bytes read back as the score"
    )


@fixture_probe("bigquery", "indexes", "discover() is asked for indexes across the whole estate")
async def _bigquery_indexes() -> Observation:
    catalogs, _estate, _pushed = await _bigquery_scan(None)
    tables = _all_tables(catalogs)
    assert tables, "the estate discovered no tables at all"
    if any(t.indexes for t in tables):
        return _works("discover() returned index rows")
    return _absent(
        f"discover() returned no index for any of {len(tables)} tables: no index read exists"
    )


@fixture_probe(
    "bigquery", "partitions", "discover() is asked for partitions across the whole estate"
)
async def _bigquery_partitions() -> Observation:
    catalogs, _estate, _pushed = await _bigquery_scan(None)
    tables = _all_tables(catalogs)
    assert tables, "the estate discovered no tables at all"
    if any(t.partitions for t in tables):
        return _works("discover() returned partition rows")
    return _absent(
        f"discover() returned no partition for any of {len(tables)} tables: no partition read"
    )


# --- Databricks ---------------------------------------------------------------------------------


@fixture_probe("databricks", "indexes", "discover() is asked for indexes across the whole estate")
async def _databricks_indexes() -> Observation:
    catalogs, _estate, _pushed = await _databricks_scan(None)
    tables = _all_tables(catalogs)
    assert tables, "the estate discovered no tables at all"
    if any(t.indexes for t in tables):
        return _works("discover() returned index rows")
    return _absent(
        f"discover() returned no index for any of {len(tables)} tables: no index read exists"
    )


@fixture_probe(
    "databricks", "partitions", "discover() is asked for partitions across the whole estate"
)
async def _databricks_partitions() -> Observation:
    catalogs, _estate, _pushed = await _databricks_scan(None)
    tables = _all_tables(catalogs)
    assert tables, "the estate discovered no tables at all"
    if any(t.partitions for t in tables):
        return _works("discover() returned partition rows")
    return _absent(
        f"discover() returned no partition for any of {len(tables)} tables: no partition read"
    )


@fixture_probe("databricks", "grants", "discover() is asked for grants across the whole estate")
async def _databricks_grants() -> Observation:
    catalogs, _estate, _pushed = await _databricks_scan(None)
    schemas = [s for c in catalogs for s in c.schemas]
    assert schemas, "the estate discovered no schemas at all"
    if any(s.grants for s in schemas):
        return _works("discover() returned grants")
    return _absent(
        f"discover() returned no grant for any of {len(schemas)} schemas: the grant axis is not "
        "implemented (Unity Catalog's privilege model is not the SQL grant model)"
    )


@fixture_probe("bigquery", "grants", "discover() is asked for grants across the whole estate")
async def _bigquery_grants() -> Observation:
    catalogs, _estate, _pushed = await _bigquery_scan(None)
    schemas = [s for c in catalogs for s in c.schemas]
    assert schemas, "the estate discovered no schemas at all"
    if any(s.grants for s in schemas):
        return _works("discover() returned grants")
    return _absent(
        f"discover() returned no grant for any of {len(schemas)} schemas: BigQuery grants are "
        "Cloud IAM bindings, which the SQL grant envelope does not model"
    )


# --- existing driver-double tests, by node id -----------------------------------------------------

_SF = "tests/test_connectors_snowflake.py"
_BQ = "tests/test_connectors_bigquery.py"
_DB = "tests/test_connectors_databricks.py"
_TS = "tests/test_connectors_triggers_and_sequences.py"

_DB_ASSEMBLY = f"{_DB}::test_databricks_discover_assembles_catalog_with_constraints"
_SF_ASSEMBLY = f"{_SF}::test_snowflake_discover_assembly"

EXISTING_EVIDENCE: dict[tuple[str, str], ExistingEvidence] = {
    # Databricks: every claimed flag already has a test that drives the public method.
    ("databricks", "catalogs"): ExistingEvidence(
        "discover() returns the configured catalog with its comment", (_DB_ASSEMBLY,)
    ),
    ("databricks", "schemas"): ExistingEvidence(
        "discover() returns the schema with its tables", (_DB_ASSEMBLY,)
    ),
    ("databricks", "constraints"): ExistingEvidence(
        "discover() returns PRIMARY KEY and FOREIGN KEY constraints from information_schema rows",
        (_DB_ASSEMBLY,),
    ),
    ("databricks", "object_comments"): ExistingEvidence(
        "discover() returns catalog, schema, table and column comments", (_DB_ASSEMBLY,)
    ),
    ("databricks", "explain"): ExistingEvidence(
        "estimate_read_query() runs EXPLAIN COST through a cursor double and parses its statistics",
        (f"{_DB}::test_databricks_estimate_read_query_uses_explain_cost",),
    ),
    ("databricks", "approximate_statistics"): ExistingEvidence(
        "profile_table() through a cursor double returns bounded counts and approximate distincts",
        (f"{_DB}::test_databricks_profile_table_computes_bounded_stats",),
    ),
    ("databricks", "views"): ExistingEvidence(
        "discover() returns a view's definition text from information_schema.views",
        (f"{_TS}::test_a_databricks_view_definition_round_trips",),
    ),
    ("databricks", "routines"): ExistingEvidence(
        "discover() returns a routine with its body and parameters",
        (f"{_TS}::test_a_databricks_routine_round_trips_with_its_parameters",),
    ),
    # Snowflake: the four flags whose existing tests drive discover() end to end.
    ("snowflake", "catalogs"): ExistingEvidence(
        "discover() returns the database as its catalog", (_SF_ASSEMBLY,)
    ),
    ("snowflake", "schemas"): ExistingEvidence(
        "discover() returns the schema with its tables and columns", (_SF_ASSEMBLY,)
    ),
    ("snowflake", "constraints"): ExistingEvidence(
        "discover() returns PRIMARY KEY and FOREIGN KEY constraints", (_SF_ASSEMBLY,)
    ),
    ("snowflake", "views"): ExistingEvidence(
        "discover() returns a view's definition text",
        (f"{_SF}::test_snowflake_discover_round_trips_a_view_definition",),
    ),
    ("snowflake", "routines"): ExistingEvidence(
        "discover() returns a routine with its body, return type and parsed parameters",
        (f"{_SF}::test_a_routine_round_trips_with_parameters_parsed_from_its_signature",),
    ),
    ("snowflake", "object_comments"): ExistingEvidence(
        "discover() returns catalog, schema, table and column comments",
        (f"{_SF}::test_comments_land_at_every_level_snowflake_exposes",),
    ),
    ("snowflake", "grants"): ExistingEvidence(
        "discover() returns schema-level grants",
        (f"{_SF}::test_schema_grants_land_on_the_schema",),
    ),
    ("snowflake", "sequences"): ExistingEvidence(
        "discover() returns a sequence's declaration and never its position",
        (f"{_SF}::test_a_snowflake_sequence_is_its_declaration_and_never_its_position",),
    ),
    ("snowflake", "query_history"): ExistingEvidence(
        "get_query_history() through a cursor double maps history rows to entries",
        (
            f"{_SF}::test_snowflake_get_query_history_maps_rows",
            f"{_SF}::test_snowflake_get_query_history_drops_incomplete_rows",
        ),
    ),
    # BigQuery.
    ("bigquery", "views"): ExistingEvidence(
        "discover() returns a view's definition text",
        (f"{_BQ}::test_bigquery_discover_round_trips_a_view_definition",),
    ),
    ("bigquery", "routines"): ExistingEvidence(
        "discover() returns a routine with its body and parameters",
        (f"{_BQ}::test_a_routine_round_trips_with_its_parameters_and_description",),
    ),
    ("bigquery", "object_comments"): ExistingEvidence(
        "discover() returns schema, table and column descriptions",
        (f"{_BQ}::test_descriptions_land_at_every_level_bigquery_exposes",),
    ),
    ("bigquery", "query_history"): ExistingEvidence(
        "get_query_history() through a client double maps job rows to entries",
        (
            f"{_BQ}::test_bigquery_get_query_history_maps_rows",
            f"{_BQ}::test_bigquery_get_query_history_drops_incomplete_rows",
        ),
    ),
}

#: Engine facts, from `aida.discovery_selection`, not restated: the engine has no such object.
NOT_APPLICABLE: dict[tuple[str, str], str] = {
    **{
        (c, "triggers"): (f"{c} has no trigger object (aida.discovery_selection._NO_TRIGGER_KIND)")
        for c in FIXTURE_CONNECTORS
        if c in _NO_TRIGGER_KIND
    },
    **{
        (c, "sequences"): (
            f"{c} has no sequence object (aida.discovery_selection._NO_SEQUENCE_KIND)"
        )
        for c in FIXTURE_CONNECTORS
        if c in _NO_SEQUENCE_KIND
    },
}

#: Not claimed, and no double can exercise "there is no such mechanism".
UNPROBED: dict[tuple[str, str], str] = {
    (c, "delegated_identity"): (
        f"not claimed; {c} authenticates with the DSN credential alone, so there is no "
        "delegated path for a double to exercise"
    )
    for c in ("oracle", "bigquery", "databricks")
}


def resolved_cells() -> dict[tuple[str, str], str]:
    """Which of the four sources covers each (connector, flag) cell."""
    cells: dict[tuple[str, str], str] = {}
    for label, source in (
        ("PROBE", FIXTURE_PROBES),
        ("EXISTING", EXISTING_EVIDENCE),
        ("NOT_APPLICABLE", NOT_APPLICABLE),
        ("UNPROBED", UNPROBED),
    ):
        for key in source:
            assert key not in cells, f"{key} is covered twice: {cells[key]} and {label}"
            cells[key] = label
    return cells


# --- CI-safe consistency tests --------------------------------------------------------------------


def test_every_fixture_cell_has_exactly_one_source_of_evidence() -> None:
    """The certification cannot silently skip a flag on a fixture-tier engine."""
    cells = resolved_cells()
    expected = {(c, f) for c in FIXTURE_CONNECTORS for f in CAPABILITY_FLAGS}
    assert sorted(expected - set(cells)) == [], "cells with no evidence source"
    assert sorted(set(cells) - expected) == [], "cells for connectors or flags that do not exist"


def test_existing_evidence_names_tests_that_exist() -> None:
    """A renamed or deleted test must fail here, not quietly stop certifying a flag."""
    missing: list[str] = []
    for evidence in EXISTING_EVIDENCE.values():
        for node_id in evidence.tests:
            path, _, name = node_id.partition("::")
            tree = ast.parse((_TESTS.parent / path).read_text(encoding="utf-8"))
            defined = {
                n.name
                for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
            }
            if name.split("[")[0] not in defined:
                missing.append(node_id)
    assert missing == [], f"existing tests that no longer exist: {missing}"


def test_not_applicable_is_never_a_flag_the_connector_claims() -> None:
    """NOT_APPLICABLE is an engine fact, so it can only sit on a flag that is declared False."""
    for connector, flag in NOT_APPLICABLE:
        claimed = getattr(_CLASSES[connector].DEFAULT_CAPABILITIES, flag)
        assert claimed is False, f"{connector} claims {flag} but the engine has no such object"


@pytest.mark.parametrize(
    ("connector", "flag"),
    sorted(FIXTURE_PROBES),
    ids=[f"{c}-{f}" for c, f in sorted(FIXTURE_PROBES)],
)
def test_fixture_probe(connector: str, flag: str, record_property: Any) -> None:
    """Run one fixture probe and record what it observed, for the certification runner."""
    the_probe = FIXTURE_PROBES[(connector, flag)]
    record_property("probe", the_probe.what)
    observation = asyncio.run(the_probe.run())
    assert observation.verdict in {WORKS, ABSENT}, observation
    record_property("verdict", observation.verdict)
    record_property("evidence", observation.evidence)
