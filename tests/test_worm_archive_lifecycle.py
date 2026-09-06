"""Review F01 + F03: the archive lifecycle, end to end against a real schema.

What each acceptance criterion from the review maps to here:

* "failed upload produces a retryable failure, not an archived count" ->
  `test_unconfigured_destination_fails_instead_of_reporting_success`
* "retrieve and verify the destination object" ->
  `test_verified_archive_is_retrievable_and_reverifies`
* "equal-timestamp batches ... produce no omitted event" ->
  `test_equal_timestamp_batch_boundary_loses_nothing`
* "late commits" -> `test_late_arriving_event_is_picked_up`
* "crash-after-upload" -> `test_crash_after_upload_recovers_idempotently`
* "simultaneous workers" -> `test_lease_stops_a_second_worker`,
  `test_membership_is_the_backstop_against_double_archive`
* legal hold blocks expiry -> `test_legal_hold_blocks_expiry_and_release_restores_it`

The schema is built from the ORM against in-memory SQLite, as every other
DB-backed test file here does; the Alembic side of the same change is
covered by `tests/test_migration_orm_drift.py` in CI.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.audit_archive_storage import (
    ArchiveObject,
    ArchiveStorageError,
    FilesystemArchiveStorage,
    LegalHoldError,
    NullArchiveStorage,
    StoredObject,
    VerificationResult,
)
from aida.audit_envelope import ENVELOPE_VERSION
from aida.db import Base
from aida.models import (
    AuditArchiveMembership,
    AuditArchiveRecord,
    AuditEvent,
    Organization,
)
from aida.observability_api import get_archive_status
from aida.security_types import SecurityContext
from aida.worm_archive import (
    STATE_FAILED,
    STATE_VERIFIED,
    ArchiveConfig,
    acquire_archive_lease,
    apply_legal_hold,
    archive_pending_audit_events,
    release_legal_hold,
    verify_archive_record,
)

BASE_TIME = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


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


def _config(**overrides: Any) -> ArchiveConfig:
    base: dict[str, Any] = {
        "retention_days": 2555,
        "storage_backend": "filesystem",
        "late_arrival_overlap_seconds": 86_400,
    }
    base.update(overrides)
    return ArchiveConfig(**base)


async def _seed_organization(session: AsyncSession) -> Organization:
    org = Organization(id=uuid4(), name="Test Bank", slug=f"test-bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.commit()
    return org


async def _seed_event(
    session: AsyncSession,
    org: Organization,
    *,
    event_id: int,
    occurred_at: datetime,
    outcome: str = "SUCCESS",
) -> AuditEvent:
    """Ids are assigned explicitly so equal-timestamp ordering is deterministic."""
    event = AuditEvent(
        id=event_id,
        organization_id=org.id,
        principal_id="analyst-1",
        principal_type="USER",
        action="data_access",
        resource_type="table",
        resource_id=f"tbl-{event_id}",
        outcome=outcome,
        correlation_id=f"corr-{event_id}",
        source_ip="10.0.0.1",
        details={"rows": event_id},
        occurred_at=occurred_at,
    )
    session.add(event)
    await session.commit()
    return event


async def _seed_events(
    session: AsyncSession, org: Organization, count: int, *, spacing_minutes: int = 1
) -> None:
    for index in range(count):
        await _seed_event(
            session,
            org,
            event_id=index + 1,
            occurred_at=BASE_TIME + timedelta(minutes=index * spacing_minutes),
        )


async def _membership_ids(session: AsyncSession, org: Organization) -> set[int]:
    rows = await session.scalars(
        select(AuditArchiveMembership.audit_event_id).where(
            AuditArchiveMembership.organization_id == org.id
        )
    )
    return {int(value) for value in rows}


async def _records(session: AsyncSession, org: Organization) -> list[AuditArchiveRecord]:
    rows = await session.scalars(
        select(AuditArchiveRecord)
        .where(AuditArchiveRecord.organization_id == org.id)
        .order_by(AuditArchiveRecord.created_at.asc())
    )
    return list(rows)


# --- F01: nothing is reported archived that was not stored -----------------


async def test_unconfigured_destination_fails_instead_of_reporting_success(
    session: AsyncSession,
) -> None:
    """The defect verbatim: no destination used to mean a success object."""
    org = await _seed_organization(session)
    await _seed_events(session, org, count=3)

    result = await archive_pending_audit_events(
        session, org.id, _config(storage_backend="none"), storage=NullArchiveStorage()
    )

    assert result is not None
    assert result.state == STATE_FAILED
    assert result.archived_count == 0
    assert result.retryable is True
    assert result.failure_reason is not None
    assert "no archive destination is configured" in result.failure_reason

    records = await _records(session, org)
    assert len(records) == 1
    assert records[0].state == STATE_FAILED
    assert records[0].storage_uri is None
    assert records[0].verified_at is None
    assert records[0].attempt_count == 1

    context = SecurityContext(
        principal_id="ops-1",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"Operations"}),
    )
    status = await get_archive_status(context=context, session=session)
    assert status.total_archives == 0
    assert status.total_events_archived == 0
    assert status.status == "NO_ARCHIVES"


async def test_verified_archive_is_retrievable_and_reverifies(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    org = await _seed_organization(session)
    await _seed_events(session, org, count=3)

    result = await archive_pending_audit_events(session, org.id, _config(), storage=storage)

    assert result is not None
    assert result.state == STATE_VERIFIED
    assert result.archived_count == 3
    assert result.serialization_version == ENVELOPE_VERSION
    assert result.checksum_algorithm == "sha256"
    assert result.storage_uri is not None
    assert result.verified_at is not None

    # The destination genuinely holds the bytes, and they re-verify.
    assert storage.read(result.storage_uri)
    assert storage.verify(result.storage_uri, checksum=result.checksum, algorithm="sha256").verified

    record = (await _records(session, org))[0]
    assert record.state == STATE_VERIFIED
    assert record.uploaded_at is not None
    assert record.retention_acknowledged_at is not None
    assert record.event_range_start_id == 1
    assert record.event_range_end_id == 3
    assert await _membership_ids(session, org) == {1, 2, 3}
    assert await verify_archive_record(session, record, storage=storage) is True


async def test_tampering_with_the_ledger_fails_reverification(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    """F02 through the persistence path: outcome is protected now."""
    org = await _seed_organization(session)
    await _seed_events(session, org, count=2)
    await archive_pending_audit_events(session, org.id, _config(), storage=storage)

    record = (await _records(session, org))[0]
    event = await session.get(AuditEvent, 1)
    assert event is not None
    event.outcome = "DENIED"
    await session.commit()

    assert record.serialization_version == ENVELOPE_VERSION
    assert await verify_archive_record(session, record) is False


async def test_archive_status_reports_only_verified_archives(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    org = await _seed_organization(session)
    context = SecurityContext(
        principal_id="ops-1",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"Operations"}),
    )
    before = await get_archive_status(context=context, session=session)
    assert before.total_archives == 0
    assert before.status == "NO_ARCHIVES"

    await _seed_events(session, org, count=5)
    result = await archive_pending_audit_events(session, org.id, _config(), storage=storage)
    assert result is not None

    after = await get_archive_status(context=context, session=session)
    assert after.total_archives == 1
    assert after.total_events_archived == 5
    assert after.latest_archive_id == result.archive_id
    assert after.latest_checksum == result.checksum
    assert after.status == "HEALTHY"


# --- F03: progress cannot skip an event ------------------------------------


async def test_equal_timestamp_batch_boundary_loses_nothing(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    """Five events sharing one timestamp, swept two at a time.

    The old cursor was `occurred_at > last_range_end`, so events 3-5 here
    were beyond the first batch's LIMIT and never selectable again. The
    composite `(occurred_at, id)` cursor plus membership makes all five
    reachable.
    """
    org = await _seed_organization(session)
    for event_id in range(1, 6):
        await _seed_event(session, org, event_id=event_id, occurred_at=BASE_TIME)

    archived: set[int] = set()
    for _ in range(4):
        result = await archive_pending_audit_events(
            session, org.id, _config(), storage=storage, batch_size=2
        )
        if result is None:
            break
        assert result.state == STATE_VERIFIED
        archived = await _membership_ids(session, org)

    assert archived == {1, 2, 3, 4, 5}
    total = await session.scalar(
        select(func.sum(AuditArchiveRecord.event_count)).where(
            AuditArchiveRecord.organization_id == org.id,
            AuditArchiveRecord.state == STATE_VERIFIED,
        )
    )
    assert total == 5


async def test_late_arriving_event_is_picked_up(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    """A commit that becomes visible after the sweep passed its timestamp."""
    org = await _seed_organization(session)
    await _seed_events(session, org, count=3)
    first = await archive_pending_audit_events(session, org.id, _config(), storage=storage)
    assert first is not None and first.archived_count == 3

    # Older than the cursor, inserted afterwards -- the case the timestamp
    # high-water mark could never see again.
    await _seed_event(session, org, event_id=99, occurred_at=BASE_TIME + timedelta(seconds=30))

    second = await archive_pending_audit_events(session, org.id, _config(), storage=storage)
    assert second is not None
    assert second.state == STATE_VERIFIED
    assert second.archived_count == 1
    assert await _membership_ids(session, org) == {1, 2, 3, 99}


async def test_events_outside_the_overlap_window_are_not_rescanned_forever(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    """The overlap window is bounded, and a swept ledger settles to no-op."""
    org = await _seed_organization(session)
    await _seed_events(session, org, count=3)
    await archive_pending_audit_events(session, org.id, _config(), storage=storage)

    assert await archive_pending_audit_events(session, org.id, _config(), storage=storage) is None


async def test_crash_after_upload_recovers_idempotently(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    """Upload succeeds, the process dies before verification, the sweep resumes.

    The resumed attempt must reassemble the *same* batch (membership is
    written at PREPARE, so it does), re-put the identical bytes (which the
    provider recognises), and land exactly one archive.
    """
    org = await _seed_organization(session)
    await _seed_events(session, org, count=3)

    crashing = _VerifyOnceFails(storage)
    failed = await archive_pending_audit_events(session, org.id, _config(), storage=crashing)
    assert failed is not None
    assert failed.state == STATE_FAILED
    assert failed.archived_count == 0
    first_archive_id = failed.archive_id

    # Events that arrive between the crash and the retry must not change the
    # batch being resumed.
    await _seed_event(session, org, event_id=50, occurred_at=BASE_TIME + timedelta(hours=1))

    resumed = await archive_pending_audit_events(session, org.id, _config(), storage=storage)
    assert resumed is not None
    assert resumed.state == STATE_VERIFIED
    assert resumed.archive_id == first_archive_id
    assert resumed.archived_count == 3

    records = await _records(session, org)
    assert len(records) == 1, "the retry must not create a second archive"
    assert await _membership_ids(session, org) == {1, 2, 3}

    # The late event is still archivable on the following sweep.
    tail = await archive_pending_audit_events(session, org.id, _config(), storage=storage)
    assert tail is not None
    assert tail.archived_count == 1
    assert await _membership_ids(session, org) == {1, 2, 3, 50}


async def test_lease_stops_a_second_worker(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    org = await _seed_organization(session)
    await _seed_events(session, org, count=2)

    assert await acquire_archive_lease(session, org.id, owner="replica-a", lease_seconds=900)
    await session.commit()

    blocked = await archive_pending_audit_events(
        session, org.id, _config(), storage=storage, owner="replica-b"
    )
    assert blocked is None
    assert await _records(session, org) == []

    # The holder itself is not blocked by its own lease.
    mine = await archive_pending_audit_events(
        session, org.id, _config(), storage=storage, owner="replica-a"
    )
    assert mine is not None
    assert mine.state == STATE_VERIFIED


async def test_an_expired_lease_is_reclaimed(session: AsyncSession) -> None:
    org = await _seed_organization(session)
    assert await acquire_archive_lease(
        session,
        org.id,
        owner="dead-replica",
        lease_seconds=60,
        now=datetime.now(UTC) - timedelta(hours=2),
    )
    await session.commit()

    assert await acquire_archive_lease(session, org.id, owner="live-replica", lease_seconds=900)


async def test_membership_is_the_backstop_against_double_archive(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    """Even past the lease, an event cannot be claimed by two archives."""
    org = await _seed_organization(session)
    org_id = org.id  # rollback below expires the ORM instance
    await _seed_events(session, org, count=2)
    result = await archive_pending_audit_events(session, org_id, _config(), storage=storage)
    assert result is not None
    record = (await _records(session, org))[0]

    session.add(
        AuditArchiveMembership(
            organization_id=org_id,
            audit_event_id=1,
            archive_record_id=record.id,
            archive_id="archive-duplicate",
            occurred_at=BASE_TIME,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()

    assert await archive_pending_audit_events(session, org_id, _config(), storage=storage) is None


# --- legal hold ------------------------------------------------------------


async def test_legal_hold_blocks_expiry_and_release_restores_it(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    org = await _seed_organization(session)
    await _seed_events(session, org, count=2)
    result = await archive_pending_audit_events(session, org.id, _config(), storage=storage)
    assert result is not None and result.storage_uri is not None
    after_retention = result.retention_until + timedelta(days=1)

    applied = await apply_legal_hold(session, result.archive_id, "litigation", storage=storage)
    assert applied["legal_hold"] is True
    record = (await _records(session, org))[0]
    assert record.legal_hold is True
    assert record.legal_hold_reason == "litigation"
    assert record.legal_hold_applied_at is not None
    assert storage.manifest(result.storage_uri)["legal_hold"] is True

    with pytest.raises(LegalHoldError):
        storage.delete(result.storage_uri, now=after_retention)

    released = await release_legal_hold(session, result.archive_id, "closed", storage=storage)
    assert released["legal_hold"] is False
    record = (await _records(session, org))[0]
    assert record.legal_hold is False
    assert record.legal_hold_released_at is not None
    storage.delete(result.storage_uri, now=after_retention)


async def test_legal_hold_on_an_unstored_archive_is_refused(
    session: AsyncSession, storage: FilesystemArchiveStorage
) -> None:
    org = await _seed_organization(session)
    await _seed_events(session, org, count=1)
    failed = await archive_pending_audit_events(
        session, org.id, _config(storage_backend="none"), storage=NullArchiveStorage()
    )
    assert failed is not None

    with pytest.raises(ArchiveStorageError, match="no stored object"):
        await apply_legal_hold(session, failed.archive_id, "litigation", storage=storage)


# --- helpers ---------------------------------------------------------------


class _VerifyOnceFails:
    """A provider that uploads for real, then fails verification exactly once.

    Stands in for a process that died between the acknowledged upload and
    the read-back: the object is on the destination, the record is not
    VERIFIED, and the next sweep has to cope.
    """

    name = "filesystem"
    available = True

    def __init__(self, inner: FilesystemArchiveStorage) -> None:
        self._inner = inner
        self._failed = False

    def put(self, obj: ArchiveObject) -> StoredObject:
        return self._inner.put(obj)

    def read(self, uri: str) -> bytes:
        return self._inner.read(uri)

    def verify(self, uri: str, *, checksum: str, algorithm: str) -> VerificationResult:
        if not self._failed:
            self._failed = True
            raise ArchiveStorageError("connection reset before read-back")
        return self._inner.verify(uri, checksum=checksum, algorithm=algorithm)

    def manifest(self, uri: str) -> dict[str, Any]:
        return self._inner.manifest(uri)

    def apply_legal_hold(self, uri: str, *, reason: str, at: datetime) -> dict[str, Any]:
        return self._inner.apply_legal_hold(uri, reason=reason, at=at)

    def release_legal_hold(self, uri: str, *, reason: str, at: datetime) -> dict[str, Any]:
        return self._inner.release_legal_hold(uri, reason=reason, at=at)

    def delete(self, uri: str, *, now: datetime) -> None:
        self._inner.delete(uri, now=now)
