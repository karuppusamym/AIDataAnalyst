"""R11-S13: `GET .../stewardship/unowned-backlog` narrows by `candidate_owner` in the query.

The Stewardship Work queue's "Candidate owner" filter could only narrow the page already loaded,
because the route took `status`, `limit` and `offset` and nothing else. It takes `candidate_owner`
now, and these tests pin what that means, against a real (in-memory SQLite) database:

* **exact and case-sensitive**, as stored -- not a substring, not case-folded;
* **before paging**: `total` counts the matches and a page past them is empty, so a client never
  narrows a wider page itself;
* **beside `status`**, whose own rules (the default hides RESOLVED) are unchanged;
* an entry with **no** candidate owner never matches one, and an empty value is no filter;
* **tenancy is unchanged** (INV-5): the organization is the first filter, another organization's
  entries are never returned whatever the value, and asking under another organization is refused;
* over HTTP the parameter is bounded like its siblings and appears in the OpenAPI document as an
  optional query parameter, so the generated types and the schema baseline pick it up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.main import app
from aida.models import Organization, UnownedAssetEscalation
from aida.stewardship_api import list_unowned_asset_backlog
from atlas.platform.config import Settings, get_settings
from atlas.platform.db import Base, get_session
from tests.support.doubles import security_context

pytestmark = pytest.mark.asyncio

_START = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _organization(session: AsyncSession) -> UUID:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org.id


_clock = iter(range(10_000))


async def _entry(
    session: AsyncSession,
    organization_id: UUID,
    *,
    owner: str | None,
    status: str = "PENDING",
) -> UnownedAssetEscalation:
    """Entries are detected in creation order, so the route's own ordering is deterministic."""
    entry = UnownedAssetEscalation(
        id=uuid4(),
        organization_id=organization_id,
        table_id=uuid4(),
        first_detected_unowned_at=_START + timedelta(minutes=next(_clock)),
        status=status,
        candidate_owner=owner,
        recipients=[],
    )
    session.add(entry)
    await session.flush()
    return entry


async def _list(
    session: AsyncSession, organization_id: UUID, **query: Any
) -> Any:
    return await list_unowned_asset_backlog(
        organization_id,
        backlog_status=None,
        limit=query.pop("limit", 100),
        offset=query.pop("offset", 0),
        context=security_context(
            organization_id=organization_id, roles=frozenset({"DataSteward"})
        ),
        session=session,
        **query,
    )


def _owners(page: Any) -> list[str | None]:
    return [item.candidate_owner for item in page.items]


# ---------------------------------------------------------------------------
# What matches
# ---------------------------------------------------------------------------


