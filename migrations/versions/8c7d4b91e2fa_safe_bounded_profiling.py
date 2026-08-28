"""safe bounded profiling

Revision ID: 8c7d4b91e2fa
Revises: cb0e8b95925b
Create Date: 2026-08-25 01:15:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8c7d4b91e2fa"
down_revision: str | Sequence[str] | None = "cb0e8b95925b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "analysis_run",
        sa.Column("profiled_tables", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "analysis_run",
        sa.Column("profiled_columns", sa.Integer(), server_default="0", nullable=False),
    )
    op.alter_column("analysis_run", "profiled_tables", server_default=None)
    op.alter_column("analysis_run", "profiled_columns", server_default=None)

    op.create_table(
        "table_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("profile_version", sa.String(length=50), nullable=False),
        sa.Column("row_count_estimate", sa.BigInteger(), nullable=True),
        sa.Column("sampled_row_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_run.id"],
            name=op.f("fk_table_profile_analysis_run_id_analysis_run"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_table_profile_datasource_id_datasource"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_table_profile_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_table_profile_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_table_profile")),
        sa.UniqueConstraint(
            "analysis_run_id", "table_id", name=op.f("uq_table_profile_analysis_run_id")
        ),
    )
    op.create_index(
        op.f("ix_table_profile_analysis_run_id"),
        "table_profile",
        ["analysis_run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_table_profile_datasource_id"),
        "table_profile",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_table_profile_organization_id"),
        "table_profile",
        ["organization_id"],
        unique=False,
    )
    op.create_index(op.f("ix_table_profile_table_id"), "table_profile", ["table_id"], unique=False)
    op.create_index(
        "ix_table_profile_org_created",
        "table_profile",
        ["organization_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "column_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("table_profile_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=False),
        sa.Column("null_count", sa.BigInteger(), nullable=False),
        sa.Column("non_null_count", sa.BigInteger(), nullable=False),
        sa.Column("approximate_distinct_count", sa.BigInteger(), nullable=False),
        sa.Column("min_length", sa.Integer(), nullable=True),
        sa.Column("max_length", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["column_id"],
            ["metadata_column.id"],
            name=op.f("fk_column_profile_column_id_metadata_column"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_column_profile_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_profile_id"],
            ["table_profile.id"],
            name=op.f("fk_column_profile_table_profile_id_table_profile"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_column_profile")),
        sa.UniqueConstraint(
            "table_profile_id", "column_id", name=op.f("uq_column_profile_table_profile_id")
        ),
    )
    op.create_index(
        op.f("ix_column_profile_column_id"),
        "column_profile",
        ["column_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_column_profile_organization_id"),
        "column_profile",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_column_profile_table_profile_id"),
        "column_profile",
        ["table_profile_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_column_profile_table_profile_id"), table_name="column_profile")
    op.drop_index(op.f("ix_column_profile_organization_id"), table_name="column_profile")
    op.drop_index(op.f("ix_column_profile_column_id"), table_name="column_profile")
    op.drop_table("column_profile")
    op.drop_index("ix_table_profile_org_created", table_name="table_profile")
    op.drop_index(op.f("ix_table_profile_table_id"), table_name="table_profile")
    op.drop_index(op.f("ix_table_profile_organization_id"), table_name="table_profile")
    op.drop_index(op.f("ix_table_profile_datasource_id"), table_name="table_profile")
    op.drop_index(op.f("ix_table_profile_analysis_run_id"), table_name="table_profile")
    op.drop_table("table_profile")
    op.drop_column("analysis_run", "profiled_columns")
    op.drop_column("analysis_run", "profiled_tables")
