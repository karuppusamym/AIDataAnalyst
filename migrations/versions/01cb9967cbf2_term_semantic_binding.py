"""SM-2: glossary term binding to semantic objects

Revision ID: 01cb9967cbf2
Revises: f3a8c62d9e17
Create Date: 2026-08-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "01cb9967cbf2"
down_revision: str | Sequence[str] | None = "f3a8c62d9e17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "term_semantic_binding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("term_id", sa.Uuid(), nullable=False),
        sa.Column("semantic_object_type", sa.String(length=30), nullable=False),
        sa.Column("semantic_object_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("approved_by", sa.String(length=255), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["governance_review_id"],
            ["governance_review.id"],
            name=op.f("fk_term_semantic_binding_governance_review_id_governance_review"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_term_semantic_binding_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["term_id"],
            ["glossary_term.id"],
            name=op.f("fk_term_semantic_binding_term_id_glossary_term"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_term_semantic_binding")),
        sa.UniqueConstraint(
            "governance_review_id",
            name=op.f("uq_term_semantic_binding_governance_review_id"),
        ),
        sa.UniqueConstraint(
            "term_id",
            "semantic_object_type",
            "semantic_object_id",
            name="uq_term_semantic_binding_term_object",
        ),
    )
    op.create_index(
        "ix_term_semantic_binding_object",
        "term_semantic_binding",
        ["semantic_object_type", "semantic_object_id"],
        unique=False,
    )
    op.create_index(
        "ix_term_semantic_binding_org_status",
        "term_semantic_binding",
        ["organization_id", "status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_term_semantic_binding_organization_id"),
        "term_semantic_binding",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_term_semantic_binding_semantic_object_id"),
        "term_semantic_binding",
        ["semantic_object_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_term_semantic_binding_term_id"),
        "term_semantic_binding",
        ["term_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_term_semantic_binding_term_id"), table_name="term_semantic_binding"
    )
    op.drop_index(
        op.f("ix_term_semantic_binding_semantic_object_id"), table_name="term_semantic_binding"
    )
    op.drop_index(
        op.f("ix_term_semantic_binding_organization_id"), table_name="term_semantic_binding"
    )
    op.drop_index("ix_term_semantic_binding_org_status", table_name="term_semantic_binding")
    op.drop_index("ix_term_semantic_binding_object", table_name="term_semantic_binding")
    op.drop_table("term_semantic_binding")
