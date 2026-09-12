"""R11-B9: the S3 archive's read-back and retention properties, proven offline.

**The gap this closes.** `Docs/60-delivery/20-capability-register.md` records
F01 as *Partial* because the four WORM properties had only been exercised
against live MinIO. Two things follow from that which the register's wording
does not spell out, and both are why this file exists:

1. CI has no object store, so `tests/test_audit_archive_s3.py`'s live block
   *skips* there. Offline, the S3 provider's read-back, retention, hold and
   version-pinning code had no coverage at all -- only a transport-failure
   test. The property was not merely unverified against AWS; it was unexercised
   anywhere that runs by default.
2. The live tests drive the *provider* directly (`live_storage.put(...)`).
   Nothing anywhere drove S3 read-back through `archive_pending_audit_events`,
   which is the entry point production actually uses. A provider that worked
   and a lifecycle that used it wrongly would have passed everything.

Both are addressed here by driving the **real** `S3ArchiveStorage` -- signing,
headers, version pinning, XML parsing, re-checksum and all -- through the
**real** `archive_pending_audit_events`, with only the socket replaced by
`tests.support.s3_object_lock_double`, a service that enforces Object Lock
rather than recording it. See that module for exactly what it does and does not
model.

**What remains unproven by this file, deliberately.** That a *particular* real
bucket enforces any of this. Object Lock must be enabled at bucket creation and
can be defeated by IAM, bucket policy or a GOVERNANCE-mode bypass, none of which
this double models. The runnable procedure for that is
`Docs/50-security/audit-archive-destination-verification.md`; the live-MinIO
tests in `tests/test_audit_archive_s3.py` are the halfway house. Nothing here
should be read as a claim about AWS.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.audit_archive_s3 import S3ArchiveStorage
from aida.audit_archive_storage import (
    ArchiveImmutabilityError,
    ArchiveObject,
    LegalHoldError,
    RetentionNotExpiredError,
)
from aida.audit_envelope import envelope_from_audit_event
from aida.db import Base
from aida.models import AuditArchiveRecord, AuditEvent, Organization
from aida.worm_archive import (
    STATE_VERIFIED,
    ArchiveConfig,
    archive_pending_audit_events,
    serialize_archive,
    verify_archive_record,
)
from tests.support.s3_object_lock_double import ObjectLockService

_BUCKET = "atlas-audit-archive"
_NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

# Placeholder key material. Nothing here reaches a network, so these never
# authenticate against anything; named the same way
# `tests/test_audit_archive_s3.py` names its own so that neither a reader nor
# ruff's S106 mistakes them for a credential.
PLACEHOLDER_ACCESS_KEY = "not-a-real-access-key"
PLACEHOLDER_KEY_MATERIAL = "not-a-real-key"


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
def service() -> ObjectLockService:
    return ObjectLockService(bucket=_BUCKET, now=_NOW)


@pytest.fixture
def storage(service: ObjectLockService) -> S3ArchiveStorage:
    """The production provider, constructed exactly as `storage_for` constructs it.

    Only `transport` differs from a deployment's own object, and it is the
    last argument for that reason.
    """
    return S3ArchiveStorage(
        endpoint="https://object-store.internal:9000",
        bucket=_BUCKET,
        access_key=PLACEHOLDER_ACCESS_KEY,
        secret_key=PLACEHOLDER_KEY_MATERIAL,
        region="eu-west-1",
        retention_mode="COMPLIANCE",
        transport=service.transport(),
    )


def _config() -> ArchiveConfig:
    return ArchiveConfig(retention_days=2555, storage_backend="s3", bucket_name=_BUCKET)


def _aware(moment: datetime) -> datetime:
    """Re-attach UTC to a datetime that went through SQLite.

    `AuditArchiveRecord.retention_until` is `DateTime(timezone=True)`, which
    PostgreSQL round-trips as an aware value and SQLite -- which has no
    timezone-aware type -- hands back naive. Test-database artifact, not a
    production one, but arithmetic on a record loaded here would otherwise
    compare naive against aware and fail for a reason that has nothing to do
    with the archive.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


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
                occurred_at=datetime(2026, 9, 12, 10, i, tzinfo=UTC),
            )
        )
    await session.commit()


async def _archive(
    session: AsyncSession, org: Organization, storage: S3ArchiveStorage
) -> AuditArchiveRecord:
    """Run the production sweep and hand back the record it wrote."""
    result = await archive_pending_audit_events(session, org.id, _config(), storage=storage)
    assert result is not None
    assert result.state == STATE_VERIFIED, result.failure_reason
    record = await session.scalar(
        select(AuditArchiveRecord).where(AuditArchiveRecord.archive_id == result.archive_id)
    )
    assert record is not None
    return record


