"""table family, canonical resolution, composite relationship candidates (RL-1/2/3)

Revision ID: 354a60c31083
Revises: f371492245ae
Create Date: 2026-08-30 17:29:50.675792
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "354a60c31083"
down_revision: str | Sequence[str] | None = "f371492245ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # RL-1: table family / temporal intelligence
    op.create_table(
        "table_family",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("family_key", sa.String(length=500), nullable=False),
        sa.Column("family_type", sa.String(length=30), nullable=False),
        sa.Column("algorithm_version", sa.String(length=50), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("datasource_id", "family_key", name="uq_table_family_key"),
    )
    op.create_index(
        op.f("ix_table_family_datasource_id"), "table_family", ["datasource_id"], unique=False
    )
    op.create_index(
        op.f("ix_table_family_organization_id"), "table_family", ["organization_id"], unique=False
    )
    op.create_index(
        "ix_table_family_org_type", "table_family", ["organization_id", "family_type"], unique=False
    )

    op.create_table(
        "table_family_member",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["family_id"], ["table_family.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("table_id", name="uq_table_family_member_table_id"),
    )
    op.create_index(
        op.f("ix_table_family_member_organization_id"),
        "table_family_member",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_table_family_member_family_id"),
        "table_family_member",
        ["family_id"],
        unique=False,
    )

    # RL-2: canonical table resolution with steward override
    op.create_table(
        "canonical_table_mapping",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("detected_canonical_table_id", sa.Uuid(), nullable=False),
        sa.Column("algorithm_version", sa.String(length=50), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("override_table_id", sa.Uuid(), nullable=True),
        sa.Column("override_reason", sa.String(length=2000), nullable=True),
        sa.Column("overridden_by", sa.String(length=255), nullable=True),
        sa.Column("overridden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["detected_canonical_table_id"], ["metadata_table.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["family_id"], ["table_family.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["override_table_id"], ["metadata_table.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("family_id", name="uq_canonical_table_mapping_family"),
    )
    op.create_index(
        op.f("ix_canonical_table_mapping_datasource_id"),
        "canonical_table_mapping",
        ["datasource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_canonical_table_mapping_family_id"),
        "canonical_table_mapping",
        ["family_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_canonical_table_mapping_organization_id"),
        "canonical_table_mapping",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_canonical_table_mapping_override_table_id"),
        "canonical_table_mapping",
        ["override_table_id"],
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
        op.f("ix_canonical_table_mapping_override_table_id"),
        table_name="canonical_table_mapping",
    )
    op.drop_index(
        op.f("ix_canonical_table_mapping_organization_id"),
        table_name="canonical_table_mapping",
    )
    op.drop_index(
        op.f("ix_canonical_table_mapping_family_id"), table_name="canonical_table_mapping"
    )
    op.drop_index(
        op.f("ix_canonical_table_mapping_datasource_id"),
        table_name="canonical_table_mapping",
    )
    op.drop_table("canonical_table_mapping")

    op.drop_index(
        op.f("ix_table_family_member_family_id"), table_name="table_family_member"
    )
    op.drop_index(
        op.f("ix_table_family_member_organization_id"), table_name="table_family_member"
    )
    op.drop_table("table_family_member")

    op.drop_index("ix_table_family_org_type", table_name="table_family")
    op.drop_index(op.f("ix_table_family_organization_id"), table_name="table_family")
    op.drop_index(op.f("ix_table_family_datasource_id"), table_name="table_family")
    op.drop_table("table_family")
