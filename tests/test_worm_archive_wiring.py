"""OB-3: the audit's finding was "Zero call sites. Nothing writes
`AuditArchiveRecord`, yet `GET /observability/archive/status` reads it -- so
it returns zeros forever while looking healthy."
(`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` Sec.2).

`aida.worm_archive.archive_pending_audit_events` is the real trigger that
was missing: it reads real `AuditEvent` rows, calls the (already-correct,
already-tested) pure `archive_audit_events`, and persists the result as a
real `AuditArchiveRecord`. This module proves the full loop end to end,
including the observability API endpoint that reads it back.
"""

import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.db import Base
from aida.models import AuditArchiveRecord, AuditEvent, Organization
from aida.observability_api import get_archive_status
from aida.security_types import SecurityContext
from aida.worm_archive import ArchiveConfig, archive_pending_audit_events

_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_organization(session: AsyncSession) -> Organization:
    org = Organization(id=uuid4(), name="Test Bank", slug=f"test-bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _seed_audit_events(session: AsyncSession, org: Organization, count: int) -> None:
    for i in range(count):
        session.add(
            AuditEvent(
                organization_id=org.id,
                principal_id="analyst-1",
                principal_type="USER",
                action="data_access",
                resource_type="table",
                resource_id=f"tbl-{i}",
                outcome="SUCCESS",
                correlation_id=f"corr-{i}",
                details={},
                occurred_at=datetime(2026, 8, 30, 12, i, tzinfo=UTC),
            )
        )
    await session.flush()


async def test_archive_pending_audit_events_persists_a_real_archive_record(
    session: AsyncSession,
) -> None:
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=3)

    config = ArchiveConfig(retention_days=2555, storage_backend="s3")
    result = await archive_pending_audit_events(session, org.id, config)
    await session.commit()

    assert result is not None
    assert result.archived_count == 3
    assert result.archive_id.startswith("archive-")

    stored = (
        await session.scalars(
            select(AuditArchiveRecord).where(AuditArchiveRecord.organization_id == org.id)
        )
    ).all()
    assert len(stored) == 1
    assert stored[0].event_count == 3
    assert stored[0].archive_id == result.archive_id
    assert stored[0].checksum == result.checksum
    assert stored[0].legal_hold is False


async def test_archive_pending_audit_events_is_incremental(session: AsyncSession) -> None:
    """A second cycle only picks up events newer than the last archive's
    `event_range_end` -- it never re-archives the same rows.
    """
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)

    config = ArchiveConfig()
    first = await archive_pending_audit_events(session, org.id, config)
    await session.commit()
    assert first is not None
    assert first.archived_count == 2

    # Nothing new yet -- the sweep is a genuine no-op, not a re-archive.
    second = await archive_pending_audit_events(session, org.id, config)
    assert second is None

    # A fresh event after the first batch is picked up on the next cycle.
    session.add(
        AuditEvent(
            organization_id=org.id,
            principal_id="analyst-1",
            principal_type="USER",
            action="data_access",
            resource_type="table",
            resource_id="tbl-new",
            outcome="SUCCESS",
            correlation_id="corr-new",
            details={},
            occurred_at=datetime(2026, 8, 30, 13, 0, tzinfo=UTC),
        )
    )
    await session.flush()

    third = await archive_pending_audit_events(session, org.id, config)
    await session.commit()
    assert third is not None
    assert third.archived_count == 1


async def test_archive_pending_audit_events_returns_none_with_nothing_to_archive(
    session: AsyncSession,
) -> None:
    org = await _seed_organization(session)
    result = await archive_pending_audit_events(session, org.id, ArchiveConfig())
    assert result is None


async def test_legal_hold_config_is_reflected_on_the_persisted_record(
    session: AsyncSession,
) -> None:
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=1)

    config = ArchiveConfig(legal_hold_enabled=True)
    result = await archive_pending_audit_events(session, org.id, config)
    await session.commit()

    assert result is not None
    assert result.legal_hold is True
    stored = await session.scalar(
        select(AuditArchiveRecord).where(AuditArchiveRecord.organization_id == org.id)
    )
    assert stored is not None
    assert stored.legal_hold is True


# --- the endpoint the audit flagged: it must stop returning zeros ----------


async def test_archive_status_endpoint_reflects_a_real_non_zero_count(
    session: AsyncSession,
) -> None:
    """This is the exact endpoint the audit named: `GET
    /observability/archive/status` reading `AuditArchiveRecord` while
    nothing ever wrote to it, so it silently reported zeros forever. After a
    real archive cycle, it must report a real, non-zero count.
    """
    org = await _seed_organization(session)

    context = SecurityContext(
        principal_id="ops-1",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"Operations"}),
    )

    before = await get_archive_status(context=context, session=session)
    assert before.total_archives == 0
    assert before.total_events_archived == 0
    assert before.status == "NO_ARCHIVES"

    await _seed_audit_events(session, org, count=5)
    result = await archive_pending_audit_events(session, org.id, ArchiveConfig())
    await session.commit()
    assert result is not None

    after = await get_archive_status(context=context, session=session)
    assert after.total_archives == 1
    assert after.total_events_archived == 5
    assert after.status == "HEALTHY"
    assert after.latest_archive_id == result.archive_id
    assert after.latest_checksum == result.checksum