# --- read-back through the production write path -----------------------------


async def test_the_sweep_stores_bytes_the_destination_hands_back(
    session: AsyncSession, storage: S3ArchiveStorage, service: ObjectLockService
) -> None:
    """The claim the register could not support: written, then read back from S3.

    Not `storage.put(...)` then `storage.read(...)` -- the write is
    `archive_pending_audit_events`, the same call `main.py`'s archive loop
    makes, and the bytes compared are the ones `serialize_archive` produced
    for that record.
    """
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=5)

    record = await _archive(session, org, storage)

    assert record.storage_backend == "s3"
    assert record.storage_uri is not None
    assert record.event_count == 5
    assert record.verified_at is not None

    retrieved = storage.read(record.storage_uri)
    rows = list(
        await session.scalars(select(AuditEvent).where(AuditEvent.organization_id == org.id))
    )
    expected = serialize_archive(
        [envelope_from_audit_event(row) for row in rows],
        archive_id=record.archive_id,
        organization_id=str(org.id),
        checksum=record.checksum,
        retention_until=_aware(record.retention_until),
        classification=_config().classification,
    )
    assert retrieved == expected

    # And the archive is self-describing: a reader years from now, with no
    # access to this codebase, gets the events and the checksum that covers
    # them out of the bytes the destination returned.
    document = json.loads(retrieved.decode("utf-8"))
    assert document["archive_id"] == record.archive_id
    assert document["checksum"] == record.checksum
    assert document["event_count"] == 5
    assert len(document["events"]) == 5

    # The destination was really asked -- a double that answered from a cache
    # the provider kept would show no GET here.
    assert any(method == "GET" for method, _ in service.calls)


async def test_the_stored_uri_pins_a_version_the_service_minted(
    session: AsyncSession, storage: S3ArchiveStorage, service: ObjectLockService
) -> None:
    """Without a pinned version a later write to the key silently changes history."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)

    record = await _archive(session, org, storage)

    key = f"{org.id}/{record.archive_id}.json"
    minted = [version.version_id for version in service.versions[key]]
    assert len(minted) == 1
    assert str(record.storage_uri).endswith(f"?versionId={minted[0]}")
    assert str(record.storage_uri).startswith(f"s3://{_BUCKET}/{key}?")


async def test_a_healthy_archive_verifies_against_the_destination(
    session: AsyncSession, storage: S3ArchiveStorage
) -> None:
    """The positive case, so the corruption test below is known to be discriminating."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=3)
    record = await _archive(session, org, storage)

    assert await verify_archive_record(session, record, storage=storage) is True


async def test_corrupted_destination_bytes_fail_verification(
    session: AsyncSession, storage: S3ArchiveStorage, service: ObjectLockService
) -> None:
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=3)
    record = await _archive(session, org, storage)
    assert record.storage_uri is not None

    key = f"{org.id}/{record.archive_id}.json"
    stored = service.versions[key][-1]
    stored.payload = stored.payload.replace(b'"outcome":"SUCCESS"', b'"outcome":"FAILURE"', 1)

    outcome = storage.verify(
        record.storage_uri, checksum=record.checksum, algorithm=record.checksum_algorithm
    )
    assert outcome.verified is False
    assert "digest" in outcome.detail

    # And the lifecycle-level verifier reaches the same answer.
    assert await verify_archive_record(session, record, storage=storage) is False


# --- retention and hold: the service refuses, not the caller -----------------


