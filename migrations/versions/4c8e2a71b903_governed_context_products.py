"""governed context products

Revision ID: 4c8e2a71b903
Revises: 04003a3d6945
Create Date: 2026-08-29 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c8e2a71b903"
down_revision: str | Sequence[str] | None = "04003a3d6945"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_product",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("product_key", sa.String(length=100), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_context_product_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            name=op.f("fk_context_product_project_id_project"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_product")),
        sa.UniqueConstraint(
            "organization_id",
            "product_key",
            name="uq_context_product_organization_id_product_key",
        ),
    )
    op.create_index(
        op.f("ix_context_product_organization_id"),
        "context_product",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_context_product_project_id"),
        "context_product",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_context_product_project_status",
        "context_product",
        ["project_id", "lifecycle_status"],
        unique=False,
    )

    op.create_table(
        "context_product_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("purpose", sa.String(length=1000), nullable=False),
        sa.Column("owner_principal", sa.String(length=255), nullable=False),
        sa.Column("table_ids", sa.JSON(), nullable=False),
        sa.Column("semantic_model_version_ids", sa.JSON(), nullable=False),
        sa.Column("glossary_term_version_ids", sa.JSON(), nullable=False),
        sa.Column("eligible_tool_version_ids", sa.JSON(), nullable=False),
        sa.Column("allowed_consumer_roles", sa.JSON(), nullable=False),
        sa.Column("lineage_depth", sa.Integer(), nullable=False),
        sa.Column("quality_requirements", sa.JSON(), nullable=False),
        sa.Column("policy_summary", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("based_on_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["based_on_version_id"],
            ["context_product_version.id"],
            name=op.f(
                "fk_context_product_version_based_on_version_id_context_product_version"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_context_product_version_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["context_product.id"],
            name=op.f("fk_context_product_version_product_id_context_product"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_product_version")),
        sa.UniqueConstraint(
            "product_id",
            "version",
            name="uq_context_product_version_product_id_version",
        ),
    )
    op.create_index(
        op.f("ix_context_product_version_organization_id"),
        "context_product_version",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_context_product_version_product_id"),
        "context_product_version",
        ["product_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_context_product_version_based_on_version_id"),
        "context_product_version",
        ["based_on_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_context_product_version_org_status",
        "context_product_version",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_context_product_version_org_status", table_name="context_product_version"
    )
    op.drop_index(
        op.f("ix_context_product_version_based_on_version_id"),
        table_name="context_product_version",
    )
    op.drop_index(
        op.f("ix_context_product_version_product_id"), table_name="context_product_version"
    )
    op.drop_index(
        op.f("ix_context_product_version_organization_id"),
        table_name="context_product_version",
    )
    op.drop_table("context_product_version")
    op.drop_index("ix_context_product_project_status", table_name="context_product")
    op.drop_index(op.f("ix_context_product_project_id"), table_name="context_product")
    op.drop_index(op.f("ix_context_product_organization_id"), table_name="context_product")
    op.drop_table("context_product")
