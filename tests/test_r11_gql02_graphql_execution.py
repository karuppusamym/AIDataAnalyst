"""R11-GQL02: governed execution through GraphQL -- one explicit mutation, once per key.

The acceptance (design section 13B, GQL-D), clause by clause:

* the mutation executes an approved tool version through the same governed path REST uses,
  and a `query` never executes anything;
* one execution root per operation -- aliases, a second field or a fragment cannot hide a
  second execution;
* a caller-scoped idempotency key: a duplicate executes nothing, the same key with other
  inputs is refused, and an outcome the platform did not learn stays PENDING and is not
  retried as a new execution;
* tool version, parameters, role binding, quality holds and product scope all decide, and a
  refusal executes nothing;
* no parameter value is stored, and a receipt never carries rows.

Driven through `POST /graphql` against the real application and an in-memory database, with
the connector doubled so it records every statement it executes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.connectors.base import QueryResult
from aida.governed_execution_models import GovernedExecutionRequest
from aida.main import app
from aida.models import (
    AuditEvent,
    DataQualityIncident,
    GovernedToolVersion,
    MetadataColumn,
    ToolExecution,
)
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session
from tests.support.doubles import FakeSqlExecutor
from tests.test_f01_context_product_execution_boundary import PRODUCT_KEY, _Scenario

pytestmark = pytest.mark.asyncio

ORDERS_SQL = "SELECT o.order_id FROM retail.orders AS o"
PARAMETER_VALUE = "PARAM-VALUE-5519"

RUN = """
mutation Run($request: ExecuteGovernedToolInput!) {
  executeGovernedTool(request: $request) {
    replayed
    qualityGateAction
    receipt {
      id status toolExecutionId queryExecutionId rowCount outcomeCode contextProductVersionId
    }
    result { columns rows maskedColumns appliedRowLimit }
  }
}
"""

RECEIPT = """
query Receipt($id: ID!) {
  governedExecution(id: $id) { id status rowCount queryExecutionId outcomeCode }
}
"""


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


class _RecordingExecutor(FakeSqlExecutor):
    """Records each statement *executed*; the dry-run estimate is not an execution."""

    def __init__(self, executed: list[str]) -> None:
        super().__init__(({"order_id": "O-1"}, {"order_id": "O-2"}))
        self._executed = executed

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        self._executed.append(sql)
        return await super().execute_read_query(sql, timeout_seconds=timeout_seconds)


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    statements: list[str] = []
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: _RecordingExecutor(statements),
    )
    return statements


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
    async with httpx.AsyncClient(transport=transport, base_url="http://gql02.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _headers(
    scenario: _Scenario, *, principal: str = "gql-analyst", roles: str = "Analyst"
) -> dict[str, str]:
    return {
        "X-Principal-Id": principal,
        "X-Principal-Type": "USER",
        "X-Roles": roles,
        "X-Business-Purpose": "Execute a governed tool through GraphQL",
        "X-Organization-Id": str(scenario.organization.id),
    }


async def _run(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    version: GovernedToolVersion,
    *,
    key: str = "gql02-key-0001",
    max_rows: int = 10,
    parameters: dict[str, Any] | None = None,
    product_key: str | None = None,
    principal: str = "gql-analyst",
    roles: str = "Analyst",
) -> httpx.Response:
    request: dict[str, Any] = {
        "toolVersionId": str(version.id),
        "idempotencyKey": key,
        "maxRows": max_rows,
    }
    if parameters is not None:
        request["parameters"] = parameters
    if product_key is not None:
        request["contextProductKey"] = product_key
    return await http.post(
        "/graphql",
        json={"operationName": "Run", "query": RUN, "variables": {"request": request}},
        headers=_headers(scenario, principal=principal, roles=roles),
    )


def _outcome(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body = response.json()
    assert "errors" not in body, body
    outcome: dict[str, Any] = body["data"]["executeGovernedTool"]
    return outcome


def _error(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["data"]["executeGovernedTool"] is None, body
    [error] = body["errors"]
    extensions: dict[str, Any] = error["extensions"]
    return extensions


async def _count(scenario: _Scenario, model: Any) -> int:
    return int(await scenario.db.scalar(select(func.count()).select_from(model)) or 0)


async def _orders_tool(scenario: _Scenario) -> GovernedToolVersion:
    return await scenario.tool_version(referenced_tables=["retail.orders"], sql_template=ORDERS_SQL)


# ---------------------------------------------------------------------------
# One execution, through the governed path
# ---------------------------------------------------------------------------


async def test_the_mutation_executes_an_approved_tool_once_and_returns_its_rows(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await _orders_tool(scenario)

    outcome = _outcome(await _run(http, scenario, version))

    assert outcome["replayed"] is False
    assert outcome["receipt"]["status"] == "COMPLETED"
    assert outcome["receipt"]["rowCount"] == 2
    assert outcome["result"]["columns"] == ["order_id"]
    assert outcome["result"]["rows"] == [["O-1"], ["O-2"]]
    assert len(executed) == 1
    assert await _count(scenario, ToolExecution) == 1
    # Audited once, by the governed tool path itself (the same action REST records).
    audits = (
        await scenario.db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "tool.execute", AuditEvent.outcome == "SUCCESS"
            )
        )
    ).all()
    assert len(audits) == 1


async def test_a_duplicate_submission_executes_nothing_and_returns_the_receipt(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await _orders_tool(scenario)
    first = _outcome(await _run(http, scenario, version))

    again = _outcome(await _run(http, scenario, version))

    assert again["replayed"] is True
    assert again["receipt"] == first["receipt"]
    assert again["result"] is None, "rows are returned once and never retained"
    assert len(executed) == 1
    assert await _count(scenario, ToolExecution) == 1


async def test_the_same_key_with_other_inputs_is_refused(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await _orders_tool(scenario)
    _outcome(await _run(http, scenario, version, max_rows=10))

    refused = _error(await _run(http, scenario, version, max_rows=11))

    assert refused == {"code": "CONFLICT", "reason": "IDEMPOTENCY_KEY_REUSED"}
    assert len(executed) == 1


async def test_keys_are_scoped_to_the_caller(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    """Another caller's identical key is their own request, not a replay of someone else's."""
    version = await _orders_tool(scenario)
    _outcome(await _run(http, scenario, version))

    other = _outcome(await _run(http, scenario, version, principal="another-analyst"))

    assert other["replayed"] is False
    assert len(executed) == 2


async def test_an_outcome_the_platform_did_not_learn_stays_pending_and_is_not_retried(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    executed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = await _orders_tool(scenario)
    calls: list[UUID] = []

    async def _lost(version_id: UUID, *args: Any, **kwargs: Any) -> Any:
        calls.append(version_id)
        raise ConnectionResetError("the connection dropped mid-execution")

    monkeypatch.setattr("aida.governed_execution.execute_tool_version", _lost)

    first = await _run(http, scenario, version)
    assert first.json()["errors"][0]["extensions"]["code"] == "INTERNAL_ERROR"
    retried = _outcome(await _run(http, scenario, version))

    assert retried["replayed"] is True
    assert retried["receipt"]["status"] == "PENDING"
    assert calls == [version.id], "a retry must read the receipt, not execute again"


# ---------------------------------------------------------------------------
# One execution per operation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "document"),
    [
        (
            "two-aliases",
            "mutation Run($request: ExecuteGovernedToolInput!) { "
            "a: executeGovernedTool(request: $request) { replayed } "
            "b: executeGovernedTool(request: $request) { replayed } }",
        ),
        (
            "fragment-spread-twice",
            "mutation Run($request: ExecuteGovernedToolInput!) { ...E ...E } "
            "fragment E on Mutation { executeGovernedTool(request: $request) { replayed } }",
        ),
        (
            "inline-fragment-with-a-second-root",
            "mutation Run($request: ExecuteGovernedToolInput!) { "
            "executeGovernedTool(request: $request) { replayed } "
            "... on Mutation { again: executeGovernedTool(request: $request) { replayed } } }",
        ),
        (
            "skip-directive-does-not-hide-a-root",
            "mutation Run($request: ExecuteGovernedToolInput!, $s: Boolean = true) { "
            "a: executeGovernedTool(request: $request) { replayed } "
            "b: executeGovernedTool(request: $request) @skip(if: $s) { replayed } }",
        ),
        ("typename-only", "mutation Run { __typename }"),
    ],
)
async def test_one_execution_root_per_operation(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    executed: list[str],
    name: str,
    document: str,
) -> None:
    version = await _orders_tool(scenario)
    variables = {
        "request": {"toolVersionId": str(version.id), "idempotencyKey": "k-" + name, "maxRows": 5}
    }

    response = await http.post(
        "/graphql",
        json={"operationName": "Run", "query": document, "variables": variables},
        headers=_headers(scenario),
    )

    assert response.status_code == 400, response.text
    assert [e["extensions"]["code"] for e in response.json()["errors"]] == [
        "EXECUTION_ROOT_INVALID"
    ]
    assert executed == []
    assert await _count(scenario, GovernedExecutionRequest) == 0


async def test_a_query_cannot_reach_the_execution(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await _orders_tool(scenario)

    response = await http.post(
        "/graphql",
        json={
            "operationName": "Q",
            "query": "query Q($r: ExecuteGovernedToolInput!) { executeGovernedTool(request: $r) "
            "{ replayed } }",
            "variables": {
                "r": {
                    "toolVersionId": str(version.id),
                    "idempotencyKey": "q-key-0001",
                    "maxRows": 1,
                }
            },
        },
        headers=_headers(scenario),
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["extensions"]["code"] == "VALIDATION_FAILED"
    assert executed == []


# ---------------------------------------------------------------------------
# What decides, and what a refusal leaves behind
# ---------------------------------------------------------------------------


async def test_a_caller_without_an_execution_role_claims_no_key(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await _orders_tool(scenario)

    refused = _error(await _run(http, scenario, version, roles="Viewer"))

    assert refused == {"code": "FORBIDDEN", "reason": "ROLE_REQUIRED"}
    assert executed == []
    assert await _count(scenario, GovernedExecutionRequest) == 0


async def test_the_tool_role_binding_still_decides(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    """The version allows Analyst; an AgentDeveloper may execute tools, but not this one."""
    version = await _orders_tool(scenario)

    refused = _error(await _run(http, scenario, version, roles="AgentDeveloper"))

    assert refused["code"] == "FORBIDDEN"
    assert executed == []
    record = await scenario.db.scalar(select(GovernedExecutionRequest))
    assert record is not None and record.status == "REJECTED"


async def test_a_critical_quality_incident_holds_the_tool(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await _orders_tool(scenario)
    scenario.db.add(
        DataQualityIncident(
            id=uuid4(),
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            table_id=scenario.orders.id,
            fingerprint="fp-gql02-critical",
            anomaly_type="FRESHNESS",
            severity="CRITICAL",
            status="OPEN",
            summary="Orders feed has not landed",
            first_observed_at=datetime.now(UTC),
            last_observed_at=datetime.now(UTC),
        )
    )
    await scenario.db.commit()

    refused = _error(await _run(http, scenario, version))

    assert refused == {"code": "CONFLICT", "reason": "QUALITY_HOLD"}
    assert executed == []


async def test_an_unknown_or_foreign_tool_version_is_not_found(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    await _orders_tool(scenario)
    unpublished = SimpleNamespace(id=uuid4())  # an id no version in this organization has

    refused = _error(await _run(http, scenario, unpublished))  # type: ignore[arg-type]

    assert refused == {"code": "NOT_FOUND", "reason": "TOOL_VERSION_NOT_FOUND"}
    assert executed == []


@pytest.mark.parametrize(
    ("key", "max_rows", "reason"),
    [
        ("short", 10, "IDEMPOTENCY_KEY_INVALID"),
        ("has spaces in it", 10, "IDEMPOTENCY_KEY_INVALID"),
        ("gql02-key-rows-0", 0, "MAX_ROWS_OUT_OF_RANGE"),
        ("gql02-key-rows-1", 1_001, "MAX_ROWS_OUT_OF_RANGE"),
    ],
)
async def test_arguments_out_of_range_claim_nothing(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    executed: list[str],
    key: str,
    max_rows: int,
    reason: str,
) -> None:
    version = await _orders_tool(scenario)

    refused = _error(await _run(http, scenario, version, key=key, max_rows=max_rows))

    assert refused == {"code": "INVALID_ARGUMENT", "reason": reason}
    assert executed == []
    assert await _count(scenario, GovernedExecutionRequest) == 0


# ---------------------------------------------------------------------------
# Product scope
# ---------------------------------------------------------------------------


async def test_through_a_product_the_tool_must_be_eligible(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await _orders_tool(scenario)
    await scenario.product(table_ids=[scenario.orders.id])  # declares no eligible tool

    refused = _error(await _run(http, scenario, version, product_key=PRODUCT_KEY))

    assert refused == {"code": "FORBIDDEN", "reason": "CONTEXT_PRODUCT_TOOL_NOT_ELIGIBLE"}
    assert executed == []
    denied = await scenario.db.scalar(
        select(AuditEvent).where(
            AuditEvent.action == "tool.execute", AuditEvent.outcome == "DENIED"
        )
    )
    assert denied is not None and denied.details["reason"] == "CONTEXT_PRODUCT_TOOL_NOT_ELIGIBLE"


async def test_through_a_product_the_tools_dependencies_must_fit_it(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await scenario.tool_version(
        referenced_tables=["retail.secret_ledger"],
        sql_template="SELECT l.amount FROM retail.secret_ledger AS l",
    )
    await scenario.product(table_ids=[scenario.orders.id], tool_version_ids=[version.id])

    refused = _error(await _run(http, scenario, version, product_key=PRODUCT_KEY))

    assert refused == {
        "code": "FORBIDDEN",
        "reason": "CONTEXT_PRODUCT_TOOL_DEPENDENCY_OUT_OF_SCOPE",
    }
    assert executed == []


async def test_through_an_unknown_product_is_not_found(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await _orders_tool(scenario)

    refused = _error(await _run(http, scenario, version, product_key="no-such-product"))

    assert refused == {"code": "NOT_FOUND", "reason": "CONTEXT_PRODUCT_NOT_FOUND"}
    assert executed == []


async def test_through_a_product_that_names_the_tool_it_runs_and_is_recorded(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await _orders_tool(scenario)
    product = await scenario.product(table_ids=[scenario.orders.id], tool_version_ids=[version.id])

    outcome = _outcome(await _run(http, scenario, version, product_key=PRODUCT_KEY))

    assert outcome["receipt"]["status"] == "COMPLETED"
    assert outcome["receipt"]["contextProductVersionId"] == str(product.id)
    assert len(executed) == 1


# ---------------------------------------------------------------------------
# Receipts, and what is stored
# ---------------------------------------------------------------------------


async def test_a_receipt_is_readable_by_its_caller_and_oversight_only(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await _orders_tool(scenario)
    receipt_id = _outcome(await _run(http, scenario, version))["receipt"]["id"]

    async def read(principal: str, roles: str) -> httpx.Response:
        return await http.post(
            "/graphql",
            json={"operationName": "Receipt", "query": RECEIPT, "variables": {"id": receipt_id}},
            headers=_headers(scenario, principal=principal, roles=roles),
        )

    own = (await read("gql-analyst", "Analyst")).json()
    other = (await read("another-analyst", "Analyst")).json()
    auditor = (await read("auditor-1", "Auditor")).json()

    assert own["data"]["governedExecution"]["status"] == "COMPLETED"
    assert own["data"]["governedExecution"]["rowCount"] == 2
    assert other["data"]["governedExecution"] is None
    assert other["errors"][0]["extensions"] == {
        "code": "NOT_FOUND",
        "reason": "EXECUTION_RECEIPT_NOT_FOUND",
    }
    assert auditor["data"]["governedExecution"]["id"] == receipt_id
    assert len(executed) == 1, "reading a receipt executes nothing"


async def test_no_parameter_value_is_stored(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = GovernedToolVersion(
        organization_id=scenario.organization.id,
        tool_id=scenario.tool.id,
        version=7,
        status="PUBLISHED",
        name="Order by id",
        description="One order",
        datasource_id=scenario.datasource.id,
        sql_template="SELECT o.order_id FROM retail.orders AS o WHERE o.order_id = :order_id",
        referenced_tables=["retail.orders"],
        parameter_schema=[{"name": "order_id", "parameter_type": "STRING", "required": True}],
        allowed_roles=["Analyst"],
        fingerprint=f"fp-tool-{uuid4().hex[:8]}",
        created_by="tool-dev",
    )
    scenario.db.add(version)
    await scenario.db.commit()

    outcome = _outcome(
        await _run(http, scenario, version, parameters={"order_id": PARAMETER_VALUE})
    )

    assert outcome["receipt"]["status"] == "COMPLETED"
    record = await scenario.db.scalar(select(GovernedExecutionRequest))
    assert record is not None
    stored = " ".join(str(getattr(record, column.key)) for column in record.__table__.columns)
    assert PARAMETER_VALUE not in stored
    audits = (await scenario.db.scalars(select(AuditEvent))).all()
    assert all(PARAMETER_VALUE not in str(audit.details) for audit in audits)


async def test_another_organizations_tool_version_is_not_found(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    """INV-5: a version id from another tenant is answered as unknown, and nothing runs."""
    other = await _Scenario(scenario.db).build()
    foreign = await other.tool_version(referenced_tables=["retail.orders"], sql_template=ORDERS_SQL)

    refused = _error(await _run(http, scenario, foreign))

    assert refused == {"code": "NOT_FOUND", "reason": "TOOL_VERSION_NOT_FOUND"}
    assert executed == []
    assert await _count(scenario, GovernedExecutionRequest) == 0


async def test_an_agent_without_a_contract_is_refused_as_on_rest(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    """R11-C6's contract gate lives in the shared path, so the transport cannot skip it."""
    version = await _orders_tool(scenario)
    headers = {
        **_headers(scenario, principal="agent:uncontracted-bot"),
        "X-Principal-Type": "AGENT",
    }

    response = await http.post(
        "/graphql",
        json={
            "operationName": "Run",
            "query": RUN,
            "variables": {
                "request": {
                    "toolVersionId": str(version.id),
                    "idempotencyKey": "agent-key-0001",
                    "maxRows": 5,
                }
            },
        },
        headers=headers,
    )

    refused = _error(response)
    assert refused["code"] == "FORBIDDEN"
    assert refused["reason"] != "EXECUTION_FORBIDDEN", "the contract's own code passes through"
    assert executed == []
    record = await scenario.db.scalar(select(GovernedExecutionRequest))
    assert record is not None and record.status == "REJECTED"