async def test_delete_before_retain_until_is_refused_by_the_service(
    session: AsyncSession, storage: S3ArchiveStorage, service: ObjectLockService
) -> None:
    """A COMPLIANCE-locked version cannot be deleted while retention stands."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)
    record = await _archive(session, org, storage)
    assert record.storage_uri is not None

    with pytest.raises(RetentionNotExpiredError):
        storage.delete(record.storage_uri, now=_NOW)

    # The provider's own pre-check is advisory. Removing it must not make the
    # object deletable, so ask the service directly with a clock it believes:
    # the object is still there afterwards.
    key = f"{org.id}/{record.archive_id}.json"
    service.advance_to(_NOW)
    response = service._delete(key, {"versionId": [service.versions[key][-1].version_id]})  # noqa: SLF001
    assert response.status_code == 403
    assert b"WORM protected" in response.content
    assert len(service.versions[key]) == 1


async def test_an_overwrite_cannot_destroy_the_locked_version(
    session: AsyncSession, storage: S3ArchiveStorage, service: ObjectLockService
) -> None:
    """Versioning, not luck, is what keeps the archived bytes readable.

    A raw write to the same key -- bypassing the provider's own
    checksum-mismatch refusal, which is what an attacker with bucket write
    would do -- mints a new version and leaves the pinned one retrievable and
    still verifying.
    """
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)
    record = await _archive(session, org, storage)
    assert record.storage_uri is not None

    key = f"{org.id}/{record.archive_id}.json"
    original = storage.read(record.storage_uri)

    raw = httpx_put(service, key, b'{"events":[]}')
    assert raw == 200
    assert len(service.versions[key]) == 2

    assert storage.read(record.storage_uri) == original
    outcome = storage.verify(
        record.storage_uri, checksum=record.checksum, algorithm=record.checksum_algorithm
    )
    assert outcome.verified is True


async def test_the_provider_refuses_a_second_archive_under_one_id(
    session: AsyncSession, storage: S3ArchiveStorage
) -> None:
    """Two archives claiming one id is a reporting problem even though S3 keeps both."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)
    record = await _archive(session, org, storage)

    with pytest.raises(ArchiveImmutabilityError):
        storage.put(
            ArchiveObject(
                archive_id=record.archive_id,
                organization_id=str(org.id),
                payload=b'{"events":[]}',
                checksum="a" * 64,
                checksum_algorithm="sha256",
                serialization_version=record.serialization_version,
                event_count=0,
                retention_until=record.retention_until,
                legal_hold=False,
                classification="CONFIDENTIAL",
            )
        )


async def test_legal_hold_outlives_retention_and_release_restores_expiry(
    session: AsyncSession, storage: S3ArchiveStorage, service: ObjectLockService
) -> None:
    """The property a filesystem root cannot enforce at all.

    Retention is allowed to lapse, and the object still cannot be deleted
    while a hold stands. Releasing the hold restores ordinary expiry, so the
    hold is genuinely the thing blocking deletion rather than a second copy
    of the retention check.
    """
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)
    record = await _archive(session, org, storage)
    assert record.storage_uri is not None
    uri = record.storage_uri

    storage.apply_legal_hold(uri, reason="litigation-hold-4471", at=_NOW)
    assert storage.legal_hold_status(uri) is True

    # Walk past retain-until. The hold is now the only thing in the way.
    lapsed = _aware(record.retention_until) + timedelta(days=1)
    service.advance_to(lapsed)
    with pytest.raises(LegalHoldError):
        storage.delete(uri, now=lapsed)

    storage.release_legal_hold(uri, reason="matter-closed", at=_NOW)
    assert storage.legal_hold_status(uri) is False

    storage.delete(uri, now=lapsed)
    key = f"{org.id}/{record.archive_id}.json"
    assert service.versions[key] == []


async def test_an_object_never_held_reports_no_hold_rather_than_failing(
    session: AsyncSession, storage: S3ArchiveStorage
) -> None:
    """`NoSuchObjectLockConfiguration` means "no hold"; every other error must raise."""
    org = await _seed_organization(session)
    await _seed_audit_events(session, org, count=2)
    record = await _archive(session, org, storage)
    assert record.storage_uri is not None

    assert storage.legal_hold_status(record.storage_uri) is False


# --- the double is not free to be wrong --------------------------------------


async def test_an_unsigned_request_would_be_refused(service: ObjectLockService) -> None:
    """The meta-test: the double rejects a request the real service would reject.

    Without this, a provider that stopped signing its requests would pass
    every test above, and the suite would be asserting against a service that
    accepts anything.
    """
    with httpx.Client(transport=service.transport()) as client:
        response = client.get(f"https://object-store.internal:9000/{_BUCKET}/anything")
    assert response.status_code == 403
    assert b"AccessDenied" in response.content


def httpx_put(service: ObjectLockService, key: str, payload: bytes) -> int:
    """A raw, correctly-signed-looking write straight at the service.

    Used to simulate somebody with bucket credentials writing over an archived
    key without going through the provider.
    """
    with httpx.Client(transport=service.transport()) as client:
        response = client.put(
            f"https://object-store.internal:9000/{_BUCKET}/{key}",
            content=payload,
            headers={
                "authorization": (
                    "AWS4-HMAC-SHA256 Credential=raw/20260912/eu-west-1/s3/aws4_request"
                ),
                "content-md5": "ignored-by-the-double",
            },
        )
    return response.status_code
