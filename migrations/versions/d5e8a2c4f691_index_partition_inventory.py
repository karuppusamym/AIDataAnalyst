"""index and partition inventory

Revision ID: d5e8a2c4f691
Revises: c3a9f1d5b6e2
Create Date: 2026-08-30 15:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e8a2c4f691"
down_revision: str | Sequence[str] | None = "c3a9f1d5b6e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column_name in ("discovered_indexes", "discovered_partitions"):
        op.add_column(
            "analysis_run",
            sa.Column(column_name, sa.Integer(), server_default="0", nullable=False),
        )
        op.alter_column("analysis_run", column_name, server_default=None)

    # Keyset-pagination indexes for CT-2: the trailing columns match list_tables'
    # and list_columns' ORDER BY exactly, so the cursor predicate can be satisfied
    # by a single index range seek instead of a growing OFFSET scan.
    op.create_index(
        "ix_metadata_table_ds_status_name_id",
        "metadata_table",
        ["datasource_id", "status", "name", "id"],
        unique=False,
    )
    op.create_index(
        "ix_metadata_column_table_status_ordinal_id",
        "metadata_column",
        ["table_id", "status", "ordinal_position", "id"],
        unique=False,
    )

    op.create_table(
        "metadata_index",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("index_type", sa.String(length=30), nullable=False),
        sa.Column("columns", sa.JSON(), nullable=False),
        sa.Column("is_unique", sa.Boolean(), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_index_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_index_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_metadata_index_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_index")),
        sa.UniqueConstraint("table_id", "name", name=op.f("uq_metadata_index_table_id")),
    )
    op.create_index(
        op.f("ix_metadata_index_datasource_id"), "metadata_index", ["datasource_id"], unique=False
    )
    op.create_index(
        op.f("ix_metadata_index_organization_id"),
        "metadata_index",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_index_table_id"), "metadata_index", ["table_id"], unique=False
    )
    op.create_index(
        "ix_metadata_index_org_type", "metadata_index", ["organization_id", "index_type"],
        unique=False,
    )
    op.create_index(
        "ix_metadata_index_table_status_name_id",
        "metadata_index",
        ["table_id", "status", "name", "id"],
        unique=False,
    )

    op.create_table(
        "metadata_partition",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("partition_type", sa.String(length=30), nullable=False),
        sa.Column("ordinal_position", sa.Integer(), nullable=False),
        sa.Column("key_columns", sa.JSON(), nullable=False),
        sa.Column("high_value", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_partition_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_partition_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_metadata_partition_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_partition")),
        sa.UniqueConstraint("table_id", "name", name=op.f("uq_metadata_partition_table_id")),
    )
    op.create_index(
        op.f("ix_metadata_partition_datasource_id"),
        "metadata_partition",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_partition_organization_id"),
        "metadata_partition",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_metadata_partition_table_id"), "metadata_partition", ["table_id"], unique=False
    )
    op.create_index(
        "ix_metadata_partition_org_type",
        "metadata_partition",
        ["organization_id", "partition_type"],
        unique=False,
    )
    op.create_index(
        "ix_metadata_partition_table_status_ordinal_id",
        "metadata_partition",
        ["table_id", "status", "ordinal_position", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_metadata_partition_table_status_ordinal_id", table_name="metadata_partition")
    op.drop_index("ix_metadata_partition_org_type", table_name="metadata_partition")
    op.drop_index(op.f("ix_metadata_partition_table_id"), table_name="metadata_partition")
    op.drop_index(op.f("ix_metadata_partition_organization_id"), table_name="metadata_partition")
    op.drop_index(op.f("ix_metadata_partition_datasource_id"), table_name="metadata_partition")
    op.drop_table("metadata_partition")

    op.drop_index("ix_metadata_index_table_status_name_id", table_name="metadata_index")
    op.drop_index("ix_metadata_index_org_type", table_name="metadata_index")
    op.drop_index(op.f("ix_metadata_index_table_id"), table_name="metadata_index")
    op.drop_index(op.f("ix_metadata_index_organization_id"), table_name="metadata_index")
    op.drop_index(op.f("ix_metadata_index_datasource_id"), table_name="metadata_index")
    op.drop_table("metadata_index")

    op.drop_index("ix_metadata_column_table_status_ordinal_id", table_name="metadata_column")
    op.drop_index("ix_metadata_table_ds_status_name_id", table_name="metadata_table")

    for column_name in ("discovered_partitions", "discovered_indexes"):
        op.drop_column("analysis_run", column_name)
