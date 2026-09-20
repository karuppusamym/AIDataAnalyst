"""A consumer's context-product listings on real PostgreSQL: no `DISTINCT` over `JSON`.

`GET /v1/projects/{id}/context-products` and `GET /v1/context-products/{id}/versions` filter a
non-lifecycle reader (an Analyst, a Viewer) to the PUBLISHED versions bound to a role they hold.
They did it by joining `context_product_role_binding` and adding `.distinct()` -- and a DISTINCT
over `select(ContextProduct, ContextProductVersion)` covers every column, including the
version's `JSON` id lists. PostgreSQL has no equality operator for `json`:

    asyncpg.exceptions.UndefinedFunctionError: could not identify an equality operator for type json

so both routes answered **500 for every Analyst and Viewer** -- which is what Ask's context-product
picker calls -- while a PlatformAdmin (no binding filter) got 200. It was live on the deployed stack
and found only because a benchmark ran the route as an Analyst. Every existing test of this listing
runs on SQLite, where DISTINCT over JSON is accepted, and `test_context_product_askable_listing.py`
asserts exactly the behaviour that was broken. So these are those scenarios, on a real server.

The join's DISTINCT also hid a second fault: the versions route's *count* joined without DISTINCT,
so a version bound to two roles the caller holds was counted twice (`total` 2 for one row).

**Database.** A private scratch database per run, `<app db>_ctxlist_<random>`, created here and
dropped when the module finishes. Built from `Base.metadata`. Only a failure to *reach* PostgreSQL
skips; anything after that fails.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import aida.main  # noqa: F401 -- registers every ORM table on Base.metadata
from aida.context_product_api import list_context_product_versions
from aida.db import Base
from aida.models import DataDomain, LineOfBusiness, Organization, Project
from atlas.platform.config import get_settings
from tests.test_context_product_askable_listing import (
    _context,
    _Estate,
    _list,
    _product,
)


def _scratch_url() -> str:
    root, _, dbname = get_settings().database_url.rpartition("/")
    return f"{root}/{dbname}_ctxlist_{uuid4().hex[:10]}"


def _maintenance_url(db_url: str) -> str:
    root, _, _ = db_url.rpartition("/")
    _, _, app_dbname = get_settings().database_url.rpartition("/")
    return f"{root}/{app_dbname}"


async def _create(db_url: str) -> None:
    _, _, dbname = db_url.rpartition("/")
    admin = create_async_engine(_maintenance_url(db_url), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await admin.dispose()
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


async def _drop(db_url: str) -> None:
    _, _, dbname = db_url.rpartition("/")
    admin = create_async_engine(_maintenance_url(db_url), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    db_url = _scratch_url()
    try:
        asyncio.run(_create(db_url))
    except Exception as exc:  # noqa: BLE001 -- unreachable PostgreSQL means skip, not fail
        pytest.skip(
            f"PostgreSQL is not reachable for {db_url!r} ({type(exc).__name__}: {exc}); this "
            "file exists because SQLite accepts DISTINCT over JSON and PostgreSQL does not."
        )
    try:
        yield db_url
    finally:
        if not os.environ.get("AIDA_CTXLIST_KEEP_DATABASE"):
            asyncio.run(_drop(db_url))


@pytest_asyncio.fixture
async def engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    created = create_async_engine(postgres_url, poolclass=NullPool)
    yield created
    await created.dispose()


@pytest_asyncio.fixture
async def db(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session


@pytest_asyncio.fixture
async def estate(db: AsyncSession) -> _Estate:
    """The SQLite suite's estate, on PostgreSQL: three products, one per lifecycle/role case."""
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    db.add(organization)
    await db.flush()
    # The real chain: PostgreSQL enforces the foreign keys SQLite does not, so the random ids the
    # SQLite estate gets away with are an IntegrityError here.
    lob = LineOfBusiness(
        organization_id=organization.id, name="Retail", code=f"RB{uuid4().hex[:6]}"
    )
    db.add(lob)
    await db.flush()
    domain = DataDomain(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name="Retail Domain",
        code=f"D{uuid4().hex[:6]}",
    )
    db.add(domain)
    await db.flush()
    project = Project(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core Banking",
        slug=f"core-{uuid4().hex[:6]}",
    )
    db.add(project)
    await db.flush()
    built = _Estate(organization, project)
    await _product(db, built, key="orders-context", status="PUBLISHED", consumer_roles=["Analyst"])
    await _product(
        db, built, key="risk-context", status="PUBLISHED", consumer_roles=["DataSteward"]
    )
    await _product(
        db, built, key="draft-context", status="DRAFT", consumer_roles=["Analyst", "DataSteward"]
    )
    # Bound to two roles a single caller can hold: the row the join used to duplicate.
    await _product(
        db, built, key="shared-context", status="PUBLISHED", consumer_roles=["Analyst", "Viewer"]
    )
    await db.commit()
    return built


