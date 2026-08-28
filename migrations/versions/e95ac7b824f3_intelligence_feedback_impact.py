"""query feedback memory and governed relationship intelligence

Revision ID: e95ac7b824f3
Revises: d84fb6a713e2
Create Date: 2026-08-25 08:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e95ac7b824f3"
down_revision: str | Sequence[str] | None = "d84fb6a713e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "governed_tool_version",
        sa.Column("referenced_tables", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "query_memory_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("query_execution_id", sa.Uuid(), nullable=False),
        sa.Column("question_hash", sa.String(length=64), nullable=False),
        sa.Column("sql_hash", sa.String(length=64), nullable=False),
        sa.Column("semantic_version", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("positive_feedback_count", sa.Integer(), nullable=False),
        sa.Column("negative_feedback_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["query_execution_id"], ["query_execution.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id"),
    )
    for column in ("organization_id", "datasource_id", "agent_run_id", "query_execution_id"):
        op.create_index(
            op.f(f"ix_query_memory_evidence_{column}"),
            "query_memory_evidence",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_query_memory_lookup",
        "query_memory_evidence",
        ["organization_id", "datasource_id", "question_hash"],
        unique=False,
    )
    op.create_table(
        "query_feedback",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("rating", sa.String(length=30), nullable=False),
        sa.Column("comment_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id", "principal_id"),
    )
    op.create_index(
        op.f("ix_query_feedback_organization_id"),
        "query_feedback",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_query_feedback_agent_run_id"),
        "query_feedback",
        ["agent_run_id"],
        unique=False,
    )
    op.create_table(
        "relationship_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("source_table_id", sa.Uuid(), nullable=False),
        sa.Column("source_column_id", sa.Uuid(), nullable=False),
        sa.Column("target_table_id", sa.Uuid(), nullable=False),
        sa.Column("target_column_id", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_column_id"], ["metadata_column.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_column_id"], ["metadata_column.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_column_id",
            "target_column_id",
            name="uq_relationship_candidate_columns",
        ),
    )
    for column in (
        "organization_id",
        "datasource_id",
        "source_table_id",
        "source_column_id",
        "target_table_id",
        "target_column_id",
    ):
        op.create_index(
            op.f(f"ix_relationship_candidate_{column}"),
            "relationship_candidate",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_relationship_candidate_org_status",
        "relationship_candidate",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_relationship_candidate_org_status", table_name="relationship_candidate")
    for column in reversed(
        (
            "organization_id",
            "datasource_id",
            "source_table_id",
            "source_column_id",
            "target_table_id",
            "target_column_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_relationship_candidate_{column}"),
            table_name="relationship_candidate",
        )
    op.drop_table("relationship_candidate")
    op.drop_index(op.f("ix_query_feedback_agent_run_id"), table_name="query_feedback")
    op.drop_index(op.f("ix_query_feedback_organization_id"), table_name="query_feedback")
    op.drop_table("query_feedback")
    op.drop_index("ix_query_memory_lookup", table_name="query_memory_evidence")
    for column in reversed(
        ("organization_id", "datasource_id", "agent_run_id", "query_execution_id")
    ):
        op.drop_index(
            op.f(f"ix_query_memory_evidence_{column}"),
            table_name="query_memory_evidence",
        )
    op.drop_table("query_memory_evidence")
    op.drop_column("governed_tool_version", "referenced_tables")
