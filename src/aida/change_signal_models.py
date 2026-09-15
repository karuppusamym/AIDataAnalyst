"""R11-FP15: persistence for change signals -- one row per detected change to a source object.

Kept out of `aida.models` for the reason `envelope_models` and `procedure_lineage_models` are:
one reviewable file for one new table, registered on the same `Base` so Alembic and
`create_all` see it. The logic that writes these rows lives in `aida.change_signals`.

Value-free by construction: a signal names a subject by id and kind, says what kind of change
it was, and nothing else -- no name, no text, no digest of text.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from aida.db import Base


class MetadataChangeSignal(Base):
    """One change to one source object (or one published meaning), awaiting its consumers.

    `status` is the per-signal watermark: PENDING until the dependency-aware rebuild
    (R11-FP16) has acted on it, then PROCESSED with what it did in `outcome`. A signal is never
    rewritten into a different change; a later change is a later signal.
    """

    __tablename__ = "metadata_change_signal"
    __table_args__ = (
        CheckConstraint(
            "subject_kind IN ('TABLE', 'VIEW', 'ROUTINE', 'GRANT', 'ONTOLOGY')",
            name="subject_kind",
        ),
        CheckConstraint(
            "signal_type IN ('DEFINITION_CHANGED', 'STRUCTURE_CHANGED', 'DEPRECATED', "
            "'REACTIVATED', 'PERMISSION_CHANGED', 'MEANING_PUBLISHED')",
            name="signal_type",
        ),
        CheckConstraint(
            "change_class IS NULL OR change_class IN ('LITERAL_ONLY', 'STRUCTURAL', "
            "'GRANT_ADDED', 'GRANT_MODIFIED', 'GRANT_REVOKED', 'SIGNATURE_CHANGED')",
            name="change_class",
        ),
        CheckConstraint("status IN ('PENDING', 'PROCESSED')", name="status"),
        Index("ix_metadata_change_signal_pending", "organization_id", "status", "detected_at"),
        Index("ix_metadata_change_signal_subject", "subject_kind", "subject_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False
    )
    # NULL only for a meaning signal (a published ontology belongs to no one datasource).
    datasource_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("datasource.id", ondelete="CASCADE"), index=True
    )
    analysis_run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("analysis_run.id", ondelete="SET NULL"), index=True
    )
    subject_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[UUID] = mapped_column(nullable=False)
    signal_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # DEFINITION_CHANGED: whether anything but literals changed. PERMISSION_CHANGED: whether the
    # grant was added, modified or revoked. A retired routine: SIGNATURE_CHANGED when one new
    # signature replaced it.
    change_class: Mapped[str | None] = mapped_column(String(20))
    # The routine that replaced a SIGNATURE_CHANGED one. No foreign key: a subject id points into
    # whichever table its kind names.
    related_subject_id: Mapped[UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_by: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
