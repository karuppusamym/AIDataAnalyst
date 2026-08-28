"""governed tool registry

Revision ID: c71a9e5f204d
Revises: b91e7d2a45c8
Create Date: 2026-08-25 06:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c71a9e5f204d"
down_revision: str | Sequence[str] | None = "b91e7d2a45c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "governed_tool",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_governed_tool_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_governed_tool_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governed_tool")),
        sa.UniqueConstraint("project_id", "slug", name=op.f("uq_governed_tool_project_id")),
    )
    op.create_index(
        op.f("ix_governed_tool_organization_id"),
        "governed_tool",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_governed_tool_project_id"),
        "governed_tool",
        ["project_id"],
        unique=False,
    )

    op.create_table(
        "governed_tool_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("tool_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("semantic_model_version_id", sa.Uuid(), nullable=True),
        sa.Column("sql_template", sa.Text(), nullable=False),
        sa.Column("parameter_schema", sa.JSON(), nullable=False),
        sa.Column("allowed_roles", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_governed_tool_version_datasource_id_datasource"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_governed_tool_version_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["semantic_model_version_id"],
            ["semantic_model_version.id"],
            name=op.f("fk_governed_tool_version_semantic_model_version_id_semantic_model_version"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_id"],
            ["governed_tool.id"],
            name=op.f("fk_governed_tool_version_tool_id_governed_tool"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_governed_tool_version")),
        sa.UniqueConstraint("tool_id", "version", name=op.f("uq_governed_tool_version_tool_id")),
    )
    for column_name in (
        "datasource_id",
        "organization_id",
        "semantic_model_version_id",
        "tool_id",
    ):
        op.create_index(
            op.f(f"ix_governed_tool_version_{column_name}"),
            "governed_tool_version",
            [column_name],
            unique=False,
        )

    op.create_table(
        "tool_execution",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("tool_version_id", sa.Uuid(), nullable=False),
        sa.Column("query_execution_id", sa.Uuid(), nullable=True),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("parameter_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("error_message", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_tool_execution_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["query_execution_id"],
            ["query_execution.id"],
            name=op.f("fk_tool_execution_query_execution_id_query_execution"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["tool_version_id"],
            ["governed_tool_version.id"],
            name=op.f("fk_tool_execution_tool_version_id_governed_tool_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_execution")),
    )
    op.create_index(
        op.f("ix_tool_execution_organization_id"),
        "tool_execution",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_tool_execution_query_execution_id"),
        "tool_execution",
        ["query_execution_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_tool_execution_tool_version_id"),
        "tool_execution",
        ["tool_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_tool_execution_org_created",
        "tool_execution",
        ["organization_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tool_execution_org_created", table_name="tool_execution")
    op.drop_index(op.f("ix_tool_execution_tool_version_id"), table_name="tool_execution")
    op.drop_index(op.f("ix_tool_execution_query_execution_id"), table_name="tool_execution")
    op.drop_index(op.f("ix_tool_execution_organization_id"), table_name="tool_execution")
    op.drop_table("tool_execution")
    for column_name in (
        "tool_id",
        "semantic_model_version_id",
        "organization_id",
        "datasource_id",
    ):
        op.drop_index(
            op.f(f"ix_governed_tool_version_{column_name}"),
            table_name="governed_tool_version",
        )
    op.drop_table("governed_tool_version")
    op.drop_index(op.f("ix_governed_tool_project_id"), table_name="governed_tool")
    op.drop_index(op.f("ix_governed_tool_organization_id"), table_name="governed_tool")
    op.drop_table("governed_tool")
