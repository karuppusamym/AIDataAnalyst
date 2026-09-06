"""Review F01: a storage provider either stores, or refuses. No third outcome.

These tests exercise the provider layer directly -- no database, no
lifecycle -- because the review's acceptance criterion for F01 starts with
"retrieve and verify the destination object" and "exercise overwrite/deletion
restrictions and legal hold against the configured service". The configured
service here is the filesystem provider, which is the one complete
implementation in this repository.

The cloud backends are covered too, but by asserting that they *refuse*:
`s3`, `gcs` and `azure_blob` resolve to a provider that raises and names
itself, so a deployment cannot select one and receive silent success.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aida.audit_archive_storage import (
    ArchiveImmutabilityError,
    ArchiveObject,
    ArchiveStorageUnavailable,
    FilesystemArchiveStorage,
    LegalHoldError,
    NullArchiveStorage,
    RetentionNotExpiredError,
    UnavailableArchiveStorage,
    build_archive_storage,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
RETENTION_UNTIL = NOW + timedelta(days=2555)
ORG = "11111111-1111-1111-1111-111111111111"


def _object(payload: bytes = b'{"events":[]}', archive_id: str = "archive-1") -> ArchiveObject:
    import hashlib

    return ArchiveObject(
        archive_id=archive_id,
        organization_id=ORG,
        payload=payload,
        checksum=hashlib.sha256(payload).hexdigest(),
        checksum_algorithm="sha256",
        serialization_version=2,
        event_count=3,
        retention_until=RETENTION_UNTIL,
        legal_hold=False,
        classification="CONFIDENTIAL",
    )


# --- the refusing providers ------------------------------------------------


def test_null_storage_refuses_rather_than_reporting_success() -> None:
    storage = NullArchiveStorage()
    assert storage.available is False
    with pytest.raises(ArchiveStorageUnavailable):
        storage.put(_object())


def test_unconfigured_backend_defaults_to_the_refusing_provider() -> None:
    storage = build_archive_storage("")
    assert isinstance(storage, NullArchiveStorage)
    assert storage.available is False


@pytest.mark.parametrize("backend", ["s3", "gcs", "azure_blob"])
def test_cloud_backends_are_explicitly_unavailable(backend: str) -> None:
    """Three backends were advertised; none was implemented. Say so."""
    storage = build_archive_storage(backend)
    assert isinstance(storage, UnavailableArchiveStorage)
    assert storage.available is False
    assert storage.name == backend
    with pytest.raises(ArchiveStorageUnavailable, match=backend):
        storage.put(_object())


def test_filesystem_without_a_root_refuses() -> None:
    storage = build_archive_storage("filesystem", filesystem_root="")
    assert storage.available is False
    with pytest.raises(ArchiveStorageUnavailable, match="filesystem_root"):
        storage.put(_object())


# --- the real provider -----------------------------------------------------


def test_put_writes_readable_frozen_bytes_and_a_manifest(tmp_path: Path) -> None:
    storage = FilesystemArchiveStorage(tmp_path)
    obj = _object(b'{"events":[1,2,3]}')

    stored = storage.put(obj)

    assert stored.provider == "filesystem"
    assert stored.retention_acknowledged_until == RETENTION_UNTIL
    assert storage.read(stored.uri) == obj.payload

    payload_path = tmp_path / ORG / "archive-1.json"
    assert payload_path.exists()
    assert not payload_path.stat().st_mode & 0o222, "stored object must not be writable"

    manifest = storage.manifest(stored.uri)
    assert manifest["checksum"] == obj.checksum
    assert manifest["checksum_algorithm"] == "sha256"
    assert manifest["serialization_version"] == 2
    assert manifest["event_count"] == 3
    assert manifest["retention_until"] == RETENTION_UNTIL.isoformat()


def test_verify_round_trips_the_destination_object(tmp_path: Path) -> None:
    storage = FilesystemArchiveStorage(tmp_path)
    obj = _object()
    stored = storage.put(obj)

    result = storage.verify(stored.uri, checksum=obj.checksum, algorithm="sha256")
    assert result.verified is True
    assert result.byte_size == len(obj.payload)


def test_verify_fails_when_the_stored_checksum_differs(tmp_path: Path) -> None:
    storage = FilesystemArchiveStorage(tmp_path)
    stored = storage.put(_object())

    result = storage.verify(stored.uri, checksum="0" * 64, algorithm="sha256")
    assert result.verified is False
    assert "!=" in result.detail


def test_reput_of_identical_bytes_is_idempotent(tmp_path: Path) -> None:
    """The crash-after-upload path: re-uploading the same archive is a no-op."""
    storage = FilesystemArchiveStorage(tmp_path)
    obj = _object()

    first = storage.put(obj)
    second = storage.put(obj)

    assert second.uri == first.uri
    assert second.stored_at == first.stored_at
    assert len(list((tmp_path / ORG).glob("*.json"))) == 2  # payload + manifest only


def test_overwriting_with_different_content_is_refused(tmp_path: Path) -> None:
    storage = FilesystemArchiveStorage(tmp_path)
    storage.put(_object(b"first"))

    with pytest.raises(ArchiveImmutabilityError):
        storage.put(_object(b"second"))

    assert storage.read(storage.uri_for(ORG, "archive-1")) == b"first"


# --- retention and legal hold ---------------------------------------------


def test_delete_is_refused_before_retention_elapses(tmp_path: Path) -> None:
    storage = FilesystemArchiveStorage(tmp_path)
    stored = storage.put(_object())

    with pytest.raises(RetentionNotExpiredError):
        storage.delete(stored.uri, now=NOW)

    assert storage.read(stored.uri)  # still there


def test_legal_hold_blocks_expiry_and_release_restores_it(tmp_path: Path) -> None:
    """The review's acceptance criterion: hold prevents expiry; release restores it."""
    storage = FilesystemArchiveStorage(tmp_path)
    stored = storage.put(_object())
    after_retention = RETENTION_UNTIL + timedelta(days=1)

    storage.apply_legal_hold(stored.uri, reason="litigation-2026-11", at=NOW)
    assert storage.manifest(stored.uri)["legal_hold"] is True

    with pytest.raises(LegalHoldError):
        storage.delete(stored.uri, now=after_retention)

    storage.release_legal_hold(stored.uri, reason="case closed", at=NOW)
    manifest = storage.manifest(stored.uri)
    assert manifest["legal_hold"] is False
    assert manifest["legal_hold_released_at"] == NOW.isoformat()

    storage.delete(stored.uri, now=after_retention)
    assert not (tmp_path / ORG / "archive-1.json").exists()
    assert not (tmp_path / ORG / "archive-1.manifest.json").exists()


def test_hold_survives_the_frozen_manifest(tmp_path: Path) -> None:
    """Applying a hold must rewrite a read-only manifest, not fail on it."""
    storage = FilesystemArchiveStorage(tmp_path)
    stored = storage.put(_object())

    storage.apply_legal_hold(stored.uri, reason="first", at=NOW)
    storage.apply_legal_hold(stored.uri, reason="second", at=NOW)

    manifest_path = tmp_path / ORG / "archive-1.manifest.json"
    assert not manifest_path.stat().st_mode & 0o222
    assert storage.manifest(stored.uri)["legal_hold_reason"] == "second"
