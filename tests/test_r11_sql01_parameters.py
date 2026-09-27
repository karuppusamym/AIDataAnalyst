"""R11-SQL01: typed parameter values for reviewed SQL -- bound, digested, never stored.

The contract, one test (or a few) per clause:

* a statement names `:placeholders`; each is declared with a governed-tool type and a value sent
  beside the text, and bound by the governed-tool renderer as one typed literal;
* a value that does not fit is a finding with a stable, value-free `PARAMETER_*` code, and no
  receipt -- the gateway is not asked about a statement that could not be bound;
* an injection attempt inside a value reaches the source as one string literal, inert;
* the receipt binds the declared types and a keyed digest of the values, so changing any value
  at Run is REVALIDATION_REQUIRED, exactly as an edited statement is;
* no value is persisted anywhere -- every table of the platform database is scanned -- or
  written to an audit record, and the history shows the template's placeholders instead.

HTTP tests drive the real routes against an in-memory database, with the connector doubled to
record every statement it is asked to estimate or execute: "the source saw one literal" is read
off the statement the connector received, not inferred.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import typing
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlglot import exp, parse, parse_one

from aida.connectors.base import QueryResult
from aida.main import app
from aida.models import AuditEvent, QueryExecution
from aida.schemas import ToolParameterDefinition
from aida.sql_workspace import (
    PARAMETER_INVALID,
    PARAMETER_TOO_LONG,
    PARAMETER_TYPE_MISMATCH,
    PARAMETER_UNDECLARED,
    PARAMETER_UNUSED,
    PARAMETER_VALUE_MAX_LENGTH,
    PARAMETER_VALUE_MISSING,
    DraftParameter,
    bind_parameters,
    parameter_fingerprint,
    statement_digest,
)
from aida.sql_workspace_api import SqlDraftParameter, SqlParameterType
from aida.sql_workspace_models import SqlDraftReceipt
from aida.tool_rendering import ToolParameterError
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session
from tests.support.doubles import FakeSqlExecutor
from tests.test_f01_context_product_execution_boundary import _Scenario
from tests.test_r11_sql01_sql_workspace import _headers

TEMPLATE = "SELECT o.order_id FROM retail.orders AS o WHERE o.order_id = :order_id"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as active:
        yield active
    await engine.dispose()


@pytest.fixture(autouse=True)
def _no_real_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aida.query_gateway.SecretResolver",
        lambda settings: type(
            "_Resolver", (), {"resolve": staticmethod(lambda ref: "postgresql://fake/db")}
        )(),
    )


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> _Scenario:
    return await _Scenario(db).build()


@pytest_asyncio.fixture
async def http(scenario: _Scenario) -> AsyncIterator[httpx.AsyncClient]:
    previous = dict(app.dependency_overrides)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield scenario.db

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://sql01.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


class _Source(FakeSqlExecutor):
    """Records what the source is asked to estimate and to execute, in order."""

    def __init__(self, log: list[tuple[str, str]]) -> None:
        super().__init__(({"order_id": "O-1"},))
        self._log = log

    async def estimate_read_query(self, sql: str, *, timeout_seconds: int) -> Any:
        self._log.append(("estimate", sql))
        return await super().estimate_read_query(sql, timeout_seconds=timeout_seconds)

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        self._log.append(("execute", sql))
        return await super().execute_read_query(sql, timeout_seconds=timeout_seconds)


@pytest.fixture
def source_log(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every statement the source was asked about -- estimates and executions."""
    log: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session", lambda connector_type, dsn: _Source(log)
    )
    return log


def _executed(log: list[tuple[str, str]]) -> list[str]:
    return [sql for kind, sql in log if kind == "execute"]


def _parameter(name: str, parameter_type: str, value: Any) -> dict[str, Any]:
    return {"name": name, "parameter_type": parameter_type, "value": value}


