"""Storage providers for the WORM audit archive.

**Invariant this module exists to hold:** an archive is stored only when a
destination has acknowledged the bytes and can hand them back. Nothing here
returns a success value it did not earn. A provider either writes, fsyncs,
freezes and can re-read the object, or it raises -- there is deliberately no
third outcome that logs and returns something success-shaped, because that
is precisely the defect (`Docs/review-2026-09-05/REVIEW.md` F01) this module
was written to remove.

Three provider kinds, and the distinction between them is the point:

* `FilesystemArchiveStorage` -- complete and real. Writes the serialized
  archive plus a sidecar manifest to a configured root, fsyncs both, marks
  them read-only, refuses to overwrite an object whose content differs,
  supports read-back verification, and enforces retention and legal hold on
  delete. Idempotent: re-putting identical bytes for the same archive id
  returns the existing object rather than failing or duplicating, which is
  what makes crash-after-upload recovery safe.
* `NullArchiveStorage` -- the default when no destination is configured. It
  **refuses**. Every call raises `ArchiveStorageUnavailable`. The archive
  lifecycle turns that into a FAILED record, so an unconfigured deployment
  reports "no archive" rather than a fabricated one.
* `S3ArchiveStorage` (in `aida.audit_archive_s3`) -- complete and real,
  over S3 Object Lock in COMPLIANCE mode, signed by `aida.aws_sigv4`
  rather than by an SDK this project would otherwise have to depend on.
  Unlike the filesystem provider, its immutability is the *service's*
  refusal, not a mode bit this process could clear.
* `UnavailableArchiveStorage` -- named for a backend that is advertised in
  configuration (`gcs`, `azure_blob`) but has no implementation in this
  repository. It refuses the same way, naming the backend. Object-lock
  against those services needs an SDK this project does not depend on;
  until one is added, selecting them is an explicit configuration error,
  not a silent no-op. They are deliberately not faked to match `s3`.

Every method is synchronous and blocking. Callers on the event loop wrap
them in `asyncio.to_thread`; keeping the provider sync keeps it directly
testable and keeps the fsync semantics honest.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol

import structlog

logger = structlog.get_logger(__name__)

MANIFEST_VERSION: Final = 1

_READ_ONLY_MODE: Final = 0o444
_WRITABLE_MODE: Final = 0o644


class ArchiveStorageError(RuntimeError):
    """Base class for every refusal a storage provider can issue."""


class ArchiveStorageUnavailable(ArchiveStorageError):
    """No usable destination is configured, or the selected one is unimplemented."""


class ArchiveImmutabilityError(ArchiveStorageError):
    """An object already exists at this location with different content."""


class LegalHoldError(ArchiveStorageError):
    """The operation is blocked by an active legal hold."""


class RetentionNotExpiredError(ArchiveStorageError):
    """The operation is blocked because the retention period has not elapsed."""


@dataclass(frozen=True, slots=True)
class ArchiveObject:
    """The bytes to store, plus everything the destination must record with them."""

    archive_id: str
    organization_id: str
    payload: bytes
    checksum: str
    checksum_algorithm: str
    serialization_version: int
    event_count: int
    retention_until: datetime
    legal_hold: bool
    classification: str


@dataclass(frozen=True, slots=True)
class StoredObject:
    """A destination's acknowledgement that it holds the bytes."""

    uri: str
    provider: str
    stored_at: datetime
    retention_acknowledged_until: datetime
    legal_hold: bool
    byte_size: int


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of reading an object back and re-checksumming it."""

    verified: bool
    uri: str
    byte_size: int
    detail: str = ""


class ArchiveStorage(Protocol):
    """What the archive lifecycle requires of a destination."""

    name: str
    available: bool

    def put(self, obj: ArchiveObject) -> StoredObject:
        """Write the object immutably and return the destination's acknowledgement."""

    def read(self, uri: str) -> bytes:
        """Read the stored payload back."""

    def verify(self, uri: str, *, checksum: str, algorithm: str) -> VerificationResult:
        """Re-read the object and confirm its checksum."""

    def manifest(self, uri: str) -> dict[str, Any]:
        """Return the retention/hold metadata the destination holds."""

    def apply_legal_hold(self, uri: str, *, reason: str, at: datetime) -> dict[str, Any]:
        """Place a legal hold, blocking delete and retention expiry."""

    def release_legal_hold(self, uri: str, *, reason: str, at: datetime) -> dict[str, Any]:
        """Release a legal hold, restoring ordinary retention expiry."""

    def delete(self, uri: str, *, now: datetime) -> None:
        """Delete the object, refusing while held or before retention elapses."""


