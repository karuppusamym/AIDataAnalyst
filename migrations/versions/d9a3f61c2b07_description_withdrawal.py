"""Retiring an approved description, through review.

Publishing a description was governed from the first commit; un-publishing one
was not possible at all. This adds the request object; the version row itself
needs no schema change, because `status` is already a free String and gains a
`WITHDRAWN` value alongside `APPROVED`/`SUPERSEDED`.

`WITHDRAWN` is deliberately distinct from `SUPERSEDED`: the latter means a newer
approved version replaced this one, and an audit has to be able to tell a
replacement from a retraction.

No backfill: nothing has been withdrawn before this migration existed.

Revision ID: d9a3f61c2b07
Revises: c7f2a4b81e50
Create Date: 2026-09-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9a3f61c2b07"
down_revision: str | Sequence[str] | None = "c7f2a4b81e50"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "description_withdrawal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("subject_type", sa.String(10), nullable=False),
        sa.Column("subject_id", sa.String(100), nullable=False),
        sa.Column("subject_label", sa.String(600), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("withdrawn_text", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("reviewed_by", sa.String(255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["governance_review_id"], ["governance_review.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("governance_review_id"),
        sa.CheckConstraint(
            "subject_type IN ('TABLE', 'COLUMN')", name="withdrawal_subject_type_is_supported"
        ),
    )
    op.create_index(
        "ix_description_withdrawal_organization_id", "description_withdrawal", ["organization_id"]
    )
    op.create_index(
        "ix_description_withdrawal_subject_id", "description_withdrawal", ["subject_id"]
    )
    op.create_index(
        "ix_description_withdrawal_org_status",
        "description_withdrawal",
        ["organization_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_description_withdrawal_org_status", table_name="description_withdrawal")
    op.drop_index("ix_description_withdrawal_subject_id", table_name="description_withdrawal")
    op.drop_index(
        "ix_description_withdrawal_organization_id", table_name="description_withdrawal"
    )
    op.drop_table("description_withdrawal")
