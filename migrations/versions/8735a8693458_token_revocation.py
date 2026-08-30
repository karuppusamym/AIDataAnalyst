"""token revocation (ID-4)

Revision ID: 8735a8693458
Revises: 93c66b5d0837
Create Date: 2026-08-30 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8735a8693458"
down_revision: str | Sequence[str] | None = "93c66b5d0837"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "revoked_token",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("token_identifier", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_by", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_revoked_token_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_revoked_token")),
        sa.UniqueConstraint(
            "token_identifier", name=op.f("uq_revoked_token_token_identifier")
        ),
    )
    op.create_index(
        op.f("ix_revoked_token_organization_id"),
        "revoked_token",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_revoked_token_expires_at",
        "revoked_token",
        ["token_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_revoked_token_expires_at", table_name="revoked_token")
    op.drop_index(op.f("ix_revoked_token_organization_id"), table_name="revoked_token")
    op.drop_table("revoked_token")
