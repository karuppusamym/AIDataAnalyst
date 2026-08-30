"""asset certification column scope (CT-5)

Revision ID: 21a56d48976e
Revises: b3f7a1c94d62
Create Date: 2026-08-30 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "21a56d48976e"
down_revision: str | Sequence[str] | None = "f3a8c62d9e17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "asset_certification",
        sa.Column("asset_type", sa.String(length=20), nullable=False, server_default="TABLE"),
    )
    op.alter_column("asset_certification", "asset_type", server_default=None)
    op.add_column(
        "asset_certification",
        sa.Column("column_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_asset_certification_column_id_metadata_column"),
        "asset_certification",
        "metadata_column",
        ["column_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_asset_certification_column_id"),
        "asset_certification",
        ["column_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_asset_certification_asset_type",
        "asset_certification",
        "asset_type IN ('TABLE', 'COLUMN')",
    )
    op.create_check_constraint(
        "ck_asset_certification_column_consistency",
        "asset_certification",
        "(asset_type = 'TABLE' AND column_id IS NULL) OR "
        "(asset_type = 'COLUMN' AND column_id IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_asset_certification_column_consistency", "asset_certification", type_="check"
    )
    op.drop_constraint("ck_asset_certification_asset_type", "asset_certification", type_="check")
    op.drop_index(op.f("ix_asset_certification_column_id"), table_name="asset_certification")
    op.drop_constraint(
        op.f("fk_asset_certification_column_id_metadata_column"),
        "asset_certification",
        type_="foreignkey",
    )
    op.drop_column("asset_certification", "column_id")
    op.drop_column("asset_certification", "asset_type")
