"""R11-FP04: what each engine's `profile_table` claims about its own scan.

`Docs/review-2026-09-16/REVIEW.md` F06.2. Before this, exactly one of the six
connectors had *any* profiling unit test (`test_connectors_databricks.py`), and
the two connectors with a real observation-scope defect were both untested:

* BigQuery set `row_count_estimate = sampled_row_count`, so a `LIMIT 10000`
  profile of a ten-million-row table arrived downstream as a full scan;
* Snowflake issued no bound at all and reported
  `sampled_row_count = min(row_count, sample_rows)`, so a genuinely exhaustive
  profile arrived as sampled.

Both errors changed the strength of relationship-approval evidence
(`relationship_validation.ProfileBounds.scope` is what flags a join's
uniqueness as sample-bounded), in opposite directions, silently. Neither could
have been caught by a test that did not exist.

Every driver here is a fake that parses the aliases out of the SQL the
connector actually generated and answers them. That is deliberate: a fake with
a hand-written row shape passes forever after the connector stops asking for a
facet, because the alias it no longer requests is simply never read. Parsing
the generated SQL means a connector that drops an aggregate fails here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from aida.connectors.base import (
    FACET_NOT_APPLICABLE,
    FACET_UNSUPPORTED,
    LENGTH_BUCKET_BOUNDS,
    LENGTH_BUCKET_SCHEME,
    OBSERVATION_SCOPE_FULL,
    OBSERVATION_SCOPE_SAMPLE,
    OBSERVATION_SCOPE_UNKNOWN,
    OBSERVATION_SCOPES,
    PROFILE_FACET_ENTROPY,
    PROFILE_FACET_LENGTH,
    TableProfileSnapshot,
)
from aida.connectors.bigquery import BigQueryConnector
from aida.connectors.databricks import DatabricksConnector
from aida.connectors.oracle import OracleConnector
from aida.connectors.postgres import PostgresConnector
from aida.connectors.registry import connector_registry
from aida.connectors.snowflake import SnowflakeConnector
from aida.connectors.sqlserver import SqlServerConnector

# `asyncio_mode = "auto"` (pyproject.toml) collects the async tests here without
# a marker, which matters because this module deliberately mixes async
# driver-level tests with synchronous ones over the pure expression generators.

# A value-shaped sentinel. No generated profiling SQL may contain one, because
# nothing about profiling compares a column against a literal -- the moment one
# appears, the statement is selecting or filtering on a value (ADR-0014).
SENTINEL_VALUE = "ZZQ-SENTINEL-PROFILESQL-6c48"

_BQ_DSN = json.dumps(
    {
        "auth_method": "workload_identity",
        "project_id": "atlas-test-project",
        "location": "europe-west2",
    }
)
_DATABRICKS_DSN = json.dumps(
    {
        "server_hostname": "dbc-test.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/test123",
        "access_token": "dapi_test_token",
    }
)

#: Aliases the shared generators produce. Matched precisely rather than with a
#: loose `AS (\w+)`, which would also capture type names out of
#: `CAST(x AS INT64)` and `AS double precision`.
_ALIAS = re.compile(r"\bAS (sampled_row_count|[a-z]{1,4}_\d+(?:_\d+)?)\b")


def _aliases(sql: str) -> list[str]:
    return _ALIAS.findall(sql)


def _answer(
    aliases: list[str],
    *,
    sampled: int,
    nulls: int = 10,
    non_nulls: int = 990,
    distinct: int = 900,
) -> dict[str, Any]:
    """One aggregate row, answering exactly the aliases the connector asked for.

    Every bucket gets a distinct count so a test can tell an off-by-one in the
    positional alignment from a correct read -- `(0, 1, 2, 3, 4)` would pass a
    reversed read only if the list happened to be symmetric.
    """
    values: dict[str, Any] = {}
    for alias in aliases:
        if alias == "sampled_row_count":
            values[alias] = sampled
        elif alias.startswith("nn_"):
            values[alias] = non_nulls
        elif alias.startswith("n_"):
            values[alias] = nulls
        elif alias.startswith("d_"):
            values[alias] = distinct
        elif alias.startswith("minl_"):
            values[alias] = 3
        elif alias.startswith("maxl_"):
            values[alias] = 40
        elif alias.startswith("bl_"):
            values[alias] = 7
        elif alias.startswith("ws_"):
            values[alias] = 2
        elif alias.startswith("lb_"):
            values[alias] = 100 + int(alias.rsplit("_", 1)[1])
        elif alias.startswith("en_"):
            values[alias] = 6.5
        else:  # pragma: no cover -- a new alias family must be taught here
            raise AssertionError(f"the fake driver does not know how to answer {alias!r}")
    return values


# --- PostgreSQL --------------------------------------------------------------


class _NullAsyncContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePostgresConnection:
    def __init__(self, *, sampled: int, estimate: int | None) -> None:
        self._sampled = sampled
        self._estimate = estimate
        self.statements: list[str] = []

    def transaction(self, *, readonly: bool) -> _NullAsyncContext:
        assert readonly, "profiling must run inside a read-only transaction"
        return _NullAsyncContext()

    async def execute(self, sql: str) -> None:
        self.statements.append(sql)

    async def fetchval(self, sql: str, *args: object) -> int | None:
        self.statements.append(sql)
        return self._estimate

    async def fetchrow(self, sql: str) -> dict[str, Any]:
        self.statements.append(sql)
        return _answer(_aliases(sql), sampled=self._sampled)

    async def close(self) -> None:
        return None


async def _profile_postgres(
    *, sample_rows: int, sampled: int, estimate: int | None = 5_000_000
) -> tuple[TableProfileSnapshot, _FakePostgresConnection]:
    connector = PostgresConnector("postgresql://user:pass@host:5432/db")
    connection = _FakePostgresConnection(sampled=sampled, estimate=estimate)

    async def _connect(dsn: str, **kwargs: object) -> _FakePostgresConnection:
        return connection

    with patch("aida.connectors.postgres.asyncpg.connect", _connect):
        snapshot = await connector.profile_table(
            "analytics",
            "accounts",
            ("account_no",),
            sample_rows=sample_rows,
            column_batch_size=20,
            timeout_seconds=30,
        )
    return snapshot, connection


async def test_postgres_profile_reports_every_value_free_facet() -> None:
    snapshot, connection = await _profile_postgres(sample_rows=1000, sampled=1000)
    column = snapshot.columns[0]

    assert column.blank_count == 7
    assert column.whitespace_only_count == 2
    assert column.length_bucket_counts == tuple(
        100 + index for index in range(len(LENGTH_BUCKET_BOUNDS))
    )
    assert column.frequency_entropy_bits == pytest.approx(6.5)
    assert column.facet_status == (), "PostgreSQL computes every facet, so none is absent"
    profile_sql = [sql for sql in connection.statements if "bounded_sample" in sql]
    assert profile_sql, "no bounded profile statement was issued"


async def test_postgres_entropy_reads_the_same_bounded_sample_as_the_counts() -> None:
    """One statement, one CTE, one sample.

    A second statement would re-run an unordered `LIMIT` and silently mix two
    different samples into one profile row -- the counts describing one set of
    rows and the entropy another, with the profile claiming a single
    observation scope over both.
    """
    _, connection = await _profile_postgres(sample_rows=1000, sampled=1000)
    profile_statements = [sql for sql in connection.statements if "bounded_sample" in sql]

    assert len(profile_statements) == 1
    sql = profile_statements[0]
    assert "GROUP BY" in sql, "the entropy aggregate is missing"
    assert sql.count("WITH bounded_sample AS") == 1
    assert "LOG(2::numeric" in sql


async def test_postgres_profiling_sql_never_carries_a_value_literal() -> None:
    """The generated SQL compares nothing against anything.

    Profiling aggregates; it does not filter. A literal in this statement would
    mean the connector had started selecting on data -- which is the shape the
    value-bearing half of FP-04 has, behind a policy this one must not borrow.
    """
    _, connection = await _profile_postgres(sample_rows=1000, sampled=1000)
    for sql in connection.statements:
        assert SENTINEL_VALUE not in sql
        # The empty string is the one literal a value-free profile needs -- it is
        # the blank test, and it names no value. Removing those pairs must leave
        # no quote behind, because any other literal would be data.
        assert "'" not in sql.replace("''", ""), (
            f"a non-empty string literal appears in a profiling statement: {sql[:200]}"
        )


# --- SQL Server --------------------------------------------------------------


class _FakeDictCursor:
    """A `pytds`-style cursor returning dict rows, answering parsed aliases."""

    def __init__(self, *, sampled: int, estimate: int | None) -> None:
        self._sampled = sampled
        self._estimate = estimate
        self._pending: dict[str, Any] | None = None
        self.statements: list[str] = []

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)
        if "sys.partitions" in sql:
            self._pending = {"estimate": self._estimate}
        else:
            self._pending = _answer(_aliases(sql), sampled=self._sampled)

    def fetchone(self) -> dict[str, Any] | None:
        return self._pending

    def close(self) -> None:
        return None


async def _profile_sqlserver(
    *, sample_rows: int, sampled: int, estimate: int | None = 5_000_000
) -> tuple[TableProfileSnapshot, _FakeDictCursor]:
    connector = SqlServerConnector("mssql://user:pass@host:1433/db")
    cursor = _FakeDictCursor(sampled=sampled, estimate=estimate)
    connection = MagicMock()
    connection.cursor.return_value = cursor

    with patch.object(connector, "_connect", return_value=connection):
        snapshot = await connector.profile_table(
            "analytics",
            "accounts",
            ("account_no",),
            sample_rows=sample_rows,
            column_batch_size=20,
            timeout_seconds=30,
        )
    return snapshot, cursor


async def test_sqlserver_profile_reports_the_distribution_and_is_honest_about_entropy() -> None:
    snapshot, cursor = await _profile_sqlserver(sample_rows=1000, sampled=400)
    column = snapshot.columns[0]

    assert column.blank_count == 7
    assert column.length_bucket_counts is not None
    assert column.frequency_entropy_bits is None
    assert [status.facet for status in column.facet_status] == [PROFILE_FACET_ENTROPY]
    assert column.facet_status[0].status == FACET_UNSUPPORTED, (
        "a None entropy also means 'this column is entirely null'; the two must "
        "not be the same answer"
    )
    profile_sql = next(sql for sql in cursor.statements if "bounded_sample" in sql)
    assert "LTRIM(RTRIM(" in profile_sql, (
        "T-SQL LEN() ignores trailing spaces, so the whitespace-only test has to "
        "compare the trimmed text rather than its length"
    )


# --- Oracle ------------------------------------------------------------------


class _FakeOracleCursor:
    def __init__(self, *, sampled: int, estimate: int | None) -> None:
        self._sampled = sampled
        self._estimate = estimate
        self._mode = ""
        self._row: tuple[Any, ...] = ()
        self.description: tuple[tuple[str], ...] = ()
        self.statements: list[str] = []

    async def __aenter__(self) -> _FakeOracleCursor:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)
        if "ALL_TABLES" in sql:
            self._mode = "estimate"
        elif "ALL_TAB_COLUMNS" in sql:
            self._mode = "types"
        else:
            self._mode = "profile"
            aliases = _aliases(sql)
            answer = _answer(aliases, sampled=self._sampled)
            # Oracle folds unquoted aliases to upper case on the way back, which
            # is the exact behaviour `_upper_cased_reader` exists for.
            self.description = tuple((alias.upper(),) for alias in aliases)
            self._row = tuple(answer[alias] for alias in aliases)

    async def fetchone(self) -> tuple[Any, ...] | None:
        if self._mode == "estimate":
            return None if self._estimate is None else (self._estimate,)
        return self._row

    async def fetchall(self) -> list[tuple[Any, ...]]:
        if self._mode == "types":
            return [("ACCOUNT_NO", "VARCHAR2")]
        return []


async def _profile_oracle(
    *, sample_rows: int, sampled: int, estimate: int | None = 5_000_000
) -> tuple[TableProfileSnapshot, _FakeOracleCursor]:
    connector = OracleConnector("oracle://user:pass@host:1521/service")
    cursor = _FakeOracleCursor(sampled=sampled, estimate=estimate)
    connection = MagicMock()
    connection.cursor.return_value = cursor

    async def _close() -> None:
        return None

    connection.close = _close
    connection.rollback = _close

    async def _connect(*, timeout_seconds: float) -> Any:
        return connection

    with patch.object(connector, "_connect", _connect):
        snapshot = await connector.profile_table(
            "ANALYTICS",
            "ACCOUNTS",
            ("ACCOUNT_NO",),
            sample_rows=sample_rows,
            column_batch_size=20,
            timeout_seconds=30,
        )
    return snapshot, cursor


async def test_oracle_profile_reads_its_upper_cased_aliases_back() -> None:
    """The failure this guards is silent, not loud: the aliases are generated in
    lower case by a shared helper and returned in upper case by Oracle, so a
    read that did not fold would find nothing and report every distribution
    facet absent -- which looks exactly like an engine that cannot compute them.
    """
    snapshot, _cursor = await _profile_oracle(sample_rows=1000, sampled=1000)
    column = snapshot.columns[0]

    assert column.blank_count == 7
    assert column.whitespace_only_count == 2
    assert column.length_bucket_counts == tuple(
        100 + index for index in range(len(LENGTH_BUCKET_BOUNDS))
    )


async def test_oracle_marks_a_lob_columns_text_facets_not_applicable() -> None:
    """A LOB's absent length is not an unimplemented one: no credential and no
    retry makes `TO_CHAR` work on a CLOB, whereas entropy is simply not asked
    for on this engine yet. Those are different answers to "should I ask again".
    """
    from aida.connectors.oracle import _profile_facet_status

    lob = _profile_facet_status("CLOB")
    statuses = {status.facet: status for status in lob}

    assert statuses[PROFILE_FACET_LENGTH].status == FACET_NOT_APPLICABLE
    assert statuses[PROFILE_FACET_ENTROPY].status == FACET_UNSUPPORTED
    assert statuses[PROFILE_FACET_LENGTH].reason_code != statuses[PROFILE_FACET_ENTROPY].reason_code


# --- BigQuery ----------------------------------------------------------------


class _FakeBigQueryRow:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def items(self) -> Any:
        return self._values.items()

    def __getitem__(self, key: str) -> Any:
        return self._values[key]


class _FakeBigQueryClient:
    def __init__(self, *, sampled: int) -> None:
        self._sampled = sampled
        self.statements: list[str] = []

    def query(self, sql: str, job_config: object = None, timeout: object = None) -> Any:
        self.statements.append(sql)
        job = MagicMock()
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            job.result.return_value = [
                _FakeBigQueryRow(
                    {"column_name": "account_no", "data_type": "STRING", "is_nullable": "YES"}
                )
            ]
        else:
            job.result.return_value = [
                _FakeBigQueryRow(_answer(_aliases(sql), sampled=self._sampled))
            ]
        return job


async def _profile_bigquery(
    *, sample_rows: int, sampled: int
) -> tuple[TableProfileSnapshot, _FakeBigQueryClient]:
    connector = BigQueryConnector(_BQ_DSN)
    client = _FakeBigQueryClient(sampled=sampled)
    with patch.object(connector, "_get_client", return_value=client):
        snapshot = await connector.profile_table(
            "analytics",
            "accounts",
            ("account_no",),
            sample_rows=sample_rows,
            column_batch_size=20,
            timeout_seconds=30,
        )
    return snapshot, client


async def test_bigquery_stops_reporting_a_bounded_sample_as_the_table_size() -> None:
    """The R11-FP04 defect, stated as its consequence.

    `row_count_estimate = sampled_row_count` was not merely redundant: it made
    `sampled >= estimate` true for every profile, which is how the downstream
    scope derivation concluded FULL. Past the bound BigQuery was asked nothing
    about the table's size, so None is the only honest estimate.
    """
    snapshot, _client = await _profile_bigquery(sample_rows=1000, sampled=1000)

    assert snapshot.observation_scope == OBSERVATION_SCOPE_SAMPLE
    assert snapshot.row_count_estimate is None, (
        "the sample size is not an estimate of the table; reporting it as one is "
        "what made a bounded profile read as exhaustive"
    )
    assert snapshot.sampled_row_count == 1000


async def test_bigquery_reports_the_counted_rows_as_the_estimate_when_the_bound_never_bit() -> None:
    """The counterpart: when the `LIMIT` did not bite, the count *is* the table's
    size and withholding it would lose a fact BigQuery did establish.
    """
    snapshot, _client = await _profile_bigquery(sample_rows=1000, sampled=42)

    assert snapshot.observation_scope == OBSERVATION_SCOPE_FULL
    assert snapshot.row_count_estimate == 42


async def test_bigquery_withdraws_text_facets_for_a_repeated_column() -> None:
    from aida.connectors.bigquery import _profile_facet_status

    statuses = {status.facet: status for status in _profile_facet_status("STRING", "REPEATED")}
    assert statuses[PROFILE_FACET_LENGTH].status == FACET_NOT_APPLICABLE
    assert "REPEATED" in statuses[PROFILE_FACET_LENGTH].reason_code


# --- Snowflake ---------------------------------------------------------------


class _FakeSnowflakeCursor:
    def __init__(self, *, row_count: int) -> None:
        self._row_count = row_count
        self._mode = ""
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        self.statements.append(sql)
        self._mode = "count" if "COUNT(*) FROM" in sql else "stats"

    def fetchone(self) -> tuple[Any, ...]:
        if self._mode == "count":
            return (self._row_count,)
        return (10, 990, 900)

    def close(self) -> None:
        return None


async def _profile_snowflake(
    *, sample_rows: int, row_count: int
) -> tuple[TableProfileSnapshot, _FakeSnowflakeCursor]:
    connector = SnowflakeConnector("snowflake://user:pass@account/db/schema?warehouse=wh")
    cursor = _FakeSnowflakeCursor(row_count=row_count)
    connection = MagicMock()
    connection.cursor.return_value = cursor

    with patch.object(connector, "_get_connection", return_value=connection):
        snapshot = await connector.profile_table(
            "ANALYTICS", "ACCOUNTS", ("ACCOUNT_NO",), sample_rows=sample_rows
        )
    return snapshot, cursor


async def test_snowflake_stops_reporting_an_unbounded_scan_as_a_sample() -> None:
    """The other half of the R11-FP04 defect.

    This adapter issues no `LIMIT` anywhere, so every aggregate covers the whole
    table -- and it reported `sampled_row_count = min(row_count, sample_rows)`,
    which made a genuinely exhaustive profile arrive as sample-bounded and
    weakened join evidence that was in fact complete.
    """
    snapshot, cursor = await _profile_snowflake(sample_rows=1000, row_count=5_000_000)

    assert snapshot.observation_scope == OBSERVATION_SCOPE_FULL
    assert snapshot.sampled_row_count == 5_000_000
    assert snapshot.row_count_estimate == 5_000_000
    assert not any("LIMIT" in sql.upper() for sql in cursor.statements), (
        "this test's FULL claim rests on there being no bound in the SQL; if a "
        "bound is added, the scope must become bounded_scan_scope's answer"
    )


async def test_snowflake_says_which_facets_it_does_not_compute() -> None:
    """Its `min_length`/`max_length` were already None and could not be told
    apart from "this type has no text form" -- two facts with opposite
    implications for whether asking again would help. Snowflake can express
    every one of these, so the reason is that this adapter does not ask.
    """
    snapshot, _cursor = await _profile_snowflake(sample_rows=1000, row_count=100)
    column = snapshot.columns[0]

    assert column.min_length is None
    statuses = {status.facet: status for status in column.facet_status}
    assert statuses[PROFILE_FACET_LENGTH].status == FACET_UNSUPPORTED
    assert statuses[PROFILE_FACET_LENGTH].reason_code == "NOT_IMPLEMENTED"
    assert PROFILE_FACET_ENTROPY in statuses


# --- Databricks --------------------------------------------------------------


class _FakeDatabricksCursor:
    def __init__(self, *, sampled: int, row_count: int) -> None:
        self._sampled = sampled
        self._row_count = row_count
        self._rows: list[tuple[Any, ...]] = []
        self.description: tuple[tuple[str], ...] = ()
        self.statements: list[str] = []

    def execute(self, sql: str) -> None:
        self.statements.append(sql)
        if "bounded_sample" not in sql:
            self._rows = [(self._row_count,)]
            self.description = (("count",),)
            return
        aliases = _aliases(sql)
        answer = _answer(aliases, sampled=self._sampled)
        self.description = tuple((alias,) for alias in aliases)
        self._rows = [tuple(answer[alias] for alias in aliases)]

    def fetchone(self) -> tuple[Any, ...]:
        return self._rows[0]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def close(self) -> None:
        return None


async def _profile_databricks(
    *, sample_rows: int, sampled: int, row_count: int = 5_000_000
) -> tuple[TableProfileSnapshot, _FakeDatabricksCursor]:
    connector = DatabricksConnector(_DATABRICKS_DSN)
    cursor = _FakeDatabricksCursor(sampled=sampled, row_count=row_count)
    connection = MagicMock()
    connection.cursor.return_value = cursor

    with patch.object(connector, "_get_connection", return_value=connection):
        snapshot = await connector.profile_table(
            "analytics", "accounts", ("account_no",), sample_rows=sample_rows
        )
    return snapshot, cursor


async def test_databricks_keeps_its_real_row_count_and_adds_a_scope() -> None:
    """Its `row_count` comes from a real `SELECT COUNT(*)` over the whole table,
    so unlike BigQuery's it is a true estimate and stays. What changes is that
    the *profile's* scope is now decided by the bound, not by comparing the
    sample with that count.
    """
    snapshot, _cursor = await _profile_databricks(sample_rows=1000, sampled=1000)

    assert snapshot.row_count_estimate == 5_000_000
    assert snapshot.observation_scope == OBSERVATION_SCOPE_SAMPLE
    assert snapshot.columns[0].length_bucket_counts is not None


# --- the cross-engine invariant ---------------------------------------------

#: Every engine, how to drive its profile, and whether its scan carries a bound.
#: Written as data so the invariant below is one test rather than six copies,
#: and so a connector added to the registry without a profiling fake fails the
#: coverage tripwire instead of quietly dropping out.
_Profiler = Callable[..., Awaitable[tuple[TableProfileSnapshot, Any]]]
_ENGINES: dict[str, tuple[_Profiler, bool]] = {
    "postgres": (_profile_postgres, True),
    "sqlserver": (_profile_sqlserver, True),
    "oracle": (_profile_oracle, True),
    "bigquery": (_profile_bigquery, True),
    "databricks": (_profile_databricks, True),
    "snowflake": (_profile_snowflake, False),
}


def test_every_implemented_connector_has_a_profiling_fake() -> None:
    """Tripwire for the invariant below. A connector with no entry here is
    exactly the connector whose observation scope is most likely wrong -- both
    engines that had the defect had no profiling test at all.
    """
    implemented = {
        definition.connector_type
        for definition in connector_registry.definitions
        if definition.implementation_status == "IMPLEMENTED"
    }
    assert implemented - set(_ENGINES) == set(), (
        f"these connectors' profiling is untested: {sorted(implemented - set(_ENGINES))}"
    )
    assert len(_ENGINES) >= 6


@pytest.mark.parametrize("engine", sorted(_ENGINES))
async def test_a_bounded_profile_never_claims_to_have_seen_the_whole_table(engine: str) -> None:
    """R11-FP04, one direction of the rule, on every engine.

    A bound that filled up says nothing about what lies past it. An engine that
    issues no bound is exempt by construction and is asserted the other way
    round below.
    """
    profiler, bounded = _ENGINES[engine]
    if not bounded:
        pytest.skip(f"{engine} issues no row bound; see the unbounded assertion below")

    snapshot, _driver = await profiler(sample_rows=1000, sampled=1000)

    assert snapshot.observation_scope == OBSERVATION_SCOPE_SAMPLE
    assert snapshot.observation_scope != OBSERVATION_SCOPE_FULL


@pytest.mark.parametrize("engine", sorted(_ENGINES))
async def test_an_unbounded_profile_never_claims_to_have_seen_only_a_sample(engine: str) -> None:
    """The other direction. For a bounded engine this is the bound that never
    bit; for Snowflake, which issues none, it is every profile it ever writes.
    """
    profiler, bounded = _ENGINES[engine]
    if bounded:
        snapshot, _driver = await profiler(sample_rows=1000, sampled=42)
    else:
        snapshot, _driver = await profiler(sample_rows=1000, row_count=5_000_000)

    assert snapshot.observation_scope == OBSERVATION_SCOPE_FULL
    assert snapshot.observation_scope != OBSERVATION_SCOPE_SAMPLE


@pytest.mark.parametrize("engine", sorted(_ENGINES))
async def test_every_engine_reports_a_scope_from_the_shared_vocabulary(engine: str) -> None:
    """A scope outside `OBSERVATION_SCOPES` reaches `ProfileBounds.scope`, fails
    its membership test, and silently falls back to the derivation this task
    replaced -- so a typo here would restore the defect rather than fail.
    """
    profiler, bounded = _ENGINES[engine]
    kwargs: dict[str, Any] = (
        {"sample_rows": 1000, "sampled": 500}
        if bounded
        else {"sample_rows": 1000, "row_count": 500}
    )
    snapshot, _driver = await profiler(**kwargs)

    assert snapshot.observation_scope in OBSERVATION_SCOPES


@pytest.mark.parametrize("engine", sorted(_ENGINES))
async def test_no_engine_returns_a_value_a_bucket_edge_or_an_exemplar(engine: str) -> None:
    """ADR-0014 at the connector boundary.

    `ColumnProfileSnapshot` has no field that could hold a value -- that is what
    `tests/test_inv6_value_freedom.py`'s field-name ratchet enforces -- so this
    checks the other half: that the populated facets are numbers, and that the
    length distribution comes back as bare positional counts rather than as
    anything carrying a boundary.
    """
    profiler, bounded = _ENGINES[engine]
    kwargs: dict[str, Any] = (
        {"sample_rows": 1000, "sampled": 500}
        if bounded
        else {"sample_rows": 1000, "row_count": 500}
    )
    snapshot, _driver = await profiler(**kwargs)

    for column in snapshot.columns:
        if column.length_bucket_counts is not None:
            assert len(column.length_bucket_counts) == len(LENGTH_BUCKET_BOUNDS)
            assert all(isinstance(count, int) for count in column.length_bucket_counts)
        for facet in (column.blank_count, column.whitespace_only_count):
            assert facet is None or isinstance(facet, int)
        assert column.frequency_entropy_bits is None or isinstance(
            column.frequency_entropy_bits, float
        )


async def test_an_empty_column_list_makes_no_scope_claim() -> None:
    """Profiling nothing is not profiling everything. Each connector's early
    return has to leave the scope UNKNOWN rather than inherit whichever of
    FULL/SAMPLE the constructor's first positional argument happens to be.
    """
    connector = PostgresConnector("postgresql://user:pass@host:5432/db")
    snapshot = await connector.profile_table(
        "analytics", "accounts", (), sample_rows=1000, column_batch_size=20, timeout_seconds=30
    )
    assert snapshot.observation_scope == OBSERVATION_SCOPE_UNKNOWN
    assert LENGTH_BUCKET_SCHEME  # the scheme name is what the counts are aligned to
