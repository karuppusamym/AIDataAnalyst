"""End-to-end coverage for CT-2 (keyset pagination) against a real database.

PostgreSQL itself is not reachable in this sandbox (no live server, no Docker
daemon), so these tests run the real ORM models and the real `list_tables`/
`list_columns` endpoint bodies against an in-memory SQLite database via
aiosqlite. SQLite is not the production target, but it is a real SQL engine
that enforces the same row-value comparison semantics `apply_keyset` relies
on (verified directly in `tests/test_pagination.py` against both the SQLite
and PostgreSQL dialects), so this exercises genuine query execution and
correctness -- not a mock -- for the parts that don't depend on PostgreSQL's
planner.

Pagination contract exercised here: the very first request (`cursor=None`)
runs the offset branch -- it returns a `total` (one `COUNT(*)`) *and* a
`next_cursor`. Every subsequent request should be made with that cursor, which
runs the keyset branch: no `COUNT(*)`, no `OFFSET`, cost bounded by `limit`.
"""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.api import list_columns, list_tables
from aida.config import Settings
from aida.db import Base
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.security_types import SecurityContext

pytestmark = pytest.mark.asyncio

_SETTINGS = Settings()


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


async def _seed_datasource(session: AsyncSession) -> tuple[DataSource, MetadataSchema]:
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
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=org.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp",
    )
    session.add(schema)
    await session.flush()
    return datasource, schema


async def _seed_tables(
    session: AsyncSession, datasource: DataSource, schema: MetadataSchema, *, count: int
) -> list[MetadataTable]:
    tables = [
        MetadataTable(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            # Zero-padded names so lexical == numeric order, avoiding an
            # off-by-one trap where "table_10" < "table_2" as plain strings.
            name=f"table_{i:04d}",
            object_type="BASE_TABLE",
            fingerprint="fp",
        )
        for i in range(count)
    ]
    session.add_all(tables)
    await session.flush()
    return tables


def _context(datasource: DataSource) -> SecurityContext:
    return SecurityContext(
        principal_id="tester",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"Viewer"}),
    )