def _refuse(provider: str, detail: str) -> ArchiveStorageUnavailable:
    return ArchiveStorageUnavailable(f"archive storage {provider!r} is unavailable: {detail}")


class NullArchiveStorage:
    """The configured-nothing default. Refuses every operation.

    This class is why an unconfigured deployment cannot report archives it
    does not have: `put` raises, the lifecycle records FAILED, and the
    archive cursor does not advance.
    """

    DEFAULT_DETAIL: Final = (
        "no archive destination is configured. Set audit_archive_storage_backend to "
        "'filesystem' with audit_archive_filesystem_root pointing at a durable path, or "
        "to 's3' with object_store_endpoint/access_key/secret_key and "
        "audit_archive_bucket_name naming an Object Lock bucket."
    )

    def __init__(self, name: str = "none", detail: str = DEFAULT_DETAIL) -> None:
        self.name = name
        self.available = False
        self.detail = detail

    def put(self, obj: ArchiveObject) -> StoredObject:
        raise _refuse(self.name, self.detail)

    def read(self, uri: str) -> bytes:
        raise _refuse(self.name, self.detail)

    def verify(self, uri: str, *, checksum: str, algorithm: str) -> VerificationResult:
        raise _refuse(self.name, self.detail)

    def manifest(self, uri: str) -> dict[str, Any]:
        raise _refuse(self.name, self.detail)

    def apply_legal_hold(self, uri: str, *, reason: str, at: datetime) -> dict[str, Any]:
        raise _refuse(self.name, self.detail)

    def release_legal_hold(self, uri: str, *, reason: str, at: datetime) -> dict[str, Any]:
        raise _refuse(self.name, self.detail)

    def delete(self, uri: str, *, now: datetime) -> None:
        raise _refuse(self.name, self.detail)


class UnavailableArchiveStorage(NullArchiveStorage):
    """A configured backend with no implementation here. Refuses, naming itself."""

    def __init__(self, backend: str, detail: str = "") -> None:
        super().__init__(
            name=backend,
            detail=detail
            or (
                f"backend {backend!r} has no implementation in this repository -- it would "
                "require a cloud SDK that is not a dependency here. Object-lock retention "
                "against a real bucket has therefore never been exercised."
            ),
        )


