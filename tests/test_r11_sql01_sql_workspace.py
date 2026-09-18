"""R11-SQL01: SQL a person reviews before it runs -- generated or pasted, one lifecycle.

The acceptance, one test (or a few) per clause:

* generated SQL causes zero executions before Run, and pasted SQL takes the same path;
* an edited statement -- or another row limit -- needs validating again;
* an expired receipt, or someone else's, cannot be run;
* the context product and access are checked again at Run and fail closed;
* forbidden SQL earns no receipt, so it has nothing to run on;
* a receipt runs once, and a second Run is told which execution it produced.

Every test drives the HTTP routes against a real application and an in-memory database, with
the connector doubled so it records every statement it executes: "zero executions" is asserted
on the connector and on the `query_execution` table, not inferred from a status code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.agent_orchestrator import DRAFT_TOOL_ANSWERS, DRAFTED, GovernedAgentOrchestrator
from aida.connectors.base import QueryResult
from aida.main import app
from aida.models import AgentRun, AuditEvent, QueryExecution
from aida.query_gateway import QueryExecutionGateway
from aida.sql_workspace import (
    RECEIPT_ALREADY_USED,
    SqlWorkspaceRefused,
    run_receipt,
    statement_digest,
)
from aida.sql_workspace_models import SqlDraftReceipt
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session
from tests.support.doubles import FakeSqlExecutor, security_context
from tests.test_f01_context_product_execution_boundary import (
    PRODUCT_KEY,
    _FakeModelGateway,
    _one_route,
    _Scenario,
)

pytestmark = pytest.mark.asyncio

ORDERS_SQL = "SELECT o.order_id FROM retail.orders AS o WHERE o.order_id = 'LITERAL-7731'"
LITERAL = "LITERAL-7731"


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
    """Records each statement *executed*. Validation's dry-run estimate opens a connector
    too -- an EXPLAIN, which returns no rows -- so an opened connector proves nothing."""

    def __init__(self, executed: list[str]) -> None:
        super().__init__(({"order_id": "O-1"},))
        self._executed = executed

    async def execute_read_query(self, sql: str, *, timeout_seconds: int) -> QueryResult:
        self._executed.append(sql)
        return await super().execute_read_query(sql, timeout_seconds=timeout_seconds)


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement the source was asked to execute -- the proof that something ran."""
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
    async with httpx.AsyncClient(transport=transport, base_url="http://sql01.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _headers(scenario: _Scenario, principal: str = "reviewing-analyst") -> dict[str, str]:
    return {
        "X-Principal-Id": principal,
        "X-Principal-Type": "USER",
        "X-Roles": "Analyst",
        "X-Business-Purpose": "Review SQL before running it",
        "X-Organization-Id": str(scenario.organization.id),
    }


async def _draft(http: httpx.AsyncClient, scenario: _Scenario, **body: Any) -> httpx.Response:
    return await http.post(
        f"/v1/datasources/{scenario.datasource.id}/sql-drafts",
        json=body,
        headers=_headers(scenario),
    )


async def _run(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    receipt_id: str,
    *,
    principal: str = "reviewing-analyst",
    **body: Any,
) -> httpx.Response:
    return await http.post(
        f"/v1/sql-drafts/{receipt_id}/run", json=body, headers=_headers(scenario, principal)
    )


async def _executions(scenario: _Scenario) -> int:
    return int(await scenario.db.scalar(select(func.count()).select_from(QueryExecution)) or 0)


def _model(monkeypatch: pytest.MonkeyPatch, sql: str, **settings: Any) -> _FakeModelGateway:
    """Route the draft endpoint's orchestrator to a model double returning `sql`."""
    model = _FakeModelGateway(sql)
    configured = Settings(_env_file=None, agent_retrieval_limit=10, **settings)

    def _build(_settings: Settings) -> GovernedAgentOrchestrator:
        orchestrator = GovernedAgentOrchestrator(configured)
        orchestrator.model_gateway = model  # type: ignore[assignment]
        orchestrator._approved_model_routes = (  # type: ignore[method-assign]
            lambda session, organization_id: _one_route()
        )
        return orchestrator

    monkeypatch.setattr("aida.sql_workspace_api.GovernedAgentOrchestrator", _build)
    return model


# ---------------------------------------------------------------------------
# Nothing runs before Run
# ---------------------------------------------------------------------------


async def test_pasted_sql_is_validated_and_receipted_without_executing(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    response = await _draft(http, scenario, sql=ORDERS_SQL)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["origin"] == "PASTED"
    assert payload["sql"] is None, "the caller holds pasted text; it is not echoed back"
    assert payload["validation"]["valid"] is True
    receipt = payload["receipt"]
    assert receipt["status"] == "VALIDATED"
    assert receipt["referenced_tables"] == ["retail.orders"]
    assert executed == [] and await _executions(scenario) == 0


async def test_generated_sql_is_drafted_validated_and_never_run_before_run(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    executed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model(monkeypatch, "SELECT o.order_id FROM retail.orders AS o")

    response = await _draft(http, scenario, question="which orders are there")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["origin"] == "GENERATED"
    assert payload["sql"] == "SELECT o.order_id FROM retail.orders AS o"
    assert payload["receipt"]["origin"] == "GENERATED"
    assert payload["receipt"]["agent_run_id"] == payload["agent_run_id"]
    assert len(model.calls) == 1
    run = await scenario.db.get(AgentRun, UUID(payload["agent_run_id"]))
    assert run is not None and run.status == DRAFTED
    assert run.plan_evidence["sql_draft"] == {"drafted": True, "executed": False}
    assert executed == [] and await _executions(scenario) == 0

    ran = await _run(http, scenario, payload["receipt"]["id"], sql=payload["sql"])

    assert ran.status_code == 200, ran.text
    assert ran.json()["execution"]["rows"] == [{"order_id": "O-1"}]
    assert ran.json()["receipt"]["status"] == "EXECUTED"
    assert len(executed) == 1 and await _executions(scenario) == 1


async def test_a_draft_a_governed_tool_answers_hands_out_no_sql(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    executed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running a tool's rendered SQL as ad-hoc SQL would drop the tool's own governance."""
    await scenario.tool_version(
        referenced_tables=["retail.orders"],
        sql_template="SELECT o.order_id FROM retail.orders AS o",
    )
    model = _model(
        monkeypatch, "SELECT o.order_id FROM retail.orders AS o", agent_tool_match_threshold=0.0
    )

    response = await _draft(http, scenario, question="order lookup")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["sql"] is None and payload["receipt"] is None
    assert payload["reason"] == DRAFT_TOOL_ANSWERS
    assert payload["selected_tool_version_id"]
    assert model.calls == []
    assert executed == [] and await _executions(scenario) == 0


async def test_generated_sql_past_the_product_is_refused_at_draft(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    executed: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await scenario.product(table_ids=[scenario.orders.id])
    _model(monkeypatch, "SELECT l.amount FROM retail.secret_ledger AS l")

    response = await _draft(
        http, scenario, question="ledger amounts", context_product_key=PRODUCT_KEY
    )

    assert response.status_code == 422
    assert executed == [] and await _executions(scenario) == 0
    assert await scenario.db.scalar(select(func.count()).select_from(SqlDraftReceipt)) == 0


# ---------------------------------------------------------------------------
# Forbidden SQL has nothing to run on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM retail.orders",
        "SELECT x.secret FROM retail.not_in_catalog AS x",
        "SELECT pg_sleep(10)",
    ],
    ids=["write", "unknown-table", "unauthorized-function"],
)
async def test_forbidden_sql_gets_findings_and_no_receipt(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str], sql: str
) -> None:
    response = await _draft(http, scenario, sql=sql)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["validation"]["valid"] is False
    assert payload["validation"]["findings"]
    assert payload["receipt"] is None
    assert executed == [] and await _executions(scenario) == 0


# ---------------------------------------------------------------------------
# A receipt binds the statement, the caller and the moment
# ---------------------------------------------------------------------------


async def _receipt(http: httpx.AsyncClient, scenario: _Scenario, **body: Any) -> str:
    response = await _draft(http, scenario, **body)
    assert response.status_code == 200, response.text
    receipt = response.json()["receipt"]
    assert receipt is not None
    return str(receipt["id"])


async def test_an_edited_statement_needs_validating_again(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL)

    edited = await _run(
        http, scenario, receipt_id, sql=ORDERS_SQL.replace("LITERAL-7731", "LITERAL-7732")
    )
    other_limit = await _run(http, scenario, receipt_id, sql=ORDERS_SQL, max_rows=5)

    for refused in (edited, other_limit):
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "REVALIDATION_REQUIRED"
    assert executed == [] and await _executions(scenario) == 0
    # A refused edit spends nothing: the statement that was validated still runs.
    original = await _run(http, scenario, receipt_id, sql=ORDERS_SQL)
    assert original.status_code == 200, original.text


async def test_an_expired_receipt_cannot_run(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL)
    receipt = await scenario.db.get(SqlDraftReceipt, UUID(receipt_id))
    assert receipt is not None
    receipt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await scenario.db.commit()

    response = await _run(http, scenario, receipt_id, sql=ORDERS_SQL)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RECEIPT_EXPIRED"
    assert executed == [] and await _executions(scenario) == 0


async def test_someone_elses_receipt_cannot_run(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL)

    response = await _run(http, scenario, receipt_id, principal="another-analyst", sql=ORDERS_SQL)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "RECEIPT_NOT_YOURS"
    assert executed == [] and await _executions(scenario) == 0


async def test_a_receipt_runs_once_and_the_second_run_names_the_first_execution(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL)

    first = await _run(http, scenario, receipt_id, sql=ORDERS_SQL)
    second = await _run(http, scenario, receipt_id, sql=ORDERS_SQL)

    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert second.json()["detail"] == {
        "code": RECEIPT_ALREADY_USED,
        "execution_id": first.json()["execution"]["execution_id"],
    }
    assert len(executed) == 1 and await _executions(scenario) == 1


async def test_a_run_that_loses_the_claim_executes_nothing(
    scenario: _Scenario, executed: list[str]
) -> None:
    """Two Runs racing: the loser read VALIDATED, but the conditional update decides."""
    context = security_context(
        organization_id=scenario.organization.id, principal_id="reviewing-analyst"
    )
    receipt = SqlDraftReceipt(
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        principal_id=context.principal_id,
        principal_type=context.principal_type,
        origin="PASTED",
        status="VALIDATED",
        statement_digest=statement_digest(
            sql=ORDERS_SQL, max_rows=None, context_product_version_id=None, workspace_id=None
        ),
        redaction_status="REDACTED",
        referenced_tables=["retail.orders"],
        finding_codes=[],
        estimate={},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    scenario.db.add(receipt)
    await scenario.db.commit()
    # The winner's claim, behind the loser's back: the loser's identity map still says
    # VALIDATED, so only the conditional update can stop it.
    await scenario.db.execute(
        update(SqlDraftReceipt)
        .where(SqlDraftReceipt.id == receipt.id)
        .values(status="EXECUTING")
        .execution_options(synchronize_session=False)
    )

    with pytest.raises(SqlWorkspaceRefused) as refused:
        await run_receipt(
            scenario.db,
            QueryExecutionGateway(Settings(_env_file=None)),
            receipt_id=receipt.id,
            context=context,
            correlation_id="corr-race",
            sql=ORDERS_SQL,
            max_rows=None,
            workspace_id=None,
            scope=None,
        )

    assert refused.value.code == RECEIPT_ALREADY_USED
    assert executed == [] and await _executions(scenario) == 0


# ---------------------------------------------------------------------------
# The product and access are decided again at Run
# ---------------------------------------------------------------------------


async def test_a_receipt_under_a_product_cannot_run_without_it(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    await scenario.product(table_ids=[scenario.orders.id])
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL, context_product_key=PRODUCT_KEY)

    response = await _run(http, scenario, receipt_id, sql=ORDERS_SQL)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REVALIDATION_REQUIRED"
    assert executed == [] and await _executions(scenario) == 0


async def test_a_product_withdrawn_from_the_caller_fails_closed(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    version = await scenario.product(table_ids=[scenario.orders.id])
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL, context_product_key=PRODUCT_KEY)
    version.allowed_consumer_roles = ["DataSteward"]
    await scenario.db.commit()

    response = await _run(
        http, scenario, receipt_id, sql=ORDERS_SQL, context_product_key=PRODUCT_KEY
    )

    assert response.status_code == 404
    assert executed == [] and await _executions(scenario) == 0


async def test_a_product_republished_since_validation_needs_validating_again(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    first = await scenario.product(table_ids=[scenario.orders.id])
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL, context_product_key=PRODUCT_KEY)
    first.status = "SUPERSEDED"
    await scenario.product(table_ids=[scenario.orders.id], version_number=2)
    await scenario.db.commit()

    response = await _run(
        http, scenario, receipt_id, sql=ORDERS_SQL, context_product_key=PRODUCT_KEY
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "REVALIDATION_REQUIRED"
    assert executed == [] and await _executions(scenario) == 0


async def test_access_revoked_after_validation_stops_the_run(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    """A receipt is a precondition, never a bypass: the gateway authorizes again at Run."""
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL)
    scenario.orders.status = "DELETED"
    await scenario.db.commit()

    response = await _run(http, scenario, receipt_id, sql=ORDERS_SQL)

    assert response.status_code in (403, 422), response.text
    assert executed == []
    receipt = await scenario.db.get(SqlDraftReceipt, UUID(receipt_id))
    assert receipt is not None
    await scenario.db.refresh(receipt)
    assert receipt.status == "FAILED"
    again = await _run(http, scenario, receipt_id, sql=ORDERS_SQL)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == RECEIPT_ALREADY_USED


# ---------------------------------------------------------------------------
# INV-6: the statement text stays with the caller
# ---------------------------------------------------------------------------


async def test_no_statement_literal_is_stored_or_audited(
    http: httpx.AsyncClient, scenario: _Scenario, executed: list[str]
) -> None:
    receipt_id = await _receipt(http, scenario, sql=ORDERS_SQL)
    ran = await _run(http, scenario, receipt_id, sql=ORDERS_SQL)
    assert ran.status_code == 200, ran.text

    receipt = await scenario.db.get(SqlDraftReceipt, UUID(receipt_id))
    assert receipt is not None
    stored = " ".join(
        str(value)
        for value in (
            receipt.redacted_sql,
            receipt.referenced_tables,
            receipt.finding_codes,
            receipt.estimate,
        )
    )
    assert LITERAL not in stored
    audits = (
        await scenario.db.scalars(select(AuditEvent).where(AuditEvent.action.like("sql_draft.%")))
    ).all()
    assert {audit.action for audit in audits} == {"sql_draft.validated", "sql_draft.run"}
    assert all(LITERAL not in str(audit.details) for audit in audits)


async def test_the_digest_binds_every_run_input() -> None:
    base: dict[str, Any] = {
        "sql": ORDERS_SQL,
        "max_rows": None,
        "context_product_version_id": None,
        "workspace_id": None,
    }
    digest = statement_digest(**base)
    for field, value in (
        ("sql", ORDERS_SQL + " "),
        ("max_rows", 10),
        ("context_product_version_id", uuid4()),
        ("workspace_id", uuid4()),
    ):
        assert statement_digest(**{**base, field: value}) != digest, field


async def test_the_draft_request_takes_exactly_one_input(
    http: httpx.AsyncClient, scenario: _Scenario
) -> None:
    both = await _draft(http, scenario, sql=ORDERS_SQL, question="orders")
    neither = await _draft(http, scenario)

    assert both.status_code == 422 and neither.status_code == 422


async def test_an_unknown_receipt_is_not_found(
    http: httpx.AsyncClient, scenario: _Scenario
) -> None:
    response = await _run(http, scenario, str(uuid4()), sql=ORDERS_SQL)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "RECEIPT_NOT_FOUND"
