"""R11-FP15: grant change classes and routine signature pairing

Revision ID: d7a4c1e9b35f
Revises: c4e1a9d3b758
Create Date: 2026-09-15

* `metadata_change_signal.change_class` also accepts GRANT_ADDED, GRANT_MODIFIED and
  GRANT_REVOKED for a PERMISSION_CHANGED signal, and SIGNATURE_CHANGED for a routine retired
  because a new signature replaced it.
* `metadata_change_signal.related_subject_id` names that replacing routine. Nullable, with no
  foreign key: a subject id points into whichever table its kind names.

`downgrade` clears the new classes, which the old constraint cannot hold, and drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a4c1e9b35f"
down_revision: str | Sequence[str] | None = "c4e1a9d3b758"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "metadata_change_signal"
_CHECK = "ck_metadata_change_signal_change_class"
_OLD_CLASSES = "change_class IS NULL OR change_class IN ('LITERAL_ONLY', 'STRUCTURAL')"
_NEW_CLASSES = (
    "change_class IS NULL OR change_class IN ('LITERAL_ONLY', 'STRUCTURAL', "
    "'GRANT_ADDED', 'GRANT_MODIFIED', 'GRANT_REVOKED', 'SIGNATURE_CHANGED')"
)


def upgrade() -> None:
    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_CHECK), _TABLE, _NEW_CLASSES)
    op.add_column(_TABLE, sa.Column("related_subject_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, "related_subject_id")
    op.execute(
        "UPDATE metadata_change_signal SET change_class = NULL WHERE change_class IN "
        "('GRANT_ADDED', 'GRANT_MODIFIED', 'GRANT_REVOKED', 'SIGNATURE_CHANGED')"
    )
    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_CHECK), _TABLE, _OLD_CLASSES)