async def test_a_sensitive_column_is_masked_exactly_as_rest_masks_it(
    http: httpx.AsyncClient, scenario: _Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Masking parity: the same tool through GraphQL and REST masks the same column the same
    way, because both run the one governed path -- and the raw value reaches neither."""
    raw_email = "person-7781@example.test"
    scenario.db.add(
        MetadataColumn(
            organization_id=scenario.organization.id,
            table_id=scenario.customer.id,
            name="email",
            ordinal_position=1,
            physical_type="text",
            nullable=True,
            classification="PII",
            fingerprint="fp-customer-email",
        )
    )
    await scenario.db.commit()
    version = await scenario.tool_version(
        referenced_tables=["retail.customer"],
        sql_template="SELECT c.email FROM retail.customer AS c",
    )
    monkeypatch.setattr(
        "aida.query_gateway.open_execution_session",
        lambda connector_type, dsn: FakeSqlExecutor(({"email": raw_email},)),
    )

    graphql = await _run(http, scenario, version)
    rest = await http.post(
        f"/v1/tool-versions/{version.id}/execute",
        json={"parameters": {}, "max_rows": 10},
        headers=_headers(scenario),
    )

    assert rest.status_code == 200, rest.text
    result = _outcome(graphql)["result"]
    rest_execution = rest.json()["execution"]
    assert result["maskedColumns"] == rest_execution["masked_columns"] == ["email"]
    assert result["rows"] == [[rest_execution["rows"][0]["email"]]]
    assert raw_email not in graphql.text and raw_email not in rest.text


# ---------------------------------------------------------------------------
# Settling a PENDING receipt from evidence, never by executing again
# ---------------------------------------------------------------------------


async def _age_receipt(scenario: _Scenario) -> None:
    """Move the only receipt past the execution deadline: it can no longer be in flight."""
    record = await scenario.db.scalar(select(GovernedExecutionRequest))
    assert record is not None
    record.created_at = datetime.now(UTC) - timedelta(hours=1)
    await scenario.db.commit()


async def test_a_pending_receipt_whose_execution_never_started_settles_as_not_started(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    executed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version = await _orders_tool(scenario)
    calls: list[UUID] = []

    async def _lost(version_id: UUID, *args: Any, **kwargs: Any) -> Any:
        calls.append(version_id)
        raise ConnectionResetError("dropped before the tool path wrote anything")

    monkeypatch.setattr("aida.governed_execution.execute_tool_version", _lost)
    await _run(http, scenario, version)
    await _age_receipt(scenario)

    settled = _outcome(await _run(http, scenario, version))

    assert settled["replayed"] is True
    assert settled["receipt"]["status"] == "FAILED"
    assert settled["receipt"]["outcomeCode"] == "NOT_STARTED"
    assert settled["receipt"]["toolExecutionId"] is None
    assert calls == [version.id] and executed == []


async def test_a_pending_receipt_whose_execution_finished_settles_from_that_execution(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    executed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source ran the statement and the response was lost on the way back: the receipt
    settles as COMPLETED from the recorded execution, and nothing runs a second time."""
    from aida.tool_api import execute_tool_version as real_execute

    version = await _orders_tool(scenario)

    async def _answered_then_lost(*args: Any, **kwargs: Any) -> Any:
        await real_execute(*args, **kwargs)
        raise ConnectionResetError("the response never reached the caller")

    monkeypatch.setattr("aida.governed_execution.execute_tool_version", _answered_then_lost)
    first = await _run(http, scenario, version)
    assert first.json()["errors"][0]["extensions"]["code"] == "INTERNAL_ERROR"
    young = _outcome(await _run(http, scenario, version))
    assert young["receipt"]["status"] == "PENDING", "not settled while it could be in flight"
    await _age_receipt(scenario)

    receipt_id = young["receipt"]["id"]
    read = await http.post(
        "/graphql",
        json={"operationName": "Receipt", "query": RECEIPT, "variables": {"id": receipt_id}},
        headers=_headers(scenario),
    )

    receipt = read.json()["data"]["governedExecution"]
    assert receipt["status"] == "COMPLETED"
    assert receipt["rowCount"] == 2 and receipt["queryExecutionId"] is not None
    assert len(executed) == 1, "settling reads the evidence; it does not execute"


async def test_a_pending_receipt_whose_execution_is_unfinished_stays_pending(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    executed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded execution that never finished may or may not have reached the source: the
    one case the platform cannot settle, so it is left for a person."""
    version = await _orders_tool(scenario)

    async def _lost(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionResetError("dropped mid-execution")

    monkeypatch.setattr("aida.governed_execution.execute_tool_version", _lost)
    await _run(http, scenario, version)
    record = await scenario.db.scalar(select(GovernedExecutionRequest))
    assert record is not None and record.tool_execution_id is not None
    scenario.db.add(
        ToolExecution(
            id=record.tool_execution_id,
            organization_id=scenario.organization.id,
            tool_version_id=version.id,
            principal_id="gql-analyst",
            parameter_fingerprint="f" * 64,
            status="RECEIVED",
        )
    )
    await scenario.db.commit()
    await _age_receipt(scenario)

    settled = _outcome(await _run(http, scenario, version))

    assert settled["receipt"]["status"] == "PENDING"
    assert executed == []
