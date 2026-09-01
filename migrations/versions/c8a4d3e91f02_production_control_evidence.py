"""production control evidence

Revision ID: c8a4d3e91f02
Revises: b4e8f2a71c90
Create Date: 2026-08-29 15:15:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8a4d3e91f02"
down_revision: str | None = "b4e8f2a71c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_product_access_request",
        sa.Column(
            "fulfillment_status",
            sa.String(length=30),
            nullable=False,
            server_default="NOT_REQUESTED",
        ),
    )
    op.add_column(
        "data_product_access_request",
        sa.Column("fulfillment_provider", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "data_product_access_request",
        sa.Column("fulfillment_reference", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "data_product_access_request",
        sa.Column("fulfillment_error", sa.String(length=1000), nullable=True),
    )
    op.add_column(
        "data_product_access_request",
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_data_product_access_fulfillment_status",
        "data_product_access_request",
        "fulfillment_status IN ('NOT_REQUESTED', 'PENDING', 'PROVISIONED', 'FAILED', 'REVOKED')",
    )

    op.create_table(
        "mcp_consumption_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("principal_type", sa.String(length=30), nullable=False),
        sa.Column("operation_kind", sa.String(length=30), nullable=False),
        sa.Column("method", sa.String(length=100), nullable=False),
        sa.Column("target_reference", sa.String(length=500), nullable=True),
        sa.Column("business_purpose", sa.String(length=200), nullable=True),
        sa.Column("correlation_id", sa.String(length=100), nullable=False),
        sa.Column("policy_decision", sa.String(length=30), nullable=False),
        sa.Column(
            "consumed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "operation_kind IN ('CONTROL', 'RESOURCE', 'PROMPT', 'TOOL')",
            name="ck_mcp_consumption_operation_kind",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_consumption_evidence_organization_id",
        "mcp_consumption_evidence",
        ["organization_id"],
    )
    op.create_index(
        "ix_mcp_consumption_evidence_correlation_id", "mcp_consumption_evidence", ["correlation_id"]
    )
    op.create_index(
        "ix_mcp_consumption_org_time",
        "mcp_consumption_evidence",
        ["organization_id", "consumed_at"],
    )
    op.create_index(
        "ix_mcp_consumption_principal_time",
        "mcp_consumption_evidence",
        ["principal_id", "consumed_at"],
    )

    op.create_table(
        "ai_trust_snapshot",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ai_asset_version_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("grade", sa.String(length=30), nullable=False),
        sa.Column("factors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blockers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("score >= 0 AND score <= 100", name="ck_ai_trust_snapshot_score"),
        sa.ForeignKeyConstraint(
            ["ai_asset_version_id"], ["ai_asset_version.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_trust_snapshot_organization_id", "ai_trust_snapshot", ["organization_id"]
    )
    op.create_index(
        "ix_ai_trust_snapshot_ai_asset_version_id", "ai_trust_snapshot", ["ai_asset_version_id"]
    )
    op.create_index(
        "ix_ai_trust_snapshot_version_time",
        "ai_trust_snapshot",
        ["ai_asset_version_id", "computed_at"],
    )

    op.create_table(
        "ai_remediation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ai_asset_version_id", sa.Uuid(), nullable=False),
        sa.Column("finding_key", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("owner_principal", sa.String(length=255), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="OPEN"),
        sa.Column(
            "resolution_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("resolved_by", sa.String(length=255), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'ACCEPTED_RISK')",
            name="ck_ai_remediation_status",
        ),
        sa.ForeignKeyConstraint(
            ["ai_asset_version_id"], ["ai_asset_version.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_remediation_organization_id", "ai_remediation", ["organization_id"])
    op.create_index(
        "ix_ai_remediation_ai_asset_version_id", "ai_remediation", ["ai_asset_version_id"]
    )
    op.create_index(
        "ix_ai_remediation_version_status", "ai_remediation", ["ai_asset_version_id", "status"]
    )


def downgrade() -> None:
    op.drop_table("ai_remediation")
    op.drop_table("ai_trust_snapshot")
    op.drop_table("mcp_consumption_evidence")
    op.drop_constraint(
        "ck_data_product_access_fulfillment_status",
        "data_product_access_request",
        type_="check",
    )
    op.drop_column("data_product_access_request", "fulfilled_at")
    op.drop_column("data_product_access_request", "fulfillment_error")
    op.drop_column("data_product_access_request", "fulfillment_reference")
    op.drop_column("data_product_access_request", "fulfillment_provider")
    op.drop_column("data_product_access_request", "fulfillment_status")