async def test_cursor_pagination_walks_every_row_exactly_once_in_order(session) -> None:
    datasource, schema = await _seed_datasource(session)
    tables = await _seed_tables(session, datasource, schema, count=23)
    await session.commit()
    context = _context(datasource)

    seen_names: list[str] = []
    cursor: str | None = None
    page_count = 0
    saw_a_count_query = False
    while True:
        page = await list_tables(
            datasource.id,
            q=None,
            object_type=None,
            table_status="ACTIVE",
            limit=5,
            offset=0,
            cursor=cursor,
            context=context,
            session=session,
            settings=_SETTINGS,
        )
        page_count += 1
        assert len(page.items) <= 5
        if page.total is not None:
            saw_a_count_query = True
            assert page_count == 1  # only the very first request may pay for a COUNT(*)
        else:
            assert page_count > 1  # every later request is pure keyset: no COUNT(*)
        seen_names.extend(item.name for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert page_count < 20, "pagination did not terminate"

    assert saw_a_count_query
    expected = sorted(table.name for table in tables)
    assert seen_names == expected  # exact order, no duplicates, no gaps
    assert page_count == 5  # ceil(23 / 5)


async def test_cursor_pagination_is_stable_under_concurrent_inserts(session) -> None:
    """The classic failure mode of OFFSET pagination: a row inserted between two
    page fetches shifts every subsequent row's position by one slot, so the next
    `OFFSET N` either re-shows a row already returned or silently skips one.
    Keyset pagination anchors on the last *key* seen (not a row count), so a row
    inserted anywhere at or before that key is simply excluded by the `> cursor`
    predicate -- it cannot perturb a page already walked or corrupt the next one.
    """
    datasource, schema = await _seed_datasource(session)
    await _seed_tables(session, datasource, schema, count=6)  # table_0000 .. table_0005
    await session.commit()
    context = _context(datasource)

    first_page = await list_tables(
        datasource.id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        limit=3,
        offset=0,
        cursor=None,
        context=context,
        session=session,
        settings=_SETTINGS,
    )
    assert [item.name for item in first_page.items] == [
        "table_0000",
        "table_0001",
        "table_0002",
    ]
    assert first_page.next_cursor is not None

    # Insert a row that sorts strictly between two rows already returned in the
    # first page ("table_0001" < "table_0001a" < "table_0002"). With OFFSET
    # pagination this insert would shift table_0003.. by one slot and cause the
    # next page to re-show table_0002. With keyset pagination the cursor is
    # anchored on ("table_0002", <its id>), and "table_0001a" sorts before that
    # anchor, so it is excluded by construction.
    schema_id = (await session.get(MetadataTable, first_page.items[0].id)).schema_id
    session.add(
        MetadataTable(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            schema_id=schema_id,
            name="table_0001a",
            object_type="BASE_TABLE",
            fingerprint="fp",
        )
    )
    await session.flush()

    second_page = await list_tables(
        datasource.id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        limit=3,
        offset=0,
        cursor=first_page.next_cursor,
        context=context,
        session=session,
        settings=_SETTINGS,
    )
    assert [item.name for item in second_page.items] == [
        "table_0003",
        "table_0004",
        "table_0005",
    ]
    # Nothing from the first page reappears, and the mid-inserted row never
    # surfaces on this forward walk (it sorts behind the cursor).
    first_names = {item.name for item in first_page.items}
    second_names = {item.name for item in second_page.items}
    assert first_names.isdisjoint(second_names)
    assert "table_0001a" not in second_names


async def test_offset_mode_first_page_reports_total_and_a_continuation_cursor(session) -> None:
    datasource, schema = await _seed_datasource(session)
    await _seed_tables(session, datasource, schema, count=4)
    await session.commit()
    context = _context(datasource)

    page = await list_tables(
        datasource.id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        limit=2,
        offset=0,
        cursor=None,
        context=context,
        session=session,
        settings=_SETTINGS,
    )
    assert page.total == 4
    assert page.next_cursor is not None  # 2 more rows remain
    assert len(page.items) == 2


async def test_last_page_reports_no_next_cursor(session) -> None:
    datasource, schema = await _seed_datasource(session)
    await _seed_tables(session, datasource, schema, count=2)
    await session.commit()
    context = _context(datasource)

    page = await list_tables(
        datasource.id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        limit=10,
        offset=0,
        cursor=None,
        context=context,
        session=session,
        settings=_SETTINGS,
    )
    assert page.total == 2
    assert page.next_cursor is None
    assert len(page.items) == 2


async def test_invalid_cursor_is_rejected_as_bad_request(session) -> None:
    datasource, _schema = await _seed_datasource(session)
    await session.commit()
    context = _context(datasource)

    with pytest.raises(HTTPException) as exc_info:
        await list_tables(
            datasource.id,
            q=None,
            object_type=None,
            table_status="ACTIVE",
            limit=10,
            offset=0,
            cursor="not-a-real-cursor",
            context=context,
            session=session,
            settings=_SETTINGS,
        )
    assert exc_info.value.status_code == 400


async def test_list_columns_cursor_pagination_orders_by_ordinal_position(session) -> None:
    datasource, schema = await _seed_datasource(session)
    tables = await _seed_tables(session, datasource, schema, count=1)
    table = tables[0]
    columns = [
        MetadataColumn(
            id=uuid4(),
            organization_id=datasource.organization_id,
            table_id=table.id,
            name=f"col_{i}",
            ordinal_position=i,
            physical_type="text",
            nullable=True,
            fingerprint="fp",
        )
        for i in range(11)
    ]
    session.add_all(columns)
    await session.commit()
    context = _context(datasource)

    seen: list[int] = []
    cursor: str | None = None
    while True:
        page = await list_columns(
            table.id,
            limit=4,
            offset=0,
            cursor=cursor,
            context=context,
            session=session,
            settings=_SETTINGS,
        )
        seen.extend(item.ordinal_position for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert seen == list(range(11))