async def _draft(http: httpx.AsyncClient, scenario: _Scenario, **body: Any) -> httpx.Response:
    return await http.post(
        f"/v1/datasources/{scenario.datasource.id}/sql-drafts",
        json=body,
        headers=_headers(scenario),
    )


async def _run(
    http: httpx.AsyncClient, scenario: _Scenario, receipt_id: str, **body: Any
) -> httpx.Response:
    return await http.post(
        f"/v1/sql-drafts/{receipt_id}/run", json=body, headers=_headers(scenario)
    )


async def _executions(scenario: _Scenario) -> int:
    return int(await scenario.db.scalar(select(func.count()).select_from(QueryExecution)) or 0)


async def _receipt(http: httpx.AsyncClient, scenario: _Scenario, **body: Any) -> str:
    response = await _draft(http, scenario, **body)
    assert response.status_code == 200, response.text
    receipt = response.json()["receipt"]
    assert receipt is not None, response.json()["validation"]
    return str(receipt["id"])


def _PLACEHOLDER(name: str) -> re.Pattern[str]:  # noqa: N802 -- reads as a constant pattern
    """A placeholder as the dialect writes it back: `:name`, or PostgreSQL's `%(name)s`."""
    return re.compile(rf"(?::{name}|%\({name}\)s)")


def _where_values(sql: str, dialect: str) -> dict[str, exp.Expression]:
    """The value side of every comparison in the statement's WHERE, by the column compared."""
    statement = parse_one(sql, read=dialect)
    where = statement.find(exp.Where)
    assert where is not None, sql
    return {
        node.this.name: node.expression
        for node in where.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
    }


# ---------------------------------------------------------------------------
# The contract is the governed tool's
# ---------------------------------------------------------------------------


def test_parameter_types_and_names_are_the_tool_contracts() -> None:
    """One vocabulary: a draft parameter is declared exactly as a tool parameter is."""
    tool_field = ToolParameterDefinition.model_fields["parameter_type"]
    assert set(typing.get_args(SqlParameterType)) == set(typing.get_args(tool_field.annotation))
    tool_name = ToolParameterDefinition.model_json_schema()["properties"]["name"]["pattern"]
    draft_name = SqlDraftParameter.model_json_schema()["properties"]["name"]["pattern"]
    assert draft_name == tool_name


# ---------------------------------------------------------------------------
# Binding: one typed literal per placeholder, reported by code when it cannot bind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["postgres", "tsql"])
def test_each_type_binds_as_one_typed_literal(dialect: str) -> None:
    binding = bind_parameters(
        "SELECT o.order_id FROM retail.orders AS o WHERE o.code = :code AND o.qty > :qty "
        "AND o.amount < :amount AND o.placed_on >= :since AND o.active = :active",
        dialect=dialect,
        parameters=[
            DraftParameter("code", "STRING", "O-1"),
            DraftParameter("qty", "INTEGER", 3),
            DraftParameter("amount", "NUMBER", 12.5),
            DraftParameter("since", "DATE", "2024-01-05"),
            DraftParameter("active", "BOOLEAN", True),
        ],
    )

    assert binding.findings == ()
    assert binding.executable_sql is not None
    assert ":" not in binding.executable_sql, "no placeholder is left for the source"
    values = _where_values(binding.executable_sql, dialect)
    code, qty, amount, since, active = (
        values[column] for column in ("code", "qty", "amount", "placed_on", "active")
    )
    assert isinstance(code, exp.Literal) and code.is_string and code.this == "O-1"
    assert isinstance(qty, exp.Literal) and not qty.is_string and qty.this == "3"
    assert isinstance(amount, exp.Literal) and not amount.is_string and amount.this == "12.5"
    assert isinstance(since, exp.Literal) and since.is_string and since.this == "2024-01-05"
    # T-SQL has no boolean literal; the renderer writes the dialect's own spelling.
    assert isinstance(active, exp.Boolean | exp.Literal)
    assert binding.parameter_types == {
        "code": "STRING",
        "qty": "INTEGER",
        "amount": "NUMBER",
        "since": "DATE",
        "active": "BOOLEAN",
    }


