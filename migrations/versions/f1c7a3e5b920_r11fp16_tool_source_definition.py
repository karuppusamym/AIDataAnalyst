"""R11-FP16: bind a procedure tool to the routine definition it was generated from

Revision ID: f1c7a3e5b920
Revises: e3b9f6a2d481
Create Date: 2026-09-15

`governed_tool_version.source_definition_fingerprint` is the fingerprint of the routine definition
a procedure tool's SQL was copied from. Approval and execution compare it with the routine as it
is now (`aida.routine_tool_hold`), because when a version was approved proves nothing about which
definition it came from.

Existing versions with a source routine are backfilled from the routine's definition history: the
newest definition version captured no later than the tool version was created. A version with no
such history keeps NULL and is checked against change signals detected after it was generated.
`downgrade` drops the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1c7a3e5b920"
down_revision: str | Sequence[str] | None = "e3b9f6a2d481"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "governed_tool_version"


def upgrade() -> None:
    op.add_column(
        _TABLE, sa.Column("source_definition_fingerprint", sa.String(length=64), nullable=True)
    )
    op.execute(
        "UPDATE governed_tool_version SET source_definition_fingerprint = ("
        "SELECT definition.body_fingerprint FROM metadata_routine_definition_version AS definition "
        "WHERE definition.routine_id = governed_tool_version.source_routine_id "
        "AND definition.captured_at <= governed_tool_version.created_at "
        "ORDER BY definition.version_number DESC LIMIT 1) "
        "WHERE source_routine_id IS NOT NULL AND source_definition_fingerprint IS NULL"
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "source_definition_fingerprint")
