"""context product owner type

Revision ID: b3e7a5c19d02
Revises: f3a8c62d9e17
Create Date: 2026-08-30 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3e7a5c19d02"
down_revision: str | Sequence[str] | None = "f3a8c62d9e17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "context_product_version",
        sa.Column(
            "owner_type",
            sa.String(length=20),
            nullable=False,
            server_default="INDIVIDUAL",
        ),
    )
    op.alter_column("context_product_version", "owner_type", server_default=None)
    op.create_check_constraint(
        "ck_context_product_version_owner_type",
        "context_product_version",
        "owner_type IN ('INDIVIDUAL', 'GROUP')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_context_product_version_owner_type",
        "context_product_version",
        type_="check",
    )
    op.drop_column("context_product_version", "owner_type")
