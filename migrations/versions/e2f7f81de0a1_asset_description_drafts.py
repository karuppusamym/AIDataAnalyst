"""asset description drafts

Revision ID: e2f7f81de0a1
Revises: 96f7c81b0ad1
Create Date: 2026-08-30 23:35:17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f7f81de0a1"
down_revision: str | Sequence[str] | None = "96f7c81b0ad1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asset_description_draft",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("drafted_text", sa.Text(), nullable=False),
        sa.Column("text_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("accuracy_score", sa.Float(), nullable=False),
        sa.Column("clarity_score", sa.Float(), nullable=False),
        sa.Column("style_score", sa.Float(), nullable=False),
        sa.Column("completeness_score", sa.Float(), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("published_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["governance_review_id"],
            ["governance_review.id"],
            name=op.f("fk_asset_description_draft_governance_review_id_governance_review"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_asset_description_draft_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_version_id"],
            ["asset_documentation_version.id"],
            name=op.f(
                "fk_asset_description_draft_published_version_id_asset_documentation_version"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_asset_description_draft_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_description_draft")),
        sa.UniqueConstraint(
            "governance_review_id", name=op.f("uq_asset_description_draft_governance_review_id")
        ),
    )
    op.create_index(
        "ix_asset_description_draft_org_status",
        "asset_description_draft",
        ["organization_id", "status"],
    )
    op.create_index(
        op.f("ix_asset_description_draft_organization_id"),
        "asset_description_draft",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_asset_description_draft_published_version_id"),
        "asset_description_draft",
        ["published_version_id"],
    )
    op.create_index(
        op.f("ix_asset_description_draft_table_id"), "asset_description_draft", ["table_id"]
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_asset_description_draft_table_id"), table_name="asset_description_draft"
    )
    op.drop_index(
        op.f("ix_asset_description_draft_published_version_id"),
        table_name="asset_description_draft",
    )
    op.drop_index(
        op.f("ix_asset_description_draft_organization_id"), table_name="asset_description_draft"
    )
    op.drop_index("ix_asset_description_draft_org_status", table_name="asset_description_draft")
    op.drop_table("asset_description_draft")
