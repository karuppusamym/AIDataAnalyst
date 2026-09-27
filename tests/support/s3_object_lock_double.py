"""An in-memory S3-compatible service that actually enforces Object Lock.

**What this is for, and what it is not.** `Docs/60-delivery/20-capability-register.md`
records F01 as *Partial* because the WORM archive's read-back and retention
properties had only ever been exercised against live MinIO -- which CI does not
have, so in CI those tests skip and nothing covers the S3 provider at all. This
double closes that gap *offline* without weakening the claim, because of where
it sits: it is an `httpx` transport, not a fake `ArchiveStorage`.

That distinction is the whole point. Substituting the storage provider would
leave the real `S3ArchiveStorage` -- SigV4 signing, the canonical query string,
the Object Lock request headers, the version-id pinning, the XML error parsing,
the re-checksum in `verify` -- entirely unexecuted, and a read-back test that
executes none of the code that does the reading back proves nothing. Swapping
only the socket means every line of that provider runs exactly as it does in
production, and the archive lifecycle above it runs unmodified too.

**What it enforces, rather than merely records:**

* Versioning. Every `PUT` to a key mints a new version id and keeps the old
  bytes. A `GET` naming a version id gets *that* version's bytes.
* COMPLIANCE-mode retention. A `DELETE` of a version whose `retain-until` is in
  the future is refused with `403 AccessDenied`, the way a real bucket refuses
  it -- not with a local exception the caller raised on itself.
* Legal hold. A held version cannot be deleted even after retention lapses, and
  releasing the hold restores ordinary expiry.
* The absent-hold code path. A version written `legal-hold-status: OFF` has no
  hold *configuration*, so `GET ?legal-hold` answers
  `404 NoSuchObjectLockConfiguration` -- the exact case
  `S3ArchiveStorage.legal_hold_status` has a branch for.

**What it is still not.** It is not AWS. It does not model IAM, bucket policy,
KMS, replication, or the difference between an AWS `403 AccessDenied` and
MinIO's `400 InvalidRequest` for a WORM-protected delete. A green run here says
the provider and the lifecycle are correct against S3's *documented* Object Lock
semantics; it does not say a particular bucket in a particular account is
configured to enforce them. `Docs/50-security/audit-archive-destination-verification.md`
is the procedure that answers that, and it needs real infrastructure.

The clock is injected so retention expiry can be reached without waiting.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import format_datetime
from itertools import count
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

_LOCK_ENABLED_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<ObjectLockConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    "<ObjectLockEnabled>Enabled</ObjectLockEnabled>"
    "</ObjectLockConfiguration>"
)

#: Metadata headers the provider sets on a write and reads back on a HEAD.
#: Echoed verbatim, because the provider's `verify` compares what it sent with
#: what came back and a double that regenerated them would make that comparison
#: vacuous.
_ECHOED_PREFIXES = ("x-amz-meta-",)


def _error(status: int, code: str, message: str) -> httpx.Response:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Error><Code>{code}</Code><Message>{message}</Message></Error>"
    ).encode()
    return httpx.Response(status, content=body, headers={"content-type": "application/xml"})


@dataclass
class _Version:
    """One immutable version of one key."""

    version_id: str
    payload: bytes
    metadata: dict[str, str]
    retain_until: datetime | None
    retention_mode: str
    last_modified: datetime
    #: None means "no hold configuration was ever created", which is a
    #: different answer from False and the provider has a branch for it.
    legal_hold: bool | None


@dataclass
class ObjectLockService:
    """An S3-compatible Object Lock service, in memory.

    Expose it to a provider with `.transport()`; the provider is otherwise
    constructed exactly as production constructs it.
    """

    bucket: str
    now: datetime = field(default_factory=lambda: datetime(2026, 9, 12, 12, 0, tzinfo=UTC))
    bucket_exists: bool = True
    object_lock_enabled: bool = True
    #: Every version ever written, newest last, per key.
    versions: dict[str, list[_Version]] = field(default_factory=dict)
    #: Requests the provider actually issued, for tests that assert on the
    #: wire rather than on the outcome.
    calls: list[tuple[str, str]] = field(default_factory=list)
    _ids: count[int] = field(default_factory=lambda: count(1))

    # --- clock ------------------------------------------------------------

    def advance_to(self, moment: datetime) -> None:
        """Move the service's clock, so retention expiry is reachable in a test."""
        self.now = moment

    # --- transport --------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        split = urlsplit(str(request.url))
        path = unquote(split.path)
        query = parse_qs(split.query, keep_blank_values=True)
        self.calls.append((request.method, f"{split.path}?{split.query}".rstrip("?")))

        # A signed request must always carry a signature; a provider that
        # stopped signing would otherwise pass every test here.
        if not request.headers.get("authorization", "").startswith("AWS4-HMAC-SHA256 "):
            return _error(403, "AccessDenied", "request was not SigV4-signed")

        segments = path.lstrip("/").split("/", 1)
        bucket = segments[0]
        key = segments[1] if len(segments) > 1 else ""
        if bucket != self.bucket:
            return _error(404, "NoSuchBucket", "no such bucket")

        if not key:
            return self._bucket_op(request, query)
        return self._object_op(request, key, query)

    # --- bucket operations ------------------------------------------------

    def _bucket_op(self, request: httpx.Request, query: dict[str, list[str]]) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200 if self.bucket_exists else 404)
        if request.method == "PUT":
            self.bucket_exists = True
            self.object_lock_enabled = (
                request.headers.get("x-amz-bucket-object-lock-enabled", "").lower() == "true"
            )
            return httpx.Response(200)
        if request.method == "GET" and "object-lock" in query:
            if not self.object_lock_enabled:
                return _error(
                    404,
                    "ObjectLockConfigurationNotFoundError",
                    "object lock configuration does not exist",
                )
            return httpx.Response(
                200, text=_LOCK_ENABLED_XML, headers={"content-type": "application/xml"}
            )
        return _error(400, "InvalidRequest", f"unsupported bucket operation {request.method}")

    # --- object operations ------------------------------------------------

    def _select(self, key: str, query: dict[str, list[str]]) -> _Version | None:
        """The version a request names, or the current one when it names none."""
        stack = self.versions.get(key, [])
        if not stack:
            return None
        requested = query.get("versionId", [""])[0]
        if not requested:
            return stack[-1]
        for version in stack:
            if version.version_id == requested:
                return version
        return None

    def _object_op(
        self, request: httpx.Request, key: str, query: dict[str, list[str]]
    ) -> httpx.Response:
        if "legal-hold" in query:
            return self._legal_hold_op(request, key, query)
        if request.method == "PUT":
            return self._put(request, key)
        if request.method in ("GET", "HEAD"):
            return self._get(request, key, query)
        if request.method == "DELETE":
            return self._delete(key, query)
        return _error(400, "InvalidRequest", f"unsupported object operation {request.method}")

    def _put(self, request: httpx.Request, key: str) -> httpx.Response:
        payload = request.content
        # S3 mandates Content-MD5 on a write carrying Object Lock headers and
        # rejects the write without it. Enforced rather than ignored so that a
        # provider which stopped sending it fails here instead of in production.
        if "content-md5" not in request.headers:
            return _error(400, "InvalidRequest", "Content-MD5 is required for a locked write")

        retain_raw = request.headers.get("x-amz-object-lock-retain-until-date", "")
        hold_header = request.headers.get("x-amz-object-lock-legal-hold-status", "")
        version = _Version(
            version_id=f"v{next(self._ids):08d}",
            payload=payload,
            metadata={
                name.lower(): value
                for name, value in request.headers.items()
                if name.lower().startswith(_ECHOED_PREFIXES)
            },
            retain_until=(
                datetime.fromisoformat(retain_raw.replace("Z", "+00:00")) if retain_raw else None
            ),
            retention_mode=request.headers.get("x-amz-object-lock-mode", ""),
            last_modified=self.now,
            legal_hold=True if hold_header == "ON" else None,
        )
        self.versions.setdefault(key, []).append(version)
        return httpx.Response(
            200,
            headers={
                "x-amz-version-id": version.version_id,
                "etag": f'"{hashlib.md5(payload, usedforsecurity=False).hexdigest()}"',
            },
        )

    def _headers_for(self, version: _Version) -> dict[str, str]:
        headers = {
            "content-length": str(len(version.payload)),
            "content-type": "application/json",
            "etag": f'"{hashlib.md5(version.payload, usedforsecurity=False).hexdigest()}"',
            "last-modified": format_datetime(version.last_modified, usegmt=True),
            "x-amz-version-id": version.version_id,
            "x-amz-object-lock-legal-hold": "ON" if version.legal_hold else "OFF",
        }
        if version.retention_mode:
            headers["x-amz-object-lock-mode"] = version.retention_mode
        if version.retain_until is not None:
            headers["x-amz-object-lock-retain-until-date"] = (
                version.retain_until.astimezone(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
        headers.update(version.metadata)
        return headers

    def _get(
        self, request: httpx.Request, key: str, query: dict[str, list[str]]
    ) -> httpx.Response:
        version = self._select(key, query)
        if version is None:
            # httpx will not send a body on a HEAD response, and S3 does not
            # either -- the status is the whole answer.
            return (
                httpx.Response(404)
                if request.method == "HEAD"
                else _error(404, "NoSuchKey", "the specified key does not exist")
            )
        headers = self._headers_for(version)
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, content=version.payload, headers=headers)

    def _delete(self, key: str, query: dict[str, list[str]]) -> httpx.Response:
        version = self._select(key, query)
        if version is None:
            return httpx.Response(204)
        # The two refusals that make this a WORM store. Both are the
        # *service's*, answered to a caller that asked properly -- which is
        # what distinguishes them from the provider's own advisory pre-checks.
        if version.legal_hold:
            return _error(403, "AccessDenied", "object is under legal hold")
        if (
            version.retain_until is not None
            and self.now < version.retain_until
            and version.retention_mode == "COMPLIANCE"
        ):
            return _error(403, "AccessDenied", "object is WORM protected until retain-until-date")
        self.versions[key] = [
            item for item in self.versions[key] if item.version_id != version.version_id
        ]
        return httpx.Response(204)

    def _legal_hold_op(
        self, request: httpx.Request, key: str, query: dict[str, list[str]]
    ) -> httpx.Response:
        version = self._select(key, query)
        if version is None:
            return _error(404, "NoSuchKey", "the specified key does not exist")
        if request.method == "PUT":
            version.legal_hold = b"<Status>ON</Status>" in request.content
            return httpx.Response(200)
        if request.method == "GET":
            if version.legal_hold is None:
                # Never held, so there is no configuration to read. The
                # provider translates exactly this code into "no hold"; any
                # other error it re-raises.
                return _error(
                    404,
                    "NoSuchObjectLockConfiguration",
                    "the specified object does not have an ObjectLock configuration",
                )
            status = "ON" if version.legal_hold else "OFF"
            return httpx.Response(
                200,
                text=(
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    f"<LegalHold><Status>{status}</Status></LegalHold>"
                ),
                headers={"content-type": "application/xml"},
            )
        return _error(400, "InvalidRequest", "unsupported legal-hold operation")
