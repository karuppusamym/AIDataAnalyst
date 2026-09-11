"""column description drafts

Deterministic, evidence-scored drafts of column descriptions, routed through
the shared governance review queue -- the column-level sibling of
`asset_description_draft` (GL-9). See `aida.column_description_service`.

`uq_column_description_draft_open` is a partial unique index: one open draft
(DRAFT or PENDING_APPROVAL) per column, enforced by the database rather than by
a read-then-insert that two concurrent generation requests could both pass.

Revision ID: d5e8a2c7f9b1
Revises: a7c41e93d2b0
Create Date: 2026-09-10 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e8a2c7f9b1"
down_revision: str | Sequence[str] | None = "a7c41e93d2b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPEN = "status IN ('DRAFT', 'PENDING_APPROVAL')"


def upgrade() -> None:
    op.create_table(
        "column_description_draft",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=False),
        sa.Column("drafted_text", sa.Text(), nullable=False),
        sa.Column("text_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("accuracy_score", sa.Float(), nullable=False),
        sa.Column("clarity_score", sa.Float(), nullable=False),
        sa.Column("style_score", sa.Float(), nullable=False),
        sa.Column("completeness_score", sa.Float(), nullable=False),
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("base_description_version", sa.Integer(), nullable=True),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("published_version_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["column_id"],
            ["metadata_column.id"],
            name=op.f("fk_column_description_draft_column_id_metadata_column"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["governance_review_id"],
            ["governance_review.id"],
            name=op.f("fk_column_description_draft_governance_review_id_governance_review"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_column_description_draft_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["published_version_id"],
            ["column_documentation_version.id"],
            name=op.f(
                "fk_column_description_draft_published_version_id_column_documentation_version"
            ),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_column_description_draft_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_column_description_draft")),
        sa.UniqueConstraint(
            "governance_review_id", name=op.f("uq_column_description_draft_governance_review_id")
        ),
    )
    op.create_index(
        op.f("ix_column_description_draft_organization_id"),
        "column_description_draft",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_column_description_draft_table_id"), "column_description_draft", ["table_id"]
    )
    op.create_index(
        op.f("ix_column_description_draft_column_id"), "column_description_draft", ["column_id"]
    )
    op.create_index(
        op.f("ix_column_description_draft_published_version_id"),
        "column_description_draft",
        ["published_version_id"],
    )
    op.create_index(
        "ix_column_description_draft_org_status",
        "column_description_draft",
        ["organization_id", "status"],
    )
    op.create_index(
        "uq_column_description_draft_open",
        "column_description_draft",
        ["column_id"],
        unique=True,
        postgresql_where=sa.text(_OPEN),
        sqlite_where=sa.text(_OPEN),
    )


def downgrade() -> None:
    op.drop_index("uq_column_description_draft_open", table_name="column_description_draft")
    op.drop_index("ix_column_description_draft_org_status", table_name="column_description_draft")
    op.drop_index(
        op.f("ix_column_description_draft_published_version_id"),
        table_name="column_description_draft",
    )
    op.drop_index(
        op.f("ix_column_description_draft_column_id"), table_name="column_description_draft"
    )
    op.drop_index(
        op.f("ix_column_description_draft_table_id"), table_name="column_description_draft"
    )
    op.drop_index(
        op.f("ix_column_description_draft_organization_id"),
        table_name="column_description_draft",
    )
    op.drop_table("column_description_draft")
