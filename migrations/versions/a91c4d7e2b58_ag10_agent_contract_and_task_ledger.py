"""AG-10 agent contract, AgentRun attribution and the agent task ledger

Revision ID: a91c4d7e2b58
Revises: d5b2e4f7a9c1
Create Date: 2026-09-04 15:30:00.000000

`Docs/00-product/08-market-deep-dive-and-target-architecture-2026-09.md`
section 4.2 ("The agent contract"). Three additive changes:

1. `agent_contract` -- the governed declaration for one `AGENT`-kind
   `AiAssetVersion`: its own workload identity, capability envelope,
   autonomy tier, budget caps, eval gate, supervisor persona and kill
   scope. One row per agent version (unique constraint).
2. `agent_run.ai_asset_version_id` -- the attribution link
   `aida.agent_roster` documents as missing, so a run can be traced to the
   registered agent that produced it. Nullable, `SET NULL` on delete: every
   existing run stays valid and unlinked, and the roster keeps reporting
   those organization-wide.
3. `agent_task` -- the ledger of agent work units, carrying the
   deterministic sampled-for-audit decision and its human outcome.

All three are additive and carry no backfill: no existing row changes
meaning, and with no contract written the platform behaves exactly as it
did before (a run that names no agent version is unconstrained by the
envelope, which is the pre-AG-10 contract).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a91c4d7e2b58"
down_revision: str | Sequence[str] | None = "d5b2e4f7a9c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_contract",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ai_asset_version_id", sa.Uuid(), nullable=False),
        sa.Column("agent_principal_id", sa.String(length=255), nullable=False),
        sa.Column("capability_envelope", sa.JSON(), nullable=False),
        sa.Column("autonomy_tier", sa.String(length=2), nullable=False),
        sa.Column("daily_token_cap", sa.Integer(), nullable=True),
        sa.Column("per_run_token_cap", sa.Integer(), nullable=True),
        sa.Column("wall_clock_seconds_cap", sa.Integer(), nullable=True),
        sa.Column("eval_gate_threshold", sa.Float(), nullable=True),
        sa.Column("supervisor_persona", sa.String(length=20), nullable=False),
        sa.Column("kill_scope", sa.String(length=10), nullable=False),
        sa.Column("kill_engaged", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sampling_rate", sa.Float(), nullable=False, server_default="0.05"),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["ai_asset_version_id"], ["ai_asset_version.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ai_asset_version_id", name="uq_agent_contract_ai_asset_version_id"),
        sa.CheckConstraint(
            "autonomy_tier IN ('T0', 'T1', 'T2', 'T3')",
            name="ck_agent_contract_autonomy_tier",
        ),
        sa.CheckConstraint(
            "supervisor_persona IN ('ANALYST', 'CONSUMER', 'STEWARD', 'REVIEWER', "
            "'OPERATOR', 'AUDITOR')",
            name="ck_agent_contract_supervisor_persona",
        ),
        sa.CheckConstraint(
            "kill_scope IN ('AGENT', 'TIER', 'ALL')", name="ck_agent_contract_kill_scope"
        ),
        sa.CheckConstraint("sampling_rate >= 0.05", name="ck_agent_contract_sampling_rate_floor"),
    )
    op.create_index(
        "ix_agent_contract_organization_id", "agent_contract", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_agent_contract_org_principal",
        "agent_contract",
        ["organization_id", "agent_principal_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_contract_org_kill",
        "agent_contract",
        ["organization_id", "kill_engaged"],
        unique=False,
    )

    op.add_column("agent_run", sa.Column("ai_asset_version_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_agent_run_ai_asset_version_id", "agent_run", ["ai_asset_version_id"], unique=False
    )
    op.create_foreign_key(
        "fk_agent_run_ai_asset_version_id",
        "agent_run",
        "ai_asset_version",
        ["ai_asset_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "agent_task",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ai_asset_version_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("agent_principal_id", sa.String(length=255), nullable=False),
        sa.Column("intent", sa.String(length=100), nullable=False),
        sa.Column("inputs_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("proposal_ref_type", sa.String(length=100), nullable=True),
        sa.Column("proposal_ref_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("sampled_for_audit", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("audit_outcome", sa.String(length=20), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["ai_asset_version_id"], ["ai_asset_version.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_run.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('PROPOSED', 'APPLIED', 'REJECTED', 'FAILED', 'SAMPLED')",
            name="ck_agent_task_status",
        ),
        sa.CheckConstraint(
            "audit_outcome IS NULL OR audit_outcome IN ('PENDING', 'AGREED', 'DISAGREED')",
            name="ck_agent_task_audit_outcome",
        ),
    )
    op.create_index(
        "ix_agent_task_organization_id", "agent_task", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_agent_task_ai_asset_version_id", "agent_task", ["ai_asset_version_id"], unique=False
    )
    op.create_index("ix_agent_task_agent_run_id", "agent_task", ["agent_run_id"], unique=False)
    op.create_index(
        "ix_agent_task_org_started", "agent_task", ["organization_id", "started_at"], unique=False
    )
    op.create_index(
        "ix_agent_task_org_status", "agent_task", ["organization_id", "status"], unique=False
    )
    op.create_index(
        "ix_agent_task_org_sampled",
        "agent_task",
        ["organization_id", "sampled_for_audit"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_task_org_sampled", table_name="agent_task")
    op.drop_index("ix_agent_task_org_status", table_name="agent_task")
    op.drop_index("ix_agent_task_org_started", table_name="agent_task")
    op.drop_index("ix_agent_task_agent_run_id", table_name="agent_task")
    op.drop_index("ix_agent_task_ai_asset_version_id", table_name="agent_task")
    op.drop_index("ix_agent_task_organization_id", table_name="agent_task")
    op.drop_table("agent_task")

    op.drop_constraint("fk_agent_run_ai_asset_version_id", "agent_run", type_="foreignkey")
    op.drop_index("ix_agent_run_ai_asset_version_id", table_name="agent_run")
    op.drop_column("agent_run", "ai_asset_version_id")

    op.drop_index("ix_agent_contract_org_kill", table_name="agent_contract")
    op.drop_index("ix_agent_contract_org_principal", table_name="agent_contract")
    op.drop_index("ix_agent_contract_organization_id", table_name="agent_contract")
    op.drop_table("agent_contract")
