"""Review F01, the cloud half: S3 Object Lock, exercised rather than described.

The tracker's ⚠ on F01 said the thing plainly -- "cloud object-lock has
never been exercised", "filesystem immutability is a guard rail, not a
security boundary". A test that writes an object and reads it back does not
lift that. So the integration tests here attempt, against a live service,
each of the operations the archive is supposed to be protected from, and
assert on the service's refusal:

* retrieve and re-checksum the stored object ->
  `test_stored_object_is_retrievable_and_rechecksums`
* overwrite -> `test_overwrite_cannot_destroy_the_locked_version` (and see
  its docstring for which S3 semantics are being relied on: versioning, not
  a refused PUT)
* delete before retain-until -> `test_delete_before_retain_until_is_refused`
* legal hold, including over a *lapsed* retention ->
  `test_legal_hold_outlives_retention_and_release_restores_expiry`

**Where they run.** Against the `minio` service in `compose.yaml`
(S3-compatible, implements Object Lock), reached at
`Settings.object_store_endpoint`. CI has no MinIO, so these skip there --
using the same rule as `tests/test_migration_orm_drift.py`: the target is
derived from settings, and *only* a failure to reach the service is a skip.
`ArchiveStorageUnavailable` is raised by the provider exclusively for
transport failure; an `S3ArchiveError` -- the service answering, and
answering wrongly -- propagates and fails the test, because that is a real
defect and not an absent environment.

The offline tests below need nothing running and always execute: URI
handling, the configuration refusals, the factory's resolution of every
backend name, and F01's original defect (a failed upload must produce a
retryable failure, never an archived count) now on the S3 path.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.audit_archive_s3 import S3ArchiveError, S3ArchiveStorage
from aida.audit_archive_storage import (
    ArchiveImmutabilityError,
    ArchiveObject,
    ArchiveStorageError,
    ArchiveStorageUnavailable,
    FilesystemArchiveStorage,
    LegalHoldError,
    NullArchiveStorage,
    RetentionNotExpiredError,
    UnavailableArchiveStorage,
    build_archive_storage,
)
from aida.db import Base
from aida.models import AuditArchiveRecord, AuditEvent, Organization
from aida.worm_archive import (
    STATE_FAILED,
    ArchiveConfig,
    archive_pending_audit_events,
)
from atlas.platform.config import get_settings

# One stable bucket across runs, because Object Lock can only be enabled
# when a bucket is created and a COMPLIANCE-locked object cannot be removed
# to tidy up after a run. Isolation between runs comes from the *key*
# prefix instead: every test gets a fresh organization id, so no two runs
# address the same object.
TEST_BUCKET = "aida-audit-archive-objectlock-test"

# The MinIO root password `compose.yaml` sets for local development. Not a
# secret: it is in the compose file, it is reachable only on localhost, and
# it is used here only so the test can derive its target from settings the
# way `tests/test_migration_orm_drift.py` does. An environment that sets
# `AIDA_OBJECT_STORE_SECRET_KEY` wins.
COMPOSE_LOCAL_CREDENTIAL = "aida-local-only"

# Placeholder key material for the offline tests, which never reach a
# service and so never authenticate. Named so that neither a reader nor
# ruff's S106 mistakes it for a credential.
PLACEHOLDER_KEY_MATERIAL = "not-a-real-key"

# A distinctive string used only to assert it never reaches a message.
LEAK_CANARY = "this-value-must-never-be-rendered"


def _payload(marker: str) -> bytes:
    return f'{{"archive":"{marker}","events":[1,2,3]}}'.encode()


def _archive_object(
    *,
    organization_id: str,
    archive_id: str = "archive-objectlock",
    payload: bytes | None = None,
    retention_seconds: int = 120,
    legal_hold: bool = False,
) -> ArchiveObject:
    body = payload if payload is not None else _payload(archive_id)
    return ArchiveObject(
        archive_id=archive_id,
        organization_id=organization_id,
        payload=body,
        checksum=hashlib.sha256(body).hexdigest(),
        checksum_algorithm="sha256",
        serialization_version=2,
        event_count=3,
        retention_until=datetime.now(UTC) + timedelta(seconds=retention_seconds),
        legal_hold=legal_hold,
        classification="CONFIDENTIAL",
    )


# --- offline: configuration, URIs, and the factory -------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"endpoint": ""}, "no endpoint"),
        ({"bucket": ""}, "no bucket"),
        ({"access_key": ""}, "must both be set"),
        ({"secret_key": ""}, "must both be set"),
        ({"retention_mode": "WHENEVER"}, "is not one of"),
        ({"endpoint": "not-a-url"}, "is not a URL"),
    ],
)
def test_incomplete_configuration_refuses_rather_than_half_working(
    kwargs: dict[str, str], expected: str
) -> None:
    """Every missing piece is a refusal naming what is missing -- and only what."""
    base = {
        "endpoint": "http://localhost:9000",
        "bucket": "b",
        "access_key": "ak",
        "secret_key": PLACEHOLDER_KEY_MATERIAL,
    }
    with pytest.raises(ArchiveStorageUnavailable) as caught:
        S3ArchiveStorage(**{**base, **kwargs})  # type: ignore[arg-type]
    assert expected in str(caught.value)


def test_a_refusal_never_carries_the_credential() -> None:
    """F01's neighbouring rule: no code path puts the secret in a message."""
    with pytest.raises(ArchiveStorageUnavailable) as caught:
        S3ArchiveStorage(
            endpoint="http://localhost:9000",
            bucket="",
            access_key="AKIAEXAMPLE",
            secret_key=LEAK_CANARY,
        )
    assert LEAK_CANARY not in str(caught.value)

    storage = S3ArchiveStorage(
        endpoint="http://127.0.0.1:1",
        bucket="b",
        access_key="AKIAEXAMPLE",
        secret_key=LEAK_CANARY,
    )
    with pytest.raises(ArchiveStorageUnavailable) as unreachable:
        storage.ensure_bucket()
    assert LEAK_CANARY not in str(unreachable.value)


