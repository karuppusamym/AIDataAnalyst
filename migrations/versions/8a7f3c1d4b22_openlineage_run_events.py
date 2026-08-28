"""openlineage run events

Revision ID: 8a7f3c1d4b22
Revises: 6f4c1d2e9a10
Create Date: 2026-08-28 10:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8a7f3c1d4b22"
down_revision: str | Sequence[str] | None = "6f4c1d2e9a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "openlineage_run_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("event_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("producer", sa.String(length=1000), nullable=False),
        sa.Column("schema_url", sa.String(length=1000), nullable=True),
        sa.Column("job_namespace", sa.String(length=500), nullable=False),
        sa.Column("job_name", sa.String(length=500), nullable=False),
        sa.Column("run_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("input_dataset_count", sa.Integer(), nullable=False),
        sa.Column("output_dataset_count", sa.Integer(), nullable=False),
        sa.Column("table_edge_count", sa.Integer(), nullable=False),
        sa.Column("column_edge_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_dataset_count", sa.Integer(), nullable=False),
        sa.Column("imported_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_openlineage_run_event_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_openlineage_run_event_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_openlineage_run_event")),
        sa.UniqueConstraint(
            "datasource_id",
            "event_fingerprint",
            name=op.f("uq_openlineage_run_event_datasource_id"),
        ),
    )
    op.create_index(
        op.f("ix_openlineage_run_event_organization_id"),
        "openlineage_run_event",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_openlineage_run_event_datasource_id"),
        "openlineage_run_event",
        ["datasource_id"],
    )
    op.create_index(
        "ix_openlineage_event_source_created",
        "openlineage_run_event",
        ["datasource_id", "created_at"],
    )
    op.create_index(
        "ix_openlineage_event_org_created",
        "openlineage_run_event",
        ["organization_id", "created_at"],
    )

    op.create_table(
        "openlineage_dataset",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("run_event_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("namespace", sa.String(length=500), nullable=False),
        sa.Column("name", sa.String(length=1000), nullable=False),
        sa.Column("matched_table_id", sa.Uuid(), nullable=True),
        sa.Column("schema_fields", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_openlineage_dataset_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_event_id"],
            ["openlineage_run_event.id"],
            name=op.f("fk_openlineage_dataset_run_event_id_openlineage_run_event"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["matched_table_id"],
            ["metadata_table.id"],
            name=op.f("fk_openlineage_dataset_matched_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_openlineage_dataset")),
        sa.UniqueConstraint(
            "run_event_id",
            "direction",
            "namespace",
            "name",
            name=op.f("uq_openlineage_dataset_run_event_id"),
        ),
    )
    op.create_index(op.f("ix_openlineage_dataset_organization_id"), "openlineage_dataset", ["organization_id"])
    op.create_index(op.f("ix_openlineage_dataset_run_event_id"), "openlineage_dataset", ["run_event_id"])
    op.create_index(op.f("ix_openlineage_dataset_matched_table_id"), "openlineage_dataset", ["matched_table_id"])
    op.create_index("ix_openlineage_dataset_run_direction", "openlineage_dataset", ["run_event_id", "direction"])

    op.create_table(
        "openlineage_table_edge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("run_event_id", sa.Uuid(), nullable=False),
        sa.Column("input_dataset_namespace", sa.String(length=500), nullable=False),
        sa.Column("input_dataset_name", sa.String(length=1000), nullable=False),
        sa.Column("input_table_id", sa.Uuid(), nullable=True),
        sa.Column("output_dataset_namespace", sa.String(length=500), nullable=False),
        sa.Column("output_dataset_name", sa.String(length=1000), nullable=False),
        sa.Column("output_table_id", sa.Uuid(), nullable=True),
        sa.Column("edge_kind", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_openlineage_table_edge_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_event_id"],
            ["openlineage_run_event.id"],
            name=op.f("fk_openlineage_table_edge_run_event_id_openlineage_run_event"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["input_table_id"],
            ["metadata_table.id"],
            name=op.f("fk_openlineage_table_edge_input_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["output_table_id"],
            ["metadata_table.id"],
            name=op.f("fk_openlineage_table_edge_output_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_openlineage_table_edge")),
        sa.UniqueConstraint(
            "run_event_id",
            "input_dataset_namespace",
            "input_dataset_name",
            "output_dataset_namespace",
            "output_dataset_name",
            name="uq_openlineage_table_edge_run_input_output",
        ),
    )
    op.create_index(op.f("ix_openlineage_table_edge_organization_id"), "openlineage_table_edge", ["organization_id"])
    op.create_index(op.f("ix_openlineage_table_edge_run_event_id"), "openlineage_table_edge", ["run_event_id"])
    op.create_index(op.f("ix_openlineage_table_edge_input_table_id"), "openlineage_table_edge", ["input_table_id"])
    op.create_index(op.f("ix_openlineage_table_edge_output_table_id"), "openlineage_table_edge", ["output_table_id"])
    op.create_index("ix_openlineage_table_edge_run", "openlineage_table_edge", ["run_event_id"])

    op.create_table(
        "openlineage_column_edge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("run_event_id", sa.Uuid(), nullable=False),
        sa.Column("input_dataset_namespace", sa.String(length=500), nullable=False),
        sa.Column("input_dataset_name", sa.String(length=1000), nullable=False),
        sa.Column("input_table_id", sa.Uuid(), nullable=True),
        sa.Column("input_column_name", sa.String(length=255), nullable=False),
        sa.Column("output_dataset_namespace", sa.String(length=500), nullable=False),
        sa.Column("output_dataset_name", sa.String(length=1000), nullable=False),
        sa.Column("output_table_id", sa.Uuid(), nullable=True),
        sa.Column("output_column_name", sa.String(length=255), nullable=False),
        sa.Column("transformation_type", sa.String(length=100), nullable=True),
        sa.Column("transformation_subtype", sa.String(length=100), nullable=True),
        sa.Column("edge_kind", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_openlineage_column_edge_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_event_id"],
            ["openlineage_run_event.id"],
            name=op.f("fk_openlineage_column_edge_run_event_id_openlineage_run_event"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["input_table_id"],
            ["metadata_table.id"],
            name=op.f("fk_openlineage_column_edge_input_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["output_table_id"],
            ["metadata_table.id"],
            name=op.f("fk_openlineage_column_edge_output_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_openlineage_column_edge")),
        sa.UniqueConstraint(
            "run_event_id",
            "input_dataset_namespace",
            "input_dataset_name",
            "input_column_name",
            "output_dataset_namespace",
            "output_dataset_name",
            "output_column_name",
            name="uq_openlineage_column_edge_run_input_output",
        ),
    )
    op.create_index(op.f("ix_openlineage_column_edge_organization_id"), "openlineage_column_edge", ["organization_id"])
    op.create_index(op.f("ix_openlineage_column_edge_run_event_id"), "openlineage_column_edge", ["run_event_id"])
    op.create_index(op.f("ix_openlineage_column_edge_input_table_id"), "openlineage_column_edge", ["input_table_id"])
    op.create_index(op.f("ix_openlineage_column_edge_output_table_id"), "openlineage_column_edge", ["output_table_id"])
    op.create_index("ix_openlineage_column_edge_run", "openlineage_column_edge", ["run_event_id"])


def downgrade() -> None:
    op.drop_index("ix_openlineage_column_edge_run", table_name="openlineage_column_edge")
    op.drop_index(op.f("ix_openlineage_column_edge_output_table_id"), table_name="openlineage_column_edge")
    op.drop_index(op.f("ix_openlineage_column_edge_input_table_id"), table_name="openlineage_column_edge")
    op.drop_index(op.f("ix_openlineage_column_edge_run_event_id"), table_name="openlineage_column_edge")
    op.drop_index(op.f("ix_openlineage_column_edge_organization_id"), table_name="openlineage_column_edge")
    op.drop_table("openlineage_column_edge")

    op.drop_index("ix_openlineage_table_edge_run", table_name="openlineage_table_edge")
    op.drop_index(op.f("ix_openlineage_table_edge_output_table_id"), table_name="openlineage_table_edge")
    op.drop_index(op.f("ix_openlineage_table_edge_input_table_id"), table_name="openlineage_table_edge")
    op.drop_index(op.f("ix_openlineage_table_edge_run_event_id"), table_name="openlineage_table_edge")
    op.drop_index(op.f("ix_openlineage_table_edge_organization_id"), table_name="openlineage_table_edge")
    op.drop_table("openlineage_table_edge")

    op.drop_index("ix_openlineage_dataset_run_direction", table_name="openlineage_dataset")
    op.drop_index(op.f("ix_openlineage_dataset_matched_table_id"), table_name="openlineage_dataset")
    op.drop_index(op.f("ix_openlineage_dataset_run_event_id"), table_name="openlineage_dataset")
    op.drop_index(op.f("ix_openlineage_dataset_organization_id"), table_name="openlineage_dataset")
    op.drop_table("openlineage_dataset")

    op.drop_index("ix_openlineage_event_org_created", table_name="openlineage_run_event")
    op.drop_index("ix_openlineage_event_source_created", table_name="openlineage_run_event")
    op.drop_index(op.f("ix_openlineage_run_event_datasource_id"), table_name="openlineage_run_event")
    op.drop_index(op.f("ix_openlineage_run_event_organization_id"), table_name="openlineage_run_event")
    op.drop_table("openlineage_run_event")
