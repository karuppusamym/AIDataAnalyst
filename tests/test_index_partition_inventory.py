"""CT-3: index and partition normalized models, persisted end-to-end.

Runs the real `persist_discovery_snapshot` assembly path (the same one
`discover_datasource` and the push-ingestion routes use) and the real
`list_indexes`/`list_partitions` API endpoint bodies against an in-memory
SQLite database, for the same reason `test_catalog_pagination.py` does:
PostgreSQL is not reachable in this sandbox, but this still exercises real
ORM inserts, real fingerprint-based drift detection, and real queries rather
than mocks.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.api import list_indexes, list_partitions
from aida.connectors.base import (
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredIndex,
    DiscoveredPartition,
    DiscoveredSchema,
    DiscoveredTable,
)
from aida.db import Base
from aida.models import (
    AnalysisRun,
    DataSource,
    LineOfBusiness,
    MetadataIndex,
    MetadataPartition,
    Organization,
    Project,
)
from aida.security_types import SecurityContext
from aida.workflows.activities import persist_discovery_snapshot

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


async def _seed_datasource_and_run(session: AsyncSession) -> tuple[DataSource, AnalysisRun]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        project_id=project.id,
        name="primary",
        connector_type="oracle",
        dialect="oracle",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    run = AnalysisRun(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
        status="RUNNING",
    )
    session.add_all([org, lob, project, datasource, run])
    await session.flush()
    return datasource, run


def _catalog_with_account_table(
    *, indexes: tuple[DiscoveredIndex, ...], partitions: tuple[DiscoveredPartition, ...]
) -> tuple[DiscoveredCatalog, ...]:
    table = DiscoveredTable(
        name="account",
        object_type="BASE_TABLE",
        columns=(
            DiscoveredColumn(
                name="account_id", ordinal_position=1, physical_type="bigint", nullable=False
            ),
            DiscoveredColumn(
                name="opened_at", ordinal_position=2, physical_type="date", nullable=True
            ),
        ),
        indexes=indexes,
        partitions=partitions,
    )
    schema = DiscoveredSchema(name="retail", tables=(table,))
    return (DiscoveredCatalog(name="bank", schemas=(schema,)),)


async def test_persist_discovery_snapshot_creates_index_and_partition_rows(session) -> None:
    datasource, run = await _seed_datasource_and_run(session)
    catalogs = _catalog_with_account_table(
        indexes=(
            DiscoveredIndex(
                name="account_pk",
                index_type="NORMAL",
                columns=("account_id",),
                is_unique=True,
                is_primary=True,
            ),
        ),
        partitions=(
            DiscoveredPartition(
                name="p2025",
                partition_type="RANGE",
                ordinal_position=1,
                key_columns=("opened_at",),
                high_value="2026-01-01",
            ),
        ),
    )

    counts = await persist_discovery_snapshot(session, run, datasource, catalogs)
    await session.commit()

    assert counts["indexes"] == 1
    assert counts["partitions"] == 1
    assert counts["created_objects"] >= 2  # at minimum the new index + partition

    index = (await session.scalars(select(MetadataIndex))).one()
    assert index.name == "account_pk"
    assert index.is_primary is True
    assert index.is_unique is True
    assert index.columns == ["account_id"]
    assert index.status == "ACTIVE"

    partition = (await session.scalars(select(MetadataPartition))).one()
    assert partition.name == "p2025"
    assert partition.partition_type == "RANGE"
    assert partition.key_columns == ["opened_at"]
    assert partition.high_value == "2026-01-01"
    assert partition.status == "ACTIVE"

    assert run.discovered_indexes == 1
    assert run.discovered_partitions == 1


async def test_rerun_without_a_partition_deprecates_it_then_reactivates_on_return(
    session,
) -> None:
    datasource, run = await _seed_datasource_and_run(session)
    index = DiscoveredIndex(
        name="account_pk", index_type="NORMAL", columns=("account_id",), is_primary=True
    )
    partition = DiscoveredPartition(
        name="p2025", partition_type="RANGE", ordinal_position=1, key_columns=("opened_at",)
    )

    # First run: table has both the index and the partition.
    await persist_discovery_snapshot(
        session, run, datasource, _catalog_with_account_table(indexes=(index,), partitions=(partition,))
    )
    await session.commit()

    # Second run: the partition was dropped upstream (e.g. merged away); the
    # index is unchanged. Full-snapshot reconciliation should deprecate the
    # partition without touching the index or the table/columns.
    await persist_discovery_snapshot(
        session, run, datasource, _catalog_with_account_table(indexes=(index,), partitions=())
    )
    await session.commit()

    stored_partition = (await session.scalars(select(MetadataPartition))).one()
    assert stored_partition.status == "DEPRECATED"
    assert stored_partition.deprecated_at is not None
    stored_index = (await session.scalars(select(MetadataIndex))).one()
    assert stored_index.status == "ACTIVE"

    # Third run: the partition reappears (e.g. re-created upstream). Identity
    # is stable across the deprecate/reactivate cycle -- same row, same id --
    # matching the behaviour already relied on for tables/columns/constraints.
    await persist_discovery_snapshot(
        session, run, datasource, _catalog_with_account_table(indexes=(index,), partitions=(partition,))
    )
    await session.commit()

    reactivated = (await session.scalars(select(MetadataPartition))).one()
    assert reactivated.id == stored_partition.id
    assert reactivated.status == "ACTIVE"
    assert reactivated.deprecated_at is None


def _context(datasource: DataSource) -> SecurityContext:
    return SecurityContext(
        principal_id="tester",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"Viewer"}),
    )


async def test_list_indexes_and_list_partitions_endpoints_return_persisted_rows(session) -> None:
    from aida.models import MetadataTable

    datasource, run = await _seed_datasource_and_run(session)
    await persist_discovery_snapshot(
        session,
        run,
        datasource,
        _catalog_with_account_table(
            indexes=(
                DiscoveredIndex(
                    name="account_pk", index_type="NORMAL", columns=("account_id",), is_primary=True
                ),
            ),
            partitions=(
                DiscoveredPartition(
                    name="p2025", partition_type="RANGE", ordinal_position=1
                ),
            ),
        ),
    )
    await session.commit()
    table = (await session.scalars(select(MetadataTable))).one()
    context = _context(datasource)

    index_page = await list_indexes(
        table.id, limit=50, offset=0, cursor=None, context=context, session=session
    )
    assert [item.name for item in index_page.items] == ["account_pk"]
    assert index_page.total == 1

    partition_page = await list_partitions(
        table.id, limit=50, offset=0, cursor=None, context=context, session=session
    )
    assert [item.name for item in partition_page.items] == ["p2025"]
    assert partition_page.total == 1
