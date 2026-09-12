"""S3 Object Lock provider for the WORM audit archive.

**Invariant this module exists to hold:** an archive stored here is held by
a service that will refuse to destroy it, and the refusal is the service's,
not this process's. `FilesystemArchiveStorage` enforces immutability with a
mode bit it could itself clear; this provider enforces it with S3 Object
Lock in COMPLIANCE mode, which no credential -- including the account root
-- can bypass or shorten before the retain-until date.

Why COMPLIANCE and not GOVERNANCE: GOVERNANCE retention is defeatable by
any principal holding `s3:BypassGovernanceRetention`, which makes it an
operational guard rail rather than a retention control. An audit archive
whose deletion protection can be waived by the operator being audited is
not a WORM archive. COMPLIANCE is one-way and deliberately inconvenient:
objects written here cannot be removed, and neither can the retention be
shortened, until the date passes.

What S3 does on an overwrite, precisely, because the answer is not "it is
refused": Object Lock requires bucket versioning, so a `PUT` to a key that
already exists creates a *new version* and leaves the locked version
intact and retrievable. That is the durability property, and it is why
every URI this provider mints pins a version id::

    s3://<bucket>/<organization_id>/<archive_id>.json?versionId=<version>

`read`, `verify`, `manifest`, the holds and `delete` all address that exact
version, so a later write to the same key -- by this code or by anyone with
the bucket credentials -- cannot change what a stored record resolves to.
On top of that, `put` refuses at the application layer: an existing object
whose recorded checksum differs raises `ArchiveImmutabilityError` rather
than versioning over it, and an identical one returns the existing version
(the crash-after-upload recovery path `FilesystemArchiveStorage` has too).

No SDK: requests are signed by `aida.aws_sigv4` and issued over `httpx`,
both already dependencies. See that module for why.

Every method is synchronous and blocking, matching the provider protocol;
callers on the event loop wrap them in `asyncio.to_thread`.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Final
from urllib.parse import quote, urlsplit

import defusedxml.ElementTree as ET
import httpx
import structlog

from aida.audit_archive_storage import (
    ArchiveImmutabilityError,
    ArchiveObject,
    ArchiveStorageError,
    ArchiveStorageUnavailable,
    LegalHoldError,
    RetentionNotExpiredError,
    StoredObject,
    VerificationResult,
)
from aida.aws_sigv4 import canonical_query_string, signed_headers_for

logger = structlog.get_logger(__name__)

#: Retention modes S3 Object Lock accepts. COMPLIANCE is the default here.
RETENTION_MODES: Final = ("COMPLIANCE", "GOVERNANCE")

#: User-metadata keys. `archive-checksum` is the audit envelope checksum the
#: `AuditArchiveRecord` row carries; `payload-sha256` is the digest of the
#: serialized bytes themselves, so `verify` can re-checksum what came back
#: rather than trusting the object's own self-description.
_META_CHECKSUM: Final = "x-amz-meta-archive-checksum"
_META_PAYLOAD_SHA256: Final = "x-amz-meta-payload-sha256"
_META_ALGORITHM: Final = "x-amz-meta-checksum-algorithm"
_META_ARCHIVE_ID: Final = "x-amz-meta-archive-id"
_META_ORGANIZATION: Final = "x-amz-meta-organization-id"
_META_EVENT_COUNT: Final = "x-amz-meta-event-count"
_META_SERIALIZATION: Final = "x-amz-meta-serialization-version"
_META_CLASSIFICATION: Final = "x-amz-meta-classification"

_URI_SCHEME: Final = "s3://"
_DEFAULT_TIMEOUT: Final = 30.0


class S3ArchiveError(ArchiveStorageError):
    """An S3 request failed. Carries the status and the service's error code."""

    def __init__(self, operation: str, status: int, code: str, message: str) -> None:
        super().__init__(f"S3 {operation} failed with HTTP {status} {code}: {message}")
        self.operation = operation
        self.status = status
        self.code = code


