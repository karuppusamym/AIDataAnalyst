"""R11-MP17: a query becomes everyone's example only on someone else's confirmation.

A run's owner rating their own answer helpful used to make its query memory
ELIGIBLE -- a template and few-shot example for every user of the datasource,
three at a time by default -- with nobody else ever looking at it. Now the
owner's rating leaves it AWAITING_SECOND_CONFIRMATION, and a PlatformAdmin,
DataSteward or Reviewer confirming it is what makes it ELIGIBLE.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings, get_settings
from aida.db import Base, get_session

# Imported at module scope, so every router is registered before the schema is built.
from aida.main import app
from aida.models import AgentRun, QueryExecution, QueryMemoryEvidence

pytestmark = pytest.mark.asyncio

OWNER = "analyst-owner"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as active:
        yield active
    await engine.dispose()


_settings: dict[str, Settings] = {}


@pytest_asyncio.fixture
async def http(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    previous = dict(app.dependency_overrides)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield session

    _settings["current"] = Settings(_env_file=None)
    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: _settings["current"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://feedback.test"
    ) as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


async def _completed_run(session: AsyncSession) -> AgentRun:
    org, ds = uuid4(), uuid4()
    execution = QueryExecution(
        organization_id=org,
        datasource_id=ds,
        principal_id=OWNER,
        status="COMPLETED",
        dialect="postgres",
        sql_hash="a" * 64,
    )
    session.add(execution)
    await session.flush()
    run = AgentRun(
        organization_id=org,
        datasource_id=ds,
        principal_id=OWNER,
        status="COMPLETED",
        question_hash="0" * 64,
        generation_source="MODEL_GATEWAY",
        query_execution_id=execution.id,
    )
    session.add(run)
    await session.flush()
    return run


def _headers(org: UUID, principal: str, roles: str) -> dict[str, str]:
    return {
        "X-Principal-Id": principal,
        "X-Principal-Type": "USER",
        "X-Roles": roles,
        "X-Business-Purpose": "Confirm an answer",
        "X-Organization-Id": str(org),
    }


async def _rate(
    http: httpx.AsyncClient, run: AgentRun, principal: str, roles: str, rating: str = "HELPFUL"
) -> httpx.Response:
    return await http.put(
        f"/v1/agent-runs/{run.id}/feedback",
        json={"rating": rating},
        headers=_headers(run.organization_id, principal, roles),
    )


async def _status(session: AsyncSession, run: AgentRun) -> str:
    memory = await session.scalar(
        select(QueryMemoryEvidence).where(QueryMemoryEvidence.agent_run_id == run.id)
    )
    assert memory is not None
    await session.refresh(memory)
    return memory.status


async def test_the_owners_own_rating_is_not_enough(
    http: httpx.AsyncClient, session: AsyncSession
) -> None:
    run = await _completed_run(session)
    assert (await _rate(http, run, OWNER, "Analyst")).status_code == 200
    assert await _status(session, run) == "AWAITING_SECOND_CONFIRMATION"


async def test_a_stewards_confirmation_makes_it_eligible(
    http: httpx.AsyncClient, session: AsyncSession
) -> None:
    run = await _completed_run(session)
    await _rate(http, run, OWNER, "Analyst")
    confirmed = await _rate(http, run, "steward-1", "DataSteward")
    assert confirmed.status_code == 200
    assert await _status(session, run) == "ELIGIBLE"


async def test_another_analyst_cannot_confirm(
    http: httpx.AsyncClient, session: AsyncSession
) -> None:
    run = await _completed_run(session)
    refused = await _rate(http, run, "analyst-2", "Analyst")
    assert refused.status_code == 403
    assert "second confirmation" in refused.json()["detail"]


async def test_any_negative_rating_suppresses_even_after_confirmation(
    http: httpx.AsyncClient, session: AsyncSession
) -> None:
    run = await _completed_run(session)
    await _rate(http, run, OWNER, "Analyst")
    await _rate(http, run, "steward-1", "DataSteward")
    await _rate(http, run, "reviewer-1", "Reviewer", rating="INCORRECT")
    assert await _status(session, run) == "SUPPRESSED"


async def test_a_single_user_estate_can_turn_the_rule_off(
    http: httpx.AsyncClient, session: AsyncSession
) -> None:
    _settings["current"] = Settings(query_memory_requires_second_confirmation=False, _env_file=None)
    run = await _completed_run(session)
    await _rate(http, run, OWNER, "Analyst")
    assert await _status(session, run) == "ELIGIBLE"
