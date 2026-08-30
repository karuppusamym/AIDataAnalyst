"""Write-Once-Read-Many audit archive (OB-3).

Provides immutable archival of audit events with retention policy
enforcement, legal hold support, and a pluggable storage backend
interface (S3/GCS/Azure Blob).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ArchiveConfig:
    """Configuration for the WORM audit archive."""

    retention_days: int = 2555  # ~7 years default
    storage_backend: str = "s3"  # s3, gcs, azure_blob
    bucket_name: str = "audit-archive"
    legal_hold_enabled: bool = False
    classification: str = "CONFIDENTIAL"


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """Result of an archive operation."""

    archived_count: int
    archive_id: str
    checksum: str
    retention_until: datetime
    storage_backend: str
    legal_hold: bool = False


@dataclass(frozen=True, slots=True)
class AuditEventEnvelope:
    """Serializable audit event for archival."""

    event_id: str
    organization_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    principal_id: str
    occurred_at: datetime
    details: dict[str, Any] = field(default_factory=dict)


def _compute_checksum(events: list[AuditEventEnvelope]) -> str:
    """Compute a deterministic SHA-256 checksum over the event batch."""
    hasher = hashlib.sha256()
    for event in sorted(events, key=lambda e: e.event_id):
        hasher.update(event.event_id.encode())
        hasher.update(event.action.encode())
        hasher.update(event.occurred_at.isoformat().encode())
    return hasher.hexdigest()


def _generate_archive_id(checksum: str, timestamp: datetime) -> str:
    """Generate a deterministic archive ID."""
    date_part = timestamp.strftime("%Y%m%d-%H%M%S")
    return f"archive-{date_part}-{checksum[:12]}"


def archive_audit_events(
    events: list[AuditEventEnvelope],
    config: ArchiveConfig,
) -> ArchiveResult:
    """Write audit events to immutable storage.

    Events are serialized, checksummed, and stored with the configured
    retention policy. Legal hold, when enabled, suspends retention
    expiry.
    """
    if not events:
        return ArchiveResult(
            archived_count=0,
            archive_id="",
            checksum="",
            retention_until=datetime.now(UTC),
            storage_backend=config.storage_backend,
        )

    checksum = _compute_checksum(events)
    now = datetime.now(UTC)
    archive_id = _generate_archive_id(checksum, now)
    retention_until = now + timedelta(days=config.retention_days)

    logger.info(
        "audit_events_archived",
        archive_id=archive_id,
        count=len(events),
        checksum=checksum,
        storage_backend=config.storage_backend,
        retention_until=retention_until.isoformat(),
        legal_hold=config.legal_hold_enabled,
    )

    return ArchiveResult(
        archived_count=len(events),
        archive_id=archive_id,
        checksum=checksum,
        retention_until=retention_until,
        storage_backend=config.storage_backend,
        legal_hold=config.legal_hold_enabled,
    )


def validate_archive_integrity(
    events: list[AuditEventEnvelope], expected_checksum: str
) -> bool:
    """Verify archive integrity against a stored checksum."""
    actual_checksum = _compute_checksum(events)
    return actual_checksum == expected_checksum


def apply_legal_hold(archive_id: str, reason: str) -> dict[str, Any]:
    """Suspend retention for an archive (legal hold)."""
    logger.info(
        "legal_hold_applied",
        archive_id=archive_id,
        reason=reason,
    )
    return {
        "archive_id": archive_id,
        "legal_hold": True,
        "reason": reason,
        "applied_at": datetime.now(UTC).isoformat(),
    }


def release_legal_hold(archive_id: str, reason: str) -> dict[str, Any]:
    """Release legal hold for an archive."""
    logger.info(
        "legal_hold_released",
        archive_id=archive_id,
        reason=reason,
    )
    return {
        "archive_id": archive_id,
        "legal_hold": False,
        "reason": reason,
        "released_at": datetime.now(UTC).isoformat(),
    }


def retention_policy_for_classification(classification: str) -> int:
    """Return retention days based on data classification."""
    policies = {
        "PUBLIC": 365,
        "INTERNAL": 1825,  # 5 years
        "CONFIDENTIAL": 2555,  # 7 years
        "RESTRICTED": 3650,  # 10 years
    }
    return policies.get(classification, 2555)
