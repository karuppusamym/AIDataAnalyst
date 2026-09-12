"""observability audit -- PRIVATE. SQLAlchemy models in this module's own
schema (`audit`, per `Docs/10-architecture/04-module-decomposition.md`
Sec.6).

Not importable from outside this module once the `module-privacy`
contract (tracker ST-02) is enforced.

Status: real content (tracker ST-05, Phase 3 of
`Docs/40-engineering/06-refactor-plan.md`). Moved verbatim from
`aida.models`, which now re-exports these classes for backward
compatibility -- every existing `from aida.models import X` caller keeps
working unchanged. This is a Python-source-location move only: these
classes still declare no `schema=` in `__table_args__` and still live in
the single shared PostgreSQL schema. The actual database schema migration
(refactor plan Sec.5 steps 2.3/2.4) is explicitly deferred to a later,
separate pass.

This is the fifth and last of the five Phase 3 leaf modules
(`06-refactor-plan.md` Sec.6's ordering: identity, connectivity,
ingestion, catalog, observability). Unlike the other four,
`src/atlas/modules/observability_audit/` did not exist before this pass
-- `scripts/generate_module.py observability_audit` scaffolded it first.

Owned tables (per Sec.4's register: "audit ledger, outbox, dead letters,
metrics, SLO state, compliance packs"):

* `AuditEvent`, `AuditArchiveRecord` -- the audit ledger and its WORM
  archive batches.
* `AuditArchiveMembership`, `AuditArchiveLease` -- which events belong to
  which archive batch, and which replica currently owns an organization's
  archive sweep. Both exist because archive progress used to be *inferred*
  from a timestamp range with no ownership claim at all
  (`Docs/review-2026-09-05/REVIEW.md` F03); each class's own docstring
  explains the invariant it holds.
* `OutboxEvent` -- the transactional outbox. "Dead letters" is not a
  separate table: a dead-lettered event is `status == "DEAD_LETTER"` on
  this same row, not a distinct record.
* SLO state -- **retired 2026-09-12 (R11-D10)**. `SloDefinition` and
  `SloMeasurement` lived here, and nothing ever wrote a measurement.
  Retirement rather than a writer was the decision because there was no
  indicator to write: an SLO was bound to nothing measurable (`slo_key`
  was a free-text slug with no registry behind it), no SLI concept
  existed anywhere in `src/`, and the only real telemetry -- the
  Prometheus exposition on `/metrics` -- is scraped by nothing in this
  repository and by no Prometheus in any compose file or `infra/`
  manifest. So "Metrics" in the register is covered by the
  OpenTelemetry-emitted metrics (`atlas.platform.telemetry`, not a
  database table at all) alone. Reinstating SLOs means designing the
  indicator binding first; migration `f3a91c27b5de` drops the two tables
  and its downgrade recreates them.
* `CompliancePackRecord` -- WORM-archived compliance pack generated from
  runtime evidence (EE.4/OB-5).
* `AccessReviewReportRecord` -- WORM-archived self-service entitlement
  report (OB-7). Its own docstring in the old `aida.models` names
  `CompliancePackRecord` as "the reproducibility bar this module sets,"
  and both live under the same "audit ledger ... compliance packs"
  registered description: immutable, checksummed, generated evidence,
  the same shape as every other table in this file. The *DTO* built from
  it, `EntitlementReportRead`, stays in `atlas.modules.identity_tenancy.
  schemas` (moved there in this same refactor pass, before this module's
  ownership of the archival record was worked out) -- a public read
  shape composed by a hand-written mapper
  (`aida.access_review_api._to_read`) is allowed to live in a different
  module from the table it is mapped from; that is exactly the kind of
  cross-module DTO composition MD-3 describes, not a violation of it.

Explicitly NOT moved here: `NotificationRuleRecord`/`NotificationEventRecord`
("routing rule for quality incidents" -- module 11 data-quality's domain),
`FreshnessWatermarkConfig`/`FreshnessObservation` (also module 11, register's
"freshness contracts, SLAs"), and `ContractViolationRecord`/
`ContractSlaRecord`/`DataContractVersion` (data-product contracts, keyed off
`product_id`, not this module's audit ledger). All stay in `aida.models`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from atlas.platform.db import Base, TimestampMixin, utc_now


class OutboxEvent(Base):
    __tablename__ = "outbox_event"
    __table_args__ = (
        Index("ix_outbox_pending", "status", "occurred_at"),
        Index("ix_outbox_due", "status", "next_attempt_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), index=True
    )
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(String(150), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(String(1000))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditArchiveRecord(Base, TimestampMixin):
    """Immutable record of an audit archive batch.

    `state` is the whole point of this row and is not decoration: an archive
    is PREPARED (this row and its membership rows exist, nothing has been
    sent), UPLOADED (a destination acknowledged the bytes), VERIFIED (the
    bytes were read back and re-checksummed) or FAILED (terminal for this
    attempt, retried on the next sweep). Only VERIFIED means an archive
    exists. `LEGACY_UNVERIFIED` marks rows written before the lifecycle
    existed, when the code returned a success object without storing
    anything -- they are evidence of an attempt, not of an archive.

    `serialization_version` selects the checksum algorithm on read-back
    (`aida.audit_envelope`). Legacy rows are version 1, which covers only
    event id, action and timestamp; version 2 covers the whole envelope.
    Verification must never guess this.
    """

    __tablename__ = "audit_archive_record"
    __table_args__ = (
        Index("ix_audit_archive_org_created", "organization_id", "created_at"),
        Index("ix_audit_archive_org_state", "organization_id", "state"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    archive_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    event_range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(30), nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    legal_hold: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- two-phase lifecycle (review F01) ---------------------------------
    state: Mapped[str] = mapped_column(String(24), default="PREPARED", nullable=False)
    storage_uri: Mapped[str | None] = mapped_column(String(1000))
    serialization_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    checksum_algorithm: Mapped[str] = mapped_column(String(30), default="sha256", nullable=False)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(1000))
    legal_hold_reason: Mapped[str | None] = mapped_column(String(500))
    legal_hold_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legal_hold_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Composite `(occurred_at, id)` cursor bounds of the batch. The id half
    # is what stops equal-timestamp events from being split across a batch
    # boundary and never selected again (review F03).
    event_range_start_id: Mapped[int | None] = mapped_column(BigInteger)
    event_range_end_id: Mapped[int | None] = mapped_column(BigInteger)


class AuditArchiveMembership(Base):
    """The fact that one audit event belongs to one archive.

    Archive progress used to be *inferred* from a timestamp range, which is
    why equal-timestamp boundaries and late commits could drop events
    permanently (review F03). Membership replaces inference: an event is
    claimed by an archive if and only if a row exists here, and the batch
    query excludes claimed events by `NOT EXISTS` rather than by comparing
    against a high-water mark.

    A row is written when the archive is PREPARED, not when it is VERIFIED,
    so a crash between upload and verification resumes the *same* batch
    instead of assembling a different one -- which is what makes retry
    idempotent. Whether the claimed archive is durable is the archive row's
    `state`, not this table's business.

    The unique constraint on `(organization_id, audit_event_id)` is the
    backstop: two workers racing past the lease can still not double-archive
    an event, because the second insert fails.
    """

    __tablename__ = "audit_archive_membership"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "audit_event_id",
            name="uq_audit_archive_membership_event",
        ),
        Index("ix_audit_archive_membership_record", "archive_record_id"),
        Index("ix_audit_archive_membership_cursor", "organization_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False
    )
    audit_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    archive_record_id: Mapped[UUID] = mapped_column(
        ForeignKey("audit_archive_record.id", ondelete="RESTRICT"), nullable=False
    )
    archive_id: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AuditArchiveLease(Base):
    """Per-organization ownership claim over the archive sweep.

    Every API replica runs the same sweep loop. Without a claim they all
    select the same batch at the same moment; the membership unique
    constraint would catch the collision, but only after both had uploaded.
    One row per organization, held by `owner` until `expires_at`, is enough
    to make that the rare case rather than the normal one.

    A lease row rather than a Postgres advisory lock, deliberately: the
    advisory lock is tied to a session and vanishes on connection loss,
    which is right for a lock and wrong for a claim that must survive the
    seconds between an upload and its verification. It also has no SQLite
    equivalent, and the test suite builds its schema from this ORM against
    in-memory SQLite. Expiry is what breaks a lease held by a dead replica.
    """

    __tablename__ = "audit_archive_lease"

    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), primary_key=True
    )
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_org_occurred", "organization_id", "occurred_at"),
        Index("ix_audit_correlation", "correlation_id"),
    )

    # `.with_variant(Integer, "sqlite")`: SQLite only rowid-aliases a primary key
    # column declared literally `INTEGER PRIMARY KEY`, so a bare `BigInteger` compiles
    # to `BIGINT` there and SQLAlchemy stops treating the column as autoincrementing
    # (every insert then supplies a NULL `id` and SQLite's NOT NULL constraint fires).
    # PostgreSQL is unaffected -- the variant only changes what SQLite's DDL compiler
    # emits, not the production `BIGINT`/`BIGSERIAL` column.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), index=True
    )
    principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    action: Mapped[str] = mapped_column(String(150), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source_ip: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CompliancePackRecord(Base, TimestampMixin):
    """WORM-archived compliance pack generated from runtime evidence."""

    __tablename__ = "compliance_pack"
    __table_args__ = (
        Index("ix_compliance_pack_org_framework", "organization_id", "framework"),
        Index("ix_compliance_pack_org_created", "organization_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    framework: Mapped[str] = mapped_column(String(50), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sections: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="GENERATED", nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AccessReviewReportRecord(Base, TimestampMixin):
    """WORM-archived self-service entitlement report (OB-7).

    Snapshots what one principal (`subject_principal_id`) was entitled to see at
    `generated_at`, built from real persisted `WorkspaceMembership` and
    `SourceBinding` rows plus an ABAC policy overlay -- never authored by hand,
    matching the reproducibility bar `CompliancePackRecord` sets for this
    module. Append-only: nothing here is ever updated or deleted, which is what
    lets a bank's access-review process point at a specific report as the record
    of what was disclosed, to whom, and when.
    """

    __tablename__ = "access_review_report"
    __table_args__ = (
        Index(
            "ix_access_review_report_org_subject",
            "organization_id",
            "subject_principal_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    subject_principal_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_principal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # True when the subject generated their own report; False when an elevated
    # role (PlatformAdmin/DataAdmin/ComplianceOfficer) pulled it on their behalf --
    # always audited via `requested_by` either way.
    is_self_service: Mapped[bool] = mapped_column(Boolean, nullable=False)
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    entitlements: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class DeliveryIntent(Base, TimestampMixin):
    """One thing that must reach one destination, and what happened to it.

    **Invariant this table exists to hold:** nothing is reported as sent
    unless a destination acknowledged it, and nothing that was accepted for
    sending can be forgotten because a destination was down.

    Two defects share this shape (`Docs/review-2026-09-05/REVIEW.md`).
    F04: SIEM routing formatted a message, logged it and returned ``True``
    without opening a socket. F12: a governance notification whose webhook
    failed was still stamped as processed, and the sweep that would have
    retried it selects only unstamped rows. Both were the same mistake --
    treating "we tried" as "it arrived" -- so both are fixed by the same
    table rather than by two parallel ledgers.

    ``kind`` discriminates: ``SIEM_SECURITY_EVENT`` rows come from
    ``aida.siem_routing``, ``GOVERNANCE_NOTIFICATION`` rows from
    ``aida.governance_notifications``. One worker
    (``aida.delivery_intents.run_delivery_worker_pass``) drains both.

    **Three timestamps, because one cannot mean three things.**
    ``requested_at`` is when the business transaction created the intent --
    the only thing a watermark may be tied to, since it is the only one that
    commits atomically with the decision it describes. ``attempted_at`` is
    the most recent transport attempt. ``delivered_at`` is set if and only if
    a destination acknowledged, and is the sole basis for claiming delivery.

    **State machine** (``state``), terminal states marked *:

        PENDING     created; no transport has been touched
        DELIVERING  claimed by one worker for the current attempt
        RETRYING    attempt failed retryably; next_attempt_at holds the backoff
        DELIVERED*  the destination acknowledged
        DEAD_LETTER* permanent failure, or the retry budget is exhausted
        DISCARDED*  never sendable as configured (channel disabled, or no
                    destination) -- recorded, never sent, never retried
        DUPLICATE*  an equivalent intent for the same destination was already
                    delivered; suppressed rather than sent twice

    **Deduplication is enforced at delivery, not by a unique constraint**, and
    that is deliberate. ``dedup_key`` is indexed but not unique: a uniqueness
    violation here would abort the *business* transaction that staged the
    intent, which would make a chat integration able to fail a governance
    decision -- exactly the coupling this whole change exists to remove. The
    worker instead refuses to send when an equivalent intent is already
    DELIVERED, or is DELIVERING with an earlier ``requested_at``; the earlier
    row wins deterministically, so two workers racing cannot both send.

    ``organization_id`` is nullable because a rejected bearer token produces a
    SOC-notable AUTH_FAILURE before any organization is known, and refusing to
    record that event would be a worse answer than a null column.
    """

    __tablename__ = "delivery_intent"
    __table_args__ = (
        Index("ix_delivery_intent_due", "state", "next_attempt_at"),
        Index("ix_delivery_intent_dedup", "organization_id", "kind", "channel", "dedup_key"),
        Index("ix_delivery_intent_org_state", "organization_id", "state"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    #: Human-readable destination label. Never a credentialed URL: see
    #: `aida.delivery_intents.destination_label`.
    destination: Mapped[str] = mapped_column(String(500), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(80), nullable=False)
    #: Already minimised at enqueue time. When `siem_include_details` is off,
    #: details are absent from this dict, so every transport rendered from it
    #: suppresses them -- there is no second place to forget.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(100))

    state: Mapped[str] = mapped_column(String(24), default="PENDING", nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_outcome: Mapped[str | None] = mapped_column(String(30))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    claimed_by: Mapped[str | None] = mapped_column(String(200))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeliveryAttempt(Base):
    """One transport attempt against one destination. Append-only.

    The intent row carries current state; this table carries the history that
    makes an outage legible after the fact -- how many times, how far apart,
    with what error, and whether the destination ever answered. Written in the
    same transaction that advances the intent, so "attempts are durable across
    a restart" is a property of the commit, not of a retry counter in memory.
    """

    __tablename__ = "delivery_attempt"
    __table_args__ = (Index("ix_delivery_attempt_intent", "intent_id", "attempt_number"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(
        ForeignKey("delivery_intent.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT")
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    transport: Mapped[str] = mapped_column(String(30), nullable=False)
    destination: Mapped[str] = mapped_column(String(500), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(String(1000))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
