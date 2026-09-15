"""R11-FP16: the routine a procedure tool was extracted from

Revision ID: e3b9f6a2d481
Revises: d7a4c1e9b35f
Create Date: 2026-09-15

`governed_tool_version.source_routine_id` names the routine whose result query a procedure tool's
SQL was copied from, so a later change to that routine holds the version
(`aida.routine_tool_hold`). Nullable and indexed, with no foreign key: the routine table is an
envelope table, and a retired routine is kept rather than deleted.

On PostgreSQL, existing versions are backfilled from the audit record each generation path
already wrote (`governed_tool.version.generated_from_procedure` and, for the tool agent,
`governed_tool.version.generated_by_agent`), which carries the routine id. `downgrade` drops the
column and its index.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3b9f6a2d481"
down_revision: str | Sequence[str] | None = "d7a4c1e9b35f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "governed_tool_version"
_INDEX = "ix_governed_tool_version_source_routine_id"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("source_routine_id", sa.Uuid(), nullable=True))
    op.create_index(op.f(_INDEX), _TABLE, ["source_routine_id"], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE governed_tool_version AS tool_version "
            "SET source_routine_id = CAST(audit.details ->> 'routine_id' AS uuid) "
            "FROM audit_event AS audit "
            "WHERE audit.resource_type = 'governed_tool_version' "
            "AND audit.resource_id = CAST(tool_version.id AS text) "
            "AND audit.action IN ('governed_tool.version.generated_from_procedure', "
            "'governed_tool.version.generated_by_agent') "
            "AND audit.details ->> 'routine_id' ~* "
            "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
        )


def downgrade() -> None:
    op.drop_index(op.f(_INDEX), table_name=_TABLE)
    op.drop_column(_TABLE, "source_routine_id")