def _findtext(root: Any, name: str) -> str:
    """Read a direct child's text regardless of XML namespace.

    S3 namespaces its response bodies (`xmlns="http://s3.amazonaws.com/doc/
    2006-03-01/"`) but not, in practice, its error documents -- and MinIO
    matches it on both counts. Matching on the local name rather than a
    fixed namespace means neither a namespace nor its absence can turn a
    real answer into a silently empty one, which is exactly the failure
    that would make an unlocked bucket look locked.
    """
    for child in root:
        tag = str(child.tag)
        if tag.rpartition("}")[2] == name:
            return (child.text or "").strip()
    return ""


def _parse_error(body: bytes) -> tuple[str, str]:
    """Pull `<Code>`/`<Message>` out of an S3 error document, forgivingly."""
    if not body:
        return ("", "")
    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return ("", body.decode("utf-8", errors="replace")[:300])
    return (_findtext(root, "Code"), _findtext(root, "Message"))


def _content_md5(payload: bytes) -> str:
    """Base64 MD5, which S3 *requires* on any write carrying Object Lock headers.

    Not a security property -- `usedforsecurity=False` says so -- it is a
    transport integrity check the service mandates for locked writes. The
    real integrity claim is the sha256 in user metadata.
    """
    return base64.b64encode(hashlib.md5(payload, usedforsecurity=False).digest()).decode("ascii")


def _iso_z(moment: datetime) -> str:
    """S3 wants a UTC instant with a literal `Z` and no microseconds."""
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _last_modified(response: httpx.Response, *, default: datetime) -> datetime:
    """The object's own `Last-Modified`, in UTC, or `default` if absent."""
    raw = response.headers.get("last-modified", "")
    if not raw:
        return default
    try:
        parsed: datetime = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return default
    return parsed.astimezone(UTC)


