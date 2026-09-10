"""OB-3: the sweep has a caller, and the endpoint stops reporting zeros.

The original audit finding was "zero call sites -- nothing writes
`AuditArchiveRecord`, yet `GET /observability/archive/status` reads it, so it
returns zeros forever while looking healthy"
(`Docs/60-delivery/04-end-to-end-audit-2026-08-30.md` Sec.2).

The 2026-09-05 review then found the caller that was added was not enough:
it wrote a record describing an archive that had never been stored anywhere
(F01). So this file now asserts the wiring *and* what the wiring is allowed
to claim -- a status endpoint that counts only VERIFIED archives, and a
sweep that reaches VERIFIED only through a destination that took the bytes.

The lifecycle itself (states, membership, leases, late arrivals, crash
recovery) is covered in `tests/test_worm_archive_lifecycle.py`; this file
stays focused on the wiring the audit named.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.audit_archive_storage import FilesystemArchiveStorage, NullArchiveStorage
from aida.db import Base
from aida.models import AuditArchiveRecord, AuditEvent, Organization
from aida.observability_api import get_archive_status
from aida.security_types import SecurityContext
from aida.worm_archive import (
    STATE_FAILED,
    STATE_VERIFIED,
    ArchiveConfig,
    archive_pending_audit_events,
    storage_for,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


@pytest.fixture
def storage(tmp_path: Path) -> FilesystemArchiveStorage:
    return FilesystemArchiveStorage(tmp_path / "worm")


def _config(tmp_path: Path) -> ArchiveConfig:
    return ArchiveConfig(
        retention_days=2555,
        storage_backend="filesystem",
        filesystem_root=str(tmp_path / "worm"),
    )


async def _seed_organization(session: AsyncSession) -> Organization:
    org = Organization(id=uuid4(), name="Test Bank", slug=f"test-bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.commit()
    return org


async def _seed_audit_events(session: AsyncSession, org: Organization, count: int) -> None:
    for i in range(count):
        session.add(
            AuditEvent(
                id=i + 1,
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
    await session.commit()


def _context(org: Organization) -> SecurityContext:
    return SecurityContext(
        principal_id="ops-1",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"Operations"}),
    )


async def test_sweep_persists_a_record_backed_by_a_stored_object(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=3)

    result = await archive_pending_audit_events(
        session, org.id, ArchiveConfig(storage_backend="filesystem"), storage=storage
    )

    assert result is not None
    assert result.state == STATE_VERIFIED
    assert result.archived_count == 3

    stored = (
        await session.scalars(
            select(AuditArchiveRecord).where(AuditArchiveRecord.organization_id == org.id)
        )
    ).all()
    assert len(stored) == 1
    assert stored[0].event_count == 3
    assert stored[0].archive_id == result.archive_id
    assert stored[0].checksum == result.checksum
    assert stored[0].storage_uri == result.storage_uri
    assert storage.read(str(stored[0].storage_uri))


async def test_second_sweep_with_nothing_new_is_a_no_op(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)
    config = ArchiveConfig(storage_backend="filesystem")

    first = await archive_pending_audit_events(session, org.id, config, storage=storage)
    assert first is not None
    assert first.archived_count == 2

    assert await archive_pending_audit_events(session, org.id, config, storage=storage) is None


async def test_sweep_returns_none_with_nothing_to_archive(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    org = await _seed_organization(session)
    result = await archive_pending_audit_events(
        session, org.id, ArchiveConfig(storage_backend="filesystem"), storage=storage
    )
    assert result is None


async def test_configured_legal_hold_reaches_the_record_and_the_object(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=1)

    config = ArchiveConfig(storage_backend="filesystem", legal_hold_enabled=True)
    result = await archive_pending_audit_events(session, org.id, config, storage=storage)

    assert result is not None
    assert result.legal_hold is True
    assert storage.manifest(str(result.storage_uri))["legal_hold"] is True

    stored = await session.scalar(
        select(AuditArchiveRecord).where(AuditArchiveRecord.organization_id == org.id)
    )
    assert stored is not None
    assert stored.legal_hold is True


# --- the endpoint the audit flagged ---------------------------------------


async def test_archive_status_endpoint_reflects_a_real_non_zero_count(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    org = await _seed_organization(session)
    context = _context(org)

    before = await get_archive_status(context=context, session=session)
    assert before.total_archives == 0
    assert before.total_events_archived == 0
    assert before.status == "NO_ARCHIVES"

    await _seed_audit_events(session, org, count=5)
    result = await archive_pending_audit_events(
        session, org.id, ArchiveConfig(storage_backend="filesystem"), storage=storage
    )
    await session.commit()
    assert result is not None

    after = await get_archive_status(context=context, session=session)
    assert after.total_archives == 1
    assert after.total_events_archived == 5
    assert after.status == "HEALTHY"
    assert after.latest_archive_id == result.archive_id
    assert after.latest_checksum == result.checksum


async def test_archive_status_stays_at_zero_when_the_destination_refuses(
    session: AsyncSession,
) -> None:
    """The regression the review found: metadata without an archive behind it."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=4)

    result = await archive_pending_audit_events(
        session, org.id, ArchiveConfig(), storage=NullArchiveStorage()
    )
    assert result is not None
    assert result.state == STATE_FAILED

    status = await get_archive_status(context=_context(org), session=session)
    assert status.total_archives == 0
    assert status.total_events_archived == 0
    assert status.status == "NO_ARCHIVES"


def test_the_loop_resolves_a_provider_that_matches_its_configuration(
    tmp_path: Path,
) -> None:
    """`main._audit_archive_loop` resolves its provider through `storage_for`."""
    assert storage_for(ArchiveConfig()).available is False
    assert storage_for(ArchiveConfig(storage_backend="s3")).available is False
    assert storage_for(_config(tmp_path)).available is True
