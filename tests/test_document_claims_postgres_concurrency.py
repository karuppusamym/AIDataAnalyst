"""Two stewards propose one data dictionary at once: each row gets one review.

`extract_claims` leaves the document MAPPED, so the status check alone let a
second call raise every claim and its review again. The guard takes a row lock
on the document before counting the claims it already has. SQLite ignores
`FOR UPDATE`, so only a real PostgreSQL shows the lock doing its job: the
second caller waits for the first to commit, sees its claims, and gets a 409.

It runs at READ COMMITTED, the isolation `atlas.platform.db` leaves in place
(PostgreSQL's default), where a statement issued after the lock is granted sees
rows the first transaction committed.

Runs against a scratch database derived from `Settings.database_url`
(`<db>_document_claims_test`), created if missing and wiped on entry; set
`AIDA_DOCUMENT_CLAIMS_TEST_DATABASE_URL` to use another. Skips cleanly when
PostgreSQL is unreachable, so check for `s` in the output before reading a
pass as proof.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import aida.main  # noqa: F401 -- registers every ORM table on Base.metadata
from aida.db import Base
from aida.document_ingestion_api import (
    DocumentCreate,
    extract_claims,
    map_document,
    upload_document,
)
from aida.models import (
    DataDomain,
    DataSource,
    DocumentClaim,
    DocumentSection,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.security import SecurityContext
from atlas.platform.config import get_settings

_DICTIONARY_CSV = (
    "schema,table,column,description\n"
    "public,customers,customer_id,unique customer identifier\n"
    "public,customers,ssn,social security number of the customer\n"
    "public,orders,,one row per customer order\n"
)


def _test_database_url() -> str:
    override = os.environ.get("AIDA_DOCUMENT_CLAIMS_TEST_DATABASE_URL")
    if override:
        return override
    root, _, dbname = get_settings().database_url.rpartition("/")
    if not root or not dbname:
        raise AssertionError("Settings.database_url does not end in '/<dbname>'")
    return f"{root}/{dbname}_document_claims_test"


def _maintenance_url(db_url: str) -> str:
    root, _, _ = db_url.rpartition("/")
    _, _, app_dbname = get_settings().database_url.rpartition("/")
    return f"{root}/{app_dbname}"


async def _probe_reachable(db_url: str) -> None:
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def _prepare_database(db_url: str) -> None:
    try:
        await _probe_reachable(db_url)
    except Exception:
        admin = create_async_engine(_maintenance_url(db_url), isolation_level="AUTOCOMMIT")
        _, _, dbname = db_url.rpartition("/")
        try:
            async with admin.connect() as conn:
                await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
        finally:
            await admin.dispose()
        await _probe_reachable(db_url)

    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    """A prepared scratch database, or a clean skip naming why not."""
    db_url = _test_database_url()
    try:
        asyncio.run(_prepare_database(db_url))
    except Exception as exc:  # noqa: BLE001 -- any connection failure means "skip"
        pytest.skip(
            f"PostgreSQL is not reachable at {db_url!r} ({type(exc).__name__}: {exc}); "
            "the SQLite test in tests/test_document_ingestion.py still covers the "
            "sequential case."
        )
    yield db_url


@pytest_asyncio.fixture
async def engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    """No shared connections (`NullPool`), and a lock timeout that turns a real
    lock cycle into a failure in seconds rather than a hung suite."""
    created = create_async_engine(
        postgres_url,
        isolation_level="READ COMMITTED",
        poolclass=NullPool,
        connect_args={"server_settings": {"lock_timeout": "15s"}},
    )
    yield created
    await created.dispose()


async def _seed(sessions: async_sessionmaker[AsyncSession]) -> tuple[UUID, SecurityContext]:
    """The full foreign-key chain, one flush per row: PostgreSQL enforces the
    keys SQLite let the ingestion tests skip."""
    async with sessions() as session:
        org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        lob = LineOfBusiness(
            id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
        )
        domain = DataDomain(
            id=uuid4(),
            organization_id=org.id,
            line_of_business_id=lob.id,
            name="Ungoverned",
            code=f"UNG{uuid4().hex[:6]}",
        )
        project = Project(
            id=uuid4(),
            organization_id=org.id,
            line_of_business_id=lob.id,
            data_domain_id=domain.id,
            name="Warehouse",
            slug=f"wh-{uuid4().hex[:8]}",
        )
        datasource = DataSource(
            id=uuid4(),
            organization_id=org.id,
            line_of_business_id=lob.id,
            data_domain_id=domain.id,
            project_id=project.id,
            name="primary",
            connector_type="postgres",
            dialect="postgres",
            environment="PROD",
            network_zone="default",
            credential_reference="env://TEST_DSN",
            capabilities={},
        )
        catalog = MetadataCatalog(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            name="primary",
            fingerprint="fp",
        )
        schema = MetadataSchema(
            id=uuid4(),
            organization_id=org.id,
            catalog_id=catalog.id,
            name="public",
            fingerprint="fp",
        )
        table = MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name="customers",
            object_type="BASE_TABLE",
            status="ACTIVE",
            fingerprint="fp",
        )
        for row in (org, lob, domain, project, datasource, catalog, schema, table):
            session.add(row)
            await session.flush()
        for ordinal, name in enumerate(("customer_id", "ssn")):
            session.add(
                MetadataColumn(
                    id=uuid4(),
                    organization_id=org.id,
                    table_id=table.id,
                    name=name,
                    ordinal_position=ordinal,
                    physical_type="VARCHAR",
                    nullable=True,
                    status="ACTIVE",
                    fingerprint="fp",
                )
            )
            await session.flush()
        await session.commit()
    context = SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"DataSteward"}),
    )
    return project.id, context


@pytest.mark.asyncio
async def test_two_simultaneous_proposals_raise_each_row_once(engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project_id, context = await _seed(sessions)
    async with sessions() as session:
        document = await upload_document(
            project_id,
            DocumentCreate(filename="dictionary.csv", content=_DICTIONARY_CSV),
            context,
            session,
        )
        document_id = document.id
        summary = await map_document(document_id, context, session)
    assert summary.matched_count == 2

    async def propose() -> object:
        async with sessions() as session:
            return await extract_claims(document_id, context, session)

    results = await asyncio.gather(propose(), propose(), return_exceptions=True)

    pages = [result for result in results if not isinstance(result, BaseException)]
    refusals = [result for result in results if isinstance(result, HTTPException)]
    assert len(pages) == 1, results
    assert getattr(pages[0], "total", None) == 2
    assert len(refusals) == 1, results
    assert refusals[0].status_code == 409
    async with sessions() as session:
        claims = await session.scalar(
            select(func.count())
            .select_from(DocumentClaim)
            .join(DocumentSection, DocumentSection.id == DocumentClaim.document_section_id)
            .where(DocumentSection.document_id == document_id)
        )
        reviews = await session.scalar(
            select(func.count())
            .select_from(GovernanceReview)
            .where(
                GovernanceReview.organization_id == context.organization_id,
                GovernanceReview.object_type == "DOCUMENT_CLAIM",
            )
        )
    assert claims == 2
    assert reviews == 2