def test_a_statement_without_parameters_passes_through_byte_for_byte() -> None:
    raw = "select o.order_id   from retail.orders o where o.order_id = 'O-1'"

    binding = bind_parameters(raw, dialect="postgres", parameters=[])

    assert binding.executable_sql == raw and binding.findings == ()
    assert binding.parameter_types == {} and binding.normalized_values == {}


@pytest.mark.parametrize(
    ("parameter_type", "value"),
    [
        ("INTEGER", "5"),
        ("INTEGER", True),
        ("INTEGER", 1.5),
        ("NUMBER", "1.5"),
        ("NUMBER", math.nan),
        ("NUMBER", math.inf),
        ("BOOLEAN", 1),
        ("BOOLEAN", "true"),
        ("DATE", "2024-13-01"),
        ("DATE", 20240105),
        ("STRING", 5),
    ],
    ids=lambda item: repr(item),
)
def test_a_value_of_the_wrong_type_is_a_type_mismatch(parameter_type: str, value: Any) -> None:
    binding = bind_parameters(
        TEMPLATE,
        dialect="postgres",
        parameters=[DraftParameter("order_id", parameter_type, value)],
    )

    assert binding.executable_sql is None
    assert [finding.as_dict() for finding in binding.findings] == [
        {
            "code": PARAMETER_TYPE_MISMATCH,
            "severity": "ERROR",
            "ref": "order_id",
            "hint": binding.findings[0].hint,
            "detail": {"parameter_type": parameter_type},
        }
    ]


@pytest.mark.parametrize(
    ("sql", "parameters", "expected"),
    [
        (
            TEMPLATE,
            [DraftParameter("order_id", "STRING", None)],
            [(PARAMETER_VALUE_MISSING, "order_id")],
        ),
        (TEMPLATE, [], [(PARAMETER_UNDECLARED, "order_id")]),
        (
            TEMPLATE,
            [DraftParameter("order_id", "STRING", "O-1"), DraftParameter("region", "STRING", "E")],
            [(PARAMETER_UNUSED, "region")],
        ),
        (
            TEMPLATE,
            [DraftParameter("region", "STRING", "E")],
            [(PARAMETER_UNDECLARED, "order_id"), (PARAMETER_UNUSED, "region")],
        ),
        (
            TEMPLATE,
            [DraftParameter("order_id", "STRING", "x" * (PARAMETER_VALUE_MAX_LENGTH + 1))],
            [(PARAMETER_TOO_LONG, "order_id")],
        ),
        (
            TEMPLATE,
            [DraftParameter("order_id", "STRING", "a"), DraftParameter("order_id", "STRING", "b")],
            [(PARAMETER_INVALID, None)],
        ),
        (TEMPLATE, [DraftParameter("order_id", "DECIMAL", 1)], [(PARAMETER_INVALID, None)]),
        (
            "SELECT o.order_id FROM retail.orders AS o WHERE o.order_id = ((:order_id",
            [DraftParameter("order_id", "STRING", "O-1")],
            [("SQL_PARSE_ERROR", None)],
        ),
    ],
    ids=[
        "missing",
        "undeclared",
        "unused",
        "undeclared-and-unused",
        "too-long",
        "declared-twice",
        "unknown-type",
        "unparseable",
    ],
)
def test_a_parameter_that_cannot_bind_is_named_by_code(
    sql: str, parameters: list[DraftParameter], expected: list[tuple[str, str | None]]
) -> None:
    binding = bind_parameters(sql, dialect="postgres", parameters=parameters)

    assert binding.executable_sql is None
    assert [(finding.code, finding.ref) for finding in binding.findings] == expected
    assert all(finding.blocking for finding in binding.findings)


