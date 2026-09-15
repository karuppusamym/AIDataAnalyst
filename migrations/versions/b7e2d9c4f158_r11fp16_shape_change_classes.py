"""R11-FP16: table shape change classes

Revision ID: b7e2d9c4f158
Revises: a5d8c2f4e613
Create Date: 2026-09-15

`metadata_change_signal.change_class` also accepts COLUMNS_ADDED, COLUMNS_RETURNED,
COLUMNS_REMOVED and COLUMNS_RETYPED for a table's STRUCTURE_CHANGED signal, so a hold can tell a
change a query that still binds survives from one that can change its answer. Signals recorded
before this carry no class and are treated as the latter.

`downgrade` clears the new classes, which the old constraint cannot hold.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7e2d9c4f158"
down_revision: str | Sequence[str] | None = "a5d8c2f4e613"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "metadata_change_signal"
_CHECK = "ck_metadata_change_signal_change_class"
_OLD_CLASSES = (
    "change_class IS NULL OR change_class IN ('LITERAL_ONLY', 'STRUCTURAL', "
    "'GRANT_ADDED', 'GRANT_MODIFIED', 'GRANT_REVOKED', 'SIGNATURE_CHANGED')"
)
_NEW_CLASSES = (
    "change_class IS NULL OR change_class IN ('LITERAL_ONLY', 'STRUCTURAL', "
    "'GRANT_ADDED', 'GRANT_MODIFIED', 'GRANT_REVOKED', 'SIGNATURE_CHANGED', "
    "'COLUMNS_ADDED', 'COLUMNS_RETURNED', 'COLUMNS_REMOVED', 'COLUMNS_RETYPED')"
)


def upgrade() -> None:
    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_CHECK), _TABLE, _NEW_CLASSES)


def downgrade() -> None:
    op.execute(
        "UPDATE metadata_change_signal SET change_class = NULL WHERE change_class IN "
        "('COLUMNS_ADDED', 'COLUMNS_RETURNED', 'COLUMNS_REMOVED', 'COLUMNS_RETYPED')"
    )
    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.create_check_constraint(op.f(_CHECK), _TABLE, _OLD_CLASSES)