async def test_an_analyst_can_list_the_products_they_consume(
    db: AsyncSession, estate: _Estate
) -> None:
    """The defect, verbatim: this was a 500 on PostgreSQL."""
    analyst = _context(estate, roles={"Analyst"})

    assert await _list(db, estate, analyst) == (["orders-context", "shared-context"], 2)
    assert await _list(db, estate, analyst, askable=True) == (["orders-context", "shared-context"], 2)


async def test_a_viewer_lists_only_what_is_bound_to_viewer(
    db: AsyncSession, estate: _Estate
) -> None:
    viewer = _context(estate, roles={"Viewer"})

    assert await _list(db, estate, viewer) == (["shared-context"], 1)
    assert await _list(db, estate, viewer, askable=True) == (["shared-context"], 1)


async def test_a_caller_holding_both_bound_roles_sees_the_product_once(
    db: AsyncSession, estate: _Estate
) -> None:
    both = _context(estate, roles={"Analyst", "Viewer"})

    keys, total = await _list(db, estate, both, askable=True)
    assert keys == ["orders-context", "shared-context"]
    assert total == 2


async def test_the_lifecycle_and_administrator_paths_are_unchanged(
    db: AsyncSession, estate: _Estate
) -> None:
    steward = _context(estate, roles={"DataSteward"})
    assert (await _list(db, estate, steward))[0] == [
        "draft-context",
        "orders-context",
        "risk-context",
        "shared-context",
    ]
    assert (await _list(db, estate, steward, askable=True))[0] == ["risk-context"]
    admin = _context(estate, roles={"PlatformAdmin"})
    assert (await _list(db, estate, admin, askable=True))[0] == [
        "orders-context",
        "risk-context",
        "shared-context",
    ]


async def _versions(
    db: AsyncSession, estate: _Estate, key: str, roles: set[str]
) -> tuple[list[int], int]:
    from sqlalchemy import select

    from aida.models import ContextProduct

    product_id = await db.scalar(
        select(ContextProduct.id).where(
            ContextProduct.project_id == estate.project.id, ContextProduct.product_key == key
        )
    )
    assert product_id is not None
    page = await list_context_product_versions(
        product_id,
        limit=100,
        offset=0,
        context=_context(estate, roles=roles),
        session=db,
    )
    return [item.version for item in page.items], page.total


async def test_an_analyst_can_list_the_versions_of_a_product_they_consume(
    db: AsyncSession, estate: _Estate
) -> None:
    """The second site, same 500."""
    assert await _versions(db, estate, "orders-context", {"Analyst"}) == ([1], 1)
    # Bound to a role they do not hold: nothing, not an error.
    assert await _versions(db, estate, "risk-context", {"Analyst"}) == ([], 0)


async def test_a_version_bound_to_two_held_roles_is_counted_once(
    db: AsyncSession, estate: _Estate
) -> None:
    """The count joined the bindings without DISTINCT: one version, two matching bindings, a
    `total` of 2 beside one row."""
    assert await _versions(db, estate, "shared-context", {"Analyst", "Viewer"}) == ([1], 1)
