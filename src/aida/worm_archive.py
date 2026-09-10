"""Write-Once-Read-Many audit archive (OB-3).

**Invariant this module exists to hold:** archive progress advances only
past events whose bytes a destination has acknowledged and handed back
intact. Everything else here follows from that.

Three properties are enforced rather than assumed, each answering a defect
recorded in `Docs/review-2026-09-05/REVIEW.md`:

*Storage is real or the attempt fails (F01).* The lifecycle is two-phase:
PREPARED (the record and its membership rows exist, nothing has been sent)
-> UPLOADED (a provider acknowledged the bytes) -> VERIFIED (the bytes were
read back and re-checksummed). FAILED is terminal for the attempt and
retried on the next sweep. `NullArchiveStorage` -- the default when no
destination is configured -- refuses, so an unconfigured deployment records
FAILED rather than a fabricated archive. Only a VERIFIED record counts.

*Integrity covers the whole envelope (F02).* The checksum comes from
`aida.audit_envelope`, over canonical length-delimited bytes of every field
of the event, and the version that produced it is stored on the row so
read-back verification selects the matching algorithm instead of guessing.

*Progress cannot skip an event (F03).* Selection uses a composite
`(occurred_at, id)` keyset cursor and excludes events that already have an
`AuditArchiveMembership` row, so equal timestamps at a batch boundary
cannot strand the remainder. A bounded overlap window behind the cursor is
re-scanned every sweep to catch commits that land with older timestamps.
An `AuditArchiveLease` per organization keeps replicas off each other's
batches, and the membership unique constraint catches the race that gets
past it.

Transaction boundary, deliberately different from `aida.events.record_audit`:
this function commits at each phase transition. A two-phase protocol whose
intermediate state is never durable is not two-phase -- a crash between
upload and verification has to leave an UPLOADED row behind for the next
sweep to find, which means the caller cannot own the commit.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import structlog
from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.audit_archive_storage import (
    ArchiveObject,
    ArchiveStorage,
    ArchiveStorageError,
    NullArchiveStorage,
    build_archive_storage,
)
from aida.audit_envelope import (
    CHECKSUM_ALGORITHM,
    ENVELOPE_VERSION,
    AuditEventEnvelope,
    canonical_envelope_bytes,
    compute_batch_checksum,
    coverage_note,
    envelope_from_audit_event,
    verify_batch_checksum,
)
from aida.models import AuditArchiveLease, AuditArchiveMembership, AuditArchiveRecord, AuditEvent

logger = structlog.get_logger(__name__)

STATE_PREPARED = "PREPARED"
STATE_UPLOADED = "UPLOADED"
STATE_VERIFIED = "VERIFIED"
STATE_FAILED = "FAILED"
STATE_LEGACY = "LEGACY_UNVERIFIED"

RESUMABLE_STATES = (STATE_PREPARED, STATE_UPLOADED, STATE_FAILED)

_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ArchiveConfig:
    """Configuration for the WORM audit archive.

    `storage_backend` defaults to `"none"`, which resolves to a provider
    that refuses. That default is the honest one: a deployment that has not
    named a destination does not have an archive, and this module will not
    manufacture evidence that it does.
    """

    retention_days: int = 2555  # ~7 years default
    storage_backend: str = "none"
    filesystem_root: str = ""
    bucket_name: str = "audit-archive"
    legal_hold_enabled: bool = False
    classification: str = "CONFIDENTIAL"
    lease_seconds: int = 900
    late_arrival_overlap_seconds: int = 86_400
    # Destination for the `s3` backend. `s3_secret_key` is `repr=False` so
    # that a traceback, a structlog kwarg or a debugger frame that renders
    # this config cannot spill the credential -- the generated destination
    # inventory asserts no secret value reaches a document, and the same
    # rule holds for anything that renders a config.
    s3_endpoint: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = field(default="", repr=False)
    s3_retention_mode: str = "COMPLIANCE"


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    """Outcome of one archive attempt.

    `archived_count` is non-zero only in the VERIFIED state. A failed upload
    reports `state == "FAILED"` with a zero count and a reason, never a
    count that implies events were stored.
    """

    archive_id: str
    state: str
    archived_count: int
    checksum: str
    checksum_algorithm: str
    serialization_version: int
    retention_until: datetime
    storage_backend: str
    storage_uri: str | None = None
    legal_hold: bool = False
    verified_at: datetime | None = None
    failure_reason: str | None = None
    retryable: bool = False

    @property
    def verified(self) -> bool:
        return self.state == STATE_VERIFIED


def default_worker_identity() -> str:
    """Identify this replica for lease ownership. Host + pid is enough."""
    return f"{socket.gethostname()}:{os.getpid()}"


def storage_for(config: ArchiveConfig) -> ArchiveStorage:
    """Resolve the provider named by `config`. Never returns a silent no-op."""
    return build_archive_storage(
        config.storage_backend,
        filesystem_root=config.filesystem_root,
        s3_endpoint=config.s3_endpoint,
        s3_bucket=config.bucket_name,
        s3_region=config.s3_region,
        s3_access_key=config.s3_access_key,
        s3_secret_key=config.s3_secret_key,
        s3_retention_mode=config.s3_retention_mode,
    )


def serialize_archive(
    envelopes: list[AuditEventEnvelope],
    *,
    archive_id: str,
    organization_id: str,
    checksum: str,
    retention_until: datetime,
    classification: str,
) -> bytes:
    """The exact bytes handed to the provider.

    A self-describing document: the events plus the version, algorithm and
    checksum needed to verify it years later without this codebase.
    """
    document = {
        "archive_id": archive_id,
        "organization_id": organization_id,
        "serialization_version": ENVELOPE_VERSION,
        "checksum_algorithm": CHECKSUM_ALGORITHM,
        "checksum": checksum,
        "checksum_coverage": coverage_note(ENVELOPE_VERSION),
        "classification": classification,
        "retention_until": retention_until.isoformat(),
        "event_count": len(envelopes),
        "events": [
            json.loads(canonical_envelope_bytes(envelope).decode("utf-8"))
            for envelope in sorted(envelopes, key=lambda e: canonical_envelope_bytes(e))
        ],
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _generate_archive_id(checksum: str, timestamp: datetime) -> str:
    """Deterministic archive id.

    Derived from the checksum so a retry of the same batch produces the same
    id, which is what lets a provider recognise a re-upload as the object it
    already holds instead of a second archive.
    """
    date_part = timestamp.strftime("%Y%m%d")
    return f"archive-{date_part}-{checksum[:16]}"


# --- lease -----------------------------------------------------------------


async def acquire_archive_lease(
    session: AsyncSession,
    organization_id: UUID,
    *,
    owner: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    """Claim the sweep for one organization. False means someone else holds it.

    Two writes, both guarded, so neither dialect needs a lock statement:
    an INSERT that the primary key rejects on collision, and an UPDATE whose
    WHERE clause only matches an expired lease or our own. The rowcount, not
    a prior SELECT, decides.
    """
    moment = now or datetime.now(UTC)
    expires_at = moment + timedelta(seconds=lease_seconds)

    taken = cast(
        "CursorResult[Any]",
        await session.execute(
            update(AuditArchiveLease)
            .where(
                AuditArchiveLease.organization_id == organization_id,
                or_(AuditArchiveLease.expires_at <= moment, AuditArchiveLease.owner == owner),
            )
            .values(owner=owner, acquired_at=moment, expires_at=expires_at)
        ),
    )
    if taken.rowcount:
        return True

    held = await session.scalar(
        select(AuditArchiveLease).where(AuditArchiveLease.organization_id == organization_id)
    )
    if held is not None:
        return False

    session.add(
        AuditArchiveLease(
            organization_id=organization_id,
            owner=owner,
            acquired_at=moment,
            expires_at=expires_at,
        )
    )
    try:
        await session.flush()
    except IntegrityError:
        # Another replica inserted the same lease between our SELECT and
        # this INSERT. Losing that race is the expected outcome, not an
        # error; the primary key is what arbitrates, not the SELECT above.
        await session.rollback()
        return False
    return True


async def release_archive_lease(
    session: AsyncSession, organization_id: UUID, *, owner: str
) -> None:
    """Drop our claim. A lease held by someone else is left alone."""
    held = await session.scalar(
        select(AuditArchiveLease).where(AuditArchiveLease.organization_id == organization_id)
    )
    if held is not None and held.owner == owner:
        await session.delete(held)
        await session.flush()


# --- selection -------------------------------------------------------------


async def _cursor_for(session: AsyncSession, organization_id: UUID) -> tuple[datetime, int]:
    """Highest `(occurred_at, audit_event_id)` this organization has claimed."""
    row = (
        await session.execute(
            select(AuditArchiveMembership.occurred_at, AuditArchiveMembership.audit_event_id)
            .where(AuditArchiveMembership.organization_id == organization_id)
            .order_by(
                AuditArchiveMembership.occurred_at.desc(),
                AuditArchiveMembership.audit_event_id.desc(),
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return _EPOCH, 0
    occurred_at, event_id = row
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=UTC)
    return occurred_at, int(event_id)


async def select_unarchived_events(
    session: AsyncSession,
    organization_id: UUID,
    *,
    batch_size: int,
    overlap_seconds: int,
) -> list[AuditEvent]:
    """The next batch, by composite keyset cursor plus a late-arrival window.

    Two disjoint reasons an event is eligible, combined in one ordered scan:

    * it sits beyond the `(occurred_at, id)` cursor -- the ordinary case,
      and the composite comparison is what stops a run of identical
      timestamps from being cut in half by `LIMIT` and never revisited; or
    * it sits inside `overlap_seconds` behind the cursor and has no
      membership row -- a commit that became visible after the sweep had
      already passed its timestamp.

    Membership, not the cursor, is what excludes an event in both cases.
    The cursor only bounds how much of the ledger is scanned.
    """
    cursor_at, cursor_id = await _cursor_for(session, organization_id)
    overlap = timedelta(seconds=overlap_seconds)
    # `datetime.min` is the never-archived sentinel; subtracting from it
    # would overflow, and the floor is already the beginning of time there.
    overlap_floor = cursor_at - overlap if cursor_at >= _EPOCH + overlap else _EPOCH

    already_claimed = (
        select(AuditArchiveMembership.id)
        .where(
            AuditArchiveMembership.organization_id == organization_id,
            AuditArchiveMembership.audit_event_id == AuditEvent.id,
        )
        .exists()
    )

    beyond_cursor = or_(
        AuditEvent.occurred_at > cursor_at,
        and_(AuditEvent.occurred_at == cursor_at, AuditEvent.id > cursor_id),
    )

    rows = (
        await session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == organization_id,
                or_(beyond_cursor, AuditEvent.occurred_at >= overlap_floor),
                ~already_claimed,
            )
            .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
            .limit(batch_size)
        )
    ).all()
    return list(rows)


async def _events_of_record(session: AsyncSession, record: AuditArchiveRecord) -> list[AuditEvent]:
    """Reload exactly the events an existing record already claims."""
    rows = (
        await session.scalars(
            select(AuditEvent)
            .join(
                AuditArchiveMembership,
                AuditArchiveMembership.audit_event_id == AuditEvent.id,
            )
            .where(AuditArchiveMembership.archive_record_id == record.id)
            .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
        )
    ).all()
    return list(rows)


# --- lifecycle -------------------------------------------------------------


def _result_from(record: AuditArchiveRecord, *, retryable: bool = False) -> ArchiveResult:
    return ArchiveResult(
        archive_id=record.archive_id,
        state=record.state,
        archived_count=record.event_count if record.state == STATE_VERIFIED else 0,
        checksum=record.checksum,
        checksum_algorithm=record.checksum_algorithm,
        serialization_version=record.serialization_version,
        retention_until=record.retention_until,
        storage_backend=record.storage_backend,
        storage_uri=record.storage_uri,
        legal_hold=record.legal_hold,
        verified_at=record.verified_at,
        failure_reason=record.failure_reason,
        retryable=retryable,
    )


async def _fail(
    session: AsyncSession,
    record: AuditArchiveRecord,
    reason: str,
) -> ArchiveResult:
    """Record a terminal-for-now failure. Membership rows are kept.

    Keeping membership is what makes the retry idempotent: the next sweep
    finds this record, reassembles the identical batch, and re-attempts it
    rather than composing a different one whose checksum would not match the
    object a half-finished upload may already have written.

    A FAILED record therefore blocks the organization's sweep until it
    succeeds, and that is the intended behaviour: skipping past it to newer
    events would leave a permanent hole in the archive while the endpoint
    went on looking healthy. `attempt_count` is the signal to alert on.
    """
    record.state = STATE_FAILED
    record.failure_reason = reason[:1000]
    record.attempt_count = (record.attempt_count or 0) + 1
    await session.commit()
    logger.warning(
        "audit_archive_attempt_failed",
        archive_id=record.archive_id,
        organization_id=str(record.organization_id),
        attempt_count=record.attempt_count,
        reason=reason,
    )
    return _result_from(record, retryable=True)


async def _upload_and_verify(
    session: AsyncSession,
    record: AuditArchiveRecord,
    envelopes: list[AuditEventEnvelope],
    storage: ArchiveStorage,
    config: ArchiveConfig,
) -> ArchiveResult:
    """Drive PREPARED/UPLOADED/FAILED forward to VERIFIED, committing each step."""
    payload = serialize_archive(
        envelopes,
        archive_id=record.archive_id,
        organization_id=str(record.organization_id),
        checksum=record.checksum,
        retention_until=record.retention_until,
        classification=config.classification,
    )

    if record.state in (STATE_PREPARED, STATE_FAILED) or not record.storage_uri:
        obj = ArchiveObject(
            archive_id=record.archive_id,
            organization_id=str(record.organization_id),
            payload=payload,
            checksum=record.checksum,
            checksum_algorithm=record.checksum_algorithm,
            serialization_version=record.serialization_version,
            event_count=record.event_count,
            retention_until=record.retention_until,
            legal_hold=record.legal_hold,
            classification=config.classification,
        )
        try:
            stored = await asyncio.to_thread(storage.put, obj)
        except ArchiveStorageError as error:
            return await _fail(session, record, f"upload refused: {error}")
        except OSError as error:  # pragma: no cover - destination-specific
            return await _fail(session, record, f"upload failed: {error}")

        record.state = STATE_UPLOADED
        record.storage_uri = stored.uri
        record.storage_backend = stored.provider
        record.uploaded_at = stored.stored_at
        record.retention_acknowledged_at = stored.stored_at
        record.legal_hold = stored.legal_hold
        record.failure_reason = None
        await session.commit()

    uri = record.storage_uri
    if uri is None:  # pragma: no cover - defensive
        return await _fail(session, record, "upload reported success without a location")

    try:
        verification = await asyncio.to_thread(
            storage.verify, uri, checksum=record.checksum, algorithm=record.checksum_algorithm
        )
    except ArchiveStorageError as error:
        return await _fail(session, record, f"verification refused: {error}")

    if not verification.verified:
        return await _fail(session, record, f"verification failed: {verification.detail}")

    record.state = STATE_VERIFIED
    record.verified_at = datetime.now(UTC)
    record.failure_reason = None
    await session.commit()
    logger.info(
        "audit_events_archived",
        archive_id=record.archive_id,
        organization_id=str(record.organization_id),
        count=record.event_count,
        checksum=record.checksum,
        serialization_version=record.serialization_version,
        storage_backend=record.storage_backend,
        storage_uri=record.storage_uri,
        retention_until=record.retention_until.isoformat(),
        legal_hold=record.legal_hold,
    )
    return _result_from(record)


async def _prepare(
    session: AsyncSession,
    organization_id: UUID,
    rows: list[AuditEvent],
    config: ArchiveConfig,
) -> tuple[AuditArchiveRecord, list[AuditEventEnvelope]] | None:
    """Claim `rows` for a new (or resumed) archive record and commit the claim.

    Returns None when another worker claimed the same events first -- the
    membership unique constraint is the arbiter, and losing is a no-op.
    """
    envelopes = [envelope_from_audit_event(row) for row in rows]
    checksum = compute_batch_checksum(envelopes)
    now = datetime.now(UTC)
    archive_id = _generate_archive_id(checksum, now)

    existing = await session.scalar(
        select(AuditArchiveRecord).where(AuditArchiveRecord.archive_id == archive_id)
    )
    if existing is not None:
        # The id is derived from the checksum, so this means an archive of
        # exactly these events already exists. Resumable states are handled
        # before we get here, so it is terminal: nothing to do, and claiming
        # the rows again would double-archive them.
        logger.warning(
            "audit_archive_id_already_exists",
            organization_id=str(organization_id),
            archive_id=archive_id,
            state=existing.state,
        )
        return None

    record = AuditArchiveRecord(
        id=uuid4(),
        organization_id=organization_id,
        archive_id=archive_id,
        event_count=len(rows),
        event_range_start=rows[0].occurred_at,
        event_range_end=rows[-1].occurred_at,
        event_range_start_id=rows[0].id,
        event_range_end_id=rows[-1].id,
        checksum=checksum,
        checksum_algorithm=CHECKSUM_ALGORITHM,
        serialization_version=ENVELOPE_VERSION,
        storage_backend=config.storage_backend,
        retention_until=now + timedelta(days=config.retention_days),
        legal_hold=config.legal_hold_enabled,
        state=STATE_PREPARED,
        attempt_count=0,
        created_by="system:worm-archive-job",
    )
    session.add(record)
    for row in rows:
        session.add(
            AuditArchiveMembership(
                organization_id=organization_id,
                audit_event_id=row.id,
                archive_record_id=record.id,
                archive_id=archive_id,
                occurred_at=row.occurred_at,
            )
        )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.info(
            "audit_archive_claim_lost",
            organization_id=str(organization_id),
            archive_id=archive_id,
        )
        return None
    return record, envelopes


async def archive_pending_audit_events(
    session: AsyncSession,
    organization_id: UUID,
    config: ArchiveConfig,
    *,
    storage: ArchiveStorage | None = None,
    batch_size: int = 1000,
    owner: str | None = None,
) -> ArchiveResult | None:
    """Archive one batch for one organization, or resume an interrupted one.

    Returns None only when there is genuinely nothing to do: no resumable
    record, no unarchived events, or the lease is held elsewhere. Any other
    outcome is an `ArchiveResult` whose `state` says what actually happened;
    a FAILED result is retryable and reports `archived_count == 0`.

    Commits as it goes -- see the module docstring on why the caller cannot
    own the transaction here.
    """
    provider = storage if storage is not None else storage_for(config)
    holder = owner or default_worker_identity()

    if not await acquire_archive_lease(
        session, organization_id, owner=holder, lease_seconds=config.lease_seconds
    ):
        logger.debug("audit_archive_lease_held_elsewhere", organization_id=str(organization_id))
        return None
    await session.commit()

    try:
        resumable = await session.scalar(
            select(AuditArchiveRecord)
            .where(
                AuditArchiveRecord.organization_id == organization_id,
                AuditArchiveRecord.state.in_(RESUMABLE_STATES),
            )
            .order_by(AuditArchiveRecord.created_at.asc())
            .limit(1)
        )
        if resumable is not None:
            rows = await _events_of_record(session, resumable)
            if not rows:  # pragma: no cover - defensive
                return await _fail(session, resumable, "record has no membership rows")
            envelopes = [envelope_from_audit_event(row) for row in rows]
            return await _upload_and_verify(session, resumable, envelopes, provider, config)

        rows = await select_unarchived_events(
            session,
            organization_id,
            batch_size=batch_size,
            overlap_seconds=config.late_arrival_overlap_seconds,
        )
        if not rows:
            return None

        prepared = await _prepare(session, organization_id, rows, config)
        if prepared is None:
            return None
        record, envelopes = prepared
        return await _upload_and_verify(session, record, envelopes, provider, config)
    finally:
        try:
            await release_archive_lease(session, organization_id, owner=holder)
            await session.commit()
        except Exception:
            # A session left unusable by a failure above cannot release the
            # lease; expiry is the fallback that exists for exactly this.
            logger.warning(
                "audit_archive_lease_release_failed",
                organization_id=str(organization_id),
                exc_info=True,
            )


# --- verification and holds ------------------------------------------------


async def verify_archive_record(
    session: AsyncSession,
    record: AuditArchiveRecord,
    *,
    storage: ArchiveStorage | None = None,
) -> bool:
    """Re-derive the checksum from the ledger under the record's own version.

    The version stored on the row selects the algorithm, so a v1 record --
    written before the envelope covered anything but id, action and
    timestamp -- still verifies, and does not get judged against a hash it
    was never computed with. `coverage_note` is what tells a caller how
    little that v1 pass actually proves.
    """
    rows = await _events_of_record(session, record)
    if not rows:
        return False
    envelopes = [envelope_from_audit_event(row) for row in rows]
    if not verify_batch_checksum(envelopes, record.checksum, version=record.serialization_version):
        return False
    if storage is None or record.storage_uri is None:
        return True
    result = await asyncio.to_thread(
        storage.verify,
        record.storage_uri,
        checksum=record.checksum,
        algorithm=record.checksum_algorithm,
    )
    return result.verified


def validate_archive_integrity(
    events: list[AuditEventEnvelope],
    expected_checksum: str,
    *,
    version: int = ENVELOPE_VERSION,
) -> bool:
    """Verify a batch against a stored checksum under the stored version."""
    return verify_batch_checksum(events, expected_checksum, version=version)


async def apply_legal_hold(
    session: AsyncSession,
    archive_id: str,
    reason: str,
    *,
    storage: ArchiveStorage,
) -> dict[str, Any]:
    """Place a hold on the destination object *and* on the record.

    Executed against the provider first: a hold recorded only in the
    database would not stop the destination expiring the object, which is
    the one thing a hold is for.
    """
    record = await session.scalar(
        select(AuditArchiveRecord).where(AuditArchiveRecord.archive_id == archive_id)
    )
    if record is None:
        raise ArchiveStorageError(f"no archive record {archive_id!r}")
    if record.storage_uri is None:
        raise ArchiveStorageError(
            f"archive {archive_id!r} is in state {record.state!r} and has no stored object to hold"
        )
    at = datetime.now(UTC)
    manifest = await asyncio.to_thread(
        storage.apply_legal_hold, record.storage_uri, reason=reason, at=at
    )
    record.legal_hold = True
    record.legal_hold_reason = reason[:500]
    record.legal_hold_applied_at = at
    record.legal_hold_released_at = None
    await session.commit()
    return {
        "archive_id": archive_id,
        "legal_hold": True,
        "reason": reason,
        "applied_at": at.isoformat(),
        "storage_uri": record.storage_uri,
        "manifest": manifest,
    }


async def release_legal_hold(
    session: AsyncSession,
    archive_id: str,
    reason: str,
    *,
    storage: ArchiveStorage,
) -> dict[str, Any]:
    """Release the hold on the object and the record, restoring ordinary expiry."""
    record = await session.scalar(
        select(AuditArchiveRecord).where(AuditArchiveRecord.archive_id == archive_id)
    )
    if record is None:
        raise ArchiveStorageError(f"no archive record {archive_id!r}")
    if record.storage_uri is None:
        raise ArchiveStorageError(f"archive {archive_id!r} has no stored object to release")
    at = datetime.now(UTC)
    manifest = await asyncio.to_thread(
        storage.release_legal_hold, record.storage_uri, reason=reason, at=at
    )
    record.legal_hold = False
    record.legal_hold_reason = reason[:500]
    record.legal_hold_released_at = at
    await session.commit()
    return {
        "archive_id": archive_id,
        "legal_hold": False,
        "reason": reason,
        "released_at": at.isoformat(),
        "storage_uri": record.storage_uri,
        "manifest": manifest,
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


__all__ = [
    "STATE_FAILED",
    "STATE_LEGACY",
    "STATE_PREPARED",
    "STATE_UPLOADED",
    "STATE_VERIFIED",
    "ArchiveConfig",
    "ArchiveResult",
    "AuditEventEnvelope",
    "NullArchiveStorage",
    "acquire_archive_lease",
    "apply_legal_hold",
    "archive_pending_audit_events",
    "default_worker_identity",
    "release_archive_lease",
    "release_legal_hold",
    "retention_policy_for_classification",
    "select_unarchived_events",
    "serialize_archive",
    "storage_for",
    "validate_archive_integrity",
    "verify_archive_record",
]
