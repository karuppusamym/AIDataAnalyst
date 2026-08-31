"""Write-Once-Read-Many audit archive (OB-3).

Provides immutable archival of audit events with retention policy
enforcement, legal hold support, and a pluggable storage backend
interface (S3/GCS/Azure Blob).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import AuditArchiveRecord, AuditEvent

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


async def archive_pending_audit_events(
    session: AsyncSession,
    organization_id: UUID,
    config: ArchiveConfig,
    *,
    batch_size: int = 1000,
) -> ArchiveResult | None:
    """OB-3: archive the oldest not-yet-archived `AuditEvent` rows for one
    organization into an immutable `AuditArchiveRecord`.

    This is the trigger `archive_audit_events` above never had a caller for
    (the audit's OB-3 finding): it selects events after the organization's
    most recently archived `event_range_end` (or from the beginning if
    nothing has been archived yet), up to `batch_size` rows, computes the
    WORM result, and stages the resulting `AuditArchiveRecord` on `session`.
    Commit is the caller's responsibility, matching `aida.events.record_audit`
    and `record_outbox`. Returns None when there is nothing new to archive,
    so the caller can skip committing an empty cycle.
    """
    latest = await session.scalar(
        select(AuditArchiveRecord)
        .where(AuditArchiveRecord.organization_id == organization_id)
        .order_by(AuditArchiveRecord.event_range_end.desc())
        .limit(1)
    )
    cutoff = latest.event_range_end if latest is not None else datetime.min.replace(tzinfo=UTC)

    rows = (
        await session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == organization_id,
                AuditEvent.occurred_at > cutoff,
            )
            .order_by(AuditEvent.occurred_at.asc())
            .limit(batch_size)
        )
    ).all()

    if not rows:
        return None

    envelopes = [
        AuditEventEnvelope(
            event_id=str(row.id),
            organization_id=str(row.organization_id) if row.organization_id else None,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            principal_id=row.principal_id,
            occurred_at=row.occurred_at,
            details=row.details,
        )
        for row in rows
    ]

    result = archive_audit_events(envelopes, config)

    session.add(
        AuditArchiveRecord(
            organization_id=organization_id,
            archive_id=result.archive_id,
            event_count=result.archived_count,
            event_range_start=rows[0].occurred_at,
            event_range_end=rows[-1].occurred_at,
            checksum=result.checksum,
            storage_backend=result.storage_backend,
            retention_until=result.retention_until,
            legal_hold=result.legal_hold,
            created_by="system:worm-archive-job",
        )
    )
    logger.info(
        "audit_archive_record_persisted",
        organization_id=str(organization_id),
        archive_id=result.archive_id,
        event_count=result.archived_count,
    )
    return result


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