def test_an_unrecognised_renderer_refusal_is_still_refused_and_never_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal with no code the workspace knows fails closed, value-free."""

    def _new_refusal(*args: Any, **kwargs: Any) -> Any:
        raise ToolParameterError("a phrase the workspace never mapped: SENTINEL-4471")

    monkeypatch.setattr("aida.sql_workspace.render_tool_sql", _new_refusal)

    binding = bind_parameters(
        TEMPLATE, dialect="postgres", parameters=[DraftParameter("order_id", "STRING", "O-1")]
    )

    assert [(finding.code, finding.ref) for finding in binding.findings] == [
        (PARAMETER_INVALID, None)
    ]
    assert "SENTINEL-4471" not in str([finding.as_dict() for finding in binding.findings])


def test_no_finding_carries_the_value() -> None:
    value = "VALUE-SENTINEL-8841"
    findings = [
        *bind_parameters(
            TEMPLATE, dialect="postgres", parameters=[DraftParameter("order_id", "INTEGER", value)]
        ).findings,
        *bind_parameters(
            TEMPLATE, dialect="postgres", parameters=[DraftParameter("order_id", "DATE", value)]
        ).findings,
    ]

    assert findings and all(value not in str(finding.as_dict()) for finding in findings)


# ---------------------------------------------------------------------------
# The digest: the values under the signing key, and the statement's digest under it too
# ---------------------------------------------------------------------------


async def test_the_value_digest_is_keyed_and_is_the_tool_executions_fingerprint() -> None:
    binding = bind_parameters(
        TEMPLATE, dialect="postgres", parameters=[DraftParameter("order_id", "STRING", "O-1")]
    )
    canonical = json.dumps({"order_id": "O-1"}, sort_keys=True, separators=(",", ":"))

    one = await parameter_fingerprint(Settings(_env_file=None, audit_hmac_key="k" * 32), binding)
    other = await parameter_fingerprint(Settings(_env_file=None, audit_hmac_key="j" * 32), binding)

    assert one is not None and other is not None and one != other
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() not in (one, other)
    assert (
        await parameter_fingerprint(
            Settings(_env_file=None),
            bind_parameters(TEMPLATE, dialect="postgres", parameters=[]),
        )
        is None
    )


async def test_the_digest_binds_the_parameter_types_and_values() -> None:
    settings = Settings(_env_file=None)
    base: dict[str, Any] = {
        "sql": TEMPLATE,
        "max_rows": None,
        "context_product_version_id": None,
        "workspace_id": None,
        "parameter_types": {"order_id": "STRING"},
        "parameter_fingerprint": "fingerprint-one",
    }
    digest = await statement_digest(settings, **base)

    for field, value in (
        ("parameter_types", {"order_id": "INTEGER"}),
        ("parameter_types", {"order_ref": "STRING"}),
        ("parameter_fingerprint", "fingerprint-two"),
    ):
        assert await statement_digest(settings, **{**base, field: value}) != digest, (field, value)


async def test_a_statement_without_parameters_digests_the_same_with_or_without_empty_ones() -> None:
    """No parameters is no parameter keys: an empty declaration adds nothing to what is signed."""
    settings = Settings(_env_file=None)
    payload: dict[str, Any] = {
        "sql": TEMPLATE,
        "max_rows": 5,
        "context_product_version_id": None,
        "workspace_id": None,
    }

    bare = await statement_digest(settings, **payload)

    assert (
        await statement_digest(
            settings, **payload, parameter_types={}, parameter_fingerprint=None
        )
        == bare
    )


# ---------------------------------------------------------------------------
# Over HTTP: validate, run, refuse
# ---------------------------------------------------------------------------


async def test_a_parameterized_statement_validates_then_runs_its_bound_values(
    http: httpx.AsyncClient, scenario: _Scenario, source_log: list[tuple[str, str]]
) -> None:
    parameters = [_parameter("order_id", "STRING", "O-1")]

    drafted = await _draft(http, scenario, sql=TEMPLATE, parameters=parameters)

    assert drafted.status_code == 200, drafted.text
    payload = drafted.json()
    assert payload["validation"]["valid"] is True
    assert payload["receipt"]["status"] == "VALIDATED"
    # The receipt keeps the template's shape -- its placeholder names the parameter.
    assert _PLACEHOLDER("order_id").search(payload["receipt"]["redacted_sql"] or "")
    # The gateway estimated the bound statement; nothing executed.
    assert _executed(source_log) == [] and await _executions(scenario) == 0
    [(kind, estimated)] = source_log
    assert kind == "estimate" and "'O-1'" in estimated

    ran = await _run(http, scenario, payload["receipt"]["id"], sql=TEMPLATE, parameters=parameters)

    assert ran.status_code == 200, ran.text
    assert ran.json()["execution"]["rows"] == [{"order_id": "O-1"}]
    [statement] = _executed(source_log)
    value = _where_values(statement, "postgres")["order_id"]
    assert isinstance(value, exp.Literal) and value.is_string and value.this == "O-1"


async def test_an_injection_attempt_inside_a_value_is_one_inert_string_literal(
    http: httpx.AsyncClient, scenario: _Scenario, source_log: list[tuple[str, str]]
) -> None:
    attack = "O-1' OR '1'='1'; DELETE FROM retail.orders; --"
    parameters = [_parameter("order_id", "STRING", attack)]
    receipt_id = await _receipt(http, scenario, sql=TEMPLATE, parameters=parameters)

    ran = await _run(http, scenario, receipt_id, sql=TEMPLATE, parameters=parameters)

    assert ran.status_code == 200, ran.text
    [statement] = _executed(source_log)
    parsed = [node for node in parse(statement, read="postgres") if node is not None]
    assert len(parsed) == 1 and isinstance(parsed[0], exp.Select), statement
    assert not list(parsed[0].find_all(exp.Delete, exp.Or)), statement
    value = _where_values(statement, "postgres")["order_id"]
    assert isinstance(value, exp.Literal) and value.is_string and value.this == attack


async def test_a_placeholder_nobody_declared_is_refused_before_the_gateway(
    http: httpx.AsyncClient, scenario: _Scenario, source_log: list[tuple[str, str]]
) -> None:
    """Before this, the guard read `:order_id` as syntax and the statement earned a receipt."""
    response = await _draft(http, scenario, sql=TEMPLATE)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["receipt"] is None
    assert payload["validation"]["valid"] is False
    assert [(f["code"], f["ref"]) for f in payload["validation"]["findings"]] == [
        (PARAMETER_UNDECLARED, "order_id")
    ]
    assert payload["validation"]["rejection_reason"] == PARAMETER_UNDECLARED
    assert source_log == [], "no connector was opened for an unbindable statement"
    audits = (await scenario.db.scalars(select(AuditEvent))).all()
    assert "query.validate.gateway" not in {audit.action for audit in audits}
    [refused] = [audit for audit in audits if audit.action == "sql_draft.validation_refused"]
    assert refused.details["finding_codes"] == [PARAMETER_UNDECLARED]


async def test_a_value_of_the_wrong_type_is_a_finding_with_no_receipt(
    http: httpx.AsyncClient, scenario: _Scenario, source_log: list[tuple[str, str]]
) -> None:
    response = await _draft(
        http, scenario, sql=TEMPLATE, parameters=[_parameter("order_id", "INTEGER", "O-1")]
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["receipt"] is None
    [finding] = payload["validation"]["findings"]
    assert finding["code"] == PARAMETER_TYPE_MISMATCH
    assert finding["ref"] == "order_id"
    assert finding["detail"] == {"parameter_type": "INTEGER"}
    assert "O-1" not in response.text
    assert source_log == []


@pytest.mark.parametrize(
    ("parameter_type", "value"),
    [("INTEGER", "5"), ("NUMBER", "1.5"), ("BOOLEAN", "true"), ("BOOLEAN", 0), ("STRING", 7)],
)
async def test_json_values_are_not_coerced_to_the_declared_type(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    source_log: list[tuple[str, str]],
    parameter_type: str,
    value: Any,
) -> None:
    """`"5"` is text: the person declared INTEGER and sent text, and is told so."""
    response = await _draft(
        http, scenario, sql=TEMPLATE, parameters=[_parameter("order_id", parameter_type, value)]
    )

    assert response.status_code == 200, response.text
    assert [f["code"] for f in response.json()["validation"]["findings"]] == [
        PARAMETER_TYPE_MISMATCH
    ]
    assert source_log == []


@pytest.mark.parametrize(
    "changed",
    [
        [_parameter("order_id", "STRING", "O-2")],
        [_parameter("order_id", "STRING", "O-1 ")],
        [_parameter("order_id", "INTEGER", "O-1")],
        [],
    ],
    ids=["another-value", "trailing-space", "another-type-that-does-not-bind", "dropped"],
)
async def test_changing_any_value_requires_validating_again(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    source_log: list[tuple[str, str]],
    changed: list[dict[str, Any]],
) -> None:
    validated = [_parameter("order_id", "STRING", "O-1")]
    receipt_id = await _receipt(http, scenario, sql=TEMPLATE, parameters=validated)

    refused = await _run(http, scenario, receipt_id, sql=TEMPLATE, parameters=changed)

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "REVALIDATION_REQUIRED"
    assert _executed(source_log) == [] and await _executions(scenario) == 0
    # A refused change spends nothing: the values that were validated still run.
    original = await _run(http, scenario, receipt_id, sql=TEMPLATE, parameters=validated)
    assert original.status_code == 200, original.text


async def test_the_same_value_declared_as_another_type_requires_validating_again(
    http: httpx.AsyncClient, scenario: _Scenario, source_log: list[tuple[str, str]]
) -> None:
    """INTEGER 5 and NUMBER 5 normalize to one value; the declared type is bound as well."""
    sql = "SELECT o.order_id FROM retail.orders AS o WHERE o.qty = :qty"
    receipt_id = await _receipt(
        http, scenario, sql=sql, parameters=[_parameter("qty", "INTEGER", 5)]
    )

    refused = await _run(
        http, scenario, receipt_id, sql=sql, parameters=[_parameter("qty", "NUMBER", 5)]
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "REVALIDATION_REQUIRED"
    assert _executed(source_log) == []


async def test_the_bound_statement_sent_as_raw_sql_is_not_the_validated_one(
    http: httpx.AsyncClient, scenario: _Scenario, source_log: list[tuple[str, str]]
) -> None:
    parameters = [_parameter("order_id", "STRING", "O-1")]
    receipt_id = await _receipt(http, scenario, sql=TEMPLATE, parameters=parameters)
    bound = bind_parameters(
        TEMPLATE, dialect="postgres", parameters=[DraftParameter("order_id", "STRING", "O-1")]
    ).executable_sql

    refused = await _run(http, scenario, receipt_id, sql=bound)

    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "REVALIDATION_REQUIRED"
    assert _executed(source_log) == []


@pytest.mark.parametrize(
    "body",
    [
        {"question": "which orders", "parameters": [_parameter("order_id", "STRING", "O-1")]},
        {
            "sql": TEMPLATE,
            "parameters": [
                _parameter("order_id", "STRING", "O-1"),
                _parameter("order_id", "STRING", "O-2"),
            ],
        },
        {"sql": TEMPLATE, "parameters": [_parameter("Order-Id", "STRING", "O-1")]},
        {"sql": TEMPLATE, "parameters": [_parameter("order_id", "DECIMAL", 1)]},
        {"sql": TEMPLATE, "parameters": [_parameter("order_id", "STRING", ["O-1"])]},
    ],
    ids=["with-a-question", "declared-twice", "bad-name", "unknown-type", "not-a-scalar"],
)
async def test_a_malformed_parameter_request_is_refused(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    source_log: list[tuple[str, str]],
    body: dict[str, Any],
) -> None:
    response = await _draft(http, scenario, **body)

    assert response.status_code == 422, response.text
    assert source_log == []


# ---------------------------------------------------------------------------
# INV-6: no value is stored, audited or listed
# ---------------------------------------------------------------------------

#: Distinctive enough that finding one anywhere can only mean the value was stored.
SENTINELS: dict[str, tuple[str, Any]] = {
    "code": ("STRING", "PARAM-SENTINEL-5519"),
    "qty": ("INTEGER", 918273645),
    "amount": ("NUMBER", 4242.125),
    "since": ("DATE", "2031-07-19"),
}
SENTINEL_TEXT = ("PARAM-SENTINEL-5519", "918273645", "4242.125", "2031-07-19")
SENTINEL_SQL = (
    "SELECT o.order_id FROM retail.orders AS o WHERE o.code = :code AND o.qty > :qty "
    "AND o.amount < :amount AND o.placed_on >= :since"
)


async def _every_stored_value(scenario: _Scenario) -> str:
    """Every row of every table in the platform database, as one string."""
    stored: list[str] = []
    for table in Base.metadata.sorted_tables:
        rows = (await scenario.db.execute(select(table))).all()
        stored.extend(repr(tuple(row)) for row in rows)
    return "\n".join(stored)


async def test_no_parameter_value_is_persisted_anywhere(
    http: httpx.AsyncClient, scenario: _Scenario, source_log: list[tuple[str, str]]
) -> None:
    parameters = [_parameter(name, kind, value) for name, (kind, value) in SENTINELS.items()]
    drafted = await _draft(http, scenario, sql=SENTINEL_SQL, parameters=parameters)
    assert drafted.status_code == 200, drafted.text
    receipt_id = drafted.json()["receipt"]["id"]
    changed = [*parameters[:-1], _parameter("since", "DATE", "2031-07-20")]
    refused = await _run(http, scenario, receipt_id, sql=SENTINEL_SQL, parameters=changed)
    assert refused.status_code == 409
    ran = await _run(http, scenario, receipt_id, sql=SENTINEL_SQL, parameters=parameters)
    assert ran.status_code == 200, ran.text
    history = await http.get(
        f"/v1/datasources/{scenario.datasource.id}/sql-drafts", headers=_headers(scenario)
    )
    assert history.status_code == 200
    # The source did receive the values -- that is what binding is for.
    [statement] = _executed(source_log)
    assert all(text in statement for text in SENTINEL_TEXT)

    stored = await _every_stored_value(scenario)
    assert stored, "the scan read the platform's rows"
    for text in SENTINEL_TEXT:
        assert text not in stored, f"{text} was persisted"
        # Nor echoed back: the draft, the run and the history show redacted shapes only.
        for response in (drafted, refused, ran, history):
            assert text not in response.text, f"{text} was returned"

    # What stands in for the values: one keyed fingerprint, the same at validate and at run.
    audits = (
        await scenario.db.scalars(select(AuditEvent).where(AuditEvent.action.like("sql_draft.%")))
    ).all()
    validated = [a for a in audits if a.action == "sql_draft.validated"]
    run = [a for a in audits if a.action == "sql_draft.run" and a.outcome == "SUCCESS"]
    assert len(validated) == 1 and len(run) == 1
    assert validated[0].details["parameter_count"] == len(SENTINELS)
    fingerprint = validated[0].details["parameter_fingerprint"]
    assert fingerprint and run[0].details["parameter_fingerprint"] == fingerprint
    receipt = await scenario.db.get(SqlDraftReceipt, UUID(receipt_id))
    assert receipt is not None and receipt.status == "EXECUTED"
    for name in SENTINELS:
        assert _PLACEHOLDER(name).search(receipt.redacted_sql or ""), "the shape names each one"
