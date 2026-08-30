"""`validate_sql`: the gateway's deterministic pipeline, without execution (N14).

Follows the suite's existing convention -- no live infrastructure, direct calls
against fakes (`tests/test_high_stakes_behaviors.py`), pure assertions on
value-free output (`tests/test_query_masking.py`, `tests/test_sql_guard.py`).

The fake connector's `execute_read_query` raises unconditionally. That is the
INV-2 assertion in this file: if any validation path ever reached the execution
surface, every test here would fail loudly rather than quietly passing.
"""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Select

from aida.config import Settings
from aida.connectors.base import ConnectorCapabilities, QueryEstimate, QueryResult
from aida.models import DataSource
from aida.query_gateway import QueryExecutionGateway
from aida.security import SecurityContext
from aida.sql_validation import (
    FINDING_BYTE_BUDGET_EXCEEDED,
    FINDING_COST_CEILING_EXCEEDED,
    FINDING_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR,
    FINDING_MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN,
    FINDING_ROW_LIMIT_APPLIED,
    FINDING_SQL_PARSE_ERROR,
    FINDING_UNKNOWN_COLUMN,
    FINDING_UNKNOWN_OR_UNAUTHORIZED_TABLE,
)

CATALOG = "core"
SCHEMA = "retail"
TABLE = "customer"
COLUMNS = ("customer_id", "state_code", "email_address", "opened_at")


# --- fakes -----------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeSession:
    """Answers the catalog reads and the binding lookup the validation path makes.

    The two catalog reads are told apart by the shape of the SELECT rather than by
    call order -- `allowed_tables` selects three names, `_catalog_columns` selects
    four -- so the fake stays correct if the phases are ever reordered. The
    authorization gate's binding lookup comes through `scalars`, which nothing else
    on this path uses.
    """

    def __init__(
        self,
        *,
        tables: list[tuple[str, str, str]],
        columns: list[tuple[str, str, str, str]],
        bindings: list[Any] | None = None,
    ) -> None:
        self._tables = tables
        self._columns = columns
        self._bindings = bindings or []
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, statement: Select[Any]) -> _FakeResult:
        if len(statement.column_descriptions) == 4:
            return _FakeResult(list(self._columns))
        return _FakeResult(list(self._tables))

    async def scalars(self, _statement: Select[Any]) -> _FakeResult:
        # The authorization gate's source-binding lookup. Empty by default, which
        # leaves the workspace unresolved -- see `CatalogSession` for why a double
        # should not invent one.
        return _FakeResult(list(self._bindings))

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _FakeExecutor:
    """A SQL executor that will cost a statement and refuses to run one."""

    connector_type = "postgres"
    dialect = "postgres"

    def __init__(self, estimate: QueryEstimate | None, *, explain: bool = True) -> None:
        self._estimate = estimate
        self._explain = explain
        self.estimated: list[str] = []

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(explain=self._explain)

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> QueryEstimate:
        self.estimated.append(sql)
        assert self._estimate is not None
        return self._estimate

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        raise AssertionError("validation must never reach execute_read_query (INV-2)")


class _FakeSecretResolver:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def resolve(self, reference: str) -> str:
        return "postgresql://fake/db"


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "default_query_row_limit": 5_000,
        "hard_query_row_limit": 100_000,
        "_env_file": None,
    }
    return Settings(**{**base, **overrides})


def _datasource() -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=uuid4(),
        line_of_business_id=uuid4(),
        project_id=uuid4(),
        name="Governed warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="restricted",
        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
        status="ACTIVE",
        max_concurrency=2,
        capabilities={},
    )


def _context(datasource: DataSource) -> SecurityContext:
    return SecurityContext(
        principal_id="agent-7",
        principal_type="AGENT",
        organization_id=datasource.organization_id,
        roles=frozenset({"AgentDeveloper"}),
    )


def _session(
    *,
    tables: list[tuple[str, str, str]] | None = None,
    columns: list[tuple[str, str, str, str]] | None = None,
) -> _FakeSession:
    return _FakeSession(
        tables=[(CATALOG, SCHEMA, TABLE)] if tables is None else tables,
        columns=(
            [(CATALOG, SCHEMA, TABLE, column) for column in COLUMNS]
            if columns is None
            else columns
        ),
    )


