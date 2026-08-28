"""constraint inventory

Revision ID: 3df18be7a420
Revises: f4a35c86d901
Create Date: 2026-08-25 02:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3df18be7a420"
down_revision: str | Sequence[str] | None = "f4a35c86d901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analysis_run",
        sa.Column("discovered_constraints", sa.Integer(), server_default="0", nullable=False),
    )
    op.alter_column("analysis_run", "discovered_constraints", server_default=None)
    op.create_table(
        "metadata_constraint",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("constraint_type", sa.String(length=30), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("referenced_table_id", sa.Uuid(), nullable=True),
        sa.Column("referenced_columns", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_constraint_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_constraint_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["referenced_table_id"],
            ["metadata_table.id"],
            name=op.f("fk_metadata_constraint_referenced_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_metadata_constraint_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_constraint")),
        sa.UniqueConstraint("table_id", "name", name=op.f("uq_metadata_constraint_table_id")),
    )
    op.create_index(
        op.f("ix_metadata_constraint_datasource_id"),
        "metadata_constraint",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_constraint_organization_id"),
        "metadata_constraint",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_constraint_referenced_table_id"),
        "metadata_constraint",
        ["referenced_table_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_constraint_table_id"),
        "metadata_constraint",
        ["table_id"],
        unique=False,
    )
    op.create_index(
        "ix_metadata_constraint_org_type",
        "metadata_constraint",
        ["organization_id", "constraint_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_metadata_constraint_org_type", table_name="metadata_constraint")
    op.drop_index(op.f("ix_metadata_constraint_table_id"), table_name="metadata_constraint")
    op.drop_index(
        op.f("ix_metadata_constraint_referenced_table_id"),
        table_name="metadata_constraint",
    )
    op.drop_index(op.f("ix_metadata_constraint_organization_id"), table_name="metadata_constraint")
    op.drop_index(op.f("ix_metadata_constraint_datasource_id"), table_name="metadata_constraint")
    op.drop_table("metadata_constraint")
    op.drop_column("analysis_run", "discovered_constraints")
