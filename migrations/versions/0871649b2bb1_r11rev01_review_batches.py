"""R11-REV01: frozen review batches and their members

Revision ID: 0871649b2bb1
Revises: cd1d15b6705b
Create Date: 2026-09-19

Two tables, declared in `aida.review_batch_models`. A review batch binds a reviewer's selection
of governance reviews to the evidence version each was inspected at; its members record, once
the batch is decided, what happened to each one and why.

**Value-free (INV-6).** Ids, codes, counts and SHA-256 fingerprints only -- no evidence text,
no rationale (that stays on `governance_review.decision_reason`), no exception message.

**Tenancy (INV-5).** Both tables carry `organization_id` with RESTRICT. A member's `review_id`
has no foreign key on purpose: a selected id that does not exist in the organization is kept as
an EXCLUDED/NOT_FOUND member so the preview can say so.

No backfill: both tables start empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0871649b2bb1"
down_revision: str | Sequence[str] | None = "cd1d15b6705b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = "review_batch"
_ITEM = "review_batch_item"


def upgrade() -> None:
    op.create_table(
        _BATCH,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_by_type", sa.String(length=30), nullable=False),
        sa.Column("selection_mode", sa.String(length=20), nullable=False),
        sa.Column("selection_truncated", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("selection_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_counts", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('FROZEN', 'DECIDED')", name=op.f(f"ck_{_BATCH}_status")),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('APPROVE', 'REJECT')",
            name=op.f(f"ck_{_BATCH}_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f(f"fk_{_BATCH}_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_BATCH}")),
    )
    op.create_index(
        op.f(f"ix_{_BATCH}_organization_id"), _BATCH, ["organization_id"], unique=False
    )
    op.create_index(
        "ix_review_batch_org_created_by",
        _BATCH,
        ["organization_id", "created_by"],
        unique=False,
    )

    op.create_table(
        _ITEM,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("object_type", sa.String(length=100), nullable=True),
        sa.Column("review_family", sa.String(length=30), nullable=True),
        sa.Column("frozen_status", sa.String(length=30), nullable=True),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("eligibility", sa.String(length=20), nullable=False),
        sa.Column("exclusion_code", sa.String(length=50), nullable=True),
        sa.Column("approve_gate_code", sa.String(length=50), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correction_subject_type", sa.String(length=30), nullable=True),
        sa.Column("correction_subject_id", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "eligibility IN ('ELIGIBLE', 'EXCLUDED')", name=op.f(f"ck_{_ITEM}_eligibility")
        ),
        sa.CheckConstraint(
            "outcome IN ('PENDING', 'APPLIED', 'REFUSED', 'SKIPPED')",
            name=op.f(f"ck_{_ITEM}_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            [f"{_BATCH}.id"],
            name=op.f(f"fk_{_ITEM}_batch_id_{_BATCH}"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f(f"fk_{_ITEM}_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_ITEM}")),
        sa.UniqueConstraint("batch_id", "review_id", name="uq_review_batch_item_member"),
        sa.UniqueConstraint("batch_id", "position", name="uq_review_batch_item_position"),
    )
    op.create_index(op.f(f"ix_{_ITEM}_batch_id"), _ITEM, ["batch_id"], unique=False)
    op.create_index(
        op.f(f"ix_{_ITEM}_organization_id"), _ITEM, ["organization_id"], unique=False
    )
    op.create_index(op.f(f"ix_{_ITEM}_review_id"), _ITEM, ["review_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{_ITEM}_review_id"), table_name=_ITEM)
    op.drop_index(op.f(f"ix_{_ITEM}_organization_id"), table_name=_ITEM)
    op.drop_index(op.f(f"ix_{_ITEM}_batch_id"), table_name=_ITEM)
    op.drop_table(_ITEM)
    op.drop_index("ix_review_batch_org_created_by", table_name=_BATCH)
    op.drop_index(op.f(f"ix_{_BATCH}_organization_id"), table_name=_BATCH)
    op.drop_table(_BATCH)
