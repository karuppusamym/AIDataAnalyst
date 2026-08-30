"""QG-1: adversarial SQL corpus per dialect -- exit condition is zero bypasses.

The corpus itself lives as structured, versioned data under
`tests/fixtures/adversarial_sql_corpus/<dialect>.json` -- one file per
certified dialect (postgres, tsql, oracle, snowflake, bigquery), each a list
of cases with an id, a category, the adversarial SQL text, a human
description, and the `SqlValidationReport` finding code(s) (from
`aida.sql_validation`) that proves *why* it was caught.

Every case is driven through the real pipeline -- `QueryExecutionGateway.
validate()`, which calls the same private `_run_validation` that `execute()`
calls (ST-16, N14) -- rather than a parallel checker, so this suite is
actually exercising the code path an agent's SQL goes through, not a
reimplementation of it that could quietly drift from the real one.

"Zero bypasses" is asserted, not hoped for: any case the pipeline accepts
fails the test loudly, by name, with the SQL that got through. There is no
skip path and no `xfail` for a known-bad case -- a case that cannot yet be
made to fail gets the underlying guard fixed (see `aida.sql_guard`), not
quietly dropped from the corpus.

Follows the fakes-only convention of `tests/test_sql_validation.py`: no live
infrastructure, a fake session answering the catalog reads, and a fake
executor whose `execute_read_query` raises unconditionally -- proof, for
every single corpus case, that a rejected statement never reaches a source
(INV-2), reused here as an extra guarantee: `estimated` stays empty too,
because every case here is refused before the dry-run cost estimate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Select

from aida.config import Settings
from aida.connectors.base import ConnectorCapabilities, QueryEstimate, QueryResult
from aida.models import DataSource
from aida.query_gateway import QueryExecutionGateway
from aida.security import SecurityContext

CORPUS_DIR = Path(__file__).parent / "fixtures" / "adversarial_sql_corpus"

#: The catalog binding every corpus case is validated against: exactly one
#: authorized table. Any corpus SQL referencing a *different* table (
#: `fin.employee`, `fin.gl_entries`, ...) is deliberately unauthorized, so
#: catalog resolution is what is expected to catch it.
CATALOG = "core"
SCHEMA = "retail"
TABLE = "customer"
COLUMNS = ("customer_id", "state_code", "email_address", "opened_at")

#: `DataSource.connector_type` for each certified dialect (module 02
#: connectivity's registry -- see `aida.connectors.registry`). The dialect
#: string is what `SqlGuard`/sqlglot key off of; `connector_type` only
#: selects which connector class *would* be opened, and none of the corpus
#: cases below ever reach that point.
CONNECTOR_TYPE_BY_DIALECT = {
    "postgres": "postgres",
    "oracle": "oracle",
    "tsql": "sqlserver",
    "bigquery": "bigquery",
    "snowflake": "snowflake",
}


def _load_corpus() -> list[tuple[str, dict[str, Any]]]:
    """Every (dialect, case) pair across every dialect fixture file."""
    pairs: list[tuple[str, dict[str, Any]]] = []
    for dialect in sorted(CONNECTOR_TYPE_BY_DIALECT):
        path = CORPUS_DIR / f"{dialect}.json"
        payload = json.loads(path.read_text())
        assert payload["dialect"] == dialect, f"{path} dialect field does not match filename"
        for case in payload["cases"]:
            pairs.append((dialect, case))
    return pairs


CORPUS = _load_corpus()
CORPUS_IDS = [f"{dialect}:{case['id']}" for dialect, case in CORPUS]


# --- fakes (same shape as tests/test_sql_validation.py) --------------------


class _FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeSession:
    """Answers the catalog reads and the authorization-gate binding lookup.

    Binds exactly one authorized table (`retail.customer`), matching
    `CATALOG`/`SCHEMA`/`TABLE`/`COLUMNS` above, regardless of dialect --
    dialect only changes how the SQL text parses, not what the fixture
    catalog contains.
    """

    def __init__(self) -> None:
        self._tables = [(CATALOG, SCHEMA, TABLE)]
        self._columns = [(CATALOG, SCHEMA, TABLE, column) for column in COLUMNS]
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Select[Any]) -> _FakeResult:
        if len(statement.column_descriptions) == 4:
            return _FakeResult(list(self._columns))
        return _FakeResult(list(self._tables))

    async def scalars(self, _statement: Select[Any]) -> _FakeResult:
        return _FakeResult([])

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _FakeExecutor:
    """A SQL executor that costs a statement and refuses to run one.

    `execute_read_query` raising unconditionally is the INV-2 assertion: if
    any corpus case's validation ever reached execution, this whole suite
    would fail loudly at that call rather than passing quietly.
    """

    connector_type = "postgres"
    dialect = "postgres"

    def __init__(self) -> None:
        self.estimated: list[str] = []

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(explain=True)

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> QueryEstimate:
        self.estimated.append(sql)
        return QueryEstimate(score=1.0, kind="FAKE_PLAN", estimated_rows=1.0)

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        raise AssertionError(
            "a corpus case reached execute_read_query -- INV-2 and QG-1 are both "
            "violated: validation must reject every adversarial case before the "
            "source is ever touched"
        )


class _FakeSecretResolver:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, reference: str) -> str:
        return "postgresql://fake/db"


def _settings() -> Settings:
    return Settings(
        default_query_row_limit=5_000,
        hard_query_row_limit=100_000,
        _env_file=None,
    )


def _datasource(dialect: str) -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        project_id=uuid4(),
        name=f"Governed warehouse ({dialect})",
        connector_type=CONNECTOR_TYPE_BY_DIALECT[dialect],
        dialect=dialect,
        environment="PROD",
        network_zone="restricted",
        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
        status="ACTIVE",
        max_concurrency=2,
        capabilities={},
    )


def _context(datasource: DataSource) -> SecurityContext:
    return SecurityContext(
        principal_id="agent-adversarial-corpus",
        principal_type="AGENT",
        organization_id=datasource.organization_id,
        roles=frozenset({"AgentDeveloper"}),
    )


async def _run(
    monkeypatch: pytest.MonkeyPatch, dialect: str, sql: str
) -> tuple[Any, _FakeExecutor]:
    executor = _FakeExecutor()
    monkeypatch.setattr("aida.query_gateway.SecretResolver", _FakeSecretResolver)
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: executor,
    )
    datasource = _datasource(dialect)
    gateway = QueryExecutionGateway(_settings())
    report = await gateway.validate(
        _FakeSession(),  # type: ignore[arg-type]
        datasource=datasource,
        context=_context(datasource),
        correlation_id="corr-adversarial-corpus",
        sql=sql,
        requested_limit=None,
    )
    return report, executor


# --- corpus integrity --------------------------------------------------


def test_every_certified_dialect_has_a_corpus_file() -> None:
    from aida.connectors.registry import connector_registry

    certified_dialects = {
        definition.dialect
        for definition in connector_registry.definitions
        if definition.dialect in CONNECTOR_TYPE_BY_DIALECT
    }
    assert certified_dialects == set(CONNECTOR_TYPE_BY_DIALECT)


def test_corpus_case_ids_are_unique_within_and_across_dialects() -> None:
    assert len(CORPUS_IDS) == len(set(CORPUS_IDS))


def test_corpus_has_meaningful_coverage_per_dialect() -> None:
    """A guard against the corpus quietly shrinking back to nothing."""
    counts: dict[str, int] = {}
    for dialect, _case in CORPUS:
        counts[dialect] = counts.get(dialect, 0) + 1
    for dialect in CONNECTOR_TYPE_BY_DIALECT:
        assert counts.get(dialect, 0) >= 10, f"{dialect} corpus has too few cases: {counts}"


def test_every_corpus_case_declares_at_least_one_expected_finding_code() -> None:
    for dialect, case in CORPUS:
        assert case["expected_codes"], f"{dialect}:{case['id']} has no expected_codes"


# --- zero bypasses -----------------------------------------------------


@pytest.mark.parametrize(("dialect", "case"), CORPUS, ids=CORPUS_IDS)
async def test_adversarial_case_is_rejected(
    dialect: str, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    report, executor = await _run(monkeypatch, dialect, case["sql"])

    assert not report.valid, (
        f"BYPASS: {dialect}:{case['id']} was accepted by the gateway's validation "
        f"pipeline -- {case['description']}\nsql: {case['sql']}"
    )
    codes = report.codes()
    assert any(code in codes for code in case["expected_codes"]), (
        f"{dialect}:{case['id']} was rejected, but not for the expected reason: "
        f"got {codes}, expected one of {case['expected_codes']}"
    )
    # Every corpus case is a guard- or catalog-level rejection, so none of
    # them should ever reach the dry-run cost estimate -- let alone execution.
    assert executor.estimated == [], (
        f"{dialect}:{case['id']} reached the connector's cost estimate; a "
        "corpus case should be refused before any source is contacted"
    )


# --- the fix must not overreach: legitimate queries still validate ---------


@pytest.mark.parametrize("dialect", sorted(CONNECTOR_TYPE_BY_DIALECT))
async def test_legitimate_query_is_still_accepted_per_dialect(
    dialect: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The corpus-driven fixes in `SqlGuard` must not reject ordinary SQL.

    Same shape as every corpus case's benign counterpart: one authorized
    table, explicit columns, a real join predicate. If this regresses, a
    QG-1 fix has gone from "closes a bypass" to "breaks the gateway".
    """
    report, _ = await _run(
        monkeypatch,
        dialect,
        "SELECT c.customer_id, c.state_code FROM retail.customer AS c "
        "WHERE c.state_code = 'NY'",
    )

    assert report.valid, report.codes()
