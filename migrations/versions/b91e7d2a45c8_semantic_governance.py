"""semantic governance

Revision ID: b91e7d2a45c8
Revises: a02f6c4d8b31
Create Date: 2026-08-25 06:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b91e7d2a45c8"
down_revision: str | Sequence[str] | None = "a02f6c4d8b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_model_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("change_summary", sa.String(length=1000), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("based_on_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["based_on_version_id"],
            ["semantic_model_version.id"],
            name=op.f("fk_semantic_model_version_based_on_version_id_semantic_model_version"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_semantic_model_version_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_semantic_model_version_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_model_version")),
        sa.UniqueConstraint(
            "project_id", "version", name=op.f("uq_semantic_model_version_project_id")
        ),
    )
    for index_name, columns in (
        (op.f("ix_semantic_model_version_based_on_version_id"), ["based_on_version_id"]),
        (op.f("ix_semantic_model_version_organization_id"), ["organization_id"]),
        (op.f("ix_semantic_model_version_project_id"), ["project_id"]),
        ("ix_semantic_model_org_status", ["organization_id", "status"]),
    ):
        op.create_index(index_name, "semantic_model_version", columns, unique=False)

    op.create_table(
        "semantic_metric",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_semantic_metric_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_semantic_metric_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_metric")),
        sa.UniqueConstraint("project_id", "slug", name=op.f("uq_semantic_metric_project_id")),
    )
    op.create_index(
        op.f("ix_semantic_metric_organization_id"),
        "semantic_metric",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_semantic_metric_project_id"),
        "semantic_metric",
        ["project_id"],
        unique=False,
    )

    op.create_table(
        "semantic_metric_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("semantic_model_version_id", sa.Uuid(), nullable=False),
        sa.Column("metric_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("aggregation", sa.String(length=30), nullable=False),
        sa.Column("grain", sa.String(length=255), nullable=False),
        sa.Column("source_table_id", sa.Uuid(), nullable=False),
        sa.Column("measure_column_id", sa.Uuid(), nullable=True),
        sa.Column("default_time_column_id", sa.Uuid(), nullable=True),
        sa.Column("allowed_dimension_column_ids", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["default_time_column_id"],
            ["metadata_column.id"],
            name=op.f("fk_semantic_metric_version_default_time_column_id_metadata_column"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["measure_column_id"],
            ["metadata_column.id"],
            name=op.f("fk_semantic_metric_version_measure_column_id_metadata_column"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["metric_id"],
            ["semantic_metric.id"],
            name=op.f("fk_semantic_metric_version_metric_id_semantic_metric"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_semantic_metric_version_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["semantic_model_version_id"],
            ["semantic_model_version.id"],
            name=op.f(
                "fk_semantic_metric_version_semantic_model_version_id_semantic_model_version"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_table_id"],
            ["metadata_table.id"],
            name=op.f("fk_semantic_metric_version_source_table_id_metadata_table"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_metric_version")),
        sa.UniqueConstraint(
            "metric_id", "version", name=op.f("uq_semantic_metric_version_metric_id")
        ),
        sa.UniqueConstraint(
            "semantic_model_version_id",
            "metric_id",
            name="uq_semantic_metric_version_model_metric",
        ),
    )
    for column_name in (
        "default_time_column_id",
        "measure_column_id",
        "metric_id",
        "organization_id",
        "semantic_model_version_id",
        "source_table_id",
    ):
        op.create_index(
            op.f(f"ix_semantic_metric_version_{column_name}"),
            "semantic_metric_version",
            [column_name],
            unique=False,
        )

    op.create_table(
        "governance_review",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("object_type", sa.String(length=100), nullable=False),
        sa.Column("object_id", sa.String(length=100), nullable=False),
        sa.Column("requested_action", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("decision_reason", sa.String(length=2000), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_governance_review_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governance_review")),
    )
    op.create_index(
        op.f("ix_governance_review_object_id"),
        "governance_review",
        ["object_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_governance_review_organization_id"),
        "governance_review",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_governance_review_org_status",
        "governance_review",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_governance_review_org_status", table_name="governance_review")
    op.drop_index(op.f("ix_governance_review_organization_id"), table_name="governance_review")
    op.drop_index(op.f("ix_governance_review_object_id"), table_name="governance_review")
    op.drop_table("governance_review")
    for column_name in (
        "source_table_id",
        "semantic_model_version_id",
        "organization_id",
        "metric_id",
        "measure_column_id",
        "default_time_column_id",
    ):
        op.drop_index(
            op.f(f"ix_semantic_metric_version_{column_name}"),
            table_name="semantic_metric_version",
        )
    op.drop_table("semantic_metric_version")
    op.drop_index(op.f("ix_semantic_metric_project_id"), table_name="semantic_metric")
    op.drop_index(op.f("ix_semantic_metric_organization_id"), table_name="semantic_metric")
    op.drop_table("semantic_metric")
    op.drop_index("ix_semantic_model_org_status", table_name="semantic_model_version")
    op.drop_index(op.f("ix_semantic_model_version_project_id"), table_name="semantic_model_version")
    op.drop_index(
        op.f("ix_semantic_model_version_organization_id"), table_name="semantic_model_version"
    )
    op.drop_index(
        op.f("ix_semantic_model_version_based_on_version_id"),
        table_name="semantic_model_version",
    )
    op.drop_table("semantic_model_version")
