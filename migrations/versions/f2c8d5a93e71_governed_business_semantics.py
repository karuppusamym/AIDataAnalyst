"""governed business semantic inference

Revision ID: f2c8d5a93e71
Revises: e4b7c2a91d35
Create Date: 2026-08-27 15:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2c8d5a93e71"
down_revision: str | Sequence[str] | None = "e4b7c2a91d35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_inference_run",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("engine_mode", sa.String(length=30), nullable=False),
        sa.Column("engine_version", sa.String(length=100), nullable=False),
        sa.Column("model_route", sa.String(length=255), nullable=True),
        sa.Column("table_count", sa.Integer(), nullable=False),
        sa.Column("proposal_count", sa.Integer(), nullable=False),
        sa.Column("model_enriched_count", sa.Integer(), nullable=False),
        sa.Column("rule_only_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"],
            ["analysis_run.id"],
            name=op.f("fk_semantic_inference_run_analysis_run_id_analysis_run"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_semantic_inference_run_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_semantic_inference_run_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_inference_run")),
    )
    for column in ("organization_id", "datasource_id", "analysis_run_id"):
        op.create_index(
            op.f(f"ix_semantic_inference_run_{column}"),
            "semantic_inference_run",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_semantic_inference_org_created",
        "semantic_inference_run",
        ["organization_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "business_domain",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("domain_key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_business_domain_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_business_domain")),
        sa.UniqueConstraint(
            "organization_id",
            "domain_key",
            name=op.f("uq_business_domain_organization_id"),
        ),
    )
    op.create_index(
        op.f("ix_business_domain_organization_id"),
        "business_domain",
        ["organization_id"],
        unique=False,
    )

    op.create_table(
        "business_entity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("entity_key", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["business_domain.id"],
            name=op.f("fk_business_entity_domain_id_business_domain"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_business_entity_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_business_entity")),
        sa.UniqueConstraint("domain_id", "entity_key", name=op.f("uq_business_entity_domain_id")),
    )
    op.create_index(
        op.f("ix_business_entity_organization_id"),
        "business_entity",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_business_entity_domain_id"),
        "business_entity",
        ["domain_id"],
        unique=False,
    )

    op.create_table(
        "metadata_enrichment_proposal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("inference_run_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("governance_review_id", sa.Uuid(), nullable=False),
        sa.Column("proposal_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("engine_type", sa.String(length=30), nullable=False),
        sa.Column("engine_version", sa.String(length=100), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("proposed_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("review_reason", sa.String(length=2000), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_tool_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_enrichment_proposal_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["governance_review_id"],
            ["governance_review.id"],
            name=op.f("fk_metadata_enrichment_proposal_governance_review_id_governance_review"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["inference_run_id"],
            ["semantic_inference_run.id"],
            name=op.f("fk_metadata_enrichment_proposal_inference_run_id_semantic_inference_run"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_enrichment_proposal_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["promoted_tool_version_id"],
            ["governed_tool_version.id"],
            name=op.f(
                "fk_metadata_enrichment_proposal_promoted_tool_version_id_governed_tool_version"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_metadata_enrichment_proposal_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_enrichment_proposal")),
        sa.UniqueConstraint(
            "governance_review_id",
            name=op.f("uq_metadata_enrichment_proposal_governance_review_id"),
        ),
        sa.UniqueConstraint(
            "inference_run_id",
            "table_id",
            name=op.f("uq_metadata_enrichment_proposal_inference_run_id"),
        ),
    )
    for column in (
        "organization_id",
        "datasource_id",
        "inference_run_id",
        "table_id",
        "promoted_tool_version_id",
    ):
        op.create_index(
            op.f(f"ix_metadata_enrichment_proposal_{column}"),
            "metadata_enrichment_proposal",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_metadata_enrichment_org_status",
        "metadata_enrichment_proposal",
        ["organization_id", "status"],
        unique=False,
    )

    op.create_table(
        "metadata_business_annotation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("domain_id", sa.Uuid(), nullable=False),
        sa.Column("entity_id", sa.Uuid(), nullable=False),
        sa.Column("source_proposal_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("business_name", sa.String(length=255), nullable=False),
        sa.Column("business_description", sa.Text(), nullable=False),
        sa.Column("table_role", sa.String(length=50), nullable=False),
        sa.Column("grain_statement", sa.String(length=1000), nullable=False),
        sa.Column("synonyms", sa.JSON(), nullable=False),
        sa.Column("suggested_questions", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_metadata_business_annotation_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["business_domain.id"],
            name=op.f("fk_metadata_business_annotation_domain_id_business_domain"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["business_entity.id"],
            name=op.f("fk_metadata_business_annotation_entity_id_business_entity"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_metadata_business_annotation_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_proposal_id"],
            ["metadata_enrichment_proposal.id"],
            name=op.f(
                "fk_metadata_business_annotation_source_proposal_id_metadata_enrichment_proposal"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_metadata_business_annotation_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metadata_business_annotation")),
        sa.UniqueConstraint("table_id", name="uq_metadata_business_annotation_table_id"),
    )
    for column in ("organization_id", "datasource_id", "domain_id", "entity_id"):
        op.create_index(
            op.f(f"ix_metadata_business_annotation_{column}"),
            "metadata_business_annotation",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in reversed(("organization_id", "datasource_id", "domain_id", "entity_id")):
        op.drop_index(
            op.f(f"ix_metadata_business_annotation_{column}"),
            table_name="metadata_business_annotation",
        )
    op.drop_table("metadata_business_annotation")
    op.drop_index("ix_metadata_enrichment_org_status", table_name="metadata_enrichment_proposal")
    for column in reversed(
        (
            "organization_id",
            "datasource_id",
            "inference_run_id",
            "table_id",
            "promoted_tool_version_id",
        )
    ):
        op.drop_index(
            op.f(f"ix_metadata_enrichment_proposal_{column}"),
            table_name="metadata_enrichment_proposal",
        )
    op.drop_table("metadata_enrichment_proposal")
    op.drop_index(op.f("ix_business_entity_domain_id"), table_name="business_entity")
    op.drop_index(op.f("ix_business_entity_organization_id"), table_name="business_entity")
    op.drop_table("business_entity")
    op.drop_index(op.f("ix_business_domain_organization_id"), table_name="business_domain")
    op.drop_table("business_domain")
    op.drop_index("ix_semantic_inference_org_created", table_name="semantic_inference_run")
    for column in reversed(("organization_id", "datasource_id", "analysis_run_id")):
        op.drop_index(
            op.f(f"ix_semantic_inference_run_{column}"),
            table_name="semantic_inference_run",
        )
    op.drop_table("semantic_inference_run")
