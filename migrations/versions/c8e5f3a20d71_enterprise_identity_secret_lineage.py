"""enterprise identity secret posture and query column lineage

Revision ID: c8e5f3a20d71
Revises: a7c4e2d91b60
Create Date: 2026-08-25 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e5f3a20d71"
down_revision: str | Sequence[str] | None = "a7c4e2d91b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "query_execution",
        sa.Column("referenced_columns", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "query_execution",
        sa.Column("column_lineage", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("query_execution", "column_lineage")
    op.drop_column("query_execution", "referenced_columns")
