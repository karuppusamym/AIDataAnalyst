"""fleet scan scheduling and admission metadata

Revision ID: d84fb6a713e2
Revises: c71a9e5f204d
Create Date: 2026-08-25 07:15:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d84fb6a713e2"
down_revision: str | Sequence[str] | None = "c71a9e5f204d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analysis_run",
        sa.Column("trigger_type", sa.String(length=30), nullable=False, server_default="MANUAL"),
    )
    op.add_column(
        "analysis_run",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
    )
    op.create_table(
        "scan_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=30), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("maintenance_start_hour_utc", sa.Integer(), nullable=True),
        sa.Column("maintenance_end_hour_utc", sa.Integer(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("datasource_id"),
    )
    op.create_index(
        op.f("ix_scan_policy_organization_id"),
        "scan_policy",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_scan_policy_datasource_id"),
        "scan_policy",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        "ix_scan_policy_due",
        "scan_policy",
        ["enabled", "next_run_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scan_policy_due", table_name="scan_policy")
    op.drop_index(op.f("ix_scan_policy_datasource_id"), table_name="scan_policy")
    op.drop_index(op.f("ix_scan_policy_organization_id"), table_name="scan_policy")
    op.drop_table("scan_policy")
    op.drop_column("analysis_run", "priority")
    op.drop_column("analysis_run", "trigger_type")
