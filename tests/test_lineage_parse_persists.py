"""A deep procedure-lineage parse leaves what it wrote once the request has ended (R11-AUD08 sweep).

`POST .../procedures/{routine_id}/lineage/parse` stores the routine's edges and its parse
coverage and audits the parse, all in the request's session, and ended with `flush()`. The API
gives every request its own session and closes (rolls back) it, so the parse answered 200 and
left nothing: the coverage read answered 404 "no parse has measured this routine's coverage yet"
and the audit ledger had no row. It was found by scanning every mutating route for a write with
no commit after the same defect turned up in Studio (`tests/test_studio_writes_persist.py`).

The other tests of this route call the handler on one shared session, where a flush is visible.
This one sends each call as its own request and reads the outcome back with a second one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.main import app
from aida.models import AuditEvent, Organization
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session
from tests.test_definition_history import _seed_routine
from tests.test_graphql_lineage import _datasource

ORG = UUID("9b90b35f-dcf5-49d3-8f0e-2f269987ae87")
STEWARD = {
    "X-Principal-Id": "dana.steward",
    "X-Roles": "DataSteward,Viewer",
    "X-Organization-Id": str(ORG),
}


@dataclass
class Estate:
    client: httpx.AsyncClient
    maker: async_sessionmaker[AsyncSession]
    datasource_id: UUID
    routine_id: UUID

    @property
    def base(self) -> str:
        return f"/v1/datasources/{self.datasource_id}/procedures/{self.routine_id}"


@pytest_asyncio.fixture
async def estate() -> AsyncIterator[Estate]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as seed:
        org = Organization(id=ORG, name="Northwind", slug=f"sample-{uuid4().hex[:6]}")
        seed.add(org)
        await seed.flush()
        datasource, schema = await _datasource(seed, org, "Ledger")
        routine = await _seed_routine(seed, org, datasource, schema)
        datasource_id, routine_id = datasource.id, routine.id
        await seed.commit()

    async def _session() -> AsyncIterator[AsyncSession]:
        # `get_session` as the API has it: a session per request, closed (rolled back) after it.
        async with maker() as session:
            yield session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, identity_provider="development"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://lineage.test"
    ) as client:
        yield Estate(client, maker, datasource_id, routine_id)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    await engine.dispose()


async def test_a_parse_is_measured_and_audited_on_the_next_request(estate: Estate) -> None:
    before = await estate.client.get(f"{estate.base}/parse-coverage", headers=STEWARD)
    assert before.status_code == 404, before.text

    parsed = await estate.client.post(f"{estate.base}/lineage/parse", headers=STEWARD)
    assert parsed.status_code == 200, parsed.text

    after = await estate.client.get(f"{estate.base}/parse-coverage", headers=STEWARD)
    assert after.status_code == 200, after.text
    async with estate.maker() as session:
        actions = (await session.scalars(select(AuditEvent.action))).all()
    assert list(actions) == ["procedure_lineage.deep_parse"]


async def test_a_parse_that_collides_with_a_concurrent_one_is_a_409_and_writes_nothing(
    estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two parses of one routine at once (a double click, or a person and the lineage agent)
    collide on the edge and coverage tables' unique constraints. The loser used to answer 500."""
    from aida import procedure_lineage_api

    async def collide(session: AsyncSession, **kwargs: object) -> None:
        raise IntegrityError("INSERT INTO routine_parse_coverage", {}, Exception("duplicate key"))

    monkeypatch.setattr(procedure_lineage_api, "record_routine_parse_coverage", collide)

    parsed = await estate.client.post(f"{estate.base}/lineage/parse", headers=STEWARD)

    assert parsed.status_code == 409, parsed.text
    assert "retry" in parsed.json()["detail"]
    async with estate.maker() as session:
        assert (await session.scalars(select(AuditEvent.action))).all() == []
