"""quality rule proposals

ADR-0029: the quality agent's proposals -- a DQ-4 threshold rule held until a
person decides it through the governance review queue. On approval the
decision adapter creates the `quality_rule`; a rejected proposal stays, so the
same rule key is never proposed again. See `aida.quality_rule_proposal_model`
and `aida.quality_rule_proposals`.

Revision ID: e3b8f14c6a92
Revises: d5e8a2c7f9b1
Create Date: 2026-09-11 01:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3b8f14c6a92"
down_revision: str | Sequence[str] | None = "d5e8a2c7f9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "quality_rule_proposal"
_RULE_TYPES = "rule_type IN ('TABLE_ROW_COUNT_MIN', 'TABLE_ROW_COUNT_MAX', 'COLUMN_NULL_RATE_MAX')"
_STATUSES = "status IN ('PENDING_APPROVAL', 'APPROVED', 'REJECTED')"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=True),
        sa.Column("rule_type", sa.String(length=30), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("governance_review_id", sa.Uuid(), nullable=True),
        sa.Column("applied_rule_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _RULE_TYPES, name=op.f("ck_quality_rule_proposal_rule_type_is_supported")
        ),
        sa.CheckConstraint(_STATUSES, name=op.f("ck_quality_rule_proposal_status_is_supported")),
        sa.ForeignKeyConstraint(
            ["applied_rule_id"],
            ["quality_rule.id"],
            name=op.f("fk_quality_rule_proposal_applied_rule_id_quality_rule"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["column_id"],
            ["metadata_column.id"],
            name=op.f("fk_quality_rule_proposal_column_id_metadata_column"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_quality_rule_proposal_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["governance_review_id"],
            ["governance_review.id"],
            name=op.f("fk_quality_rule_proposal_governance_review_id_governance_review"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_quality_rule_proposal_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["table_id"],
            ["metadata_table.id"],
            name=op.f("fk_quality_rule_proposal_table_id_metadata_table"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quality_rule_proposal")),
        sa.UniqueConstraint(
            "governance_review_id", name=op.f("uq_quality_rule_proposal_governance_review_id")
        ),
    )
    op.create_index(op.f("ix_quality_rule_proposal_organization_id"), _TABLE, ["organization_id"])
    op.create_index(op.f("ix_quality_rule_proposal_datasource_id"), _TABLE, ["datasource_id"])
    op.create_index(op.f("ix_quality_rule_proposal_column_id"), _TABLE, ["column_id"])
    op.create_index(op.f("ix_quality_rule_proposal_applied_rule_id"), _TABLE, ["applied_rule_id"])
    op.create_index("ix_quality_rule_proposal_org_status", _TABLE, ["organization_id", "status"])
    op.create_index(
        "ix_quality_rule_proposal_rule_key", _TABLE, ["table_id", "rule_type", "column_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_quality_rule_proposal_rule_key", table_name=_TABLE)
    op.drop_index("ix_quality_rule_proposal_org_status", table_name=_TABLE)
    op.drop_index(op.f("ix_quality_rule_proposal_applied_rule_id"), table_name=_TABLE)
    op.drop_index(op.f("ix_quality_rule_proposal_column_id"), table_name=_TABLE)
    op.drop_index(op.f("ix_quality_rule_proposal_datasource_id"), table_name=_TABLE)
    op.drop_index(op.f("ix_quality_rule_proposal_organization_id"), table_name=_TABLE)
    op.drop_table(_TABLE)
