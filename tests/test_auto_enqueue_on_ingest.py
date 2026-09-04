"""ING-4 / P0-01: auto-enqueue AI drafts on new-table ingest.

Covers the P0-01 fix (`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md`,
`Docs/60-delivery/10-session-2026-09-04-auto-enqueue.md`) end to end
against a real in-memory SQLite engine, mirroring the aiosqlite pattern
`tests/test_catalog_pagination.py` established.

What is exercised:

1. `persist_discovery_snapshot` emits one
   `catalog.table.newly_created.v1` outbox event per *newly created*
   table (not per reactivated or updated one) and *only* when
   `AIDA_AUTO_ENQUEUE_ON_INGEST=true`.
2. Setting `AIDA_AUTO_ENQUEUE_ON_INGEST=false` suppresses emission
   entirely without changing the ingest behavior in any other way.
3. `handle_newly_created_table` is idempotent: delivering the same
   event twice yields exactly one `AssetDescriptionDraft`, not two.
4. `handle_newly_created_table` respects a stewarded description --
   an `AssetDocumentationVersion` with `status='APPROVED'` on a table
   causes the handler to skip that table (never overwrite).
5. `handle_newly_created_table` DEFERS semantic inference (records a
   `business_semantics.inference.auto_enqueue_deferred.v1` outbox
   event and a `DEFERRED` audit outcome, never fails) when no
   `AnalysisRun` has reached `COMPLETED` yet -- matching the HTTP
   endpoint's 409 gate without turning a defer into a failure on the
   auto-enqueue path.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.connectors.base import (
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredSchema,
    DiscoveredTable,
)
from aida.db import Base
from aida.models import (
    AnalysisRun,
    AssetDescriptionDraft,
    AssetDocumentation,
    AssetDocumentationVersion,
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataTable,
    Organization,
    OutboxEvent,
    Project,
)
from aida.newly_created_table_drafter import (
    NEWLY_CREATED_TABLE_EVENT_TYPE,
    handle_newly_created_table,
)
from aida.workflows.activities import persist_discovery_snapshot

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with maker() as db_session:
        yield db_session
    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """The `Settings` object is `lru_cache`d on `get_settings`; a test that
    patches `AIDA_AUTO_ENQUEUE_ON_INGEST` via monkeypatch would otherwise
    read the cached instance and see the previous test's value. Clearing
    around every test is cheaper than yielding a live env fixture per test."""
    from aida.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_datasource(session: AsyncSession) -> tuple[DataSource, AnalysisRun]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(),
        organization_id=org.id,
        name="Retail",
        code=f"RTL{uuid4().hex[:6]}",
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Deposits",
        code=f"DEP{uuid4().hex[:6]}",
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
    session.add_all([org, lob, domain, project, datasource])
    await session.flush()
    run = AnalysisRun(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        mode="INCREMENTAL",
        trigger_type="MANUAL",
        status="RUNNING",
    )
    session.add(run)
    await session.flush()
    return datasource, run


def _column(name: str, position: int) -> DiscoveredColumn:
    return DiscoveredColumn(
        name=name,
        ordinal_position=position,
        physical_type="bigint",
        nullable=False,
    )


def _catalog(table_names: list[str]) -> tuple[DiscoveredCatalog, ...]:
    tables = tuple(
        DiscoveredTable(
            name=name,
            object_type="BASE_TABLE",
            columns=(_column("id", 1), _column("customer_id", 2)),
        )
        for name in table_names
    )
    return (
        DiscoveredCatalog(
            name="bank",
            schemas=(DiscoveredSchema(name="public", tables=tables),),
        ),
    )


async def _newly_created_events(session: AsyncSession) -> list[OutboxEvent]:
    return list(
        await session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type == NEWLY_CREATED_TABLE_EVENT_TYPE
            )
        )
    )


async def test_ingest_of_three_new_tables_emits_three_newly_created_events(
    session: AsyncSession,
) -> None:
    datasource, run = await _seed_datasource(session)

    await persist_discovery_snapshot(
        session,
        run,
        datasource,
        _catalog(["accounts", "customers", "transactions"]),
        deprecate_missing=False,
        connector_capabilities={},
    )
    await session.flush()

    events = await _newly_created_events(session)
    assert len(events) == 3
    assert {UUID(event.payload["datasource_id"]) for event in events} == {datasource.id}
    emitted_table_ids = {UUID(event.payload["table_id"]) for event in events}
    persisted_ids = {
        row.id
        for row in await session.scalars(
            select(MetadataTable).where(MetadataTable.datasource_id == datasource.id)
        )
    }
    assert emitted_table_ids == persisted_ids
    # Auto-enqueue audit row records the batch-level summary.
    audits = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "AUTO_ENQUEUE_DRAFTS_ON_INGEST"
            )
        )
    )
    assert len(audits) == 1
    assert audits[0].details["newly_created_table_count"] == 3


async def test_reingest_of_the_same_tables_does_not_re_emit_events(
    session: AsyncSession,
) -> None:
    """A `catalog.table.newly_created.v1` event fires on the first sighting
    only; a subsequent snapshot that re-observes the exact same tables is
    an update, not a creation, and must not re-emit."""
    datasource, run = await _seed_datasource(session)
    catalog = _catalog(["accounts", "customers"])

    await persist_discovery_snapshot(
        session, run, datasource, catalog, deprecate_missing=False,
        connector_capabilities={},
    )
    await session.flush()
    assert len(await _newly_created_events(session)) == 2

    # Second pass: same catalog, same tables. No new events.
    await persist_discovery_snapshot(
        session, run, datasource, catalog, deprecate_missing=False,
        connector_capabilities={},
    )
    await session.flush()
    assert len(await _newly_created_events(session)) == 2


async def test_config_flag_false_suppresses_emission(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIDA_AUTO_ENQUEUE_ON_INGEST", "false")
    datasource, run = await _seed_datasource(session)

    await persist_discovery_snapshot(
        session,
        run,
        datasource,
        _catalog(["accounts", "customers", "transactions"]),
        deprecate_missing=False,
        connector_capabilities={},
    )
    await session.flush()

    events = await _newly_created_events(session)
    assert events == []
    # And no auto-enqueue audit row when the flag is off.
    audits = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "AUTO_ENQUEUE_DRAFTS_ON_INGEST"
            )
        )
    )
    assert audits == []
    # But ingest itself still worked -- the tables landed.
    persisted = list(
        await session.scalars(
            select(MetadataTable).where(MetadataTable.datasource_id == datasource.id)
        )
    )
    assert {row.name for row in persisted} == {
        "accounts",
        "customers",
        "transactions",
    }


async def test_handler_is_idempotent_across_duplicate_events(
    session: AsyncSession,
) -> None:
    datasource, run = await _seed_datasource(session)
    await persist_discovery_snapshot(
        session, run, datasource, _catalog(["accounts"]),
        deprecate_missing=False, connector_capabilities={},
    )
    await session.flush()
    table = await session.scalar(
        select(MetadataTable).where(MetadataTable.datasource_id == datasource.id)
    )
    assert table is not None
    payload = {
        "organization_id": str(datasource.organization_id),
        "datasource_id": str(datasource.id),
        "table_id": str(table.id),
        "analysis_run_id": str(run.id),
    }

    await handle_newly_created_table(session, payload)
    await session.flush()
    drafts_after_first = list(
        await session.scalars(
            select(AssetDescriptionDraft).where(
                AssetDescriptionDraft.table_id == table.id
            )
        )
    )
    assert len(drafts_after_first) == 1

    # Same event, second delivery: no second draft, no crash.
    await handle_newly_created_table(session, payload)
    await session.flush()
    drafts_after_second = list(
        await session.scalars(
            select(AssetDescriptionDraft).where(
                AssetDescriptionDraft.table_id == table.id
            )
        )
    )
    assert len(drafts_after_second) == 1
    assert drafts_after_second[0].id == drafts_after_first[0].id


async def test_handler_skips_table_with_approved_description(
    session: AsyncSession,
) -> None:
    datasource, run = await _seed_datasource(session)
    await persist_discovery_snapshot(
        session, run, datasource, _catalog(["accounts"]),
        deprecate_missing=False, connector_capabilities={},
    )
    await session.flush()
    table = await session.scalar(
        select(MetadataTable).where(MetadataTable.datasource_id == datasource.id)
    )
    assert table is not None
    # A stewarded description already exists on this table: the handler
    # must never overwrite it with an AI draft.
    documentation = AssetDocumentation(
        id=uuid4(),
        organization_id=datasource.organization_id,
        table_id=table.id,
    )
    session.add(documentation)
    await session.flush()
    session.add(
        AssetDocumentationVersion(
            id=uuid4(),
            organization_id=datasource.organization_id,
            documentation_id=documentation.id,
            version=1,
            status="APPROVED",
            aliases=[],
            readme="Stewarded description of the accounts table.",
            created_by="steward-alice",
            approved_by="steward-carol",
        )
    )
    await session.flush()

    await handle_newly_created_table(
        session,
        {
            "organization_id": str(datasource.organization_id),
            "datasource_id": str(datasource.id),
            "table_id": str(table.id),
            "analysis_run_id": str(run.id),
        },
    )
    await session.flush()
    drafts = list(
        await session.scalars(
            select(AssetDescriptionDraft).where(
                AssetDescriptionDraft.table_id == table.id
            )
        )
    )
    assert drafts == []


async def test_handler_defers_semantic_inference_when_no_analysis_run_completed(
    session: AsyncSession,
) -> None:
    """The analysis run seeded here is `RUNNING`, not `COMPLETED`, so the
    HTTP-endpoint gate (`create_semantic_inference_run` returns 409) is
    tripped -- but on the auto-enqueue path the handler must DEFER
    (audit outcome `DEFERRED`, emit
    `business_semantics.inference.auto_enqueue_deferred.v1`), never
    fail, so a later profiling-complete pass can pick the table back
    up. The description draft still lands regardless."""
    datasource, run = await _seed_datasource(session)
    await persist_discovery_snapshot(
        session, run, datasource, _catalog(["accounts"]),
        deprecate_missing=False, connector_capabilities={},
    )
    await session.flush()
    table = await session.scalar(
        select(MetadataTable).where(MetadataTable.datasource_id == datasource.id)
    )
    assert table is not None

    await handle_newly_created_table(
        session,
        {
            "organization_id": str(datasource.organization_id),
            "datasource_id": str(datasource.id),
            "table_id": str(table.id),
            "analysis_run_id": str(run.id),
        },
    )
    await session.flush()

    # A description draft still lands -- the defer is specific to the
    # semantic-inference half, not the whole handler.
    drafts = list(
        await session.scalars(
            select(AssetDescriptionDraft).where(
                AssetDescriptionDraft.table_id == table.id
            )
        )
    )
    assert len(drafts) == 1

    # And the DEFERRED audit outcome + deferred outbox row are recorded,
    # so an operator can see the auto-enqueue path noticed the gap.
    audits = list(
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "AUTO_ENQUEUE_DRAFTS_ON_INGEST",
                AuditEvent.resource_id == str(table.id),
            )
        )
    )
    assert any(audit.outcome == "DEFERRED" for audit in audits)
    deferred_events = list(
        await session.scalars(
            select(OutboxEvent).where(
                OutboxEvent.event_type
                == "business_semantics.inference.auto_enqueue_deferred.v1"
            )
        )
    )
    assert len(deferred_events) == 1
    assert UUID(deferred_events[0].payload["table_id"]) == table.id