def test_stored_uri_pins_a_version() -> None:
    """A URI without a versionId is malformed, because a key alone is not stable.

    This is the difference between the two providers stated as a test: a
    filesystem path names content, an S3 key names *whatever the latest
    write left there*. Only a version id names an object.
    """
    storage = S3ArchiveStorage(
        endpoint="http://localhost:9000",
        bucket="b",
        access_key="ak",
        secret_key=PLACEHOLDER_KEY_MATERIAL,
    )
    uri = storage.uri_for("org-1", "archive-1", "v-abc")
    assert uri == "s3://b/org-1/archive-1.json?versionId=v-abc"
    assert storage._parts(uri) == ("org-1/archive-1.json", "v-abc")

    for malformed in ("s3://b/org-1/archive-1.json", "filesystem://org-1/archive-1", "s3://b/"):
        with pytest.raises(ArchiveStorageError):
            storage._parts(malformed)

    with pytest.raises(ArchiveStorageError) as wrong_bucket:
        storage._parts("s3://other/org-1/a.json?versionId=v")
    assert "configured for 'b'" in str(wrong_bucket.value)


def test_factory_resolves_s3_only_when_it_is_fully_configured() -> None:
    configured = build_archive_storage(
        "s3",
        s3_endpoint="http://localhost:9000",
        s3_bucket="audit-archive",
        s3_access_key="ak",
        s3_secret_key=PLACEHOLDER_KEY_MATERIAL,
    )
    assert isinstance(configured, S3ArchiveStorage)
    assert configured.available is True
    assert configured.retention_mode == "COMPLIANCE"

    misconfigured = build_archive_storage("s3", s3_endpoint="", s3_bucket="", s3_access_key="")
    assert isinstance(misconfigured, UnavailableArchiveStorage)
    assert misconfigured.available is False
    with pytest.raises(ArchiveStorageUnavailable):
        misconfigured.put(_archive_object(organization_id="org-1"))


