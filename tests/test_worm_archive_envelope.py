"""Review F02: the archive checksum must cover the whole audit envelope.

The defect these tests exist to keep fixed: the original `_compute_checksum`
hashed event id, action and timestamp by bare concatenation, so organization,
principal, resource, outcome, correlation id and details could all be
rewritten without changing the checksum -- and the checksum was the only
integrity evidence an `AuditArchiveRecord` carried.

The acceptance criteria from the review, and where each is checked here:

* changing any protected field invalidates verification ->
  `test_changing_any_protected_field_invalidates_verification`
* order and encoding are deterministic ->
  `test_batch_checksum_is_order_independent`, `test_canonical_bytes_are_stable`
* added fields cannot silently escape the contract ->
  `test_envelope_fields_match_dataclass`,
  `test_audit_event_columns_are_all_covered`
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from aida.audit_envelope import (
    AUDIT_EVENT_COLUMN_MAP,
    ENVELOPE_FIELDS,
    ENVELOPE_VERSION,
    LEGACY_ENVELOPE_VERSION,
    AuditEventEnvelope,
    EnvelopeVersionError,
    canonical_envelope_bytes,
    compute_batch_checksum,
    coverage_note,
    envelope_dataclass_fields,
    verify_batch_checksum,
)
from aida.models import AuditEvent

OCCURRED_AT = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


def _envelope(event_id: str = "1", **overrides: object) -> AuditEventEnvelope:
    base = {
        "event_id": event_id,
        "organization_id": "11111111-1111-1111-1111-111111111111",
        "action": "data_access",
        "resource_type": "table",
        "resource_id": "tbl-1",
        "principal_id": "analyst-1",
        "principal_type": "USER",
        "outcome": "SUCCESS",
        "correlation_id": "corr-1",
        "source_ip": "10.0.0.1",
        "occurred_at": OCCURRED_AT,
        "details": {"rows": 12, "nested": {"a": 1}},
    }
    base.update(overrides)
    return AuditEventEnvelope(**base)  # type: ignore[arg-type]


# --- the contract: nothing escapes the hash --------------------------------


def test_envelope_fields_match_dataclass() -> None:
    """A field added to the envelope and not to `ENVELOPE_FIELDS` fails here.

    This is the gate the review asked for: it is not possible to widen the
    envelope and leave the checksum behind, because the covered set and the
    declared set are compared directly.
    """
    assert set(envelope_dataclass_fields()) == set(ENVELOPE_FIELDS)


def test_audit_event_columns_are_all_covered() -> None:
    """Every column of the audit ledger row maps onto a covered field."""
    columns = {column.name for column in AuditEvent.__table__.columns}
    assert columns == set(AUDIT_EVENT_COLUMN_MAP)
    assert set(AUDIT_EVENT_COLUMN_MAP.values()) == set(ENVELOPE_FIELDS)


PROTECTED_MUTATIONS: list[tuple[str, object]] = [
    ("event_id", "999"),
    ("organization_id", "22222222-2222-2222-2222-222222222222"),
    ("action", "data_export"),
    ("resource_type", "view"),
    ("resource_id", "tbl-2"),
    ("principal_id", "attacker-1"),
    ("principal_type", "SERVICE"),
    ("outcome", "DENIED"),
    ("correlation_id", "corr-2"),
    ("source_ip", "10.0.0.2"),
    ("occurred_at", datetime(2026, 6, 15, 12, 0, 1, tzinfo=UTC)),
    ("details", {"rows": 13, "nested": {"a": 1}}),
]


@pytest.mark.parametrize(("field_name", "new_value"), PROTECTED_MUTATIONS)
def test_changing_any_protected_field_invalidates_verification(
    field_name: str, new_value: object
) -> None:
    original = _envelope()
    checksum = compute_batch_checksum([original])
    tampered = dataclasses.replace(original, **{field_name: new_value})

    assert verify_batch_checksum([original], checksum) is True
    assert verify_batch_checksum([tampered], checksum) is False


def test_every_protected_field_has_a_mutation_case() -> None:
    """The parametrised list above must not drift behind the envelope."""
    assert {name for name, _ in PROTECTED_MUTATIONS} == set(ENVELOPE_FIELDS)


# --- determinism and framing ----------------------------------------------


def test_canonical_bytes_are_stable() -> None:
    envelope = _envelope()
    assert canonical_envelope_bytes(envelope) == canonical_envelope_bytes(envelope)
    rendered = canonical_envelope_bytes(envelope).decode("utf-8")
    assert '"action":"data_access"' in rendered
    assert ", " not in rendered  # no insignificant whitespace
    assert rendered.index('"action"') < rendered.index('"correlation_id"')  # sorted keys


def test_batch_checksum_is_order_independent() -> None:
    a, b, c = _envelope("1"), _envelope("2"), _envelope("3")
    assert compute_batch_checksum([a, b, c]) == compute_batch_checksum([c, a, b])


def test_batch_checksum_depends_on_membership() -> None:
    a, b = _envelope("1"), _envelope("2")
    assert compute_batch_checksum([a, b]) != compute_batch_checksum([a])
    assert compute_batch_checksum([a, b]) != compute_batch_checksum([b])


def test_field_boundaries_cannot_be_shifted() -> None:
    """`("ab", "c")` and `("a", "bc")` must not hash alike.

    This is the concrete failure mode of the concatenating hash the review
    found; canonical JSON plus length-delimited framing is what rules it out.
    """
    left = _envelope("1", action="ab", resource_type="c")
    right = _envelope("1", action="a", resource_type="bc")
    assert canonical_envelope_bytes(left) != canonical_envelope_bytes(right)
    assert compute_batch_checksum([left]) != compute_batch_checksum([right])


def test_naive_timestamps_are_rejected() -> None:
    naive = _envelope("1", occurred_at=datetime(2026, 6, 15, 12, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical_envelope_bytes(naive)


# --- versioning ------------------------------------------------------------


def test_v1_verification_is_preserved_for_legacy_records() -> None:
    """A record checksummed under v1 still verifies -- under v1."""
    envelopes = [_envelope("1"), _envelope("2")]
    legacy_checksum = compute_batch_checksum(envelopes, version=LEGACY_ENVELOPE_VERSION)
    assert (
        verify_batch_checksum(envelopes, legacy_checksum, version=LEGACY_ENVELOPE_VERSION) is True
    )
    assert verify_batch_checksum(envelopes, legacy_checksum, version=ENVELOPE_VERSION) is False


def test_v1_coverage_is_genuinely_narrower() -> None:
    """v1 is preserved, not endorsed: it still misses the tampering v2 catches."""
    original = _envelope("1")
    tampered = dataclasses.replace(original, outcome="DENIED", principal_id="attacker-1")
    legacy_checksum = compute_batch_checksum([original], version=LEGACY_ENVELOPE_VERSION)

    assert (
        verify_batch_checksum([tampered], legacy_checksum, version=LEGACY_ENVELOPE_VERSION) is True
    )
    assert "NARROW" in coverage_note(LEGACY_ENVELOPE_VERSION)
    assert "FULL" in coverage_note(ENVELOPE_VERSION)


def test_unknown_version_is_an_error_not_a_fallback() -> None:
    with pytest.raises(EnvelopeVersionError):
        compute_batch_checksum([_envelope()], version=99)
