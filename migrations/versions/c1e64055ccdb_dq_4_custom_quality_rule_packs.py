"""DQ-4: custom quality rule packs and rule scheduling

Revision ID: c1e64055ccdb
Revises: 933ad6ad0731
Create Date: 2026-08-31 00:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1e64055ccdb"
down_revision: str | Sequence[str] | None = "933ad6ad0731"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality_rule_pack",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("datasource_id", "name"),
    )
    for column in ("organization_id", "datasource_id"):
        op.create_index(op.f(f"ix_quality_rule_pack_{column}"), "quality_rule_pack", [column])
    op.create_index(
        "ix_quality_rule_pack_org_enabled", "quality_rule_pack", ["organization_id", "enabled"]
    )

    op.create_table(
        "quality_rule",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("rule_pack_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid()),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("rule_type", sa.String(30), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rule_pack_id"], ["quality_rule_pack.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["column_id"], ["metadata_column.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "rule_type IN ('TABLE_ROW_COUNT_MIN', 'TABLE_ROW_COUNT_MAX', 'COLUMN_NULL_RATE_MAX')",
            name="ck_quality_rule_type",
        ),
    )
    for column in ("organization_id", "rule_pack_id", "table_id", "column_id"):
        op.create_index(op.f(f"ix_quality_rule_{column}"), "quality_rule", [column])
    op.create_index(
        "ix_quality_rule_pack_enabled", "quality_rule", ["rule_pack_id", "enabled"]
    )


def downgrade() -> None:
    op.drop_table("quality_rule")
    op.drop_table("quality_rule_pack")
