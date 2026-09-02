"""studio: context product builder materialization evidence (ST-A7)

Revision ID: 7e6460d905fe
Revises: a1c7e4d2f9b3
Create Date: 2026-09-02 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7e6460d905fe"
down_revision: str | Sequence[str] | None = "a1c7e4d2f9b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "studio_context_product_materialization",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("change_set_id", sa.Uuid(), nullable=False),
        sa.Column("change_item_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=30), nullable=False),
        sa.Column("context_product_id", sa.Uuid(), nullable=False),
        sa.Column("context_product_version_id", sa.Uuid(), nullable=False),
        sa.Column("governance_review_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f(
                "fk_studio_context_product_materialization_organization_id_organization"
            ),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["change_set_id"],
            ["studio_change_set.id"],
            name=op.f(
                "fk_studio_context_product_materialization_change_set_id_studio_change_set"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["change_item_id"],
            ["studio_change_item.id"],
            name=op.f(
                "fk_studio_context_product_materialization_change_item_id_studio_change_item"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["context_product_id"],
            ["context_product.id"],
            name=op.f(
                "fk_studio_context_product_materialization_context_product_id_context_product"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["context_product_version_id"],
            ["context_product_version.id"],
            name=op.f(
                "fk_studio_context_product_materialization_context_product_version_id_"
                "context_product_version"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["governance_review_id"],
            ["governance_review.id"],
            name=op.f(
                "fk_studio_context_product_materialization_governance_review_id_"
                "governance_review"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_studio_context_product_materialization")),
        sa.CheckConstraint(
            "operation IN ('CREATE', 'UPDATE', 'DELETE')",
            name="ck_studio_cp_materialization_operation",
        ),
    )
    op.create_index(
        op.f("ix_studio_context_product_materialization_organization_id"),
        "studio_context_product_materialization",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_studio_context_product_materialization_change_set_id"),
        "studio_context_product_materialization",
        ["change_set_id"],
    )
    op.create_index(
        op.f("ix_studio_context_product_materialization_change_item_id"),
        "studio_context_product_materialization",
        ["change_item_id"],
    )
    op.create_index(
        op.f("ix_studio_context_product_materialization_context_product_id"),
        "studio_context_product_materialization",
        ["context_product_id"],
    )
    op.create_index(
        op.f("ix_studio_context_product_materialization_context_product_version_id"),
        "studio_context_product_materialization",
        ["context_product_version_id"],
    )
    op.create_index(
        op.f("ix_studio_context_product_materialization_governance_review_id"),
        "studio_context_product_materialization",
        ["governance_review_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_studio_context_product_materialization_governance_review_id"),
        table_name="studio_context_product_materialization",
    )
    op.drop_index(
        op.f("ix_studio_context_product_materialization_context_product_version_id"),
        table_name="studio_context_product_materialization",
    )
    op.drop_index(
        op.f("ix_studio_context_product_materialization_context_product_id"),
        table_name="studio_context_product_materialization",
    )
    op.drop_index(
        op.f("ix_studio_context_product_materialization_change_item_id"),
        table_name="studio_context_product_materialization",
    )
    op.drop_index(
        op.f("ix_studio_context_product_materialization_change_set_id"),
        table_name="studio_context_product_materialization",
    )
    op.drop_index(
        op.f("ix_studio_context_product_materialization_organization_id"),
        table_name="studio_context_product_materialization",
    )
    op.drop_table("studio_context_product_materialization")
