"""SM-4: metric suggestions from approved annotations

Revision ID: b799d3cd61f6
Revises: bb909675ad3c
Create Date: 2026-08-31 07:01:22.470674
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b799d3cd61f6"
down_revision: str | Sequence[str] | None = "bb909675ad3c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_metric_proposal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("measure_column_id", sa.Uuid(), nullable=False),
        sa.Column("source_annotation_id", sa.Uuid(), nullable=False),
        sa.Column("proposed_slug", sa.String(length=100), nullable=False),
        sa.Column("proposed_name", sa.String(length=200), nullable=False),
        sa.Column("proposed_description", sa.Text(), nullable=False),
        sa.Column("proposed_aggregation", sa.String(length=30), nullable=False),
        sa.Column("proposed_grain", sa.String(length=1000), nullable=False),
        sa.Column("accuracy_score", sa.Float(), nullable=False),
        sa.Column("clarity_score", sa.Float(), nullable=False),
        sa.Column("style_score", sa.Float(), nullable=False),
        sa.Column("completeness_score", sa.Float(), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("published_metric_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["governance_review_id"],
            ["governance_review.id"],
            name=op.f("fk_semantic_metric_proposal_governance_review_id_governance_review"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["measure_column_id"],
            ["metadata_column.id"],
            name=op.f("fk_semantic_metric_proposal_measure_column_id_metadata_column"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_semantic_metric_proposal_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_semantic_metric_proposal_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_metric_version_id"],
            ["semantic_metric_version.id"],
            name=op.f(
                "fk_semantic_metric_proposal_published_metric_version_id_semantic_metric_ver"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_annotation_id"],
            ["metadata_business_annotation.id"],
            name=op.f(
                "fk_semantic_metric_proposal_source_annotation_id_metadata_business_annotatio"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_semantic_metric_proposal_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_metric_proposal")),
        sa.UniqueConstraint(
            "governance_review_id",
            name=op.f("uq_semantic_metric_proposal_governance_review_id"),
        ),
        sa.UniqueConstraint(
            "table_id",
            "measure_column_id",
            "source_annotation_id",
            name="uq_semantic_metric_proposal_evidence",
        ),
    )
    op.create_index(
        "ix_semantic_metric_proposal_org_status",
        "semantic_metric_proposal",
        ["organization_id", "status"],
    )
    op.create_index(
        op.f("ix_semantic_metric_proposal_measure_column_id"),
        "semantic_metric_proposal",
        ["measure_column_id"],
    )
    op.create_index(
        op.f("ix_semantic_metric_proposal_organization_id"),
        "semantic_metric_proposal",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_semantic_metric_proposal_project_id"),
        "semantic_metric_proposal",
        ["project_id"],
    )
    op.create_index(
        op.f("ix_semantic_metric_proposal_published_metric_version_id"),
        "semantic_metric_proposal",
        ["published_metric_version_id"],
    )
    op.create_index(
        op.f("ix_semantic_metric_proposal_source_annotation_id"),
        "semantic_metric_proposal",
        ["source_annotation_id"],
    )
    op.create_index(
        op.f("ix_semantic_metric_proposal_table_id"),
        "semantic_metric_proposal",
        ["table_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_semantic_metric_proposal_table_id"), table_name="semantic_metric_proposal"
    )
    op.drop_index(
        op.f("ix_semantic_metric_proposal_source_annotation_id"),
        table_name="semantic_metric_proposal",
    )
    op.drop_index(
        op.f("ix_semantic_metric_proposal_published_metric_version_id"),
        table_name="semantic_metric_proposal",
    )
    op.drop_index(
        op.f("ix_semantic_metric_proposal_project_id"), table_name="semantic_metric_proposal"
    )
    op.drop_index(
        op.f("ix_semantic_metric_proposal_organization_id"),
        table_name="semantic_metric_proposal",
    )
    op.drop_index(
        op.f("ix_semantic_metric_proposal_measure_column_id"),
        table_name="semantic_metric_proposal",
    )
    op.drop_index(
        "ix_semantic_metric_proposal_org_status", table_name="semantic_metric_proposal"
    )
    op.drop_table("semantic_metric_proposal")
