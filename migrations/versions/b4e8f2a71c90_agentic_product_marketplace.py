"""agentic product marketplace and AI registry

Revision ID: b4e8f2a71c90
Revises: 9a6d4f21c8b7
Create Date: 2026-08-29 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4e8f2a71c90"
down_revision: str | Sequence[str] | None = "9a6d4f21c8b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column[sa.DateTime], sa.Column[sa.DateTime]]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    op.create_table(
        "data_product",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("product_key", sa.String(100), nullable=False),
        sa.Column("lifecycle_status", sa.String(30), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "lifecycle_status IN ('CANDIDATE', 'ACTIVE', 'RETIRED')",
            name="ck_data_product_lifecycle",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "organization_id", "product_key", name="uq_data_product_org_product_key"
        ),
    )
    op.create_index("ix_data_product_organization_id", "data_product", ["organization_id"])
    op.create_index("ix_data_product_project_id", "data_product", ["project_id"])
    op.create_index(
        "ix_data_product_project_lifecycle",
        "data_product",
        ["project_id", "lifecycle_status"],
    )

    op.create_table(
        "data_product_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("domain_name", sa.String(200), nullable=False),
        sa.Column("owner_principal", sa.String(255), nullable=False),
        sa.Column("usage_terms", sa.Text(), nullable=False),
        sa.Column("classification", sa.String(30), nullable=False),
        sa.Column("certification_status", sa.String(30), nullable=False),
        sa.Column("quality_score", sa.Integer(), nullable=True),
        sa.Column("lineage_coverage", sa.Integer(), nullable=False),
        sa.Column("context_product_version_id", sa.Uuid(), nullable=True),
        sa.Column("discoverable_roles", sa.JSON(), nullable=False),
        sa.Column("consumer_roles", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("version > 0", name="ck_data_product_version_positive"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'REVIEW_REQUIRED', 'PUBLISHED', 'SUPERSEDED', "
            "'REJECTED', 'RETIRED')",
            name="ck_data_product_version_status",
        ),
        sa.CheckConstraint(
            "quality_score IS NULL OR (quality_score >= 0 AND quality_score <= 100)",
            name="ck_data_product_quality_score",
        ),
        sa.CheckConstraint(
            "lineage_coverage >= 0 AND lineage_coverage <= 100",
            name="ck_data_product_lineage_coverage",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["data_product.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["context_product_version_id"],
            ["context_product_version.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id", "version", name="uq_data_product_version_product_version"
        ),
    )
    op.create_index(
        "ix_data_product_version_organization_id", "data_product_version", ["organization_id"]
    )
    op.create_index("ix_data_product_version_product_id", "data_product_version", ["product_id"])
    op.create_index(
        "ix_data_product_version_context_product_version_id",
        "data_product_version",
        ["context_product_version_id"],
    )
    op.create_index("ix_data_product_version_domain_name", "data_product_version", ["domain_name"])
    op.create_index(
        "ix_data_product_version_org_status",
        "data_product_version",
        ["organization_id", "status"],
    )
    op.create_index(
        "uq_data_product_version_one_published",
        "data_product_version",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PUBLISHED'"),
    )

    op.create_table(
        "data_product_port",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_product_version_id", sa.Uuid(), nullable=False),
        sa.Column("port_key", sa.String(100), nullable=False),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False),
        sa.Column("asset_type", sa.String(50), nullable=False),
        sa.Column("asset_id", sa.String(255), nullable=False),
        sa.CheckConstraint(
            "direction IN ('INPUT', 'OUTPUT')", name="ck_data_product_port_direction"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["data_product_version_id"], ["data_product_version.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "data_product_version_id", "port_key", name="uq_data_product_port_version_key"
        ),
    )
    op.create_index(
        "ix_data_product_port_organization_id", "data_product_port", ["organization_id"]
    )
    op.create_index(
        "ix_data_product_port_data_product_version_id",
        "data_product_port",
        ["data_product_version_id"],
    )
    op.create_index(
        "ix_data_product_port_org_asset",
        "data_product_port",
        ["organization_id", "asset_type", "asset_id"],
    )

    op.create_table(
        "data_product_role_binding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_product_version_id", sa.Uuid(), nullable=False),
        sa.Column("role_kind", sa.String(20), nullable=False),
        sa.Column("role_name", sa.String(100), nullable=False),
        sa.CheckConstraint(
            "role_kind IN ('DISCOVER', 'CONSUME')", name="ck_data_product_role_binding_kind"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["data_product_version_id"], ["data_product_version.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "data_product_version_id",
            "role_kind",
            "role_name",
            name="uq_data_product_role_binding_version_kind_role",
        ),
    )
    op.create_index(
        "ix_data_product_role_binding_organization_id",
        "data_product_role_binding",
        ["organization_id"],
    )
    op.create_index(
        "ix_data_product_role_binding_data_product_version_id",
        "data_product_role_binding",
        ["data_product_version_id"],
    )
    op.create_index(
        "ix_data_product_role_binding_lookup",
        "data_product_role_binding",
        ["organization_id", "role_kind", "role_name", "data_product_version_id"],
    )

    op.create_table(
        "data_contract_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("compatibility_mode", sa.String(30), nullable=False),
        sa.Column("compatibility_status", sa.String(30), nullable=False),
        sa.Column("compatibility_findings", sa.JSON(), nullable=False),
        sa.Column("schema_definition", sa.JSON(), nullable=False),
        sa.Column("quality_rules", sa.JSON(), nullable=False),
        sa.Column("freshness_sla_minutes", sa.Integer(), nullable=True),
        sa.Column("availability_sla_percent", sa.Float(), nullable=True),
        sa.Column("producer_principal", sa.String(255), nullable=False),
        sa.Column("consumer_roles", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("version > 0", name="ck_data_contract_version_positive"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'REVIEW_REQUIRED', 'PUBLISHED', 'SUPERSEDED', 'REJECTED')",
            name="ck_data_contract_version_status",
        ),
        sa.CheckConstraint(
            "compatibility_status IN ('INITIAL', 'COMPATIBLE', 'BREAKING')",
            name="ck_data_contract_compatibility_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["product_id"], ["data_product.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "product_id", "version", name="uq_data_contract_version_product_version"
        ),
    )
    op.create_index(
        "ix_data_contract_version_organization_id", "data_contract_version", ["organization_id"]
    )
    op.create_index("ix_data_contract_version_product_id", "data_contract_version", ["product_id"])
    op.create_index(
        "ix_data_contract_version_org_status",
        "data_contract_version",
        ["organization_id", "status"],
    )
    op.create_index(
        "uq_data_contract_version_one_published",
        "data_contract_version",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PUBLISHED'"),
    )

    op.create_table(
        "data_product_access_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_product_version_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("purpose", sa.String(2000), nullable=False),
        sa.Column("duration_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("governance_review_id", sa.Uuid(), nullable=False),
        sa.Column("decided_by", sa.String(255), nullable=True),
        sa.Column("decision_reason", sa.String(2000), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'REVOKED', 'EXPIRED')",
            name="ck_data_product_access_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["data_product_version_id"], ["data_product_version.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["governance_review_id"], ["governance_review.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("governance_review_id"),
    )
    op.create_index(
        "ix_data_product_access_request_organization_id",
        "data_product_access_request",
        ["organization_id"],
    )
    op.create_index(
        "ix_data_product_access_request_data_product_version_id",
        "data_product_access_request",
        ["data_product_version_id"],
    )
    op.create_index(
        "ix_data_product_access_request_expires_at",
        "data_product_access_request",
        ["expires_at"],
    )
    op.create_index(
        "ix_data_product_access_org_status",
        "data_product_access_request",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_data_product_access_requester_product",
        "data_product_access_request",
        ["organization_id", "requested_by", "data_product_version_id"],
    )
    op.create_index(
        "uq_data_product_access_one_pending",
        "data_product_access_request",
        ["data_product_version_id", "requested_by"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.create_table(
        "ai_asset",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("asset_key", sa.String(100), nullable=False),
        sa.Column("asset_kind", sa.String(30), nullable=False),
        sa.Column("lifecycle_status", sa.String(30), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "asset_kind IN ('AI_USE_CASE', 'MODEL', 'AGENT')", name="ck_ai_asset_kind"
        ),
        sa.CheckConstraint(
            "lifecycle_status IN ('ACTIVE', 'RETIRED')", name="ck_ai_asset_lifecycle"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "asset_key", name="uq_ai_asset_org_key"),
    )
    op.create_index("ix_ai_asset_organization_id", "ai_asset", ["organization_id"])
    op.create_index(
        "ix_ai_asset_org_kind_lifecycle",
        "ai_asset",
        ["organization_id", "asset_kind", "lifecycle_status"],
    )

    op.create_table(
        "ai_asset_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("intended_use", sa.Text(), nullable=False),
        sa.Column("owner_principal", sa.String(255), nullable=False),
        sa.Column("provider_type", sa.String(50), nullable=False),
        sa.Column("risk_tier", sa.String(30), nullable=False),
        sa.Column("documentation_url", sa.String(1000), nullable=True),
        sa.Column("context_product_version_ids", sa.JSON(), nullable=False),
        sa.Column("model_route_ids", sa.JSON(), nullable=False),
        sa.Column("policy_control_ids", sa.JSON(), nullable=False),
        sa.Column("evaluation_evidence", sa.JSON(), nullable=False),
        sa.Column("runtime_evidence", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("version > 0", name="ck_ai_asset_version_positive"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'REVIEW_REQUIRED', 'APPROVED', 'SUPERSEDED', "
            "'REJECTED', 'RETIRED')",
            name="ck_ai_asset_version_status",
        ),
        sa.CheckConstraint(
            "risk_tier IN ('LOW', 'MEDIUM', 'HIGH', 'PROHIBITED')",
            name="ck_ai_asset_risk_tier",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["asset_id"], ["ai_asset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", "version", name="uq_ai_asset_version_asset_version"),
    )
    op.create_index("ix_ai_asset_version_organization_id", "ai_asset_version", ["organization_id"])
    op.create_index("ix_ai_asset_version_asset_id", "ai_asset_version", ["asset_id"])
    op.create_index(
        "ix_ai_asset_version_org_status", "ai_asset_version", ["organization_id", "status"]
    )
    op.create_index(
        "uq_ai_asset_version_one_approved",
        "ai_asset_version",
        ["asset_id"],
        unique=True,
        postgresql_where=sa.text("status = 'APPROVED'"),
    )

    op.create_table(
        "ai_assessment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("ai_asset_version_id", sa.Uuid(), nullable=False),
        sa.Column("framework", sa.String(100), nullable=False),
        sa.Column("framework_version", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("control_results", sa.JSON(), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("assessed_by", sa.String(255), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("score >= 0 AND score <= 100", name="ck_ai_assessment_score"),
        sa.CheckConstraint(
            "status IN ('PASS', 'NEEDS_REMEDIATION', 'FAIL')",
            name="ck_ai_assessment_status",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["ai_asset_version_id"], ["ai_asset_version.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_assessment_organization_id", "ai_assessment", ["organization_id"])
    op.create_index(
        "ix_ai_assessment_ai_asset_version_id", "ai_assessment", ["ai_asset_version_id"]
    )
    op.create_index(
        "ix_ai_assessment_version_created",
        "ai_assessment",
        ["ai_asset_version_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("ai_assessment")
    op.drop_table("ai_asset_version")
    op.drop_table("ai_asset")
    op.drop_table("data_product_access_request")
    op.drop_table("data_contract_version")
    op.drop_table("data_product_role_binding")
    op.drop_table("data_product_port")
    op.drop_table("data_product_version")
    op.drop_table("data_product")
