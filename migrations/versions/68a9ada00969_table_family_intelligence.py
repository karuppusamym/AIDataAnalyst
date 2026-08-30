"""RL-1: table family / temporal intelligence

Revision ID: 68a9ada00969
Revises: 8e965e8d626d
Create Date: 2026-08-30 14:07:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "68a9ada00969"
down_revision: str | Sequence[str] | None = "8e965e8d626d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "table_family_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("family_type", sa.String(length=20), nullable=False),
        sa.Column("member_table_ids", sa.JSON(), nullable=False),
        sa.Column("base_table_id", sa.Uuid(), nullable=True),
        sa.Column("detection_rule", sa.String(length=100), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("review_reason", sa.String(length=2000), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_table_family_candidate_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_table_family_candidate_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["schema_id"],
            ["metadata_schema.id"],
            name=op.f("fk_table_family_candidate_schema_id_metadata_schema"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["base_table_id"],
            ["metadata_table.id"],
            name=op.f("fk_table_family_candidate_base_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_family_candidate")),
    )
    op.create_index(
        op.f("ix_table_family_candidate_organization_id"),
        "table_family_candidate",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_table_family_candidate_datasource_id"),
        "table_family_candidate",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_table_family_candidate_base_table_id"),
        "table_family_candidate",
        ["base_table_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_table_family_candidate_schema_id"),
        "table_family_candidate",
        ["schema_id"],
        unique=False,
    )
    op.create_index(
        "ix_table_family_candidate_org_status",
        "table_family_candidate",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_table_family_candidate_org_status", table_name="table_family_candidate"
    )
    op.drop_index(
        op.f("ix_table_family_candidate_schema_id"), table_name="table_family_candidate"
    )
    op.drop_index(
        op.f("ix_table_family_candidate_base_table_id"), table_name="table_family_candidate"
    )
    op.drop_index(
        op.f("ix_table_family_candidate_datasource_id"), table_name="table_family_candidate"
    )
    op.drop_index(
        op.f("ix_table_family_candidate_organization_id"), table_name="table_family_candidate"
    )
    op.drop_table("table_family_candidate")
