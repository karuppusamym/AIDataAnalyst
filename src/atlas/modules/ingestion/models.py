"""ingestion -- PRIVATE. SQLAlchemy models in this module's own schema
(`ingestion`, per `Docs/10-architecture/04-module-decomposition.md` Sec.6).

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

Owned tables (per Sec.4's register: "ingestion jobs, envelopes, batch
manifests, chunks"):

* `MetadataIngestionJob` -- idempotent evidence for one canonical metadata
  push or stream delivery.
* `MetadataIngestionBatch` -- durable manifest for a resumable, chunked
  metadata snapshot.
* `MetadataIngestionChunk` -- one checksum-addressed chunk of a batch; the
  validated payload is erased after successful processing (Sec.6's
  "payload columns nulled after successful processing").

Explicitly NOT moved here despite the "metadata ingestion envelope"
naming in the same neighborhood: `aida.envelope_models`'s
`MetadataViewDefinition`/`MetadataRoutine`/`MetadataRoutineParameter`/
`MetadataObjectDescription`/`MetadataSourceGrant` are the *persisted
catalog* records an envelope ingests into (views, routines, grants as
first-class catalog objects) -- module 04 (catalog)'s domain, not this
module's pipeline/job state. They stay in `aida.envelope_models` pending
catalog's own extraction pass.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from atlas.platform.db import Base, TimestampMixin


class MetadataIngestionJob(Base, TimestampMixin):
    """Idempotent evidence for a canonical metadata push or stream delivery."""

    __tablename__ = "metadata_ingestion_job"
    __table_args__ = (
        UniqueConstraint("datasource_id", "idempotency_key"),
        Index("ix_metadata_ingestion_org_status", "organization_id", "status"),
        Index("ix_metadata_ingestion_source_created", "datasource_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="SET NULL"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    envelope_version: Mapped[str] = mapped_column(String(20), nullable=False)
    producer: Mapped[str] = mapped_column(String(200), nullable=False)
    transport: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(20), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    object_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    change_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MetadataIngestionBatch(Base, TimestampMixin):
    """Durable manifest for a resumable, chunked metadata snapshot."""

    __tablename__ = "metadata_ingestion_batch"
    __table_args__ = (
        UniqueConstraint("datasource_id", "batch_key"),
        Index("ix_ingestion_batch_source_created", "datasource_id", "created_at"),
        Index("ix_ingestion_batch_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    analysis_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="SET NULL"), index=True
    )
    batch_key: Mapped[str] = mapped_column(String(200), nullable=False)
    envelope_version: Mapped[str] = mapped_column(String(20), nullable=False)
    producer: Mapped[str] = mapped_column(String(200), nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(20), nullable=False)
    expected_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    received_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    object_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    change_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_class: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))


class MetadataIngestionChunk(Base, TimestampMixin):
    """Checksum-addressed chunk; the validated payload is erased after successful processing."""

    __tablename__ = "metadata_ingestion_chunk"
    __table_args__ = (
        UniqueConstraint("batch_id", "chunk_number", name="uq_ingestion_chunk_batch_number"),
        UniqueConstraint("batch_id", "chunk_key", name="uq_ingestion_chunk_batch_key"),
        Index("ix_ingestion_chunk_batch_status", "batch_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    datasource_id: Mapped[UUID] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), nullable=False, index=True
    )
    batch_id: Mapped[UUID] = mapped_column(
        ForeignKey("metadata_ingestion_batch.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_number: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_key: Mapped[str] = mapped_column(String(200), nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    object_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    change_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="RECEIVED", nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
