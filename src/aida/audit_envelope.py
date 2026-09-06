"""Canonical, versioned serialization of an audit event for archival hashing.

**Invariant this module exists to hold:** the checksum stored on an
`AuditArchiveRecord` covers *every* field of the audit envelope, and a
change to any one of them makes verification fail. A hash that covers a
subset is worse than no hash, because it certifies the untouched subset
while leaving the interesting fields -- tenant, actor, resource, outcome --
free to be rewritten without detection.

Two things follow from that, and both are enforced here rather than left to
the caller:

1. **Serialization is canonical and length-delimited.** Bytes are produced
   by JSON with sorted keys, explicit `(",", ":")` separators and UTF-8
   encoding, then framed with an 8-byte big-endian length before being fed
   to the hasher. Bare concatenation of field values is not used anywhere:
   `("ab", "c")` and `("a", "bc")` must not hash alike, and only framing
   guarantees that.

2. **Serialization is versioned, and verification selects the algorithm by
   the version stored alongside the checksum.** `ENVELOPE_VERSION` is the
   version new archives are written with. Version 1 is preserved verbatim
   in `_checksum_v1` purely so pre-existing records keep verifying; it
   covers only event id, action and timestamp, and `VERSION_COVERAGE`
   records that narrower coverage so a caller can report it honestly
   instead of implying a v1 record was protected the way a v2 one is.

`ENVELOPE_FIELDS` is the covered field set, and it is the single definition
of what "the envelope" means. `canonical_envelope_bytes` builds its JSON
object by iterating it, so a field added to `AuditEventEnvelope` and not to
`ENVELOPE_FIELDS` cannot silently escape the hash -- it fails
`tests/test_worm_archive_envelope.py::test_envelope_fields_match_dataclass`
first. Adding a field to the covered set is a serialization change and
therefore a version bump, not an edit to v2.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, Final

from aida.models import AuditEvent

ENVELOPE_VERSION: Final = 2
"""Serialization version new archives are written with."""

LEGACY_ENVELOPE_VERSION: Final = 1
"""Version 1: event id + action + occurred_at only. Read-only, see module docstring."""

CHECKSUM_ALGORITHM: Final = "sha256"

SUPPORTED_ENVELOPE_VERSIONS: Final = (LEGACY_ENVELOPE_VERSION, ENVELOPE_VERSION)

VERSION_COVERAGE: Final[dict[int, str]] = {
    LEGACY_ENVELOPE_VERSION: (
        "NARROW: covers event_id, action and occurred_at only. Organization, principal, "
        "resource, outcome, correlation id and details are NOT protected."
    ),
    ENVELOPE_VERSION: "FULL: covers every field of the audit envelope.",
}

ENVELOPE_FIELDS: Final = (
    "event_id",
    "organization_id",
    "principal_id",
    "principal_type",
    "action",
    "resource_type",
    "resource_id",
    "outcome",
    "correlation_id",
    "source_ip",
    "occurred_at",
    "details",
)
"""Every field the v2 checksum covers, in declaration order.

