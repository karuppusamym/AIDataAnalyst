"""Studio's writing routes leave their writes behind once the request has ended (R11-AUD08).

The running API hands every request its own `AsyncSession` (`get_session`), which closes, and
so rolls back, whatever was not committed. `create_change_set`, `add_item`, `remove_item`,
`run_tests` and `detect_conflicts_endpoint` flushed and audited but never committed: a change set
that answered 201 was gone on the next request, and so was its audit row, so an author could not
get a change set to `submit_change_set` (the one route that did commit) at all.

Nothing saw it. `tests/test_studio.py` is a unit test over `aida.studio` with no database, and the
other Studio tests share one session across every call, where an uncommitted flush is still
visible. These tests send every call as its own request against one in-memory database, the way
a client does, and read each outcome back with a *second* request or a fresh session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.main import app
from aida.models import AuditEvent, Organization
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session

ORG = UUID("9b90b35f-dcf5-49d3-8f0e-2f269987ae87")
AUTHOR = {
    "X-Principal-Id": "dana.steward",
    "X-Roles": "DataSteward,Viewer",
    "X-Organization-Id": str(ORG),
}
METRIC = {"name": "Net revenue", "aggregation": "SUM", "grain": "day"}


class Api:
    """The application over ASGI, plus a way to look at the database from outside a request."""

    def __init__(self, client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]) -> None:
        self.client = client
        self.maker = maker

    async def create(self, name: str = "Quarter close changes") -> dict[str, Any]:
        response = await self.client.post(
            "/v1/studio/change-sets", json={"name": name}, headers=AUTHOR
        )
        assert response.status_code == 201, response.text
        created: dict[str, Any] = response.json()
        return created

    async def add(self, change_set_id: str, **fields: Any) -> dict[str, Any]:
        body = {
            "object_type": "METRIC",
            "object_id": "net_revenue",
            "operation": "CREATE",
            "after_snapshot": METRIC,
            **fields,
        }
        response = await self.client.post(
            f"/v1/studio/change-sets/{change_set_id}/items", json=body, headers=AUTHOR
        )
        assert response.status_code == 201, response.text
        added: dict[str, Any] = response.json()
        return added

    async def change_set(self, change_set_id: str) -> httpx.Response:
        return await self.client.get(f"/v1/studio/change-sets/{change_set_id}", headers=AUTHOR)

    async def items(self, change_set_id: str) -> list[dict[str, Any]]:
        response = await self.client.get(
            f"/v1/studio/change-sets/{change_set_id}/items", headers=AUTHOR
        )
        assert response.status_code == 200, response.text
        listed: list[dict[str, Any]] = response.json()
        return listed

    async def audit_actions(self) -> list[str]:
        async with self.maker() as session:
            rows = await session.scalars(select(AuditEvent.action))
            return sorted(rows.all())


@pytest_asyncio.fixture
async def api() -> AsyncIterator[Api]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as seed:
        seed.add(Organization(id=ORG, name="Northwind", slug="sample-bank"))
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
        transport=httpx.ASGITransport(app=app), base_url="http://studio.test"
    ) as client:
        yield Api(client, maker)
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    await engine.dispose()


async def test_a_created_change_set_is_there_on_the_next_request(api: Api) -> None:
    created = await api.create()

    fetched = await api.change_set(created["id"])
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "DRAFT"
    listed = await api.client.get("/v1/studio/change-sets", headers=AUTHOR)
    assert [row["id"] for row in listed.json()] == [created["id"]]


async def test_the_creation_is_audited_and_the_audit_row_survives_too(api: Api) -> None:
    await api.create()

    assert await api.audit_actions() == ["studio.change_set.create"]


async def test_an_added_item_is_there_on_the_next_request(api: Api) -> None:
    created = await api.create()
    added = await api.add(created["id"])

    items = await api.items(created["id"])
    assert [item["id"] for item in items] == [added["id"]]
    assert items[0]["test_status"] == "UNTESTED"


async def test_a_removed_item_stays_removed(api: Api) -> None:
    created = await api.create()
    kept = await api.add(created["id"], object_id="gross_revenue")
    dropped = await api.add(created["id"], object_id="net_revenue")

    removal = await api.client.delete(
        f"/v1/studio/change-sets/{created['id']}/items/{dropped['id']}", headers=AUTHOR
    )
    assert removal.status_code == 204, removal.text

    assert [item["id"] for item in await api.items(created["id"])] == [kept["id"]]


async def test_run_tests_keeps_the_status_and_each_items_verdict(api: Api) -> None:
    created = await api.create()
    await api.add(created["id"])
    await api.add(created["id"], object_id="broken", after_snapshot={"name": "no aggregation"})

    ran = await api.client.post(f"/v1/studio/change-sets/{created['id']}/test", headers=AUTHOR)
    assert ran.status_code == 200, ran.text
    assert ran.json()["passed"] is False

    assert (await api.change_set(created["id"])).json()["status"] == "TESTING"
    verdicts = {item["object_id"]: item["test_status"] for item in await api.items(created["id"])}
    assert verdicts == {"net_revenue": "PASSED", "broken": "FAILED"}


async def test_detect_conflicts_keeps_its_verdict(api: Api) -> None:
    created = await api.create()
    await api.add(created["id"])

    found = await api.client.post(
        f"/v1/studio/change-sets/{created['id']}/detect-conflicts",
        json={"METRIC:net_revenue": {"name": "Net revenue"}},
        headers=AUTHOR,
    )
    assert found.status_code == 200, found.text
    assert [c["current_value"] for c in found.json()] == ["ALREADY_EXISTS"]

    assert (await api.change_set(created["id"])).json()["conflict_status"] == "CONFLICTED"


async def test_an_author_can_take_a_change_set_from_nothing_to_submitted(api: Api) -> None:
    created = await api.create()
    await api.add(created["id"])
    ran = await api.client.post(f"/v1/studio/change-sets/{created['id']}/test", headers=AUTHOR)
    assert ran.json()["passed"] is True

    submitted = await api.client.post(
        f"/v1/studio/change-sets/{created['id']}/submit", headers=AUTHOR
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "SUBMITTED"
    assert (await api.change_set(created["id"])).json()["status"] == "SUBMITTED"


async def test_each_of_the_five_writes_leaves_its_audit_row(api: Api) -> None:
    created = await api.create()
    kept = await api.add(created["id"])
    dropped = await api.add(created["id"], object_id="gross_revenue")
    removal = await api.client.delete(
        f"/v1/studio/change-sets/{created['id']}/items/{dropped['id']}", headers=AUTHOR
    )
    assert removal.status_code == 204, removal.text
    detected = await api.client.post(
        f"/v1/studio/change-sets/{created['id']}/detect-conflicts", headers=AUTHOR
    )
    assert detected.status_code == 200, detected.text
    ran = await api.client.post(f"/v1/studio/change-sets/{created['id']}/test", headers=AUTHOR)
    assert ran.status_code == 200, ran.text

    assert kept["id"]
    assert await api.audit_actions() == [
        "studio.change_item.add",
        "studio.change_item.add",
        "studio.change_item.remove",
        "studio.change_set.create",
        "studio.change_set.test",
        "studio.detect_conflicts",
    ]