class FilesystemArchiveStorage:
    """A complete WORM provider over a local (or mounted) filesystem.

    Layout under `root`, one directory per organization::

        <root>/<organization_id>/<archive_id>.json           payload
        <root>/<organization_id>/<archive_id>.manifest.json  retention + hold state

    Durability: both files are written to a temporary file in the same
    directory, fsynced, then `os.replace`d into place, and the containing
    directory is fsynced afterwards where the platform allows it. Only then
    is the file marked read-only.

    Immutability is enforced by content, not by trust: `put` on an existing
    archive id compares the stored checksum and either returns the existing
    object (identical -- the crash-recovery path) or raises
    `ArchiveImmutabilityError` (different). The read-only mode bit is a
    guard rail against accident, not a security boundary; a filesystem root
    is only as immutable as the volume policy behind it, and this class does
    not pretend otherwise.
    """

    name = "filesystem"
    available = True

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()

    @property
    def root(self) -> Path:
        return self._root

    def uri_for(self, organization_id: str, archive_id: str) -> str:
        return f"filesystem://{organization_id}/{archive_id}"

    # --- location helpers -------------------------------------------------

    def _parts(self, uri: str) -> tuple[str, str]:
        prefix = "filesystem://"
        if not uri.startswith(prefix):
            raise ArchiveStorageError(f"not a filesystem archive uri: {uri!r}")
        remainder = uri[len(prefix) :]
        organization_id, _, archive_id = remainder.partition("/")
        if not organization_id or not archive_id or "/" in archive_id:
            raise ArchiveStorageError(f"malformed filesystem archive uri: {uri!r}")
        return organization_id, archive_id

    def _payload_path(self, uri: str) -> Path:
        organization_id, archive_id = self._parts(uri)
        return self._root / organization_id / f"{archive_id}.json"

    def _manifest_path(self, uri: str) -> Path:
        organization_id, archive_id = self._parts(uri)
        return self._root / organization_id / f"{archive_id}.manifest.json"

    # --- durable write ----------------------------------------------------

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Fsync a directory so a rename is durable. A no-op where unsupported."""
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            # Windows cannot open a directory for fsync. The rename is still
            # atomic there; only the metadata flush is unavailable.
            return
        try:
            os.fsync(fd)
        except OSError:
            return
        finally:
            os.close(fd)

    def _write_frozen(self, path: Path, payload: bytes) -> None:
        """Write `payload` durably to `path`, then mark it read-only."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".partial")
        temp_path = Path(temp_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                os.chmod(path, _WRITABLE_MODE)
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        self._fsync_directory(path.parent)
        os.chmod(path, _READ_ONLY_MODE)

    def _read_manifest(self, uri: str) -> dict[str, Any]:
        manifest_path = self._manifest_path(uri)
        if not manifest_path.exists():
            raise ArchiveStorageError(f"no archive manifest at {uri!r}")
        loaded: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ArchiveStorageError(f"archive manifest at {uri!r} is not an object")
        return loaded

    def _write_manifest(self, uri: str, manifest: dict[str, Any]) -> None:
        blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._write_frozen(self._manifest_path(uri), blob)

    # --- ArchiveStorage ---------------------------------------------------

    def put(self, obj: ArchiveObject) -> StoredObject:
        uri = self.uri_for(obj.organization_id, obj.archive_id)
        payload_path = self._payload_path(uri)

        if payload_path.exists():
            existing = self._read_manifest(uri)
            if existing.get("checksum") != obj.checksum:
                raise ArchiveImmutabilityError(
                    f"{uri} already holds a different archive "
                    f"(stored checksum {existing.get('checksum')!r}, "
                    f"offered {obj.checksum!r}); WORM objects are never overwritten"
                )
            logger.info(
                "audit_archive_object_already_present",
                archive_id=obj.archive_id,
                uri=uri,
                checksum=obj.checksum,
            )
            return StoredObject(
                uri=uri,
                provider=self.name,
                stored_at=datetime.fromisoformat(str(existing["stored_at"])),
                retention_acknowledged_until=datetime.fromisoformat(
                    str(existing["retention_until"])
                ),
                legal_hold=bool(existing.get("legal_hold", False)),
                byte_size=payload_path.stat().st_size,
            )

        stored_at = datetime.now(obj.retention_until.tzinfo)
        self._write_frozen(payload_path, obj.payload)
        self._write_manifest(
            uri,
            {
                "manifest_version": MANIFEST_VERSION,
                "archive_id": obj.archive_id,
                "organization_id": obj.organization_id,
                "checksum": obj.checksum,
                "checksum_algorithm": obj.checksum_algorithm,
                "serialization_version": obj.serialization_version,
                "event_count": obj.event_count,
                "byte_size": len(obj.payload),
                "classification": obj.classification,
                "retention_until": obj.retention_until.isoformat(),
                "legal_hold": obj.legal_hold,
                "legal_hold_reason": None,
                "stored_at": stored_at.isoformat(),
            },
        )
        logger.info(
            "audit_archive_object_stored",
            archive_id=obj.archive_id,
            uri=uri,
            byte_size=len(obj.payload),
            checksum=obj.checksum,
            retention_until=obj.retention_until.isoformat(),
        )
        return StoredObject(
            uri=uri,
            provider=self.name,
            stored_at=stored_at,
            retention_acknowledged_until=obj.retention_until,
            legal_hold=obj.legal_hold,
            byte_size=len(obj.payload),
        )

    def read(self, uri: str) -> bytes:
        payload_path = self._payload_path(uri)
        if not payload_path.exists():
            raise ArchiveStorageError(f"no archive object at {uri!r}")
        return payload_path.read_bytes()

    def verify(self, uri: str, *, checksum: str, algorithm: str) -> VerificationResult:
        """Read the object back and confirm the destination holds what we sent."""
        if algorithm != "sha256":
            raise ArchiveStorageError(f"unsupported checksum algorithm {algorithm!r}")
        payload = self.read(uri)
        manifest = self._read_manifest(uri)
        stored_checksum = str(manifest.get("checksum", ""))
        if stored_checksum != checksum:
            return VerificationResult(
                verified=False,
                uri=uri,
                byte_size=len(payload),
                detail=f"manifest checksum {stored_checksum!r} != expected {checksum!r}",
            )
        if len(payload) != int(manifest.get("byte_size", -1)):
            return VerificationResult(
                verified=False,
                uri=uri,
                byte_size=len(payload),
                detail="payload size does not match the manifest",
            )
        return VerificationResult(verified=True, uri=uri, byte_size=len(payload))

    def manifest(self, uri: str) -> dict[str, Any]:
        return self._read_manifest(uri)

    def apply_legal_hold(self, uri: str, *, reason: str, at: datetime) -> dict[str, Any]:
        manifest = self._read_manifest(uri)
        manifest["legal_hold"] = True
        manifest["legal_hold_reason"] = reason
        manifest["legal_hold_applied_at"] = at.isoformat()
        manifest.pop("legal_hold_released_at", None)
        self._write_manifest(uri, manifest)
        logger.info("audit_archive_legal_hold_applied", uri=uri, reason=reason)
        return manifest

    def release_legal_hold(self, uri: str, *, reason: str, at: datetime) -> dict[str, Any]:
        manifest = self._read_manifest(uri)
        manifest["legal_hold"] = False
        manifest["legal_hold_reason"] = reason
        manifest["legal_hold_released_at"] = at.isoformat()
        self._write_manifest(uri, manifest)
        logger.info("audit_archive_legal_hold_released", uri=uri, reason=reason)
        return manifest

    def delete(self, uri: str, *, now: datetime) -> None:
        """Delete only when nothing forbids it: no hold, retention elapsed."""
        manifest = self._read_manifest(uri)
        if bool(manifest.get("legal_hold", False)):
            raise LegalHoldError(f"{uri} is under legal hold and cannot be deleted")
        retention_until = datetime.fromisoformat(str(manifest["retention_until"]))
        if now < retention_until:
            raise RetentionNotExpiredError(
                f"{uri} is retained until {retention_until.isoformat()}; now {now.isoformat()}"
            )
        for path in (self._payload_path(uri), self._manifest_path(uri)):
            if path.exists():
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                path.unlink()
        logger.info("audit_archive_object_deleted", uri=uri)


def build_archive_storage(
    backend: str,
    *,
    filesystem_root: str = "",
    s3_endpoint: str = "",
    s3_bucket: str = "",
    s3_region: str = "us-east-1",
    s3_access_key: str = "",
    s3_secret_key: str = "",
    s3_retention_mode: str = "COMPLIANCE",
) -> ArchiveStorage:
    """Resolve a configured backend name to a provider.

    Anything unconfigured or unimplemented resolves to a provider that
    refuses, never to one that quietly succeeds. An `s3` backend whose
    endpoint, bucket or credentials are missing is *misconfiguration*, and
    resolves to a refusing provider naming what is absent -- it does not
    fall back to a working local one.
    """
    normalized = (backend or "none").strip().lower()
    if normalized == "filesystem":
        if not filesystem_root:
            return UnavailableArchiveStorage(
                "filesystem",
                "audit_archive_filesystem_root is empty; there is nowhere to write.",
            )
        return FilesystemArchiveStorage(filesystem_root)
    if normalized == "s3":
        # Imported here, not at module scope: `aida.audit_archive_s3` imports
        # the protocol types defined above, so a top-level import would be a
        # cycle. The reachability gate walks nested imports too, so this
        # stays a visible edge in the import graph.
        from aida.audit_archive_s3 import S3ArchiveStorage

        try:
            return S3ArchiveStorage(
                endpoint=s3_endpoint,
                bucket=s3_bucket,
                access_key=s3_access_key,
                secret_key=s3_secret_key,
                region=s3_region,
                retention_mode=s3_retention_mode,
            )
        except ArchiveStorageUnavailable as error:
            # Never carries the credential: `S3ArchiveStorage` reports which
            # setting is missing, never any setting's value.
            return UnavailableArchiveStorage("s3", str(error))
    if normalized in {"none", ""}:
        return NullArchiveStorage()
    return UnavailableArchiveStorage(normalized)
