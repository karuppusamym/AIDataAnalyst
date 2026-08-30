"""bi lineage: report -> metric -> column edges (LN-4)

Revision ID: a67d5816d225
Revises: 8a7f3c1d4b22
Create Date: 2026-08-30 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a67d5816d225"
down_revision: str | Sequence[str] | None = "8a7f3c1d4b22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bi_connection",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("bi_tool", sa.String(length=30), nullable=False),
        sa.Column("connection_key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("site_or_workspace", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_bi_connection_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_bi_connection_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_bi_connection_datasource_id_datasource"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bi_connection")),
        sa.UniqueConstraint(
            "organization_id", "connection_key", name=op.f("uq_bi_connection_organization_id")
        ),
    )
    op.create_index(op.f("ix_bi_connection_organization_id"), "bi_connection", ["organization_id"])
    op.create_index(op.f("ix_bi_connection_project_id"), "bi_connection", ["project_id"])
    op.create_index(op.f("ix_bi_connection_datasource_id"), "bi_connection", ["datasource_id"])
    op.create_index("ix_bi_connection_project_status", "bi_connection", ["project_id", "status"])

    op.create_table(
        "bi_artifact_import",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("bi_tool", sa.String(length=30), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("report_count", sa.Integer(), nullable=False),
        sa.Column("metric_count", sa.Integer(), nullable=False),
        sa.Column("report_metric_edge_count", sa.Integer(), nullable=False),
        sa.Column("metric_column_edge_count", sa.Integer(), nullable=False),
        sa.Column("matched_column_count", sa.Integer(), nullable=False),
        sa.Column("unmatched_column_count", sa.Integer(), nullable=False),
        sa.Column("imported_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_bi_artifact_import_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["bi_connection.id"],
            name=op.f("fk_bi_artifact_import_connection_id_bi_connection"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bi_artifact_import")),
        sa.UniqueConstraint(
            "connection_id",
            "artifact_fingerprint",
            name=op.f("uq_bi_artifact_import_connection_id"),
        ),
    )
    op.create_index(
        op.f("ix_bi_artifact_import_organization_id"), "bi_artifact_import", ["organization_id"]
    )
    op.create_index(
        op.f("ix_bi_artifact_import_connection_id"), "bi_artifact_import", ["connection_id"]
    )
    op.create_index(
        "ix_bi_artifact_import_org_created",
        "bi_artifact_import",
        ["organization_id", "created_at"],
    )

    op.create_table(
        "bi_report_node",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_import_id", sa.Uuid(), nullable=False),
        sa.Column("parent_report_id", sa.Uuid(), nullable=True),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("report_type", sa.String(length=30), nullable=False),
        sa.Column("project_name", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_bi_report_node_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_import_id"],
            ["bi_artifact_import.id"],
            name=op.f("fk_bi_report_node_artifact_import_id_bi_artifact_import"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_report_id"],
            ["bi_report_node.id"],
            name=op.f("fk_bi_report_node_parent_report_id_bi_report_node"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bi_report_node")),
        sa.UniqueConstraint(
            "artifact_import_id", "external_id", name=op.f("uq_bi_report_node_artifact_import_id")
        ),
    )
    op.create_index(
        op.f("ix_bi_report_node_organization_id"), "bi_report_node", ["organization_id"]
    )
    op.create_index(
        op.f("ix_bi_report_node_artifact_import_id"), "bi_report_node", ["artifact_import_id"]
    )
    op.create_index(
        op.f("ix_bi_report_node_parent_report_id"), "bi_report_node", ["parent_report_id"]
    )
    op.create_index(
        "ix_bi_report_node_import_type", "bi_report_node", ["artifact_import_id", "report_type"]
    )

    op.create_table(
        "bi_metric_node",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_import_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("field_type", sa.String(length=30), nullable=False),
        sa.Column("datasource_name", sa.String(length=255), nullable=True),
        sa.Column("formula_hash", sa.String(length=64), nullable=True),
        sa.Column("formula_present", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_bi_metric_node_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_import_id"],
            ["bi_artifact_import.id"],
            name=op.f("fk_bi_metric_node_artifact_import_id_bi_artifact_import"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bi_metric_node")),
        sa.UniqueConstraint(
            "artifact_import_id", "external_id", name=op.f("uq_bi_metric_node_artifact_import_id")
        ),
    )
    op.create_index(
        op.f("ix_bi_metric_node_organization_id"), "bi_metric_node", ["organization_id"]
    )
    op.create_index(
        op.f("ix_bi_metric_node_artifact_import_id"), "bi_metric_node", ["artifact_import_id"]
    )
    op.create_index(
        "ix_bi_metric_node_import_type", "bi_metric_node", ["artifact_import_id", "field_type"]
    )

    op.create_table(
        "bi_report_metric_edge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_import_id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("metric_id", sa.Uuid(), nullable=False),
        sa.Column("edge_kind", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_bi_report_metric_edge_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_import_id"],
            ["bi_artifact_import.id"],
            name=op.f("fk_bi_report_metric_edge_artifact_import_id_bi_artifact_import"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["bi_report_node.id"],
            name=op.f("fk_bi_report_metric_edge_report_id_bi_report_node"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["metric_id"],
            ["bi_metric_node.id"],
            name=op.f("fk_bi_report_metric_edge_metric_id_bi_metric_node"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bi_report_metric_edge")),
        sa.UniqueConstraint(
            "artifact_import_id",
            "report_id",
            "metric_id",
            name="uq_bi_report_metric_edge_import_report_metric",
        ),
    )
    op.create_index(
        op.f("ix_bi_report_metric_edge_organization_id"),
        "bi_report_metric_edge",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_bi_report_metric_edge_artifact_import_id"),
        "bi_report_metric_edge",
        ["artifact_import_id"],
    )
    op.create_index(
        op.f("ix_bi_report_metric_edge_report_id"), "bi_report_metric_edge", ["report_id"]
    )
    op.create_index(
        op.f("ix_bi_report_metric_edge_metric_id"), "bi_report_metric_edge", ["metric_id"]
    )
    op.create_index(
        "ix_bi_report_metric_edge_import", "bi_report_metric_edge", ["artifact_import_id"]
    )

    op.create_table(
        "bi_metric_column_edge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_import_id", sa.Uuid(), nullable=False),
        sa.Column("metric_id", sa.Uuid(), nullable=False),
        sa.Column("source_database_name", sa.String(length=255), nullable=True),
        sa.Column("source_schema_name", sa.String(length=255), nullable=True),
        sa.Column("source_table_name", sa.String(length=255), nullable=False),
        sa.Column("source_column_name", sa.String(length=255), nullable=False),
        sa.Column("matched_table_id", sa.Uuid(), nullable=True),
        sa.Column("matched_column_id", sa.Uuid(), nullable=True),
        sa.Column("edge_kind", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_bi_metric_column_edge_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_import_id"],
            ["bi_artifact_import.id"],
            name=op.f("fk_bi_metric_column_edge_artifact_import_id_bi_artifact_import"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["metric_id"],
            ["bi_metric_node.id"],
            name=op.f("fk_bi_metric_column_edge_metric_id_bi_metric_node"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["matched_table_id"],
            ["metadata_table.id"],
            name=op.f("fk_bi_metric_column_edge_matched_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["matched_column_id"],
            ["metadata_column.id"],
            name=op.f("fk_bi_metric_column_edge_matched_column_id_metadata_column"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bi_metric_column_edge")),
        sa.UniqueConstraint(
            "artifact_import_id",
            "metric_id",
            "source_database_name",
            "source_schema_name",
            "source_table_name",
            "source_column_name",
            name="uq_bi_metric_column_edge_import_metric_source",
        ),
    )
    op.create_index(
        op.f("ix_bi_metric_column_edge_organization_id"),
        "bi_metric_column_edge",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_bi_metric_column_edge_artifact_import_id"),
        "bi_metric_column_edge",
        ["artifact_import_id"],
    )
    op.create_index(
        op.f("ix_bi_metric_column_edge_metric_id"), "bi_metric_column_edge", ["metric_id"]
    )
    op.create_index(
        op.f("ix_bi_metric_column_edge_matched_table_id"),
        "bi_metric_column_edge",
        ["matched_table_id"],
    )
    op.create_index(
        op.f("ix_bi_metric_column_edge_matched_column_id"),
        "bi_metric_column_edge",
        ["matched_column_id"],
    )
    op.create_index(
        "ix_bi_metric_column_edge_import", "bi_metric_column_edge", ["artifact_import_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_bi_metric_column_edge_import", table_name="bi_metric_column_edge")
    op.drop_index(
        op.f("ix_bi_metric_column_edge_matched_column_id"), table_name="bi_metric_column_edge"
    )
    op.drop_index(
        op.f("ix_bi_metric_column_edge_matched_table_id"), table_name="bi_metric_column_edge"
    )
    op.drop_index(op.f("ix_bi_metric_column_edge_metric_id"), table_name="bi_metric_column_edge")
    op.drop_index(
        op.f("ix_bi_metric_column_edge_artifact_import_id"), table_name="bi_metric_column_edge"
    )
    op.drop_index(
        op.f("ix_bi_metric_column_edge_organization_id"), table_name="bi_metric_column_edge"
    )
    op.drop_table("bi_metric_column_edge")

    op.drop_index("ix_bi_report_metric_edge_import", table_name="bi_report_metric_edge")
    op.drop_index(op.f("ix_bi_report_metric_edge_metric_id"), table_name="bi_report_metric_edge")
    op.drop_index(op.f("ix_bi_report_metric_edge_report_id"), table_name="bi_report_metric_edge")
    op.drop_index(
        op.f("ix_bi_report_metric_edge_artifact_import_id"), table_name="bi_report_metric_edge"
    )
    op.drop_index(
        op.f("ix_bi_report_metric_edge_organization_id"), table_name="bi_report_metric_edge"
    )
    op.drop_table("bi_report_metric_edge")

    op.drop_index("ix_bi_metric_node_import_type", table_name="bi_metric_node")
    op.drop_index(op.f("ix_bi_metric_node_artifact_import_id"), table_name="bi_metric_node")
    op.drop_index(op.f("ix_bi_metric_node_organization_id"), table_name="bi_metric_node")
    op.drop_table("bi_metric_node")

    op.drop_index("ix_bi_report_node_import_type", table_name="bi_report_node")
    op.drop_index(op.f("ix_bi_report_node_parent_report_id"), table_name="bi_report_node")
    op.drop_index(op.f("ix_bi_report_node_artifact_import_id"), table_name="bi_report_node")
    op.drop_index(op.f("ix_bi_report_node_organization_id"), table_name="bi_report_node")
    op.drop_table("bi_report_node")

    op.drop_index("ix_bi_artifact_import_org_created", table_name="bi_artifact_import")
    op.drop_index(op.f("ix_bi_artifact_import_connection_id"), table_name="bi_artifact_import")
    op.drop_index(
        op.f("ix_bi_artifact_import_organization_id"), table_name="bi_artifact_import"
    )
    op.drop_table("bi_artifact_import")

    op.drop_index("ix_bi_connection_project_status", table_name="bi_connection")
    op.drop_index(op.f("ix_bi_connection_datasource_id"), table_name="bi_connection")
    op.drop_index(op.f("ix_bi_connection_project_id"), table_name="bi_connection")
    op.drop_index(op.f("ix_bi_connection_organization_id"), table_name="bi_connection")
    op.drop_table("bi_connection")