async def test_only_entries_with_exactly_that_candidate_owner_are_returned(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    for owner in ("Finance Data", "Risk Data", None, "Finance Data"):
        await _entry(session, org, owner=owner)

    page = await _list(session, org, candidate_owner="Finance Data")

    assert _owners(page) == ["Finance Data", "Finance Data"]
    assert page.total == 2


@pytest.mark.parametrize(
    "value",
    [
        "finance data",
        "FINANCE DATA",
        "Finance",
        "Data",
        "Finance Data ",
        " Finance Data",
        "Finance%",
    ],
    ids=["lower", "upper", "prefix", "suffix", "trailing-space", "leading-space", "wildcard"],
)
async def test_the_match_is_exact_and_case_sensitive(session: AsyncSession, value: str) -> None:
    """Not the substring, case-folded match the screen used to apply to the page it had."""
    org = await _organization(session)
    await _entry(session, org, owner="Finance Data")

    page = await _list(session, org, candidate_owner=value)

    assert page.items == [] and page.total == 0


async def test_a_value_that_differs_only_by_case_matches_its_own_entry_only(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    await _entry(session, org, owner="Finance Data")
    lower = await _entry(session, org, owner="finance data")

    page = await _list(session, org, candidate_owner="finance data")

    assert [item.id for item in page.items] == [lower.id]


async def test_an_entry_with_no_candidate_owner_never_matches_one(session: AsyncSession) -> None:
    org = await _organization(session)
    await _entry(session, org, owner=None)
    await _entry(session, org, owner="")

    page = await _list(session, org, candidate_owner="Finance Data")

    assert page.items == [] and page.total == 0


@pytest.mark.parametrize("none", [None, ""], ids=["absent", "empty"])
async def test_no_value_is_no_filter(session: AsyncSession, none: str | None) -> None:
    org = await _organization(session)
    for owner in ("Finance Data", None, "Risk Data"):
        await _entry(session, org, owner=owner)

    everything = await _list(session, org, candidate_owner=none)
    omitted = await _list(session, org)

    assert everything.total == omitted.total == 3
    assert _owners(everything) == _owners(omitted) == ["Finance Data", None, "Risk Data"]


# ---------------------------------------------------------------------------
# Before paging
# ---------------------------------------------------------------------------


async def test_the_total_counts_the_matches_and_paging_happens_after_the_filter(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    # The matches are spread through a backlog three times their size, so a page of the wider
    # backlog that a client narrowed itself would hold a fraction of them.
    for index in range(15):
        await _entry(session, org, owner="Finance Data" if index % 3 == 0 else f"Other {index}")

    first = await _list(session, org, candidate_owner="Finance Data", limit=2, offset=0)
    second = await _list(session, org, candidate_owner="Finance Data", limit=2, offset=2)
    last = await _list(session, org, candidate_owner="Finance Data", limit=2, offset=4)
    past = await _list(session, org, candidate_owner="Finance Data", limit=2, offset=6)

    assert [len(page.items) for page in (first, second, last, past)] == [2, 2, 1, 0]
    assert {page.total for page in (first, second, last, past)} == {5}
    seen = [item.id for page in (first, second, last) for item in page.items]
    assert len(set(seen)) == 5, "the pages are disjoint and hold every match"
    assert {owner for page in (first, second, last) for owner in _owners(page)} == {"Finance Data"}


# ---------------------------------------------------------------------------
# Beside status
# ---------------------------------------------------------------------------


async def test_the_default_still_hides_resolved_entries_and_status_can_ask_for_them(
    session: AsyncSession,
) -> None:
    org = await _organization(session)
    await _entry(session, org, owner="Finance Data", status="ROUTED")
    resolved = await _entry(session, org, owner="Finance Data", status="RESOLVED")
    await _entry(session, org, owner="Risk Data", status="RESOLVED")

    by_default = await _list(session, org, candidate_owner="Finance Data")
    only_resolved = await list_unowned_asset_backlog(
        org,
        backlog_status="resolved",
        limit=100,
        offset=0,
        context=security_context(organization_id=org, roles=frozenset({"DataSteward"})),
        session=session,
        candidate_owner="Finance Data",
    )

    assert [item.status for item in by_default.items] == ["ROUTED"]
    assert [item.id for item in only_resolved.items] == [resolved.id]
    assert only_resolved.total == 1


# ---------------------------------------------------------------------------
# Tenancy is unchanged
# ---------------------------------------------------------------------------


async def test_another_organizations_entries_are_never_returned_whatever_the_owner(
    session: AsyncSession,
) -> None:
    ours = await _organization(session)
    theirs = await _organization(session)
    mine = await _entry(session, ours, owner="Finance Data")
    await _entry(session, theirs, owner="Finance Data")
    await _entry(session, theirs, owner="Finance Data")

    with_the_value = await _list(session, ours, candidate_owner="Finance Data")
    without = await _list(session, ours)

    assert [item.id for item in with_the_value.items] == [mine.id]
    assert with_the_value.total == 1
    assert [item.id for item in without.items] == [mine.id]


async def test_asking_under_another_organization_is_refused(session: AsyncSession) -> None:
    ours = await _organization(session)
    theirs = await _organization(session)
    await _entry(session, theirs, owner="Finance Data")

    with pytest.raises(HTTPException) as refused:
        await list_unowned_asset_backlog(
            theirs,
            backlog_status=None,
            limit=100,
            offset=0,
            context=security_context(organization_id=ours, roles=frozenset({"DataSteward"})),
            session=session,
            candidate_owner="Finance Data",
        )

    assert refused.value.status_code == 403


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def http(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    previous = dict(app.dependency_overrides)

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://s13.test"
    ) as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _headers(org: UUID, roles: str = "DataSteward") -> dict[str, str]:
    return {
        "X-Principal-Id": "steward-1",
        "X-Principal-Type": "USER",
        "X-Roles": roles,
        "X-Business-Purpose": "Work the unowned backlog",
        "X-Organization-Id": str(org),
    }


def _url(org: UUID) -> str:
    return f"/v1/organizations/{org}/stewardship/unowned-backlog"


async def test_the_route_reads_the_query_parameter_and_narrows_the_backlog(
    session: AsyncSession, http: httpx.AsyncClient
) -> None:
    org = await _organization(session)
    wanted = "risk-data-stewards@tenant.example"
    await _entry(session, org, owner=wanted)
    await _entry(session, org, owner="Finance Data")
    await _entry(session, org, owner=None)
    await session.commit()

    response = await http.get(_url(org), params={"candidate_owner": wanted}, headers=_headers(org))

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["candidate_owner"] for item in body["items"]] == [wanted]
    assert body["total"] == 1
    everything = await http.get(_url(org), headers=_headers(org))
    assert everything.json()["total"] == 3


async def test_the_value_is_bounded_like_its_siblings(
    session: AsyncSession, http: httpx.AsyncClient
) -> None:
    org = await _organization(session)
    await session.commit()

    fits = await http.get(_url(org), params={"candidate_owner": "x" * 255}, headers=_headers(org))
    too_long = await http.get(
        _url(org), params={"candidate_owner": "x" * 256}, headers=_headers(org)
    )

    assert fits.status_code == 200
    assert too_long.status_code == 422


async def test_roles_and_tenancy_are_unchanged_over_http(
    session: AsyncSession, http: httpx.AsyncClient
) -> None:
    ours = await _organization(session)
    theirs = await _organization(session)
    await _entry(session, theirs, owner="Finance Data")
    await session.commit()
    params = {"candidate_owner": "Finance Data"}

    # `Operations` is a real role, and not one of the roles that read this backlog.
    not_a_reader = await http.get(
        _url(ours), params=params, headers=_headers(ours, "Operations")
    )
    across = await http.get(_url(theirs), params=params, headers=_headers(ours))

    assert not_a_reader.status_code == 403
    assert across.status_code == 403


async def test_the_openapi_document_names_it_as_an_optional_bounded_query_parameter() -> None:
    """What the UI's generated types and the schema baseline are made from."""
    path = "/v1/organizations/{organization_id}/stewardship/unowned-backlog"
    operation = app.openapi()["paths"][path]["get"]
    parameters = {
        parameter["name"]: parameter
        for parameter in operation["parameters"]
        if parameter["in"] in ("path", "query")  # the rest are the identity headers
    }

    parameter = parameters["candidate_owner"]
    assert parameter["in"] == "query" and parameter.get("required", False) is False
    assert {"type": "string", "maxLength": 255} in parameter["schema"]["anyOf"]
    assert set(parameters) == {"organization_id", "status", "limit", "offset", "candidate_owner"}
