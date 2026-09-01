"""context product support window (AT-7a / AT-D1)

Revision ID: c1a4d7e9f062
Revises: 09be3ab5b008
Create Date: 2026-09-01 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1a4d7e9f062"
down_revision: str | Sequence[str] | None = "09be3ab5b008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "context_product_version",
        sa.Column("support_window_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "context_product_version",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "context_product_version",
        sa.Column("support_window_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "context_product_version",
        sa.Column("superseded_by_version_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f(
            "fk_context_product_version_superseded_by_version_id_context_product_version"
        ),
        "context_product_version",
        "context_product_version",
        ["superseded_by_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_context_product_version_superseded_by_version_id"),
        "context_product_version",
        ["superseded_by_version_id"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_context_product_version_support_window_days",
        "context_product_version",
        "support_window_days IS NULL OR support_window_days >= 0",
    )
    op.drop_constraint(
        "ck_context_product_version_status",
        "context_product_version",
        type_="check",
    )
    op.create_check_constraint(
        "ck_context_product_version_status",
        "context_product_version",
        "status IN ('DRAFT', 'REVIEW_REQUIRED', 'PUBLISHED', 'SUPPORTED', 'SUPERSEDED', "
        "'REJECTED', 'DEPRECATION_REVIEW', 'DEPRECATED')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_context_product_version_status",
        "context_product_version",
        type_="check",
    )
    op.create_check_constraint(
        "ck_context_product_version_status",
        "context_product_version",
        "status IN ('DRAFT', 'REVIEW_REQUIRED', 'PUBLISHED', 'SUPERSEDED', "
        "'REJECTED', 'DEPRECATION_REVIEW', 'DEPRECATED')",
    )
    op.drop_constraint(
        "ck_context_product_version_support_window_days",
        "context_product_version",
        type_="check",
    )
    op.drop_index(
        op.f("ix_context_product_version_superseded_by_version_id"),
        table_name="context_product_version",
    )
    op.drop_constraint(
        op.f(
            "fk_context_product_version_superseded_by_version_id_context_product_version"
        ),
        "context_product_version",
        type_="foreignkey",
    )
    op.drop_column("context_product_version", "superseded_by_version_id")
    op.drop_column("context_product_version", "support_window_ends_at")
    op.drop_column("context_product_version", "superseded_at")
    op.drop_column("context_product_version", "support_window_days")
