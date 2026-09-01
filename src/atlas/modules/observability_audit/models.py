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
* `OutboxEvent` -- the transactional outbox. "Dead letters" is not a
  separate table: a dead-lettered event is `status == "DEAD_LETTER"` on
  this same row, not a distinct record.
* `SloDefinition`, `SloMeasurement` -- SLO state. "Metrics" in the
  register is covered by these two plus OpenTelemetry-emitted metrics
  (`atlas.platform.telemetry`, not a database table at all), not a
  separate metrics table.
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
    Float,
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


class SloDefinition(Base, TimestampMixin):
    """Service-level objective definition, org-scoped."""

    __tablename__ = "slo_definition"
    __table_args__ = (
        UniqueConstraint("organization_id", "slo_key"),
        Index("ix_slo_definition_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slo_key: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    target: Mapped[float] = mapped_column(Float, nullable=False)
    window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)


class SloMeasurement(Base):
    """Point-in-time SLO measurement."""

    __tablename__ = "slo_measurement"
    __table_args__ = (
        Index("ix_slo_measurement_slo_time", "slo_id", "measured_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slo_id: Mapped[UUID] = mapped_column(
        ForeignKey("slo_definition.id", ondelete="CASCADE"), nullable=False, index=True
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)
    budget_remaining: Mapped[float] = mapped_column(Float, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class AuditArchiveRecord(Base, TimestampMixin):
    """Immutable record of an audit archive batch."""

    __tablename__ = "audit_archive_record"
    __table_args__ = (
        Index("ix_audit_archive_org_created", "organization_id", "created_at"),
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
