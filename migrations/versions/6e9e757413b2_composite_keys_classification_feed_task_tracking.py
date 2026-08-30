"""classification feed evidence, task-level tracking

Module 05 (profiling-and-classification) open work PR-3/PR-4 (PR-1 composite
key inference is a separate, already-merged migration -- see
``6500275e1d36_composite_key_candidate``):

- ``classification_evidence`` -- append-only provenance ledger for every
  column classification decision (rule-based or externally authoritative),
  with ``is_current`` marking the row that matches
  ``metadata_column.classification`` right now.
- ``metadata_column.classification_source`` -- "RULE" or
  "EXTERNAL_AUTHORITATIVE"; backfilled to "RULE" for every existing row since
  every classification made before this migration was rule-based.
- ``analysis_task`` -- the operator-facing mirror of Temporal's per-activity
  attempt/heartbeat/retry state (module 05 sec 6/10, PR-4), read by
  ``GET /v1/analysis-runs/{id}/tasks``.

Revision ID: 6e9e757413b2
Revises: f371492245ae
Create Date: 2026-08-30 18:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6e9e757413b2"
down_revision: str | Sequence[str] | None = "f371492245ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "metadata_column",
        sa.Column(
            "classification_source",
            sa.String(length=30),
            nullable=False,
            server_default="RULE",
        ),
    )

    op.create_table(
        "classification_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=False),
        sa.Column("classification", sa.String(length=30), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("rule_id", sa.String(length=100), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("matched_signal", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_classification_evidence_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["column_id"],
            ["metadata_column.id"],
            name=op.f("fk_classification_evidence_column_id_metadata_column"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_classification_evidence")),
    )
    op.create_index(
        op.f("ix_classification_evidence_organization_id"),
        "classification_evidence",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_classification_evidence_column_id"),
        "classification_evidence",
        ["column_id"],
        unique=False,
    )
    op.create_index(
        "ix_classification_evidence_column_current",
        "classification_evidence",
        ["column_id", "is_current"],
        unique=False,
    )

    op.create_table(
        "analysis_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=True),
        sa.Column("task_type", sa.String(length=50), nullable=False),
        sa.Column("task_key", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_detail", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error_class", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_history", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_analysis_task_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_run.id"],
            name=op.f("fk_analysis_task_analysis_run_id_analysis_run"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_analysis_task_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analysis_task")),
        sa.UniqueConstraint(
            "analysis_run_id", "task_key", name="uq_analysis_task_run_key"
        ),
    )
    op.create_index(
        op.f("ix_analysis_task_organization_id"),
        "analysis_task",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_analysis_task_analysis_run_id"),
        "analysis_task",
        ["analysis_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_analysis_task_table_id"), "analysis_task", ["table_id"], unique=False
    )
    op.create_index(
        "ix_analysis_task_run_status",
        "analysis_task",
        ["analysis_run_id", "status"],
        unique=False,
    )

def downgrade() -> None:
    op.drop_index("ix_analysis_task_run_status", table_name="analysis_task")
    op.drop_index(op.f("ix_analysis_task_table_id"), table_name="analysis_task")
    op.drop_index(op.f("ix_analysis_task_analysis_run_id"), table_name="analysis_task")
    op.drop_index(op.f("ix_analysis_task_organization_id"), table_name="analysis_task")
    op.drop_table("analysis_task")

    op.drop_index(
        "ix_classification_evidence_column_current", table_name="classification_evidence"
    )
    op.drop_index(
        op.f("ix_classification_evidence_column_id"), table_name="classification_evidence"
    )
    op.drop_index(
        op.f("ix_classification_evidence_organization_id"),
        table_name="classification_evidence",
    )
    op.drop_table("classification_evidence")

    op.drop_column("metadata_column", "classification_source")
