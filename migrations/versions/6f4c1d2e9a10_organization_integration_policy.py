"""organization integration policy

Revision ID: 6f4c1d2e9a10
Revises: e4b7c2a91d35
Create Date: 2026-08-28 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6f4c1d2e9a10"
down_revision: str | Sequence[str] | None = "e4b7c2a91d35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_integration_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("transformation_metadata_integrations", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_organization_integration_policy_organization_id_organization"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_integration_policy")),
        sa.UniqueConstraint(
            "organization_id",
            name=op.f("uq_organization_integration_policy_organization_id"),
        ),
    )
    op.create_index(
        op.f("ix_organization_integration_policy_organization_id"),
        "organization_integration_policy",
        ["organization_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_organization_integration_policy_organization_id"),
        table_name="organization_integration_policy",
    )
    op.drop_table("organization_integration_policy")
