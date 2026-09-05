"""Workbook re-import: batched, reviewed edits to table and column descriptions.

The write half of the download/edit/re-upload round trip whose read half
landed in `b4e1c7a90d33`. An upload parses and diffs; only an APPROVE decision
on the batch's single `GovernanceReview` publishes anything.

1. `model_import_batch` -- one uploaded workbook, one review.
2. `model_import_change` -- one field on one object the workbook would change,
   carrying `expected_version` (the version the editor was looking at) so a
   change that someone else has since superseded is skipped rather than
   silently overwriting them.

One review per batch rather than per change, unlike `document_claim`: a
workbook's changes share one provenance and are reviewed as one edit.

Revision ID: c7f2a4b81e50
Revises: b4e1c7a90d33
Create Date: 2026-09-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7f2a4b81e50"
down_revision: str | Sequence[str] | None = "b4e1c7a90d33"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_import_batch",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("change_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("applied_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uploaded_by", sa.String(255), nullable=False),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["governance_review_id"], ["governance_review.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("governance_review_id"),
    )
    op.create_index(
        "ix_model_import_batch_organization_id", "model_import_batch", ["organization_id"]
    )
    op.create_index("ix_model_import_batch_datasource_id", "model_import_batch", ["datasource_id"])
    op.create_index(
        "ix_model_import_batch_org_status", "model_import_batch", ["organization_id", "status"]
    )

    op.create_table(
        "model_import_change",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("sheet_name", sa.String(64), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("subject_type", sa.String(10), nullable=False),
        sa.Column("subject_id", sa.String(100), nullable=False),
        sa.Column("subject_label", sa.String(600), nullable=False),
        sa.Column("field", sa.String(50), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("skip_reason", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["batch_id"], ["model_import_batch.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "subject_type IN ('TABLE', 'COLUMN')", name="import_subject_type_is_supported"
        ),
    )
    op.create_index(
        "ix_model_import_change_organization_id", "model_import_change", ["organization_id"]
    )
    op.create_index("ix_model_import_change_batch_id", "model_import_change", ["batch_id"])
    op.create_index("ix_model_import_change_subject_id", "model_import_change", ["subject_id"])
    op.create_index(
        "ix_model_import_change_batch_status", "model_import_change", ["batch_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_model_import_change_batch_status", table_name="model_import_change")
    op.drop_index("ix_model_import_change_subject_id", table_name="model_import_change")
    op.drop_index("ix_model_import_change_batch_id", table_name="model_import_change")
    op.drop_index("ix_model_import_change_organization_id", table_name="model_import_change")
    op.drop_table("model_import_change")
    op.drop_index("ix_model_import_batch_org_status", table_name="model_import_batch")
    op.drop_index("ix_model_import_batch_datasource_id", table_name="model_import_batch")
    op.drop_index("ix_model_import_batch_organization_id", table_name="model_import_batch")
    op.drop_table("model_import_batch")
