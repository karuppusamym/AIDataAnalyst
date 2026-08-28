"""governed agent retrieval planning and evaluations

Revision ID: a7c4e2d91b60
Revises: f16bd8c935a4
Create Date: 2026-08-25 13:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4e2d91b60"
down_revision: str | Sequence[str] | None = "f16bd8c935a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_run",
        sa.Column("retrieval_evidence", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "agent_run",
        sa.Column("plan_evidence", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "agent_run",
        sa.Column("recommended_tool_version_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_agent_run_recommended_tool_version_id_governed_tool_version"),
        "agent_run",
        "governed_tool_version",
        ["recommended_tool_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_agent_run_recommended_tool_version_id"),
        "agent_run",
        ["recommended_tool_version_id"],
        unique=False,
    )
    op.create_table(
        "agent_evaluation_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("suite_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("scenario_count", sa.Integer(), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("pass_rate", sa.Float(), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_agent_evaluation_run_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_evaluation_run")),
    )
    op.create_index(
        op.f("ix_agent_evaluation_run_organization_id"),
        "agent_evaluation_run",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_evaluation_org_created",
        "agent_evaluation_run",
        ["organization_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_evaluation_org_created", table_name="agent_evaluation_run")
    op.drop_index(
        op.f("ix_agent_evaluation_run_organization_id"),
        table_name="agent_evaluation_run",
    )
    op.drop_table("agent_evaluation_run")
    op.drop_index(op.f("ix_agent_run_recommended_tool_version_id"), table_name="agent_run")
    op.drop_constraint(
        op.f("fk_agent_run_recommended_tool_version_id_governed_tool_version"),
        "agent_run",
        type_="foreignkey",
    )
    op.drop_column("agent_run", "recommended_tool_version_id")
    op.drop_column("agent_run", "plan_evidence")
    op.drop_column("agent_run", "retrieval_evidence")
