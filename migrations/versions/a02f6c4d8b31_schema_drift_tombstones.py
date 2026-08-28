"""schema drift tombstones

Revision ID: a02f6c4d8b31
Revises: 3df18be7a420
Create Date: 2026-08-25 05:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a02f6c4d8b31"
down_revision: str | Sequence[str] | None = "3df18be7a420"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_run", sa.Column("resumed_from_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_analysis_run_resumed_from_run_id_analysis_run"),
        "analysis_run",
        "analysis_run",
        ["resumed_from_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_analysis_run_resumed_from_run_id"),
        "analysis_run",
        ["resumed_from_run_id"],
        unique=False,
    )
    for column_name in ("created_objects", "changed_objects", "deprecated_objects"):
        op.add_column(
            "analysis_run",
            sa.Column(column_name, sa.Integer(), server_default="0", nullable=False),
        )
        op.alter_column("analysis_run", column_name, server_default=None)

    op.add_column(
        "metadata_column",
        sa.Column("status", sa.String(length=30), server_default="ACTIVE", nullable=False),
    )
    op.alter_column("metadata_column", "status", server_default=None)

    for table_name in (
        "metadata_catalog",
        "metadata_schema",
        "metadata_table",
        "metadata_column",
        "metadata_constraint",
    ):
        op.add_column(
            table_name,
            sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for table_name in (
        "metadata_constraint",
        "metadata_column",
        "metadata_table",
        "metadata_schema",
        "metadata_catalog",
    ):
        op.drop_column(table_name, "deprecated_at")
    op.drop_column("metadata_column", "status")
    for column_name in ("deprecated_objects", "changed_objects", "created_objects"):
        op.drop_column("analysis_run", column_name)
    op.drop_index(op.f("ix_analysis_run_resumed_from_run_id"), table_name="analysis_run")
    op.drop_constraint(
        op.f("fk_analysis_run_resumed_from_run_id_analysis_run"),
        "analysis_run",
        type_="foreignkey",
    )
    op.drop_column("analysis_run", "resumed_from_run_id")
