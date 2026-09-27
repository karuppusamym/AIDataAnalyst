"""R11-MP06: Ask's stages, streamed as server-sent events.

`POST /v1/datasources/{id}/agent-analyses/stream` runs the same governed Ask as
the single-shot route and reports each stage as the run reaches it, then ends
with exactly one `result` or `error` event carrying what the single-shot route
would have answered. Driven over ASGI on the retrieval-wiring scenario, whose
governed tool needs a parameter the question never supplies -- so the run
reaches the plan stage and refuses with a structured clarification, with no
warehouse and no model route.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_runtime import RuntimeStage, RuntimeState
from aida.config import Settings, get_settings
from aida.db import get_session

# Imported at module scope, so every router is registered before `db` builds the schema.
from aida.main import app
from aida.models import AgentRun
from aida.orchestration_stages import RunLedger
from tests.test_agent_orchestrator_retrieval_wiring import _Scenario, db  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def scenario(db: AsyncSession) -> AsyncIterator[_Scenario]:  # noqa: F811
    yield await _Scenario(db).build()


@pytest_asyncio.fixture
async def http(scenario: _Scenario) -> AsyncIterator[httpx.AsyncClient]:
    previous_overrides = dict(app.dependency_overrides)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield scenario.db

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ask.test") as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous_overrides)


def _headers(scenario: _Scenario) -> dict[str, str]:
    return {
        "X-Principal-Id": "ask-analyst",
        "X-Principal-Type": "USER",
        "X-Roles": "Analyst",
        "X-Business-Purpose": "Ask with streamed stages",
        "X-Organization-Id": str(scenario.organization.id),
    }


def _body(scenario: _Scenario) -> dict[str, Any]:
    return {"question": "orders", "preferred_tool_version_id": str(scenario.tool_version.id)}


def _events(text: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


async def test_the_stream_reports_stages_then_the_same_refusal_as_the_single_shot_route(
    http: httpx.AsyncClient, scenario: _Scenario
) -> None:
    path = f"/v1/datasources/{scenario.datasource.id}/agent-analyses"
    single = await http.post(path, json=_body(scenario), headers=_headers(scenario))
    streamed = await http.post(f"{path}/stream", json=_body(scenario), headers=_headers(scenario))

    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")
    events = _events(streamed.text)
    stages = [data["stage"] for name, data in events if name == "stage"]
    # In order, from where the run was opened to the refusal.
    assert stages[:3] == ["RECEIVED", "AUTHORIZED", "SCREENED"]
    assert "PLANNED" in stages
    assert stages[-1] == "REJECTED"
    final_name, final = events[-1]
    assert final_name == "error"
    assert single.status_code == 409
    assert final["status"] == single.status_code
    assert final["detail"] == single.json()["detail"]
    assert final["detail"]["code"] == "MISSING_TOOL_PARAMETERS"


async def test_admission_refusals_are_ordinary_http_errors_before_any_stream(
    http: httpx.AsyncClient, scenario: _Scenario
) -> None:
    response = await http.post(
        f"/v1/datasources/{uuid4()}/agent-analyses/stream",
        json=_body(scenario),
        headers=_headers(scenario),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "datasource not found"


async def test_a_role_the_route_refuses_never_opens_a_stream(
    http: httpx.AsyncClient, scenario: _Scenario
) -> None:
    headers = {**_headers(scenario), "X-Roles": "Viewer"}
    response = await http.post(
        f"/v1/datasources/{scenario.datasource.id}/agent-analyses/stream",
        json=_body(scenario),
        headers=headers,
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# The ledger's listener observes and cannot fail a run
# ---------------------------------------------------------------------------


def _ledger() -> RunLedger:
    run = AgentRun(
        id=uuid4(),
        organization_id=uuid4(),
        datasource_id=uuid4(),
        principal_id="p1",
        question_hash="0" * 64,
        generation_source="PENDING",
    )
    return RunLedger(agent_run=run, state=RuntimeState(request_id=str(run.id)))


def test_the_listener_hears_every_transition() -> None:
    heard: list[str] = []
    ledger = _ledger()
    ledger.stage_listener = heard.append
    ledger.advance(RuntimeStage.AUTHORIZED, control_type="DETERMINISTIC")
    ledger.advance(RuntimeStage.SCREENED, control_type="DETERMINISTIC")
    assert heard == ["AUTHORIZED", "SCREENED"]


def test_a_listener_that_raises_is_dropped_and_the_run_goes_on() -> None:
    def _broken(_stage: str) -> None:
        raise RuntimeError("client went away")

    ledger = _ledger()
    ledger.stage_listener = _broken
    ledger.advance(RuntimeStage.AUTHORIZED, control_type="DETERMINISTIC")
    ledger.advance(RuntimeStage.SCREENED, control_type="DETERMINISTIC")
    assert ledger.state.stage is RuntimeStage.SCREENED
    assert ledger.stage_listener is None