async def _validate(
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
    *,
    estimate: QueryEstimate | None = None,
    explain: bool = True,
    settings: Settings | None = None,
    session: _FakeSession | None = None,
    requested_limit: int | None = None,
) -> tuple[Any, _FakeExecutor, _FakeSession]:
    resolved_settings = settings or _settings()
    executor = _FakeExecutor(
        estimate or QueryEstimate(score=12.5, kind="POSTGRES_PLAN", estimated_rows=100.0),
        explain=explain,
    )
    monkeypatch.setattr("aida.query_gateway.SecretResolver", _FakeSecretResolver)
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: executor,
    )
    datasource = _datasource()
    fake_session = session or _session()
    gateway = QueryExecutionGateway(resolved_settings)
    report = await gateway.validate(
        fake_session,  # type: ignore[arg-type]
        datasource=datasource,
        context=_context(datasource),
        correlation_id="corr-1",
        sql=sql,
        requested_limit=requested_limit,
    )
    return report, executor, fake_session


# --- tests -----------------------------------------------------------------


async def test_valid_query_reports_the_applied_row_limit_and_never_executes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, executor, session = await _validate(
        monkeypatch,
        "SELECT c.customer_id FROM retail.customer AS c",
    )

    assert report.valid
    assert report.rejection_reason() is None
    assert report.referenced_tables == ("retail.customer",)
    assert report.applied_row_limit == 5_000
    limit_finding = next(item for item in report.findings if item.code == FINDING_ROW_LIMIT_APPLIED)
    assert limit_finding.severity == "INFO"
    assert limit_finding.detail["applied_row_limit"] == 5_000
    assert limit_finding.detail["clamped"] is False
    assert report.plan_cost == 12.5
    assert len(executor.estimated) == 1
    assert session.commits == 1


async def test_requested_limit_above_the_hard_limit_is_reported_as_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _, _ = await _validate(
        monkeypatch,
        "SELECT c.customer_id FROM retail.customer AS c",
        settings=_settings(default_query_row_limit=500, hard_query_row_limit=1_000),
        requested_limit=50_000,
    )

    limit_finding = next(item for item in report.findings if item.code == FINDING_ROW_LIMIT_APPLIED)
    assert report.valid
    assert report.applied_row_limit == 1_000
    assert limit_finding.detail["clamped"] is True
    assert limit_finding.detail["hard_row_limit"] == 1_000


async def test_write_statement_is_rejected_before_any_source_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, executor, _ = await _validate(
        monkeypatch,
        "DELETE FROM retail.customer WHERE customer_id = 42",
    )

    assert not report.valid
    assert FINDING_MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN in report.codes()
    assert executor.estimated == []
    assert "42" not in str([finding.as_dict() for finding in report.findings])


