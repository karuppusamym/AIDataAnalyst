"""auditable agent runs

Revision ID: f4a35c86d901
Revises: 8c7d4b91e2fa
Create Date: 2026-08-25 01:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a35c86d901"
down_revision: str | Sequence[str] | None = "8c7d4b91e2fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("question_hash", sa.String(length=64), nullable=False),
        sa.Column("generation_source", sa.String(length=50), nullable=False),
        sa.Column("model_route", sa.String(length=255), nullable=True),
        sa.Column("semantic_version", sa.String(length=100), nullable=True),
        sa.Column("policy_version", sa.String(length=100), nullable=False),
        sa.Column("query_execution_id", sa.Uuid(), nullable=True),
        sa.Column("step_trace", sa.JSON(), nullable=False),
        sa.Column("failure_reason", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_agent_run_datasource_id_datasource"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_agent_run_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["query_execution_id"],
            ["query_execution.id"],
            name=op.f("fk_agent_run_query_execution_id_query_execution"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_run")),
    )
    op.create_index(
        op.f("ix_agent_run_datasource_id"), "agent_run", ["datasource_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_run_organization_id"),
        "agent_run",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_run_query_execution_id"),
        "agent_run",
        ["query_execution_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_run_org_created",
        "agent_run",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_run_org_status",
        "agent_run",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_org_status", table_name="agent_run")
    op.drop_index("ix_agent_run_org_created", table_name="agent_run")
    op.drop_index(op.f("ix_agent_run_query_execution_id"), table_name="agent_run")
    op.drop_index(op.f("ix_agent_run_organization_id"), table_name="agent_run")
    op.drop_index(op.f("ix_agent_run_datasource_id"), table_name="agent_run")
    op.drop_table("agent_run")
