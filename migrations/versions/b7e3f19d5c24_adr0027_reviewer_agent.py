"""ADR-0027 reviewer agent: pre-review columns, audit samples, suspension state

Revision ID: b7e3f19d5c24
Revises: a91c4d7e2b58
Create Date: 2026-09-04 18:00:00.000000

Three additive changes, none of which alters an existing row's meaning:

1. Six nullable pre-review columns on ``governance_review``. A review that
   has never been pre-reviewed keeps exactly its pre-ADR-0027 shape, and
   every read site treats NULL as "no recommendation".
2. ``review_audit_sample`` -- the ledger of agent decisions the deterministic
   sampler routed to a human (ADR-0027 condition (b)). Unique per review, so
   one decision cannot be sampled twice.
3. ``reviewer_agent_state`` -- per-organization suspension (condition (c)),
   so one human action stops one tenant's agent decisions without a
   deployment and without touching any other tenant.

With ``AIDA_REVIEWER_AGENT_ENABLED`` false -- the default -- nothing writes
to any of this and every review item waits for a human, exactly as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e3f19d5c24"
down_revision: str | Sequence[str] | None = "a91c4d7e2b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("governance_review", sa.Column("risk_tier", sa.String(length=2), nullable=True))
    op.add_column(
        "governance_review",
        sa.Column("pre_review_recommendation", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "governance_review", sa.Column("pre_review_confidence", sa.Float(), nullable=True)
    )
    op.add_column("governance_review", sa.Column("pre_review_evidence", sa.JSON(), nullable=True))
    op.add_column(
        "governance_review",
        sa.Column("pre_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "governance_review", sa.Column("pre_reviewed_by", sa.String(length=255), nullable=True)
    )
    op.create_index(
        "ix_governance_review_org_pre_reviewed",
        "governance_review",
        ["organization_id", "pre_reviewed_at"],
        unique=False,
    )

    op.create_table(
        "review_audit_sample",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("governance_review_id", sa.Uuid(), nullable=False),
        sa.Column("agent_principal_id", sa.String(length=255), nullable=False),
        sa.Column("object_type", sa.String(length=100), nullable=False),
        sa.Column("risk_tier", sa.String(length=2), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "human_outcome", sa.String(length=20), nullable=False, server_default="PENDING"
        ),
        sa.Column("human_principal_id", sa.String(length=255), nullable=True),
        sa.Column("human_rationale", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["governance_review_id"], ["governance_review.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("governance_review_id", name="uq_review_audit_sample_review"),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')", name="ck_review_audit_sample_decision"
        ),
        sa.CheckConstraint(
            "human_outcome IN ('PENDING', 'AGREED', 'DISAGREED')",
            name="ck_review_audit_sample_outcome",
        ),
    )
    op.create_index(
        "ix_review_audit_sample_organization_id",
        "review_audit_sample",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_review_audit_sample_org_outcome",
        "review_audit_sample",
        ["organization_id", "human_outcome"],
        unique=False,
    )

    op.create_table(
        "reviewer_agent_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("suspended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("suspended_by", sa.String(length=255), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspension_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", name="uq_reviewer_agent_state_org"),
    )


def downgrade() -> None:
    op.drop_table("reviewer_agent_state")
    op.drop_index("ix_review_audit_sample_org_outcome", table_name="review_audit_sample")
    op.drop_index("ix_review_audit_sample_organization_id", table_name="review_audit_sample")
    op.drop_table("review_audit_sample")
    op.drop_index("ix_governance_review_org_pre_reviewed", table_name="governance_review")
    op.drop_column("governance_review", "pre_reviewed_by")
    op.drop_column("governance_review", "pre_reviewed_at")
    op.drop_column("governance_review", "pre_review_evidence")
    op.drop_column("governance_review", "pre_review_confidence")
    op.drop_column("governance_review", "pre_review_recommendation")
    op.drop_column("governance_review", "risk_tier")
