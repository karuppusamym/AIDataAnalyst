"""canonical resolution steward override, composite relationship candidates (RL-2/3)

RL-1 (table family / temporal intelligence) is NOT part of this migration: it
shipped independently as `table_family_candidate` (see
`68a9ada00969_table_family_intelligence.py`), reached via this revision's
merged-in parent. A duplicate `table_family` / `table_family_member` pair
used to be created here; it has been dropped in favor of that shipped
implementation, and `canonical_table_mapping` below is now additive to
`table_family_candidate` instead of the removed `table_family`.

Revision ID: 354a60c31083
Revises: 99823f633c68
Create Date: 2026-08-30 17:29:50.675792
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "354a60c31083"
down_revision: str | Sequence[str] | None = "99823f633c68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # RL-2: canonical table resolution with steward override, additive to the
    # already-shipped `table_family_candidate` (see `68a9ada00969`).
    op.create_table(
        "canonical_table_mapping",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("family_candidate_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_table_id", sa.Uuid(), nullable=False),
        sa.Column("resolved_by", sa.String(length=255), nullable=False),
        sa.Column("rationale", sa.String(length=2000), nullable=False),
        sa.Column("is_steward_override", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["canonical_table_id"], ["metadata_table.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["family_candidate_id"], ["table_family_candidate.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "family_candidate_id", name="uq_canonical_table_mapping_family_candidate"
        ),
    )
    op.create_index(
        op.f("ix_canonical_table_mapping_organization_id"),
        "canonical_table_mapping",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_canonical_table_mapping_family_candidate_id"),
        "canonical_table_mapping",
        ["family_candidate_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_canonical_table_mapping_canonical_table_id"),
        "canonical_table_mapping",
        ["canonical_table_id"],
        unique=False,
    )

    # RL-3: composite (multi-column) relationship candidates
    op.create_table(
        "relationship_candidate_group",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("source_table_id", sa.Uuid(), nullable=False),
        sa.Column("target_table_id", sa.Uuid(), nullable=False),
        sa.Column("member_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("detection_rule", sa.String(length=100), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("review_reason", sa.String(length=2000), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "datasource_id",
            "member_fingerprint",
            name="uq_relationship_candidate_group_fingerprint",
        ),
    )
    for column in ("organization_id", "datasource_id", "source_table_id", "target_table_id"):
        op.create_index(
            op.f(f"ix_relationship_candidate_group_{column}"),
            "relationship_candidate_group",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_relationship_candidate_group_org_status",
        "relationship_candidate_group",
        ["organization_id", "status"],
        unique=False,
    )

    op.create_table(
        "relationship_candidate_group_member",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_column_id", sa.Uuid(), nullable=False),
        sa.Column("target_column_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"], ["relationship_candidate_group.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_column_id"], ["metadata_column.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_column_id"], ["metadata_column.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id", "ordinal", name="uq_relationship_candidate_group_member_ordinal"
        ),
        sa.UniqueConstraint(
            "group_id",
            "source_column_id",
            "target_column_id",
            name="uq_relationship_candidate_group_member_columns",
        ),
    )
    op.create_index(
        op.f("ix_relationship_candidate_group_member_group_id"),
        "relationship_candidate_group_member",
        ["group_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_relationship_candidate_group_member_source_column_id"),
        "relationship_candidate_group_member",
        ["source_column_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_relationship_candidate_group_member_target_column_id"),
        "relationship_candidate_group_member",
        ["target_column_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_relationship_candidate_group_member_target_column_id"),
        table_name="relationship_candidate_group_member",
    )
    op.drop_index(
        op.f("ix_relationship_candidate_group_member_source_column_id"),
        table_name="relationship_candidate_group_member",
    )
    op.drop_index(
        op.f("ix_relationship_candidate_group_member_group_id"),
        table_name="relationship_candidate_group_member",
    )
    op.drop_table("relationship_candidate_group_member")

    op.drop_index(
        "ix_relationship_candidate_group_org_status",
        table_name="relationship_candidate_group",
    )
    for column in reversed(
        ("organization_id", "datasource_id", "source_table_id", "target_table_id")
    ):
        op.drop_index(
            op.f(f"ix_relationship_candidate_group_{column}"),
            table_name="relationship_candidate_group",
        )
    op.drop_table("relationship_candidate_group")

    op.drop_index(
        op.f("ix_canonical_table_mapping_canonical_table_id"),
        table_name="canonical_table_mapping",
    )
    op.drop_index(
        op.f("ix_canonical_table_mapping_family_candidate_id"),
        table_name="canonical_table_mapping",
    )
    op.drop_index(
        op.f("ix_canonical_table_mapping_organization_id"),
        table_name="canonical_table_mapping",
    )
    op.drop_table("canonical_table_mapping")
