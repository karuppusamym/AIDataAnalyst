"""`GET /v1/search`: a column hit says which table and datasource it belongs to (R11-AUD08).

The handler built each column's table id and then dropped it: the hit's `datasource_id` was
None and its evidence `metadata` was empty, so five columns called `customer_id` were five
identical rows and the Search screen could not open any of them (`searchTargetFor` in
`ui-next/src/lib/searchTargets.ts` opens a column's table from `evidence.metadata.table_id`).

Runs the real handler against in-memory SQLite, seeded the way
`tests/test_documentation_worklist_api.py` seeds its estate.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.models import DataSource, MetadataCatalog, MetadataColumn, MetadataSchema, MetadataTable
from aida.search_api import global_search
from tests.support.doubles import security_context
from tests.test_documentation_worklist_api import _seed_datasource, _seed_table

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def _another_datasource(session: AsyncSession, first: DataSource) -> DataSource:
    """A second source in the same organization, which `_seed_datasource` cannot make (it
    creates a new organization each time)."""
    datasource = DataSource(
        id=uuid4(),
        organization_id=first.organization_id,
        line_of_business_id=first.line_of_business_id,
        data_domain_id=first.data_domain_id,
        project_id=first.project_id,
        name=f"src-{uuid4().hex[:8]}",
        connector_type="sqlserver",
        dialect="tsql",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN_2",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=first.organization_id,
        datasource_id=datasource.id,
        name="lending",
        fingerprint="fp",
    )
    session.add_all([datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=first.organization_id,
        catalog_id=catalog.id,
        name="dbo",
        fingerprint="fp",
    )
    session.add(schema)
    await session.flush()
    datasource._test_schema = schema  # type: ignore[attr-defined]
    return datasource


async def _column(session: AsyncSession, table: MetadataTable, name: str) -> MetadataColumn:
    column = MetadataColumn(
        id=uuid4(),
        organization_id=table.organization_id,
        table_id=table.id,
        name=name,
        ordinal_position=1,
        physical_type="VARCHAR",
        nullable=True,
        status="ACTIVE",
        fingerprint=f"fp-{name}",
    )
    session.add(column)
    await session.flush()
    return column


async def _search(
    session: AsyncSession, organization_id: UUID, q: str, **filters: Any
) -> dict[str, Any]:
    return await global_search(
        q=q,
        organization_id=organization_id,
        datasource_id=filters.get("datasource_id"),
        object_type=filters.get("object_type"),
        limit=25,
        offset=0,
        context=security_context(organization_id=organization_id),
        session=session,
    )


async def test_a_column_hit_names_its_table_and_datasource(session: AsyncSession) -> None:
    first = await _seed_datasource(session)
    second = await _another_datasource(session, first)
    accounts = await _seed_table(session, first, name="accounts")
    loans = await _seed_table(session, second, name="loans")
    on_accounts = await _column(session, accounts, "customer_id")
    on_loans = await _column(session, loans, "customer_id")

    answer = await _search(session, first.organization_id, "customer_id", object_type="COLUMN")

    by_id = {item["object_id"]: item for item in answer["items"]}
    assert set(by_id) == {str(on_accounts.id), str(on_loans.id)}
    for column, table in ((on_accounts, accounts), (on_loans, loans)):
        hit = by_id[str(column.id)]
        assert hit["datasource_id"] == table.datasource_id
        assert hit["qualified_name"] == f"{table.name}.customer_id"
        assert hit["evidence"]["metadata"] == {
            "column_id": str(column.id),
            "table_id": str(table.id),
            "table_name": table.name,
        }


async def test_a_table_hit_keeps_its_own_id_in_the_evidence(session: AsyncSession) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="customer_accounts")

    answer = await _search(session, datasource.organization_id, "customer", object_type="TABLE")

    (hit,) = answer["items"]
    assert hit["datasource_id"] == datasource.id
    assert hit["evidence"]["metadata"] == {"table_id": str(table.id)}


async def test_a_column_of_another_organization_is_never_a_hit(session: AsyncSession) -> None:
    ours = await _seed_datasource(session)
    theirs = await _seed_datasource(session)
    await _column(session, await _seed_table(session, theirs, name="accounts"), "customer_id")

    answer = await _search(session, ours.organization_id, "customer_id")

    assert answer["items"] == [] and answer["total"] == 0