Serialization sorts keys, so this order is documentation rather than wire
format -- but the *membership* of this tuple is the contract.
"""

AUDIT_EVENT_COLUMN_MAP: Final[dict[str, str]] = {
    "id": "event_id",
    "organization_id": "organization_id",
    "principal_id": "principal_id",
    "principal_type": "principal_type",
    "action": "action",
    "resource_type": "resource_type",
    "resource_id": "resource_id",
    "outcome": "outcome",
    "correlation_id": "correlation_id",
    "source_ip": "source_ip",
    "occurred_at": "occurred_at",
    "details": "details",
}
"""`AuditEvent` column -> envelope field. Every column of the ledger row maps
to a covered field; a column added to `AuditEvent` without an entry here fails
`test_audit_event_columns_are_all_covered`.
"""

_FRAME_WIDTH: Final = 8


class EnvelopeVersionError(ValueError):
    """Raised when a checksum is requested or verified under an unknown version."""


@dataclass(frozen=True, slots=True)
class AuditEventEnvelope:
    """The complete, serializable form of one audit ledger row.

    Every field here is protected by the v2 checksum. Construct it from an
    ORM row with `envelope_from_audit_event` rather than by hand, so the
    column mapping stays in one place.
    """

    event_id: str
    organization_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    principal_id: str
    occurred_at: datetime
    principal_type: str = "UNKNOWN"
    outcome: str = "UNKNOWN"
    correlation_id: str = ""
    source_ip: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a ledger timestamp that came back without an offset.

    `AuditEvent.occurred_at` is declared `DateTime(timezone=True)` and is
    always written in UTC, but SQLite -- the dialect the test suite builds
    its schema on -- has no timestamptz and returns naive values. Assuming
    UTC is correct *for this column*, and doing it here keeps the assumption
    at the ORM boundary instead of inside the serializer, which stays strict.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def envelope_from_audit_event(row: AuditEvent) -> AuditEventEnvelope:
    """Project an `AuditEvent` ORM row onto the envelope, losing nothing."""
    return AuditEventEnvelope(
        event_id=str(row.id),
        organization_id=str(row.organization_id) if row.organization_id else None,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        principal_id=row.principal_id,
        principal_type=row.principal_type,
        outcome=row.outcome,
        correlation_id=row.correlation_id,
        source_ip=row.source_ip,
        occurred_at=_as_utc(row.occurred_at),
        details=dict(row.details or {}),
    )


def _jsonable(value: Any) -> Any:
    """Reduce a field value to something `json.dumps` renders deterministically."""
    if isinstance(value, datetime):
        # Normalised to UTC before rendering: the same instant read back
        # through a connection with a different session timezone would
        # otherwise produce different bytes and so a different checksum.
        # A naive value is rejected outright rather than assumed to be UTC
        # here -- see `envelope_from_audit_event`, which is the one place
        # allowed to make that assumption, and only about the ledger.
        if value.tzinfo is None:
            raise ValueError("audit envelope timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def canonical_envelope_bytes(envelope: AuditEventEnvelope) -> bytes:
    """Canonical UTF-8 JSON for one envelope: sorted keys, no insignificant space.

    Built by iterating `ENVELOPE_FIELDS`, so the covered set and the bytes
    can never disagree.
    """
    payload = {name: _jsonable(getattr(envelope, name)) for name in ENVELOPE_FIELDS}
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _frame(hasher: Any, blob: bytes) -> None:
    hasher.update(len(blob).to_bytes(_FRAME_WIDTH, "big"))
    hasher.update(blob)


def _checksum_v2(events: list[AuditEventEnvelope]) -> str:
    """SHA-256 over length-delimited canonical envelopes, in canonical order.

    Ordering is by the serialized bytes themselves: a total order that needs
    no tie-break rule, so two batches holding the same events hash alike
    however they were selected.
    """
    blobs = sorted(canonical_envelope_bytes(event) for event in events)
    hasher = hashlib.sha256()
    _frame(hasher, f"atlas.audit.envelope/v{ENVELOPE_VERSION}".encode())
    hasher.update(len(blobs).to_bytes(_FRAME_WIDTH, "big"))
    for blob in blobs:
        _frame(hasher, blob)
    return hasher.hexdigest()


def _checksum_v1(events: list[AuditEventEnvelope]) -> str:
    """The original, narrower checksum. Preserved byte-for-byte for read-back only.

    Do not extend this: any change here would invalidate every stored v1
    record it exists to verify.
    """
    hasher = hashlib.sha256()
    for event in sorted(events, key=lambda e: e.event_id):
        hasher.update(event.event_id.encode())
        hasher.update(event.action.encode())
        hasher.update(event.occurred_at.isoformat().encode())
    return hasher.hexdigest()


def compute_batch_checksum(
    events: list[AuditEventEnvelope],
    *,
    version: int = ENVELOPE_VERSION,
) -> str:
    """Checksum a batch under `version`. New archives must use the default."""
    if version == ENVELOPE_VERSION:
        return _checksum_v2(events)
    if version == LEGACY_ENVELOPE_VERSION:
        return _checksum_v1(events)
    raise EnvelopeVersionError(
        f"unknown audit envelope version {version!r}; supported: {SUPPORTED_ENVELOPE_VERSIONS}"
    )


def verify_batch_checksum(
    events: list[AuditEventEnvelope],
    expected_checksum: str,
    *,
    version: int = ENVELOPE_VERSION,
) -> bool:
    """Recompute under the *stored* version and compare.

    The version is an input, not an assumption: verifying a v1 record with
    the v2 algorithm would report tampering on an untouched archive, and
    verifying a v2 record with the v1 algorithm would report integrity it
    never established.
    """
    return compute_batch_checksum(events, version=version) == expected_checksum


def coverage_note(version: int) -> str:
    """Human-readable statement of what `version` actually protects."""
    return VERSION_COVERAGE.get(version, f"UNKNOWN: version {version} is not recognised.")


def envelope_dataclass_fields() -> tuple[str, ...]:
    """Declared field names of `AuditEventEnvelope`, for the coverage test."""
    return tuple(f.name for f in fields(AuditEventEnvelope))
