"""R11-MP26: governed follow-up questions.

The redaction rules are tested directly; the routes are driven over ASGI with
the orchestrator replaced by a stand-in that records what it was given and
answers as a completed run would, so the tests prove what reaches a follow-up,
who may continue a conversation, and what is kept.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import aida.api as api_module
from aida.config import Settings, get_settings
from aida.conversations import (
    EarlierTurn,
    redact_with_earlier,
    stale_conversations_stmt,
    uses_an_earlier_value,
)
from aida.db import get_session

# Imported at module scope, so every router is registered before `db` builds the schema.
from aida.main import app
from aida.models import AgentRun, AskConversation, AuditEvent, QueryExecution, utc_now
from aida.question_redaction import restore_values
from tests.test_agent_orchestrator_retrieval_wiring import _Scenario, db  # noqa: F401

# ---------------------------------------------------------------------------
# Redaction across turns
# ---------------------------------------------------------------------------


def test_a_value_in_the_question_and_the_earlier_sql_gets_one_token() -> None:
    earlier = (
        EarlierTurn(
            1,
            "balance of account ATLAS_VALUE_1",
            "SELECT balance FROM accounts WHERE account_no = '004512339871'",
        ),
    )
    current, shown = redact_with_earlier("same for 004512339871 last month", earlier)
    assert "004512339871" not in current.text
    assert "004512339871" not in shown[0]["sql"]
    token = next(iter(current.values))
    assert token in current.text and token in shown[0]["sql"]
    # What the model writes with that token restores to the real value, locally.
    assert "'004512339871'" in restore_values(f"WHERE account_no = '{token}'", current.values)


def test_an_earlier_questions_own_tokens_are_renamed_and_refused_if_used() -> None:
    earlier = (EarlierTurn(3, "balance of account ATLAS_VALUE_1", "SELECT 1"),)
    _current, shown = redact_with_earlier("and last month?", earlier)
    assert shown[0]["question"] == "balance of account ATLAS_EARLIER_3_1"
    assert uses_an_earlier_value("WHERE account_no = 'ATLAS_EARLIER_3_1'")
    assert not uses_an_earlier_value("WHERE account_no = 'ATLAS_VALUE_1'")


def test_no_earlier_turns_is_the_ordinary_redaction() -> None:
    current, shown = redact_with_earlier("account 004512339871", ())
    assert current.text == "account ATLAS_VALUE_1" and shown == []


# ---------------------------------------------------------------------------
# Over the routes
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> AsyncIterator[_Scenario]:  # noqa: F811
    yield await _Scenario(db).build()


@dataclass
class _Calls:
    earlier: list[tuple[EarlierTurn, ...]]
    questions: list[str]


@pytest_asyncio.fixture
async def calls(scenario: _Scenario, monkeypatch: pytest.MonkeyPatch) -> _Calls:
    """Replace the orchestrator: each run records what it was given, then leaves a
    completed run behind whose execution ran `SELECT <n>`."""
    seen = _Calls(earlier=[], questions=[])

    class _Orchestrator:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def run(self, session: AsyncSession, **kwargs: Any) -> Any:
            seen.earlier.append(kwargs["earlier_turns"])
            seen.questions.append(kwargs["question"])
            datasource = kwargs["datasource"]
            execution = QueryExecution(
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                principal_id="ask-analyst",
                status="SUCCEEDED",
                dialect="postgres",
                sql_hash="0" * 64,
                # Stored text only; nothing executes it.
                normalized_sql=f"SELECT {len(seen.questions)} FROM orders WHERE id = '004512339871'",  # noqa: S608, E501
            )
            session.add(execution)
            await session.flush()
            run = AgentRun(
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                principal_id="ask-analyst",
                question_hash="0" * 64,
                generation_source="MODEL_GATEWAY",
                status="COMPLETED",
                query_execution_id=execution.id,
            )
            session.add(run)
            await session.commit()
            gateway = SimpleNamespace(
                execution=execution,
                masked_columns=(),
                rows=(),
                applied_row_limit=None,
                row_limit_source=None,
            )
            return SimpleNamespace(agent_run=run, gateway_result=gateway, explanation="done")

    monkeypatch.setattr(api_module, "GovernedAgentOrchestrator", _Orchestrator)
    return seen


@pytest_asyncio.fixture
async def http(scenario: _Scenario) -> AsyncIterator[httpx.AsyncClient]:
    previous = dict(app.dependency_overrides)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield scenario.db

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ask.test"
    ) as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _headers(scenario: _Scenario, principal: str = "ask-analyst") -> dict[str, str]:
    return {
        "X-Principal-Id": principal,
        "X-Principal-Type": "USER",
        "X-Roles": "Analyst",
        "X-Business-Purpose": "Follow-up questions",
        "X-Organization-Id": str(scenario.organization.id),
    }


async def _ask(
    http: httpx.AsyncClient,
    scenario: _Scenario,
    question: str,
    conversation_id: UUID | str | None = None,
    principal: str = "ask-analyst",
) -> httpx.Response:
    body: dict[str, Any] = {"question": question}
    if conversation_id is not None:
        body["conversation_id"] = str(conversation_id)
    return await http.post(
        f"/v1/datasources/{scenario.datasource.id}/agent-analyses",
        json=body,
        headers=_headers(scenario, principal),
    )


@pytest.mark.asyncio
async def test_a_first_question_starts_a_conversation_and_a_follow_up_carries_it(
    http: httpx.AsyncClient, scenario: _Scenario, calls: _Calls
) -> None:
    first = await _ask(http, scenario, "orders for account 004512339871")
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]
    assert first.json()["conversation_turn"] == 1
    assert calls.earlier[0] == ()

    follow = await _ask(http, scenario, "now by region", conversation_id)
    assert follow.status_code == 200, follow.text
    assert follow.json()["conversation_id"] == conversation_id
    assert follow.json()["conversation_turn"] == 2
    [carried] = calls.earlier[1]
    # The earlier question as stored (redacted) and the SQL its execution ran.
    assert carried.question == "orders for account ATLAS_VALUE_1"
    assert carried.sql.startswith("SELECT 1 FROM orders")

    stored = await scenario.db.get(AskConversation, UUID(conversation_id))
    assert stored is not None
    assert [t["question"] for t in stored.turns] == [
        "orders for account ATLAS_VALUE_1",
        "now by region",
    ]
    # No raw value is kept anywhere on the conversation.
    assert "004512339871" not in str(stored.turns) + stored.title


@pytest.mark.asyncio
async def test_someone_elses_conversation_does_not_exist_for_you(
    http: httpx.AsyncClient, scenario: _Scenario, calls: _Calls
) -> None:
    first = await _ask(http, scenario, "orders")
    conversation_id = first.json()["conversation_id"]
    stranger = await _ask(http, scenario, "and yesterday", conversation_id, principal="other")
    assert stranger.status_code == 404
    assert len(calls.questions) == 1  # refused before anything ran
    read = await http.get(
        f"/v1/conversations/{conversation_id}", headers=_headers(scenario, "other")
    )
    assert read.status_code == 404
    listed = await http.get("/v1/conversations", headers=_headers(scenario, "other"))
    assert listed.json() == []


@pytest.mark.asyncio
async def test_an_unknown_conversation_is_refused_before_the_run(
    http: httpx.AsyncClient, scenario: _Scenario, calls: _Calls
) -> None:
    response = await _ask(http, scenario, "orders", uuid4())
    assert response.status_code == 404
    assert calls.questions == []


@pytest.mark.asyncio
async def test_the_owner_lists_reads_and_deletes_their_conversation(
    http: httpx.AsyncClient, scenario: _Scenario, calls: _Calls
) -> None:
    first = await _ask(http, scenario, "orders")
    conversation_id = first.json()["conversation_id"]
    await _ask(http, scenario, "by month", conversation_id)

    listed = await http.get("/v1/conversations", headers=_headers(scenario))
    assert [c["id"] for c in listed.json()] == [conversation_id]
    assert listed.json()[0]["turn_count"] == 2

    read = await http.get(f"/v1/conversations/{conversation_id}", headers=_headers(scenario))
    assert [t["question"] for t in read.json()["turns"]] == ["orders", "by month"]

    deleted = await http.delete(f"/v1/conversations/{conversation_id}", headers=_headers(scenario))
    assert deleted.status_code == 204
    assert await scenario.db.get(AskConversation, UUID(conversation_id)) is None
    audit = await scenario.db.scalar(
        select(AuditEvent).where(AuditEvent.action == "conversation.delete")
    )
    assert audit is not None and audit.details == {"turns": 2}
    # The runs stay: they are the audit record and never held the question.
    runs = (await scenario.db.scalars(select(AgentRun))).all()
    assert len(runs) == 2


@pytest.mark.asyncio
async def test_a_full_conversation_asks_for_a_new_one(
    http: httpx.AsyncClient, scenario: _Scenario, calls: _Calls
) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, conversation_max_turns=2
    )
    first = await _ask(http, scenario, "orders")
    conversation_id = first.json()["conversation_id"]
    assert (await _ask(http, scenario, "by month", conversation_id)).status_code == 200
    full = await _ask(http, scenario, "by week", conversation_id)
    assert full.status_code == 409
    assert "start a new one" in full.json()["detail"]


def test_the_reaper_rule_selects_conversations_past_retention() -> None:
    from datetime import timedelta

    statement = str(stale_conversations_stmt(utc_now(), timedelta(days=30)))
    assert "ask_conversation.last_turn_at <" in statement


# ---------------------------------------------------------------------------
# In the orchestrator
# ---------------------------------------------------------------------------


def _follow_up_request(earlier: tuple[EarlierTurn, ...]) -> Any:
    from aida.models import DataSource
    from aida.orchestration_stages import OrchestrationRequest
    from tests.support.doubles import security_context

    org = uuid4()
    datasource = DataSource(
        id=uuid4(),
        organization_id=org,
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="conversation-fixture",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://x",
        status="ACTIVE",
    )
    return OrchestrationRequest(
        datasource=datasource,
        context=security_context(organization_id=org),
        correlation_id="c1",
        question="same for 004512339871 by month",
        candidate_sql=None,
        preferred_tool_version_id=None,
        tool_parameters={},
        requested_limit=None,
        earlier_turns=earlier,
    )


def test_the_orchestrator_redacts_the_follow_up_with_the_earlier_sql() -> None:
    from aida.agent_orchestrator import GovernedAgentOrchestrator

    earlier = (
        EarlierTurn(1, "orders by region", "SELECT * FROM orders WHERE acct = '004512339871'"),
    )
    orchestrator = GovernedAgentOrchestrator(Settings(_env_file=None))
    request = _follow_up_request(earlier)
    question = orchestrator._question_for_providers(request)
    shown = orchestrator._earlier_turns_for_providers(request)
    token = next(iter(question.values))
    assert token in question.text and token in shown[0]["sql"]
    assert "004512339871" not in question.text + shown[0]["sql"]
    # Retrieval searches on the previous question as well: "by month" alone names nothing.
    assert orchestrator._retrieval_question(request).startswith("orders by region ")


@pytest.mark.asyncio
async def test_a_statement_that_needs_an_earlier_value_is_refused(
    db: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aida.agent_orchestrator import AgentPolicyRejected, GovernedAgentOrchestrator
    from aida.orchestration_stages import ValidatedStatement

    orchestrator = GovernedAgentOrchestrator(Settings(_env_file=None))
    statement = ValidatedStatement(
        sql="SELECT * FROM orders WHERE acct = 'ATLAS_EARLIER_1_1'", generation_source="MODEL"
    )

    async def _generate(*_args: Any, **_kwargs: Any) -> ValidatedStatement:
        return statement

    async def _repair(*args: Any, **_kwargs: Any) -> ValidatedStatement:
        return statement

    refused: list[str] = []

    async def _persist(_session: Any, _request: Any, _ledger: Any, reason: str) -> None:
        refused.append(reason)

    monkeypatch.setattr(orchestrator, "_generate_statement", _generate)
    monkeypatch.setattr(orchestrator, "_repair_generated_statement", _repair)
    monkeypatch.setattr(orchestrator, "_persist_rejection", _persist)
    planned = SimpleNamespace(
        plan=SimpleNamespace(strategy="FREEFORM_SQL", selected_tool_version_id=None)
    )
    with pytest.raises(AgentPolicyRejected, match="earlier question"):
        await orchestrator._stage_validate(
            db,
            _follow_up_request((EarlierTurn(1, "balance of ATLAS_VALUE_1", "SELECT 1"),)),
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(context_product_scope=None),  # type: ignore[arg-type]
            planned,  # type: ignore[arg-type]
        )
    assert refused == ["FOLLOW_UP_NEEDS_AN_EARLIER_VALUE"]
