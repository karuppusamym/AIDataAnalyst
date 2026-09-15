"""R11-C8: a workbook import can be reversed, naming the sample that asked

Revision ID: e6d2b9f4a1c7
Revises: d8a3f1c6b204
Create Date: 2026-09-14

The reviewer agent can approve a small `MODEL_IMPORT_BATCH`, and a human who
disputed that decision had no correction to file from the sample. A reversal is
an ordinary batch that puts back what the disputed one replaced. Three changes
make that possible:

* `model_import_change.published_version` records the version each change
  published when it applied, so a reversal names exactly the version it undoes
  and a later edit is skipped as stale rather than overwritten. Not backfilled:
  on a change applied before this it means "not recorded", and that batch is
  refused a reversal.
* `model_import_batch.reverses_batch_id` marks a reversal, which the reviewer
  agent pins at T2; `review_audit_sample_id` is the sample-to-correction edge
  the other correction paths already carry.
* `model_import_change.new_value` becomes nullable. `NULL` appears only on a
  reversal, meaning "the field had no value before": undoing a description the
  batch added withdraws it rather than inventing text.

`downgrade` drops the three columns, and with them the record of which versions
applied batches published. It restores `new_value NOT NULL`, and refuses (the
constraint cannot be applied) while any reversal change carries `NULL`, rather
than deleting a governed record to make room for an older schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6d2b9f4a1c7"
down_revision: str | Sequence[str] | None = "d8a3f1c6b204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_import_batch",
        sa.Column("reverses_batch_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "model_import_batch",
        sa.Column("review_audit_sample_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_model_import_batch_reverses_batch_id_model_import_batch"),
        "model_import_batch",
        "model_import_batch",
        ["reverses_batch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_model_import_batch_review_audit_sample_id_review_audit_sample"),
        "model_import_batch",
        "review_audit_sample",
        ["review_audit_sample_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_model_import_batch_reverses_batch_id"),
        "model_import_batch",
        ["reverses_batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_model_import_batch_review_audit_sample_id"),
        "model_import_batch",
        ["review_audit_sample_id"],
        unique=False,
    )
    op.add_column(
        "model_import_change",
        sa.Column("published_version", sa.Integer(), nullable=True),
    )
    op.alter_column(
        "model_import_change", "new_value", existing_type=sa.Text(), nullable=True
    )


def downgrade() -> None:
    op.alter_column(
        "model_import_change", "new_value", existing_type=sa.Text(), nullable=False
    )
    op.drop_column("model_import_change", "published_version")
    op.drop_index(
        op.f("ix_model_import_batch_review_audit_sample_id"),
        table_name="model_import_batch",
    )
    op.drop_index(
        op.f("ix_model_import_batch_reverses_batch_id"),
        table_name="model_import_batch",
    )
    op.drop_constraint(
        op.f("fk_model_import_batch_review_audit_sample_id_review_audit_sample"),
        "model_import_batch",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_model_import_batch_reverses_batch_id_model_import_batch"),
        "model_import_batch",
        type_="foreignkey",
    )
    op.drop_column("model_import_batch", "review_audit_sample_id")
    op.drop_column("model_import_batch", "reverses_batch_id")
