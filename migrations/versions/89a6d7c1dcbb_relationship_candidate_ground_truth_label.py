"""relationship_candidate_ground_truth_label (RL-7 optional additive extra)

Adds one new table, `relationship_candidate_ground_truth_label`
(`aida.models.RelationshipCandidateGroundTruthLabel`). It lets a later,
stronger signal (a labelled banking corpus, or a usage-confirmation) supersede
a `RelationshipCandidate`'s original steward APPROVE/REJECT decision for
confidence-calibration purposes only (RL-7,
`intelligence_api.get_relationship_candidate_confidence_calibration`) --
without touching `relationship_candidate` itself. Purely additive: no existing
column, table, or row is altered. Nothing populates this table yet; it is
schema-only until such a signal exists in this environment.

The FK/PK/index/unique-constraint names below are hand-shortened (`candidate_id`
rather than `relationship_candidate_id`) to stay under Postgres's 63-byte
NAMEDATALEN limit given this table's already-long name -- see the column
comment on `RelationshipCandidateGroundTruthLabel.candidate_id` in
`aida/models.py`.

Revision ID: 89a6d7c1dcbb
Revises: 12aa5b4dd87d
Create Date: 2026-08-30 18:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "89a6d7c1dcbb"
down_revision: str | Sequence[str] | None = "12aa5b4dd87d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "relationship_candidate_ground_truth_label",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=30), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("rationale", sa.String(length=2000), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_relationship_candidate_ground_truth_label_organization_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["relationship_candidate.id"],
            name=op.f("fk_relationship_candidate_ground_truth_label_candidate_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relationship_candidate_ground_truth_label")),
        sa.UniqueConstraint(
            "candidate_id",
            name="uq_relationship_candidate_ground_truth_label",
        ),
    )
    op.create_index(
        op.f("ix_relationship_candidate_ground_truth_label_organization_id"),
        "relationship_candidate_ground_truth_label",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_relationship_candidate_ground_truth_label_candidate_id"),
        "relationship_candidate_ground_truth_label",
        ["candidate_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_relationship_candidate_ground_truth_label_candidate_id"),
        table_name="relationship_candidate_ground_truth_label",
    )
    op.drop_index(
        op.f("ix_relationship_candidate_ground_truth_label_organization_id"),
        table_name="relationship_candidate_ground_truth_label",
    )
    op.drop_table("relationship_candidate_ground_truth_label")
