"""R11-FP15: metadata change signals

Revision ID: 9c3f5a1e7d42
Revises: 7b4e2d9a6c13
Create Date: 2026-09-15

`metadata_change_signal` holds one row per detected change to a source object -- a redefined
view or routine (classed LITERAL_ONLY or STRUCTURAL), a reshaped, retired or returning table,
view or routine, a changed or revoked grant -- and one per published ontology version
(`aida.change_signals`). Value-free: ids, kinds and classes only. `status` is the per-signal
watermark the dependency-aware rebuild (R11-FP16) advances. Creates exactly what
`aida.change_signal_models` declares; `downgrade` drops the table and every signal in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c3f5a1e7d42"
down_revision: str | Sequence[str] | None = "7b4e2d9a6c13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "metadata_change_signal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=True),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=True),
        sa.Column("subject_kind", sa.String(length=20), nullable=False),
        sa.Column("subject_id", sa.Uuid(), nullable=False),
        sa.Column("signal_type", sa.String(length=30), nullable=False),
        sa.Column("change_class", sa.String(length=20), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_by", sa.String(length=255), nullable=True),
        sa.Column("outcome", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "subject_kind IN ('TABLE', 'VIEW', 'ROUTINE', 'GRANT', 'ONTOLOGY')",
            name=op.f("ck_metadata_change_signal_subject_kind"),
        ),
        sa.CheckConstraint(
            "signal_type IN ('DEFINITION_CHANGED', 'STRUCTURE_CHANGED', 'DEPRECATED', "
            "'REACTIVATED', 'PERMISSION_CHANGED', 'MEANING_PUBLISHED')",
            name=op.f("ck_metadata_change_signal_signal_type"),
        ),
        sa.CheckConstraint(
            "change_class IS NULL OR change_class IN ('LITERAL_ONLY', 'STRUCTURAL')",
            name=op.f("ck_metadata_change_signal_change_class"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'PROCESSED')", name=op.f("ck_metadata_change_signal_status")
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_run.id"],
            name=op.f("fk_metadata_change_signal_analysis_run_id_analysis_run"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_change_signal_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_change_signal_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_change_signal")),
    )
    op.create_index(
        "ix_metadata_change_signal_pending",
        "metadata_change_signal",
        ["organization_id", "status", "detected_at"],
        unique=False,
    )
    op.create_index(
        "ix_metadata_change_signal_subject",
        "metadata_change_signal",
        ["subject_kind", "subject_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_change_signal_datasource_id"),
        "metadata_change_signal",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_change_signal_analysis_run_id"),
        "metadata_change_signal",
        ["analysis_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_metadata_change_signal_analysis_run_id"), table_name="metadata_change_signal"
    )
    op.drop_index(
        op.f("ix_metadata_change_signal_datasource_id"), table_name="metadata_change_signal"
    )
    op.drop_index("ix_metadata_change_signal_subject", table_name="metadata_change_signal")
    op.drop_index("ix_metadata_change_signal_pending", table_name="metadata_change_signal")
    op.drop_table("metadata_change_signal")
