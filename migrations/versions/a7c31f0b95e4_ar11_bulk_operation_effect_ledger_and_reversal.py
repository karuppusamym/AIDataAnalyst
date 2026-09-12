"""AR-11: record what a bulk operation applied, and link a reversal to its sample

Revision ID: a7c31f0b95e4
Revises: d41a7b8e6c02
Create Date: 2026-09-12 10:30:00.000000

Row R11-C8. `bulk_stewardship_operation` recorded `applied_count` but never
which subjects it applied to, and every branch of
`stewardship_service.apply_bulk_operation` skips subjects already in the
requested state or gone stale since the request. A LINK_TERM over forty tables
reporting 28 applied did not say which 28, so nothing could undo it without
also deleting the twelve links that predated the operation and were never its
to remove. `applied_subject_ids` is that record; it is what bounds a
compensating action to the blast radius of the operation it compensates.

`reverses_operation_id` marks an operation raised to undo another one (a
reversal is an ordinary bulk operation -- same table, same review, same
maker-checker -- rather than a parallel vocabulary). `review_risk_tiers.
risk_tier_for` reads it to pin such an operation at T2, so no agent may decide
a reversal whatever its size.

`review_audit_sample_id` is AR-11's sample-to-correction link: set when the
reversal was raised from a human's DISAGREED verdict on a sampled agent
decision, so the correction is reachable from the sample and the sampled
decision is reachable from the correction.

Backfill is deliberately *not* attempted. `applied_subject_ids` defaults to an
empty list on existing rows, and `request_bulk_operation_reversal` refuses an
operation whose list is empty rather than falling back to `subject_ids`: for a
pre-existing row an empty list means "not recorded", not "changed nothing",
and guessing would let a reversal exceed the blast radius of what it reverses.
Operations applied before this migration are therefore not reversible through
this path, which is the correct answer rather than a limitation to route
around.

`downgrade` drops all three columns and their two indexes. It loses the
effect ledger for any operation applied while the column existed -- there is
nowhere else that information is written -- so a downgrade past this point
makes those operations permanently unreversible.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c31f0b95e4"
down_revision: str | Sequence[str] | None = "d41a7b8e6c02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bulk_stewardship_operation",
        sa.Column(
            "applied_subject_ids",
            sa.JSON(),
            nullable=False,
            # Server default so the NOT NULL holds for rows that already
            # exist; the ORM supplies its own default for new rows.
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "bulk_stewardship_operation",
        sa.Column("reverses_operation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "bulk_stewardship_operation",
        sa.Column("review_audit_sample_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_bulk_stewardship_operation_reverses_operation_id_bulk_stewardship_operation"),
        "bulk_stewardship_operation",
        "bulk_stewardship_operation",
        ["reverses_operation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_bulk_stewardship_operation_review_audit_sample_id_review_audit_sample"),
        "bulk_stewardship_operation",
        "review_audit_sample",
        ["review_audit_sample_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_bulk_stewardship_operation_reverses_operation_id"),
        "bulk_stewardship_operation",
        ["reverses_operation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_bulk_stewardship_operation_review_audit_sample_id"),
        "bulk_stewardship_operation",
        ["review_audit_sample_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_bulk_stewardship_operation_review_audit_sample_id"),
        table_name="bulk_stewardship_operation",
    )
    op.drop_index(
        op.f("ix_bulk_stewardship_operation_reverses_operation_id"),
        table_name="bulk_stewardship_operation",
    )
    op.drop_constraint(
        op.f("fk_bulk_stewardship_operation_review_audit_sample_id_review_audit_sample"),
        "bulk_stewardship_operation",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_bulk_stewardship_operation_reverses_operation_id_bulk_stewardship_operation"),
        "bulk_stewardship_operation",
        type_="foreignkey",
    )
    op.drop_column("bulk_stewardship_operation", "review_audit_sample_id")
    op.drop_column("bulk_stewardship_operation", "reverses_operation_id")
    op.drop_column("bulk_stewardship_operation", "applied_subject_ids")
