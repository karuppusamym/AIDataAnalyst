"""R11-C8: a description withdrawal names the sample that raised it

Revision ID: d8a3f1c6b204
Revises: c4e7b2d9a613
Create Date: 2026-09-13

`bulk_stewardship_operation.review_audit_sample_id` (a7c31f0b95e4) is AR-11's
sample-to-correction edge: a reversal raised from a human's DISAGREED verdict on
a sampled agent decision names that sample, so the correction is reachable from
the sample and the sampled decision from the correction.

A business annotation an agent should not have approved is now corrected by
withdrawing the version it wrote, through `description_withdrawal`. The same edge
belongs on that row, for the same reason -- and because the oversight bounds
count a disputed sample as unresolved while its correction waits, which they can
only do if the correction says which sample it answers.

Nullable, and not backfilled: a steward's withdrawal raised directly, and every
withdrawal made before this, answers no sample. `downgrade` drops the column,
and with it the link for any withdrawal raised from a sample in the meantime.

The subject-type check widens to admit `ANNOTATION`, the business annotation
version an agent approved. `downgrade` restores the narrower check, and refuses
(the constraint cannot be created) while any annotation withdrawal exists,
rather than deleting a governed record to make room for an older schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8a3f1c6b204"
down_revision: str | Sequence[str] | None = "c4e7b2d9a613"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBJECT_TYPE_CHECK = "ck_description_withdrawal_withdrawal_subject_type_is_supported"


def upgrade() -> None:
    op.drop_constraint(op.f(_SUBJECT_TYPE_CHECK), "description_withdrawal", type_="check")
    op.create_check_constraint(
        op.f(_SUBJECT_TYPE_CHECK),
        "description_withdrawal",
        "subject_type IN ('TABLE', 'COLUMN', 'ANNOTATION')",
    )
    op.add_column(
        "description_withdrawal",
        sa.Column("review_audit_sample_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_description_withdrawal_review_audit_sample_id_review_audit_sample"),
        "description_withdrawal",
        "review_audit_sample",
        ["review_audit_sample_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_description_withdrawal_review_audit_sample_id"),
        "description_withdrawal",
        ["review_audit_sample_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_description_withdrawal_review_audit_sample_id"),
        table_name="description_withdrawal",
    )
    op.drop_constraint(
        op.f("fk_description_withdrawal_review_audit_sample_id_review_audit_sample"),
        "description_withdrawal",
        type_="foreignkey",
    )
    op.drop_column("description_withdrawal", "review_audit_sample_id")
    op.drop_constraint(op.f(_SUBJECT_TYPE_CHECK), "description_withdrawal", type_="check")
    op.create_check_constraint(
        op.f(_SUBJECT_TYPE_CHECK),
        "description_withdrawal",
        "subject_type IN ('TABLE', 'COLUMN')",
    )
