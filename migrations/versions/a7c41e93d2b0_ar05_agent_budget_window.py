"""AR-05: agent contract budget window

Gives `AgentContract.daily_token_cap` a runtime consumer. See
`aida.agent_budget` for why a table exists rather than a `SUM(...)` over
`agent_run`: a read-then-check cannot bound concurrent runs, and the cap has
to live inside the `WHERE` of a conditional UPDATE for the database to be the
thing that decides who fits.

Revision ID: a7c41e93d2b0
Revises: c3f0a71d5e94
Create Date: 2026-09-09 22:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c41e93d2b0"
down_revision: str | Sequence[str] | None = "c3f0a71d5e94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_budget_window",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ai_asset_version_id", sa.Uuid(), nullable=False),
        sa.Column("window_date", sa.Date(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["ai_asset_version_id"],
            ["ai_asset_version.id"],
            name=op.f("fk_agent_budget_window_ai_asset_version_id_ai_asset_version"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_agent_budget_window_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_budget_window")),
        sa.UniqueConstraint(
            "organization_id",
            "ai_asset_version_id",
            "window_date",
            name="uq_agent_budget_window_scope",
        ),
        sa.CheckConstraint("reserved_tokens >= 0", name="ck_agent_budget_window_non_negative"),
    )
    op.create_index(
        op.f("ix_agent_budget_window_organization_id"),
        "agent_budget_window",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_agent_budget_window_ai_asset_version_id"),
        "agent_budget_window",
        ["ai_asset_version_id"],
    )
    op.create_index(
        "ix_agent_budget_window_org_date",
        "agent_budget_window",
        ["organization_id", "window_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_budget_window_org_date", table_name="agent_budget_window")
    op.drop_index(
        op.f("ix_agent_budget_window_ai_asset_version_id"), table_name="agent_budget_window"
    )
    op.drop_index(
        op.f("ix_agent_budget_window_organization_id"), table_name="agent_budget_window"
    )
    op.drop_table("agent_budget_window")
