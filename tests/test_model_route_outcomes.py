"""R11-MP11: each model route's record, counted from the Ask runs themselves."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings, get_settings
from aida.db import Base, get_session

# Imported at module scope, so every router is registered before the schema is built.
from aida.main import app
from aida.model_route_outcomes import load_route_outcomes, summarize_route_outcomes
from aida.models import AgentRun

FALLBACK = {
    "model_call_attempts": [
        {"route_key": "primary", "outcome": "SKIPPED_CIRCUIT_OPEN"},
        {"route_key": "backup", "outcome": "SUCCEEDED"},
    ]
}
REPAIRED = {"sql_repair": {"attempts": [{"findings": ["UNKNOWN_COLUMN"], "result": "VALID"}]}}
STILL_BAD = {
    "sql_repair": {"attempts": [{"findings": ["UNKNOWN_COLUMN"], "result": "STILL_REFUSED"}]}
}
AGREED = {"sql_candidate": {"result": "COMPARED", "agreement": {"level": "SAME_SOURCES"}}}
DISAGREED = {"sql_candidate": {"result": "COMPARED", "agreement": {"level": "DIFFERENT"}}}
STATED = {
    "model_call_evidence": {
        "provider_reported_cost_usd": 0.004,
        "provider_cached_input_tokens": 800,
    }
}


def test_counts_come_from_what_each_run_recorded() -> None:
    # Most runs first.
    [primary, backup] = summarize_route_outcomes(
        [
            ("primary", "COMPLETED", REPAIRED | AGREED),
            ("primary", "REJECTED", STILL_BAD | DISAGREED),
            ("primary", "COMPLETED", {}),
            ("backup", "COMPLETED", FALLBACK | STATED),
            ("backup", "FAILED", STATED),
        ]
    )
    assert (primary.route_key, primary.runs, primary.completed, primary.rejected) == (
        "primary",
        3,
        2,
        1,
    )
    assert (primary.repairs_attempted, primary.repairs_valid) == (2, 1)
    assert (
        primary.candidates_compared,
        primary.candidates_same_sources,
        primary.candidates_different,
    ) == (2, 1, 1)
    assert primary.stated_cost_usd is None
    assert (backup.runs, backup.failed, backup.fallback_runs, backup.circuit_skips) == (2, 1, 1, 1)
    assert backup.stated_cost_usd == pytest.approx(0.008)
    assert backup.cached_input_tokens == 1600


def test_a_run_with_no_route_belongs_to_no_route() -> None:
    assert summarize_route_outcomes([(None, "REJECTED", {}), ("", "REJECTED", None)]) == []


def test_malformed_evidence_is_skipped_not_fatal() -> None:
    [outcome] = summarize_route_outcomes(
        [
            ("r", "COMPLETED", {"model_call_attempts": "nope", "sql_repair": [1]}),
            ("r", "COMPLETED", {"model_call_evidence": {"provider_reported_cost_usd": True}}),
            ("r", "COMPLETED", "not a mapping"),
        ]
    )
    assert outcome.runs == 3
    assert outcome.fallback_runs == 0
    assert outcome.stated_cost_usd is None


# ---------------------------------------------------------------------------
# Reading the window
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


def _run(
    org: UUID,
    route: str | None,
    *,
    status: str = "COMPLETED",
    age_days: float = 0,
    evidence: dict[str, Any] | None = None,
) -> AgentRun:
    return AgentRun(
        id=uuid4(),
        organization_id=org,
        datasource_id=uuid4(),
        principal_id="analyst",
        question_hash="0" * 64,
        generation_source="MODEL_GATEWAY",
        status=status,
        model_route=route,
        plan_evidence=evidence or {},
        created_at=datetime.now(UTC) - timedelta(days=age_days),
    )


@pytest.mark.asyncio
async def test_the_window_is_one_organization_and_says_when_it_was_cut(
    session: AsyncSession,
) -> None:
    org, other = uuid4(), uuid4()
    session.add_all(
        [
            _run(org, "primary"),
            _run(org, "primary", status="REJECTED"),
            _run(org, "primary"),
            _run(org, "primary", age_days=30),  # outside the window
            _run(other, "primary"),  # another organization
        ]
    )
    await session.flush()
    since = datetime.now(UTC) - timedelta(days=7)

    outcomes, considered, truncated = await load_route_outcomes(session, org, since=since)
    assert (considered, truncated) == (3, False)
    assert [(o.route_key, o.runs, o.rejected) for o in outcomes] == [("primary", 3, 1)]

    _outcomes, considered, truncated = await load_route_outcomes(session, org, since=since, limit=2)
    assert (considered, truncated) == (2, True)


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def http(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    previous = dict(app.dependency_overrides)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://outcomes.test"
    ) as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _headers(org: UUID, roles: str) -> dict[str, str]:
    return {
        "X-Principal-Id": "auditor-1",
        "X-Principal-Type": "USER",
        "X-Roles": roles,
        "X-Business-Purpose": "Review model route outcomes",
        "X-Organization-Id": str(org),
    }


@pytest.mark.asyncio
async def test_the_route_answers_an_auditor_and_refuses_another_organization(
    http: httpx.AsyncClient, session: AsyncSession
) -> None:
    org = uuid4()
    session.add_all([_run(org, "primary", evidence=REPAIRED), _run(org, "primary")])
    await session.flush()

    response = await http.get(
        f"/v1/organizations/{org}/model-route-outcomes?days=7", headers=_headers(org, "Auditor")
    )
    assert response.status_code == 200
    body = response.json()
    assert body["runs_considered"] == 2
    assert body["truncated"] is False
    assert body["routes"][0]["route_key"] == "primary"
    assert body["routes"][0]["repairs_valid"] == 1

    refused = await http.get(
        f"/v1/organizations/{uuid4()}/model-route-outcomes", headers=_headers(org, "Auditor")
    )
    assert refused.status_code == 403


@pytest.mark.asyncio
async def test_the_window_is_bounded_to_ninety_days(http: httpx.AsyncClient) -> None:
    org = uuid4()
    response = await http.get(
        f"/v1/organizations/{org}/model-route-outcomes?days=365", headers=_headers(org, "Auditor")
    )
    assert response.status_code == 422
