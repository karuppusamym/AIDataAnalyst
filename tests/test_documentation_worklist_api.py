"""AT-5 -- `GET /v1/organizations/{organization_id}/stewardship/documentation-worklist`.

Runs the real endpoint body (`aida.stewardship_api.list_documentation_worklist`)
against an in-memory SQLite database, following `test_asset_evidence.py`'s own
rationale: PostgreSQL is unreachable in this sandbox, but SQLite is a real SQL
engine that enforces the same row semantics the composed queries rely on.

Seeds real `QueryExecution` (governed SQL execution history, `query_gateway.py`)
and `ConsumptionRecord` (CX-4 MCP reads, `consumption_lineage.py`) rows, plus
GL-9 documentation state, and asserts the endpoint ranks by their combined
real volume, excludes documented tables, and enforces cross-org isolation the
same way `list_catalog_rows`/`list_unowned_asset_backlog` do.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.config import Settings
from aida.db import Base
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    ConsumptionRecord,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    QueryExecution,
)
from aida.stewardship_api import list_documentation_worklist
from tests.support.doubles import security_context

pytestmark = pytest.mark.asyncio

_SETTINGS = Settings()
_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


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


async def _seed_datasource(session: AsyncSession, *, organization_id=None) -> DataSource:
    org = Organization(
        id=organization_id or uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}"
    )
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
        name=f"src-{uuid4().hex[:8]}",
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
    datasource._test_schema = schema  # type: ignore[attr-defined]
    return datasource


async def _seed_table(session: AsyncSession, datasource: DataSource, *, name: str) -> MetadataTable:
    schema = datasource._test_schema  # type: ignore[attr-defined]
    table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return table


async def _seed_execution(
    session: AsyncSession,
    datasource: DataSource,
    *,
    referenced_tables: list[str],
    created_at: datetime,
    status: str = "COMPLETED",
) -> QueryExecution:
    execution = QueryExecution(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        principal_id="analyst@bank.example",
        status=status,
        dialect=datasource.dialect,
        sql_hash="deadbeef" * 8,
        referenced_tables=referenced_tables,
        created_at=created_at,
    )
    session.add(execution)
    await session.flush()
    return execution


async def _seed_consumption(
    session: AsyncSession, table: MetadataTable, *, consumed_at: datetime
) -> ConsumptionRecord:
    record = ConsumptionRecord(
        id=uuid4(),
        organization_id=table.organization_id,
        consumer_id="mcp-client",
        consumer_type="AGENT",
        resource_type="metadata_table",
        resource_id=str(table.id),
        channel="MCP",
        correlation_id=uuid4().hex,
        policy_decision="ALLOW",
        consumed_at=consumed_at,
    )
    session.add(record)
    await session.flush()
    return record


async def _document_table(session: AsyncSession, table: MetadataTable) -> None:
    documentation = AssetDocumentation(
        id=uuid4(), organization_id=table.organization_id, table_id=table.id
    )
    session.add(documentation)
    await session.flush()
    session.add(
        AssetDocumentationVersion(
            id=uuid4(),
            organization_id=table.organization_id,
            documentation_id=documentation.id,
            version=1,
            status="APPROVED",
            readme="A real, approved description.",
            created_by="drafter",
            approved_by="reviewer",
            approved_at=_NOW,
        )
    )
    await session.flush()


def _context(datasource: DataSource, **overrides: object) -> object:
    return security_context(organization_id=datasource.organization_id, **overrides)


async def _worklist(
    datasource: DataSource,
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    include_zero_volume: bool = False,
    context: object | None = None,
):
    return await list_documentation_worklist(
        datasource.organization_id,
        limit,
        offset,
        include_zero_volume,
        context or _context(datasource),
        session,
        _SETTINGS,
    )


# ---------------------------------------------------------------------------
# Ranked by real query volume
# ---------------------------------------------------------------------------


async def test_ranks_undocumented_tables_by_real_gateway_execution_volume(session) -> None:
    datasource = await _seed_datasource(session)
    await _seed_table(session, datasource, name="hot_table")
    await _seed_table(session, datasource, name="cold_table")

    # Three executions touched `hot_table`, one touched `cold_table`.
    for offset_minutes in (0, 10, 20):
        await _seed_execution(
            session,
            datasource,
            referenced_tables=["hot_table"],
            created_at=_NOW - timedelta(minutes=offset_minutes),
        )
    await _seed_execution(
        session, datasource, referenced_tables=["cold_table"], created_at=_NOW
    )
    await session.commit()

    page = await _worklist(datasource, session)

    assert page.total == 2
    assert [item.table_name for item in page.items] == ["hot_table", "cold_table"]
    assert page.items[0].query_execution_count == 3
    assert page.items[0].rank == 1
    assert page.items[1].query_execution_count == 1


async def test_gateway_and_consumption_volume_are_added_together(session) -> None:
    datasource = await _seed_datasource(session)
    await _seed_table(session, datasource, name="gateway_heavy")
    consumption_heavy = await _seed_table(session, datasource, name="consumption_heavy")

    for _ in range(2):
        await _seed_execution(
            session, datasource, referenced_tables=["gateway_heavy"], created_at=_NOW
        )
    for _ in range(5):
        await _seed_consumption(session, consumption_heavy, consumed_at=_NOW)
    await session.commit()

    page = await _worklist(datasource, session)

    by_name = {item.table_name: item for item in page.items}
    assert by_name["consumption_heavy"].query_volume == 5
    assert by_name["consumption_heavy"].consumption_read_count == 5
    assert by_name["consumption_heavy"].query_execution_count == 0
    assert by_name["gateway_heavy"].query_volume == 2
    assert by_name["gateway_heavy"].query_execution_count == 2
    # Ranked by the combined volume: 5 beats 2.
    assert [item.table_name for item in page.items] == ["consumption_heavy", "gateway_heavy"]


async def test_only_completed_executions_count_toward_volume(session) -> None:
    datasource = await _seed_datasource(session)
    await _seed_table(session, datasource, name="rejected_only")
    await _seed_execution(
        session,
        datasource,
        referenced_tables=["rejected_only"],
        created_at=_NOW,
        status="REJECTED",
    )
    await session.commit()

    page = await _worklist(datasource, session)

    # No COMPLETED execution and no consumption read -> zero volume -> excluded
    # by default, per this worklist's own documented design choice.
    assert page.total == 0


# ---------------------------------------------------------------------------
# Documented tables excluded
# ---------------------------------------------------------------------------


async def test_documented_table_is_excluded_even_with_high_query_volume(session) -> None:
    datasource = await _seed_datasource(session)
    documented = await _seed_table(session, datasource, name="documented_and_hot")
    await _seed_table(session, datasource, name="undocumented_and_cold")
    await _document_table(session, documented)

    for _ in range(10):
        await _seed_execution(
            session, datasource, referenced_tables=["documented_and_hot"], created_at=_NOW
        )
    await _seed_execution(
        session, datasource, referenced_tables=["undocumented_and_cold"], created_at=_NOW
    )
    await session.commit()

    page = await _worklist(datasource, session)

    assert page.total == 1
    assert page.items[0].table_name == "undocumented_and_cold"


# ---------------------------------------------------------------------------
# Zero-volume design choice, exercised end to end
# ---------------------------------------------------------------------------


async def test_zero_volume_tables_excluded_by_default_and_included_when_opted_in(session) -> None:
    datasource = await _seed_datasource(session)
    await _seed_table(session, datasource, name="queried")
    await _seed_table(session, datasource, name="never_touched")
    await _seed_execution(
        session, datasource, referenced_tables=["queried"], created_at=_NOW
    )
    await session.commit()

    default_page = await _worklist(datasource, session)
    assert default_page.total == 1
    assert [item.table_name for item in default_page.items] == ["queried"]

    opted_in_page = await _worklist(datasource, session, include_zero_volume=True)
    assert opted_in_page.total == 2
    assert [item.table_name for item in opted_in_page.items] == ["queried", "never_touched"]
    assert opted_in_page.items[-1].query_volume == 0


# ---------------------------------------------------------------------------
# Cross-org isolation and pagination
# ---------------------------------------------------------------------------


async def test_cross_org_tables_never_leak_into_the_worklist(session) -> None:
    datasource_a = await _seed_datasource(session)
    datasource_b = await _seed_datasource(session)
    await _seed_table(session, datasource_a, name="org_a_table")
    table_b = await _seed_table(session, datasource_b, name="org_b_table")
    await _seed_execution(
        session, datasource_a, referenced_tables=["org_a_table"], created_at=_NOW
    )
    await _seed_execution(
        session, datasource_b, referenced_tables=["org_b_table"], created_at=_NOW
    )
    await session.commit()

    page = await _worklist(datasource_a, session)

    assert [item.table_name for item in page.items] == ["org_a_table"]
    assert table_b.id not in {item.table_id for item in page.items}


async def test_pagination_returns_a_bounded_page_with_the_full_total(session) -> None:
    datasource = await _seed_datasource(session)
    for index in range(5):
        name = f"table_{index}"
        await _seed_table(session, datasource, name=name)
        # Fewer executions for a higher index -> descending by index gives a
        # deterministic, known order.
        for _ in range(5 - index):
            await _seed_execution(
                session, datasource, referenced_tables=[name], created_at=_NOW
            )
    await session.commit()

    page_one = await _worklist(datasource, session, limit=2, offset=0)
    page_two = await _worklist(datasource, session, limit=2, offset=2)

    assert page_one.total == 5
    assert [item.table_name for item in page_one.items] == ["table_0", "table_1"]
    assert [item.table_name for item in page_two.items] == ["table_2", "table_3"]
