"""AG-10 extension: reviewed, eval-gated agent contract requests

Revision ID: 69702d37d798
Revises: d9a3f61c2b07
Create Date: 2026-09-05 12:00:00.000000

`agent_contract_request` -- a self-service extension of AG-10's agent
contract. `AgentContract` today is written directly by a trusted role
(`PlatformAdmin`/`AgentDeveloper`/`ModelRiskManager`) with no review and no
evaluation check; that write path is unchanged and stays available for
corrections. This table adds a second, *reviewed* path: a requested contract
definition is submitted, decided through the existing `governance_review`
maker-checker queue (one new `object_type` value, no schema change to that
table), and only written to `agent_contract` if the decision is APPROVE *and*
the AT-8/N17 evaluation gate (`aida.agent_eval_gate.compute_agent_eval_gate`)
currently shows PASS for the target agent version -- checked live, at
decision time, by `semantic_api._apply_governance_review_decision`'s new
`AGENT_CONTRACT_REQUEST` branch.

Purely additive: no existing table, column or row changes meaning.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "69702d37d798"
down_revision: str | Sequence[str] | None = "d9a3f61c2b07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_contract_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ai_asset_version_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("eval_gate_verdict", sa.String(length=20), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["ai_asset_version_id"], ["ai_asset_version.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["governance_review_id"], ["governance_review.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ACTIVATED', 'REJECTED', 'EVAL_BLOCKED')",
            name="ck_agent_contract_request_status",
        ),
    )
    op.create_index(
        "ix_agent_contract_request_organization_id",
        "agent_contract_request",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_agent_contract_request_org_status",
        "agent_contract_request",
        ["organization_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_agent_contract_request_version",
        "agent_contract_request",
        ["ai_asset_version_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_contract_request_version", table_name="agent_contract_request")
    op.drop_index("ix_agent_contract_request_org_status", table_name="agent_contract_request")
    op.drop_index(
        "ix_agent_contract_request_organization_id", table_name="agent_contract_request"
    )
    op.drop_table("agent_contract_request")