async def test_unparseable_sql_yields_a_parse_finding_that_withholds_the_parser_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sqlglot's ParseError quotes the offending fragment, literals included.

    Echoing it would put a source-shaped value into a finding, an audit detail
    and a persisted `error_message`, so the message is dropped and only the code
    survives (INV-6).
    """
    report, executor, session = await _validate(
        monkeypatch,
        "SELECT ((( FROM retail.customer WHERE email_address = 'private@example.com'",
    )

    assert not report.valid
    assert report.codes() == (FINDING_SQL_PARSE_ERROR,)
    assert report.rejection_reason() == FINDING_SQL_PARSE_ERROR
    assert report.normalized_sql is None
    assert executor.estimated == []
    assert "private@example.com" not in str(report.as_dict())
    assert "private@example.com" not in str(session.added[0].details)


async def test_unauthorized_table_is_reported_per_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, executor, _ = await _validate(
        monkeypatch,
        "SELECT g.entry_id FROM fin.gl_entries AS g",
        session=_session(tables=[(CATALOG, SCHEMA, TABLE)], columns=[]),
    )

    assert not report.valid
    finding = next(
        item for item in report.findings if item.code == FINDING_UNKNOWN_OR_UNAUTHORIZED_TABLE
    )
    assert finding.ref == "fin.gl_entries"
    assert report.rejection_reason() == "UNKNOWN_OR_UNAUTHORIZED_TABLES: fin.gl_entries"
    assert executor.estimated == []


async def test_column_absent_from_the_catalog_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, executor, _ = await _validate(
        monkeypatch,
        "SELECT c.email_addr FROM retail.customer AS c",
    )

    assert not report.valid
    finding = next(item for item in report.findings if item.code == FINDING_UNKNOWN_COLUMN)
    assert finding.ref == "retail.customer.email_addr"
    assert report.rejection_reason() == "UNKNOWN_COLUMNS: retail.customer.email_addr"
    assert executor.estimated == []


async def test_projection_aliases_and_cte_names_are_not_reported_as_unknown_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _, _ = await _validate(
        monkeypatch,
        "WITH active AS (SELECT c.customer_id AS cid FROM retail.customer AS c) "
        "SELECT cid FROM active",
    )

    assert report.valid, report.codes()
    assert FINDING_UNKNOWN_COLUMN not in report.codes()


async def test_cost_ceiling_exceeded_is_a_finding_with_numbers_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _, _ = await _validate(
        monkeypatch,
        "SELECT c.customer_id FROM retail.customer AS c",
        estimate=QueryEstimate(score=9_999.0, kind="POSTGRES_PLAN", estimated_rows=1e6),
        settings=_settings(max_postgres_plan_cost=1_000.0),
    )

    assert not report.valid
    finding = next(item for item in report.findings if item.code == FINDING_COST_CEILING_EXCEEDED)
    assert finding.detail == {"plan_cost": 9_999.0, "limit": 1_000.0}
    assert report.rejection_reason() == "QUERY_COST_EXCEEDS_POLICY: 9999.0 > 1000.0"
    assert report.plan_cost == 9_999.0


async def test_byte_budget_exceeded_selects_the_byte_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _, _ = await _validate(
        monkeypatch,
        "SELECT c.customer_id FROM retail.customer AS c",
        estimate=QueryEstimate(
            score=8.2e11, kind="BIGQUERY_DRY_RUN_BYTES", estimated_bytes=820_000_000_000
        ),
        settings=_settings(max_bigquery_dry_run_bytes=10_000_000_000),
    )

    assert not report.valid
    assert FINDING_BYTE_BUDGET_EXCEEDED in report.codes()
    assert FINDING_COST_CEILING_EXCEEDED not in report.codes()
    assert report.estimated_bytes == 820_000_000_000
    assert (report.rejection_reason() or "").startswith("QUERY_BYTES_EXCEED_POLICY")


async def test_connector_without_explain_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report, _, _ = await _validate(
        monkeypatch,
        "SELECT c.customer_id FROM retail.customer AS c",
        explain=False,
    )

    assert not report.valid
    assert FINDING_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR in report.codes()
    assert report.rejection_reason() == "QUERY_ESTIMATE_UNAVAILABLE_FOR_CONNECTOR"
    assert report.plan_cost is None


async def test_findings_and_report_carry_no_literal_values_from_the_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-6: a finding names objects, codes, hints and numbers -- never a value."""
    report, _, _ = await _validate(
        monkeypatch,
        "SELECT c.customer_id FROM retail.customer AS c "
        "WHERE c.email_address = 'private@example.com' AND c.state_code = 'NY'",
    )

    serialized = str(report.as_dict())
    assert report.valid, report.codes()
    assert "private@example.com" not in serialized
    assert "'NY'" not in serialized
    assert "email_address" in (report.normalized_sql or "")


async def test_validation_records_a_distinct_audit_action_and_no_execution_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, session = await _validate(
        monkeypatch,
        "SELECT c.customer_id FROM retail.customer AS c",
    )

    actions = [getattr(item, "action", None) for item in session.added]
    assert actions == ["query.validate.gateway"]
    assert all(type(item).__name__ != "QueryExecution" for item in session.added)
    audit = session.added[0]
    assert audit.outcome == "SUCCESS"
    assert audit.details["executed"] is False
    assert audit.details["finding_codes"] == [FINDING_ROW_LIMIT_APPLIED]


