"""composite key inference, classification feed evidence, task-level tracking

Module 05 (profiling-and-classification) open work PR-1/PR-3/PR-4:

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
- ``key_inference_candidate`` -- proposed single-column or composite (2-4
  column) keys, evidence-backed and review-gated exactly like
  ``relationship_candidate`` (module 05 sec 13, PR-1).

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

    op.create_table(
        "key_inference_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("table_profile_id", sa.Uuid(), nullable=True),
        sa.Column("column_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("column_names", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("column_count", sa.Integer(), nullable=False),
        sa.Column("key_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("detection_rule", sa.String(length=100), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("estimated_distinctness_ratio", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING"),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("review_reason", sa.String(length=2000), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_key_inference_candidate_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_key_inference_candidate_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_key_inference_candidate_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["table_profile_id"],
            ["table_profile.id"],
            name=op.f("fk_key_inference_candidate_table_profile_id_table_profile"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_key_inference_candidate")),
        sa.UniqueConstraint(
            "table_id",
            "key_fingerprint",
            name="uq_key_inference_candidate_table_fingerprint",
        ),
    )
    op.create_index(
        op.f("ix_key_inference_candidate_organization_id"),
        "key_inference_candidate",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_key_inference_candidate_datasource_id"),
        "key_inference_candidate",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_key_inference_candidate_table_id"),
        "key_inference_candidate",
        ["table_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_key_inference_candidate_table_profile_id"),
        "key_inference_candidate",
        ["table_profile_id"],
        unique=False,
    )
    op.create_index(
        "ix_key_inference_candidate_org_status",
        "key_inference_candidate",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_key_inference_candidate_org_status", table_name="key_inference_candidate"
    )
    op.drop_index(
        op.f("ix_key_inference_candidate_table_profile_id"),
        table_name="key_inference_candidate",
    )
    op.drop_index(
        op.f("ix_key_inference_candidate_table_id"), table_name="key_inference_candidate"
    )
    op.drop_index(
        op.f("ix_key_inference_candidate_datasource_id"),
        table_name="key_inference_candidate",
    )
    op.drop_index(
        op.f("ix_key_inference_candidate_organization_id"),
        table_name="key_inference_candidate",
    )
    op.drop_table("key_inference_candidate")

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
