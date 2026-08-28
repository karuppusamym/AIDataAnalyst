"""dbt artifact inventory and lineage

Revision ID: e4b7c2a91d35
Revises: d9f6a4b31e82
Create Date: 2026-08-27 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4b7c2a91d35"
down_revision: str | Sequence[str] | None = "d9f6a4b31e82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dbt_project",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("project_key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("repository_url", sa.String(length=1000), nullable=True),
        sa.Column("target_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_dbt_project_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_dbt_project_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_dbt_project_datasource_id_datasource"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dbt_project")),
        sa.UniqueConstraint(
            "organization_id", "project_key", name=op.f("uq_dbt_project_organization_id")
        ),
    )
    op.create_index(op.f("ix_dbt_project_organization_id"), "dbt_project", ["organization_id"])
    op.create_index(op.f("ix_dbt_project_project_id"), "dbt_project", ["project_id"])
    op.create_index(op.f("ix_dbt_project_datasource_id"), "dbt_project", ["datasource_id"])
    op.create_index("ix_dbt_project_project_status", "dbt_project", ["project_id", "status"])

    op.create_table(
        "dbt_artifact_import",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("dbt_project_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("dbt_schema_version", sa.String(length=255), nullable=False),
        sa.Column("dbt_version", sa.String(length=50), nullable=True),
        sa.Column("invocation_id", sa.String(length=255), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("resource_count", sa.Integer(), nullable=False),
        sa.Column("model_count", sa.Integer(), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("test_count", sa.Integer(), nullable=False),
        sa.Column("lineage_edge_count", sa.Integer(), nullable=False),
        sa.Column("matched_resource_count", sa.Integer(), nullable=False),
        sa.Column("unmatched_resource_count", sa.Integer(), nullable=False),
        sa.Column("imported_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_dbt_artifact_import_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dbt_project_id"],
            ["dbt_project.id"],
            name=op.f("fk_dbt_artifact_import_dbt_project_id_dbt_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dbt_artifact_import")),
        sa.UniqueConstraint(
            "dbt_project_id",
            "manifest_fingerprint",
            name=op.f("uq_dbt_artifact_import_dbt_project_id"),
        ),
    )
    op.create_index(
        op.f("ix_dbt_artifact_import_organization_id"),
        "dbt_artifact_import",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_dbt_artifact_import_dbt_project_id"),
        "dbt_artifact_import",
        ["dbt_project_id"],
    )
    op.create_index(
        "ix_dbt_artifact_org_created", "dbt_artifact_import", ["organization_id", "created_at"]
    )

    op.create_table(
        "dbt_resource",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_import_id", sa.Uuid(), nullable=False),
        sa.Column("unique_id", sa.String(length=500), nullable=False),
        sa.Column("resource_type", sa.String(length=30), nullable=False),
        sa.Column("package_name", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("database_name", sa.String(length=255), nullable=True),
        sa.Column("schema_name", sa.String(length=255), nullable=True),
        sa.Column("relation_name", sa.String(length=1000), nullable=True),
        sa.Column("materialization", sa.String(length=100), nullable=True),
        sa.Column("original_file_path", sa.String(length=1000), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("compiled_sql_hash", sa.String(length=64), nullable=True),
        sa.Column("compiled_sql_redacted", sa.Text(), nullable=True),
        sa.Column("sql_parse_status", sa.String(length=30), nullable=False),
        sa.Column("column_names", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("depends_on_unique_ids", sa.JSON(), nullable=False),
        sa.Column("matched_table_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_dbt_resource_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_import_id"],
            ["dbt_artifact_import.id"],
            name=op.f("fk_dbt_resource_artifact_import_id_dbt_artifact_import"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["matched_table_id"],
            ["metadata_table.id"],
            name=op.f("fk_dbt_resource_matched_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dbt_resource")),
        sa.UniqueConstraint(
            "artifact_import_id", "unique_id", name=op.f("uq_dbt_resource_artifact_import_id")
        ),
    )
    op.create_index(op.f("ix_dbt_resource_organization_id"), "dbt_resource", ["organization_id"])
    op.create_index(
        op.f("ix_dbt_resource_artifact_import_id"), "dbt_resource", ["artifact_import_id"]
    )
    op.create_index(op.f("ix_dbt_resource_matched_table_id"), "dbt_resource", ["matched_table_id"])
    op.create_index(
        "ix_dbt_resource_import_type", "dbt_resource", ["artifact_import_id", "resource_type"]
    )

    op.create_table(
        "dbt_lineage_edge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_import_id", sa.Uuid(), nullable=False),
        sa.Column("source_resource_id", sa.Uuid(), nullable=False),
        sa.Column("target_resource_id", sa.Uuid(), nullable=False),
        sa.Column("edge_type", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_dbt_lineage_edge_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_import_id"],
            ["dbt_artifact_import.id"],
            name=op.f("fk_dbt_lineage_edge_artifact_import_id_dbt_artifact_import"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_resource_id"],
            ["dbt_resource.id"],
            name=op.f("fk_dbt_lineage_edge_source_resource_id_dbt_resource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_resource_id"],
            ["dbt_resource.id"],
            name=op.f("fk_dbt_lineage_edge_target_resource_id_dbt_resource"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dbt_lineage_edge")),
        sa.UniqueConstraint(
            "artifact_import_id",
            "source_resource_id",
            "target_resource_id",
            name="uq_dbt_lineage_edge_import_source_target",
        ),
    )
    op.create_index(
        op.f("ix_dbt_lineage_edge_organization_id"), "dbt_lineage_edge", ["organization_id"]
    )
    op.create_index(
        op.f("ix_dbt_lineage_edge_artifact_import_id"),
        "dbt_lineage_edge",
        ["artifact_import_id"],
    )
    op.create_index(
        op.f("ix_dbt_lineage_edge_source_resource_id"),
        "dbt_lineage_edge",
        ["source_resource_id"],
    )
    op.create_index(
        op.f("ix_dbt_lineage_edge_target_resource_id"),
        "dbt_lineage_edge",
        ["target_resource_id"],
    )


def downgrade() -> None:
    op.drop_table("dbt_lineage_edge")
    op.drop_table("dbt_resource")
    op.drop_table("dbt_artifact_import")
    op.drop_table("dbt_project")