async def test_validation_never_calls_execute_read_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-2, proven rather than asserted.

    `_FakeExecutor.execute_read_query` raises, so reaching it anywhere in the
    validation path -- valid statement, refused statement, over-budget statement
    -- turns into a test failure instead of a source touch.
    """
    for sql in (
        "SELECT c.customer_id FROM retail.customer AS c",
        "SELECT c.email_addr FROM retail.customer AS c",
        "UPDATE retail.customer SET state_code = 'NY'",
    ):
        report, executor, _ = await _validate(monkeypatch, sql)
        assert isinstance(report.valid, bool)
        with pytest.raises(AssertionError, match="INV-2"):
            await executor.execute_read_query("SELECT 1", timeout_seconds=1)


# --- MCP surface -----------------------------------------------------------


class _McpSession(_FakeSession):
    """The validation fake plus the `.get()` the MCP handler uses to resolve
    the datasource. Same anti-enumeration path as the native lineage tools."""

    def __init__(self, datasource: DataSource | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._datasource = datasource

    async def get(self, _model: type[object], _identity: object) -> object | None:
        return self._datasource


def test_the_validation_tool_is_declared_once_with_a_complete_input_schema() -> None:
    from aida.mcp_server import (
        NATIVE_VALIDATION_TOOL_DEFINITIONS,
        NATIVE_VALIDATION_TOOL_SLUGS,
    )

    assert NATIVE_VALIDATION_TOOL_SLUGS == {"validate_sql"}
    definition = NATIVE_VALIDATION_TOOL_DEFINITIONS[0]
    schema = definition["inputSchema"]
    assert schema["required"] == ["datasource_id", "sql"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"datasource_id", "sql", "max_rows"}


async def test_the_validation_tool_denies_an_ineligible_caller_like_an_unknown_tool() -> None:
    from aida.mcp_server import _handle_native_validation_tool_call

    caller = SecurityContext(
        principal_id="viewer-with-no-validation-role",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset(),
    )

    result = await _handle_native_validation_tool_call(
        "validate_sql",
        {"datasource_id": str(uuid4()), "sql": "SELECT 1"},
        _McpSession(None, tables=[], columns=[]),  # type: ignore[arg-type]
        caller,
        _settings(),
        "corr-mcp",
    )

    assert result["isError"] is True
    assert result["content"] == [
        {"type": "text", "text": "Tool 'validate_sql' not found or not published."}
    ]


async def test_the_validation_tool_returns_findings_and_never_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from aida.mcp_server import _handle_native_validation_tool_call

    executor = _FakeExecutor(QueryEstimate(score=3.0, kind="POSTGRES_PLAN", estimated_rows=10.0))
    monkeypatch.setattr("aida.query_gateway.SecretResolver", _FakeSecretResolver)
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: executor,
    )
    datasource = _datasource()
    session = _McpSession(
        datasource,
        tables=[(CATALOG, SCHEMA, TABLE)],
        columns=[(CATALOG, SCHEMA, TABLE, column) for column in COLUMNS],
    )

    result = await _handle_native_validation_tool_call(
        "validate_sql",
        {
            "datasource_id": str(datasource.id),
            "sql": "SELECT c.customer_id FROM retail.customer AS c "
            "WHERE c.state_code = 'NY'",
        },
        session,  # type: ignore[arg-type]
        _context(datasource),
        _settings(),
        "corr-mcp",
    )

    assert "isError" not in result
    body = json.loads(result["content"][1]["text"].removeprefix("```json\n").removesuffix("\n```"))
    assert body["valid"] is True
    assert body["governance"] == {
        "executed": False,
        "value_free": True,
        "literals_redacted": True,
    }
    assert "'NY'" not in str(result)
    assert [item["code"] for item in body["findings"]] == [FINDING_ROW_LIMIT_APPLIED]
