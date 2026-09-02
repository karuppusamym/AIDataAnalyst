"""AT-11: column_derived_classification -- derived lineage classification stored
separately from asserted

Adds the ``column_derived_classification`` table. A column's *derived*
classification (propagated to it along data lineage from a more-sensitive
upstream column) is kept strictly separate from its *asserted* classification on
``metadata_column.classification`` -- because for us a classification is an ABAC
enforcement input, not a label, and an inferred value must never silently become
an enforced one. Promotion from derived to asserted goes through the shared
maker-checker review queue (a ``COLUMN_CLASSIFICATION_PROMOTION``
``governance_review``); see ``aida.classification_propagation``. Each row carries
its evidence: the ordered ``edge_chain`` the classification travelled and the
``graph_version`` fingerprint of the lineage graph it was computed over.

Revision ID: a1c7e4d2f9b3
Revises: eb8987ff4f66
Create Date: 2026-09-01 06:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7e4d2f9b3"
down_revision: str | Sequence[str] | None = "a1c9e7d4b2f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "column_derived_classification",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=False),
        sa.Column("classification", sa.String(30), nullable=False),
        sa.Column("origin_column_id", sa.Uuid(), nullable=True),
        sa.Column("origin_classification", sa.String(30), nullable=False),
        sa.Column("edge_chain", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("graph_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="DERIVED"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("review_id", sa.Uuid(), nullable=True),
        sa.Column("promoted_by", sa.String(255), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["column_id"], ["metadata_column.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["origin_column_id"], ["metadata_column.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('DERIVED', 'PROMOTION_PENDING', 'PROMOTED', 'PROMOTION_REJECTED')",
            name="ck_column_derived_classification_status",
        ),
    )
    op.create_index(
        op.f("ix_column_derived_classification_organization_id"),
        "column_derived_classification",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_column_derived_classification_column_id"),
        "column_derived_classification",
        ["column_id"],
    )
    op.create_index(
        op.f("ix_column_derived_classification_origin_column_id"),
        "column_derived_classification",
        ["origin_column_id"],
    )
    op.create_index(
        op.f("ix_column_derived_classification_review_id"),
        "column_derived_classification",
        ["review_id"],
    )
    op.create_index(
        "ix_column_derived_classification_column_current",
        "column_derived_classification",
        ["column_id", "is_current"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_column_derived_classification_column_current",
        table_name="column_derived_classification",
    )
    op.drop_index(
        op.f("ix_column_derived_classification_review_id"),
        table_name="column_derived_classification",
    )
    op.drop_index(
        op.f("ix_column_derived_classification_origin_column_id"),
        table_name="column_derived_classification",
    )
    op.drop_index(
        op.f("ix_column_derived_classification_column_id"),
        table_name="column_derived_classification",
    )
    op.drop_index(
        op.f("ix_column_derived_classification_organization_id"),
        table_name="column_derived_classification",
    )
    op.drop_table("column_derived_classification")