def _parse_iso_z(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class S3ArchiveStorage:
    """A complete WORM provider over S3 Object Lock (or an S3-compatible service).

    Constructed from the deployment's object-store settings. `ensure_bucket`
    creates the bucket *with Object Lock enabled* when it is missing --
    which is the only moment lock can be enabled on a bucket, so a bucket
    that pre-exists without it is reported rather than silently used.
    """

    name = "s3"
    available = True

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        retention_mode: str = "COMPLIANCE",
        session_token: str = "",
        timeout: float = _DEFAULT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not endpoint:
            raise ArchiveStorageUnavailable("archive storage 's3' is unavailable: no endpoint")
        if not bucket:
            raise ArchiveStorageUnavailable("archive storage 's3' is unavailable: no bucket")
        if not access_key or not secret_key:
            raise ArchiveStorageUnavailable(
                "archive storage 's3' is unavailable: object_store_access_key and "
                "object_store_secret_key must both be set"
            )
        mode = retention_mode.strip().upper()
        if mode not in RETENTION_MODES:
            raise ArchiveStorageUnavailable(
                f"archive storage 's3' is unavailable: retention mode {retention_mode!r} "
                f"is not one of {RETENTION_MODES}"
            )

        self._endpoint = endpoint.rstrip("/")
        split = urlsplit(self._endpoint)
        if not split.scheme or not split.netloc:
            raise ArchiveStorageUnavailable(
                f"archive storage 's3' is unavailable: endpoint {endpoint!r} is not a URL"
            )
        self._host = split.netloc
        self.bucket = bucket
        self.region = region
        self.retention_mode = mode
        self._access_key = access_key
        # Never logged, never returned, never placed on a dataclass with a
        # default repr. It leaves this object only as HMAC input.
        self._secret_key = secret_key
        self._session_token = session_token
        self._timeout = timeout
        # The only seam in this class, and it is deliberately below the
        # signature: every request still gets built, signed and parsed by the
        # code above, so a substituted transport exercises SigV4, the
        # canonical query string, the Object Lock headers and the XML error
        # parsing exactly as production does. Substituting the *provider*
        # instead -- the obvious alternative -- would prove none of that, and
        # a read-back test that proves none of that proves nothing.
        #
        # Production passes nothing and gets httpx's real transport. Only
        # tests pass one, which is why there is no setting for it.
        self._transport = transport

    # --- signed transport -------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        payload: bytes = b"",
        operation: str,
        expected: tuple[int, ...] = (200, 204),
    ) -> httpx.Response:
        """Issue one signed request, raising `S3ArchiveError` on anything unexpected.

        There is no branch here that swallows a failure: a status outside
        `expected` raises, so no caller can mistake a refused write for a
        stored object.
        """
        moment = datetime.now(UTC)
        signed = signed_headers_for(
            method=method,
            host=self._host,
            path=path,
            query=query,
            headers=headers,
            payload=payload,
            moment=moment,
            region=self.region,
            access_key=self._access_key,
            secret_key=self._secret_key,
            session_token=self._session_token,
        )
        # The query string is built here, in the same canonical form the
        # signature commits to, and handed to httpx as part of the URL --
        # not as `params`. Passing a dict would leave the wire order and the
        # percent-encoding to the client, and a query string that differs
        # from the signed one by so much as a parameter's position is a 403
        # with nothing in it to say why.
        canonical_query = canonical_query_string(query)
        url = f"{self._endpoint}{path}"
        if canonical_query:
            url = f"{url}?{canonical_query}"
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.request(
                    method,
                    url,
                    headers=signed,
                    content=payload or None,
                )
        except httpx.HTTPError as error:
            raise ArchiveStorageUnavailable(
                f"archive storage 's3' is unavailable: {operation} could not reach "
                f"{self._host} ({type(error).__name__}: {error})"
            ) from error

        if response.status_code not in expected:
            code, message = _parse_error(response.content)
            raise S3ArchiveError(operation, response.status_code, code, message)
        return response

    @staticmethod
    def _object_key(organization_id: str, archive_id: str) -> str:
        return f"{organization_id}/{archive_id}.json"

    def _path_for(self, key: str) -> str:
        return f"/{quote(self.bucket, safe='')}/{quote(key, safe='/')}"

    def uri_for(self, organization_id: str, archive_id: str, version_id: str) -> str:
        key = self._object_key(organization_id, archive_id)
        return f"{_URI_SCHEME}{self.bucket}/{key}?versionId={version_id}"

    def _parts(self, uri: str) -> tuple[str, str]:
        """Split a stored URI into its object key and the version it pins."""
        if not uri.startswith(_URI_SCHEME):
            raise ArchiveStorageError(f"not an s3 archive uri: {uri!r}")
        remainder = uri[len(_URI_SCHEME) :]
        location, _, query = remainder.partition("?")
        bucket, _, key = location.partition("/")
        if bucket != self.bucket:
            raise ArchiveStorageError(
                f"archive uri {uri!r} names bucket {bucket!r}, but this provider is "
                f"configured for {self.bucket!r}"
            )
        version_id = ""
        for item in query.split("&"):
            name, _, value = item.partition("=")
            if name == "versionId":
                version_id = value
        if not key or not version_id:
            raise ArchiveStorageError(
                f"malformed s3 archive uri {uri!r}: a stored object must pin a versionId, "
                "otherwise a later write to the same key would change what it resolves to"
            )
        return key, version_id

    # --- bucket -----------------------------------------------------------

    def ensure_bucket(self) -> bool:
        """Create the bucket with Object Lock enabled if it does not exist.

        Returns True when this call created it. Object Lock can only be
        turned on at creation time, so a pre-existing bucket is left alone
        and `bucket_has_object_lock` is the way to find out whether it is
        actually usable for WORM.

        A `HEAD` answering 301 means the bucket exists in a different region
        than the one configured; that is reported rather than treated as
        absent, because creating it here would either fail or make a second
        bucket somewhere nobody expects.
        """
        path = f"/{quote(self.bucket, safe='')}"
        head = self._request(
            "HEAD", path, operation="HeadBucket", expected=(200, 204, 301, 400, 403, 404)
        )
        if head.status_code in (200, 204):
            return False
        if head.status_code == 301:
            raise ArchiveStorageError(
                f"bucket {self.bucket!r} exists in region "
                f"{head.headers.get('x-amz-bucket-region', 'unknown')!r}, not the configured "
                f"{self.region!r}; set audit_archive_s3_region to match"
            )
        # Outside us-east-1, S3 requires the region in a body -- omitting it
        # silently creates (or refuses to create) the bucket in us-east-1.
        # MinIO accepts either form.
        body = b""
        if self.region != "us-east-1":
            body = (
                "<CreateBucketConfiguration>"
                f"<LocationConstraint>{self.region}</LocationConstraint>"
                "</CreateBucketConfiguration>"
            ).encode()
        self._request(
            "PUT",
            path,
            headers={"x-amz-bucket-object-lock-enabled": "true"},
            payload=body,
            operation="CreateBucket",
            expected=(200, 204),
        )
        logger.info(
            "audit_archive_bucket_created",
            bucket=self.bucket,
            endpoint=self._host,
            object_lock="enabled",
        )
        return True

    def bucket_has_object_lock(self) -> bool:
        """Ask the service whether this bucket actually enforces Object Lock."""
        response = self._request(
            "GET",
            f"/{quote(self.bucket, safe='')}",
            query={"object-lock": ""},
            operation="GetObjectLockConfiguration",
            expected=(200, 404),
        )
        if response.status_code != 200:
            return False
        return _findtext(ET.fromstring(response.text), "ObjectLockEnabled") == "Enabled"

    # --- ArchiveStorage ---------------------------------------------------

    def put(self, obj: ArchiveObject) -> StoredObject:
        """Write the object under Object Lock retention and return its pinned version.

        Refuses at the application layer before it writes: an object already
        at this key whose recorded checksum differs is a different archive
        under the same identity, and versioning over it -- while it would
        not destroy anything -- would leave two archives claiming one id.
        """
        key = self._object_key(obj.organization_id, obj.archive_id)
        existing = self._head_current(key)
        if existing is not None:
            stored_checksum = existing.headers.get(_META_CHECKSUM, "")
            if stored_checksum != obj.checksum:
                raise ArchiveImmutabilityError(
                    f"s3://{self.bucket}/{key} already holds a different archive "
                    f"(stored checksum {stored_checksum!r}, offered {obj.checksum!r}); "
                    "WORM objects are never overwritten"
                )
            version_id = existing.headers.get("x-amz-version-id", "")
            uri = self.uri_for(obj.organization_id, obj.archive_id, version_id)
            logger.info(
                "audit_archive_object_already_present",
                archive_id=obj.archive_id,
                uri=uri,
                checksum=obj.checksum,
            )
            retain_until = existing.headers.get("x-amz-object-lock-retain-until-date", "")
            return StoredObject(
                uri=uri,
                provider=self.name,
                # When the object was actually written, not when this retry
                # noticed it: the record's `uploaded_at` must describe the
                # upload, and a crash-recovery pass can run days later.
                stored_at=_last_modified(existing, default=datetime.now(UTC)),
                retention_acknowledged_until=(
                    _parse_iso_z(retain_until) if retain_until else obj.retention_until
                ),
                legal_hold=existing.headers.get("x-amz-object-lock-legal-hold", "OFF") == "ON",
                byte_size=int(existing.headers.get("content-length", len(obj.payload))),
            )

        retain_until = _iso_z(obj.retention_until)
        headers = {
            "content-type": "application/json",
            "content-md5": _content_md5(obj.payload),
            "x-amz-object-lock-mode": self.retention_mode,
            "x-amz-object-lock-retain-until-date": retain_until,
            "x-amz-object-lock-legal-hold-status": "ON" if obj.legal_hold else "OFF",
            _META_ARCHIVE_ID: obj.archive_id,
            _META_ORGANIZATION: obj.organization_id,
            _META_CHECKSUM: obj.checksum,
            _META_ALGORITHM: obj.checksum_algorithm,
            _META_PAYLOAD_SHA256: hashlib.sha256(obj.payload).hexdigest(),
            _META_EVENT_COUNT: str(obj.event_count),
            _META_SERIALIZATION: str(obj.serialization_version),
            _META_CLASSIFICATION: obj.classification,
        }
        response = self._request(
            "PUT",
            self._path_for(key),
            headers=headers,
            payload=obj.payload,
            operation="PutObject",
            expected=(200,),
        )
        version_id = response.headers.get("x-amz-version-id", "")
        if not version_id:
            raise ArchiveStorageError(
                f"s3://{self.bucket}/{key} was accepted without a versionId; the bucket is "
                "not versioned, so Object Lock cannot be in force on it"
            )
        stored_at = datetime.now(UTC)
        uri = self.uri_for(obj.organization_id, obj.archive_id, version_id)
        logger.info(
            "audit_archive_object_stored",
            archive_id=obj.archive_id,
            uri=uri,
            byte_size=len(obj.payload),
            checksum=obj.checksum,
            retention_mode=self.retention_mode,
            retention_until=retain_until,
        )
        return StoredObject(
            uri=uri,
            provider=self.name,
            stored_at=stored_at,
            retention_acknowledged_until=obj.retention_until,
            legal_hold=obj.legal_hold,
            byte_size=len(obj.payload),
        )

    def _head_current(self, key: str) -> httpx.Response | None:
        """HEAD the current version of a key, or None when there is no object."""
        response = self._request(
            "HEAD",
            self._path_for(key),
            operation="HeadObject",
            expected=(200, 404),
        )
        return response if response.status_code == 200 else None

    def _head_version(self, uri: str) -> httpx.Response:
        key, version_id = self._parts(uri)
        return self._request(
            "HEAD",
            self._path_for(key),
            query={"versionId": version_id},
            operation="HeadObject",
            expected=(200,),
        )

    def read(self, uri: str) -> bytes:
        key, version_id = self._parts(uri)
        response = self._request(
            "GET",
            self._path_for(key),
            query={"versionId": version_id},
            operation="GetObject",
            expected=(200,),
        )
        return response.content

    def verify(self, uri: str, *, checksum: str, algorithm: str) -> VerificationResult:
        """Retrieve the pinned version and re-checksum the bytes that came back.

        Two independent checks, and the second is the one a filesystem
        provider cannot make as strongly: the envelope checksum recorded on
        the object must match the record, *and* the sha256 recomputed over
        the retrieved bytes must match the digest stored beside them. A
        service that handed back different bytes fails here even if its own
        metadata still claims otherwise.
        """
        if algorithm != "sha256":
            raise ArchiveStorageError(f"unsupported checksum algorithm {algorithm!r}")
        payload = self.read(uri)
        head = self._head_version(uri)
        stored_checksum = head.headers.get(_META_CHECKSUM, "")
        if stored_checksum != checksum:
            return VerificationResult(
                verified=False,
                uri=uri,
                byte_size=len(payload),
                detail=f"object checksum {stored_checksum!r} != expected {checksum!r}",
            )
        expected_digest = head.headers.get(_META_PAYLOAD_SHA256, "")
        actual_digest = hashlib.sha256(payload).hexdigest()
        if expected_digest != actual_digest:
            return VerificationResult(
                verified=False,
                uri=uri,
                byte_size=len(payload),
                detail=(
                    f"retrieved payload digest {actual_digest} != stored {expected_digest!r}"
                ),
            )
        declared = int(head.headers.get("content-length", -1))
        if len(payload) != declared:
            return VerificationResult(
                verified=False,
                uri=uri,
                byte_size=len(payload),
                detail=f"retrieved {len(payload)} bytes, object declares {declared}",
            )
        return VerificationResult(verified=True, uri=uri, byte_size=len(payload))

    def manifest(self, uri: str) -> dict[str, Any]:
        """Return what the *service* says it holds, not what we asked it to hold."""
        key, version_id = self._parts(uri)
        head = self._head_version(uri)
        retain_until = head.headers.get("x-amz-object-lock-retain-until-date", "")
        return {
            "provider": self.name,
            "bucket": self.bucket,
            "key": key,
            "version_id": version_id,
            "archive_id": head.headers.get(_META_ARCHIVE_ID, ""),
            "organization_id": head.headers.get(_META_ORGANIZATION, ""),
            "checksum": head.headers.get(_META_CHECKSUM, ""),
            "checksum_algorithm": head.headers.get(_META_ALGORITHM, ""),
            "payload_sha256": head.headers.get(_META_PAYLOAD_SHA256, ""),
            "serialization_version": int(head.headers.get(_META_SERIALIZATION, 0) or 0),
            "event_count": int(head.headers.get(_META_EVENT_COUNT, 0) or 0),
            "classification": head.headers.get(_META_CLASSIFICATION, ""),
            "byte_size": int(head.headers.get("content-length", 0) or 0),
            "retention_mode": head.headers.get("x-amz-object-lock-mode", ""),
            "retention_until": retain_until,
            "legal_hold": head.headers.get("x-amz-object-lock-legal-hold", "OFF") == "ON",
            "etag": head.headers.get("etag", ""),
        }

    # --- legal hold -------------------------------------------------------

    def _set_legal_hold(self, uri: str, status: str) -> None:
        key, version_id = self._parts(uri)
        body = f"<LegalHold><Status>{status}</Status></LegalHold>".encode()
        self._request(
            "PUT",
            self._path_for(key),
            query={"legal-hold": "", "versionId": version_id},
            headers={"content-md5": _content_md5(body), "content-type": "application/xml"},
            payload=body,
            operation="PutObjectLegalHold",
            expected=(200, 204),
        )

    def legal_hold_status(self, uri: str) -> bool:
        """Read the hold back from the service rather than from our own record.

        An object written with `legal-hold-status: OFF` has never had a hold
        *configuration* created, and the service answers
        `NoSuchObjectLockConfiguration` rather than reporting `OFF`. That one
        code means "no hold"; every other failure is re-raised, so a
        permissions or transport error can never be read as an absent hold.
        """
        key, version_id = self._parts(uri)
        try:
            response = self._request(
                "GET",
                self._path_for(key),
                query={"legal-hold": "", "versionId": version_id},
                operation="GetObjectLegalHold",
                expected=(200, 404),
            )
        except S3ArchiveError as error:
            if error.code == "NoSuchObjectLockConfiguration":
                return False
            raise
        if response.status_code != 200:
            return False
        return _findtext(ET.fromstring(response.text), "Status") == "ON"

    def apply_legal_hold(self, uri: str, *, reason: str, at: datetime) -> dict[str, Any]:
        self._set_legal_hold(uri, "ON")
        manifest = self.manifest(uri)
        manifest["legal_hold_reason"] = reason
        manifest["legal_hold_applied_at"] = at.isoformat()
        logger.info("audit_archive_legal_hold_applied", uri=uri, reason=reason)
        return manifest

    def release_legal_hold(self, uri: str, *, reason: str, at: datetime) -> dict[str, Any]:
        self._set_legal_hold(uri, "OFF")
        manifest = self.manifest(uri)
        manifest["legal_hold_reason"] = reason
        manifest["legal_hold_released_at"] = at.isoformat()
        logger.info("audit_archive_legal_hold_released", uri=uri, reason=reason)
        return manifest

    # --- delete -----------------------------------------------------------

    def delete(self, uri: str, *, now: datetime) -> None:
        """Delete the pinned version -- which S3 will refuse while it is locked.

        The pre-checks below exist so the caller gets the same
        `LegalHoldError` / `RetentionNotExpiredError` the filesystem
        provider raises, but they are not what enforces anything: the
        `DELETE` names a specific version, so if the checks were removed the
        service would still answer 403 `AccessDenied`. That difference --
        our refusal is advisory, S3's is binding -- is the whole reason this
        provider exists.
        """
        key, version_id = self._parts(uri)
        head = self._head_version(uri)
        if head.headers.get("x-amz-object-lock-legal-hold", "OFF") == "ON":
            raise LegalHoldError(f"{uri} is under legal hold and cannot be deleted")
        retain_until = head.headers.get("x-amz-object-lock-retain-until-date", "")
        if retain_until:
            retention_until = _parse_iso_z(retain_until)
            if now < retention_until:
                raise RetentionNotExpiredError(
                    f"{uri} is retained until {retention_until.isoformat()}; "
                    f"now {now.isoformat()}"
                )
        self._request(
            "DELETE",
            self._path_for(key),
            query={"versionId": version_id},
            operation="DeleteObject",
            expected=(200, 204),
        )
        logger.info("audit_archive_object_deleted", uri=uri, version_id=version_id)