def test_the_other_cloud_backends_are_still_explicitly_unavailable(tmp_path: Path) -> None:
    """`gcs`/`azure_blob` are not quietly given the S3 provider's behaviour.

    Implementing one provider completely is F01's own guidance. A backend
    with no implementation must keep refusing and naming itself, so that
    selecting it is a configuration error rather than a silent no-op.
    """
    for backend in ("gcs", "azure_blob"):
        storage = build_archive_storage(backend)
        assert isinstance(storage, UnavailableArchiveStorage)
        assert storage.available is False
        assert storage.name == backend
        with pytest.raises(ArchiveStorageUnavailable) as caught:
            storage.read("anything")
        assert backend in str(caught.value)

    assert isinstance(build_archive_storage("none"), NullArchiveStorage)
    assert isinstance(
        build_archive_storage("filesystem", filesystem_root=str(tmp_path)),
        FilesystemArchiveStorage,
    )


# --- offline: F01's original defect, now on the S3 path --------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def test_unreachable_s3_fails_retryably_and_archives_nothing(
    session: AsyncSession,
) -> None:
    """The F01 defect verbatim, against a destination that cannot be reached.

    The provider is configured correctly and points at a closed port, so
    `put` raises rather than returning something success-shaped. The
    lifecycle must turn that into a FAILED record with a zero count and
    `retryable`, must not stamp a `storage_uri` or `verified_at`, and must
    keep the membership rows so the retry reassembles the identical batch.
    """
    org = Organization(id=uuid4(), name="Test Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.commit()
    for index in range(3):
        session.add(
            AuditEvent(
                id=index + 1,
                organization_id=org.id,
                principal_id="analyst-1",
                principal_type="USER",
                action="data_access",
                resource_type="table",
                resource_id=f"tbl-{index}",
                outcome="SUCCESS",
                correlation_id=f"corr-{index}",
                source_ip="10.0.0.1",
                details={"rows": index},
                occurred_at=datetime(2026, 8, 30, 12, index, tzinfo=UTC),
            )
        )
    await session.commit()

    storage = S3ArchiveStorage(
        endpoint="http://127.0.0.1:1",  # closed port: connection refused, not a 4xx
        bucket=TEST_BUCKET,
        access_key="ak",
        secret_key=PLACEHOLDER_KEY_MATERIAL,
    )
    result = await archive_pending_audit_events(
        session,
        org.id,
        ArchiveConfig(storage_backend="s3", bucket_name=TEST_BUCKET),
        storage=storage,
    )

    assert result is not None
    assert result.state == STATE_FAILED
    assert result.archived_count == 0
    assert result.retryable is True
    assert result.failure_reason is not None
    assert "upload refused" in result.failure_reason

    records = list(
        await session.scalars(
            select(AuditArchiveRecord).where(AuditArchiveRecord.organization_id == org.id)
        )
    )
    assert len(records) == 1
    assert records[0].state == STATE_FAILED
    assert records[0].storage_uri is None
    assert records[0].verified_at is None
    assert records[0].attempt_count == 1


# --- live MinIO -------------------------------------------------------------


@pytest.fixture(scope="module")
def live_storage() -> Iterator[S3ArchiveStorage]:
    """A provider against the running object store, or a clean skip.

    Only transport failure skips. The provider raises
    `ArchiveStorageUnavailable` for exactly that and `S3ArchiveError` for
    anything the service actually answered, so a service that is up and
    misbehaving fails here instead of disappearing into a skip.
    """
    settings = get_settings()
    storage = S3ArchiveStorage(
        endpoint=settings.object_store_endpoint,
        bucket=TEST_BUCKET,
        access_key=settings.object_store_access_key or "aida",
        secret_key=settings.object_store_secret_key or COMPOSE_LOCAL_CREDENTIAL,
        region=settings.audit_archive_s3_region,
        retention_mode=settings.audit_archive_s3_retention_mode,
    )
    try:
        storage.ensure_bucket()
    except ArchiveStorageUnavailable as exc:
        pytest.skip(
            f"No S3-compatible object store at {settings.object_store_endpoint!r} ({exc}); "
            "these tests need one that implements Object Lock. Start the local stack "
            "(`docker compose up -d minio`, see compose.yaml's `minio` service) or point "
            "AIDA_OBJECT_STORE_ENDPOINT/ACCESS_KEY/SECRET_KEY at one. CI has no object "
            "store, so this is a skip there rather than a failure."
        )
    yield storage


@pytest.fixture
def organization_id() -> str:
    """A fresh key prefix per test, so runs never address the same object."""
    return f"org-{uuid4().hex}"


def test_bucket_actually_enforces_object_lock(live_storage: S3ArchiveStorage) -> None:
    """Object Lock cannot be turned on after creation, so this is not a formality.

    A bucket created without it accepts every write and retains nothing --
    and looks identical from the outside until someone deletes an archive.
    """
    assert live_storage.bucket_has_object_lock() is True


def test_stored_object_is_retrievable_and_rechecksums(
    live_storage: S3ArchiveStorage, organization_id: str
) -> None:
    """F01's first acceptance criterion, against the destination itself."""
    obj = _archive_object(organization_id=organization_id)
    stored = live_storage.put(obj)

    assert stored.provider == "s3"
    assert stored.byte_size == len(obj.payload)
    assert "versionId=" in stored.uri
    assert live_storage.read(stored.uri) == obj.payload

    verification = live_storage.verify(stored.uri, checksum=obj.checksum, algorithm="sha256")
    assert verification.verified is True
    assert verification.byte_size == len(obj.payload)

    # The service's own account of what it holds, not ours.
    manifest = live_storage.manifest(stored.uri)
    assert manifest["retention_mode"] == "COMPLIANCE"
    assert manifest["checksum"] == obj.checksum
    assert manifest["payload_sha256"] == hashlib.sha256(obj.payload).hexdigest()
    assert manifest["event_count"] == obj.event_count
    assert manifest["legal_hold"] is False
    assert manifest["retention_until"]

    # A wrong expected checksum must fail verification rather than pass it.
    wrong = live_storage.verify(stored.uri, checksum="0" * 64, algorithm="sha256")
    assert wrong.verified is False
    assert "!= expected" in wrong.detail


def test_reput_of_identical_bytes_is_idempotent(
    live_storage: S3ArchiveStorage, organization_id: str
) -> None:
    """Crash-after-upload recovery: the retry finds the object, not a duplicate."""
    obj = _archive_object(organization_id=organization_id)
    first = live_storage.put(obj)
    second = live_storage.put(obj)
    assert second.uri == first.uri
    assert live_storage.read(second.uri) == obj.payload


def test_overwrite_cannot_destroy_the_locked_version(
    live_storage: S3ArchiveStorage, organization_id: str
) -> None:
    """Overwrite, and the S3 semantics being relied on, stated exactly.

    S3 does **not** refuse a `PUT` to a key that already holds a locked
    object. Object Lock requires bucket versioning, so the write is accepted
    and becomes a *new version*; the locked version is untouched and still
    retrievable by its version id. That -- not a refused write -- is the
    property this provider depends on, which is why every URI it mints pins
    a version.

    So this test does the destructive thing on purpose: it writes different
    bytes straight at the key, out of band, the way an attacker or a buggy
    caller with the same credentials would. Then it asserts the archived
    version still returns the original bytes and still re-checksums.

    The second half is the provider's own, stricter refusal: `put` will not
    version over an archive id that already holds different content.
    """
    obj = _archive_object(organization_id=organization_id, payload=_payload("original"))
    stored = live_storage.put(obj)
    key = live_storage._object_key(organization_id, obj.archive_id)

    tampered = _payload("tampered")
    overwrite = live_storage._request(
        "PUT",
        live_storage._path_for(key),
        headers={"content-type": "application/json"},
        payload=tampered,
        operation="PutObject",
        expected=(200,),
    )
    new_version = overwrite.headers.get("x-amz-version-id", "")
    assert new_version and new_version not in stored.uri

    # The current version is the tampered one -- and the archive is not.
    assert live_storage.read(stored.uri) == obj.payload
    assert live_storage.verify(stored.uri, checksum=obj.checksum, algorithm="sha256").verified

    # And the provider refuses to be the one doing that in the first place.
    different = replace(obj, payload=tampered, checksum=hashlib.sha256(tampered).hexdigest())
    with pytest.raises(ArchiveImmutabilityError) as caught:
        live_storage.put(different)
    assert "never overwritten" in str(caught.value)


def test_delete_before_retain_until_is_refused(
    live_storage: S3ArchiveStorage, organization_id: str
) -> None:
    """The service refuses, and would refuse even without the provider's pre-check.

    Two assertions, and the second is the one that matters. The provider
    raises `RetentionNotExpiredError` from a pre-check -- convenient, and
    exactly what the filesystem provider does. Then the same delete is
    issued straight at the service with the pre-check bypassed, and the
    service refuses it too. Our refusal is advisory; that one is binding.
    """
    obj = _archive_object(organization_id=organization_id, retention_seconds=120)
    stored = live_storage.put(obj)

    with pytest.raises(RetentionNotExpiredError):
        live_storage.delete(stored.uri, now=datetime.now(UTC))

    key, version_id = live_storage._parts(stored.uri)
    with pytest.raises(S3ArchiveError) as refused:
        live_storage._request(
            "DELETE",
            live_storage._path_for(key),
            query={"versionId": version_id},
            operation="DeleteObject",
            expected=(200, 204),
        )
    assert refused.value.status in (400, 403)
    assert "WORM" in str(refused.value) or refused.value.code in (
        "AccessDenied",
        "InvalidRequest",
    )

    # Still there, still intact, after both attempts.
    assert live_storage.read(stored.uri) == obj.payload


def test_legal_hold_outlives_retention_and_release_restores_expiry(
    live_storage: S3ArchiveStorage, organization_id: str
) -> None:
    """Apply, hold past expiry, release -- against the service, not a record.

    Retention is set to a few seconds so the test can wait for it to lapse.
    Once it has, retention is no longer what protects the object, so a
    delete that the service still refuses is being refused by the legal hold
    alone. That is the property F01 asks for: "while held the object cannot
    be deleted even if retention has lapsed."
    """
    retention_seconds = 3
    obj = _archive_object(
        organization_id=organization_id,
        archive_id="archive-legal-hold",
        retention_seconds=retention_seconds,
    )
    stored = live_storage.put(obj)
    key, version_id = live_storage._parts(stored.uri)

    assert live_storage.legal_hold_status(stored.uri) is False

    applied = live_storage.apply_legal_hold(stored.uri, reason="litigation", at=datetime.now(UTC))
    assert applied["legal_hold"] is True
    assert applied["legal_hold_reason"] == "litigation"
    assert live_storage.legal_hold_status(stored.uri) is True

    # Wait out the retention period, then confirm it really has lapsed.
    time.sleep(retention_seconds + 3)
    assert datetime.now(UTC) > obj.retention_until

    with pytest.raises(LegalHoldError):
        live_storage.delete(stored.uri, now=datetime.now(UTC))

    with pytest.raises(S3ArchiveError) as refused:
        live_storage._request(
            "DELETE",
            live_storage._path_for(key),
            query={"versionId": version_id},
            operation="DeleteObject",
            expected=(200, 204),
        )
    assert refused.value.status in (400, 403)

    released = live_storage.release_legal_hold(stored.uri, reason="closed", at=datetime.now(UTC))
    assert released["legal_hold"] is False
    assert live_storage.legal_hold_status(stored.uri) is False

    # With no hold and retention elapsed, deletion is now allowed -- which is
    # what makes the refusals above evidence of the hold rather than of some
    # unrelated permission problem.
    live_storage.delete(stored.uri, now=datetime.now(UTC))
    with pytest.raises(S3ArchiveError) as gone:
        live_storage.read(stored.uri)
    assert gone.value.status in (403, 404, 405)
