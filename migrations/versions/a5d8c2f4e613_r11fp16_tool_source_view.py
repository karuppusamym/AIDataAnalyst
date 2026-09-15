"""R11-FP16: bind a view tool to the view it was generated from

Revision ID: a5d8c2f4e613
Revises: f1c7a3e5b920
Create Date: 2026-09-15

`governed_tool_version.source_view_table_id` names the view (its `metadata_table` id) a view
tool's SQL was generated from, beside `source_routine_id`. With `source_definition_fingerprint` it
binds the version to the view definition it was generated from (`aida.tool_source_binding`).
Nullable and indexed, with no foreign key, like `source_routine_id`.

On PostgreSQL, versions the tool agent generated are backfilled from their audit record, which
names the view. No definition history exists for views, so their fingerprint stays NULL and they
are checked against change signals detected after they were generated. `downgrade` drops the
column and its index.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5d8c2f4e613"
down_revision: str | Sequence[str] | None = "f1c7a3e5b920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "governed_tool_version"
_INDEX = "ix_governed_tool_version_source_view_table_id"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("source_view_table_id", sa.Uuid(), nullable=True))
    op.create_index(op.f(_INDEX), _TABLE, ["source_view_table_id"], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE governed_tool_version AS tool_version "
            "SET source_view_table_id = CAST(audit.details ->> 'table_id' AS uuid) "
            "FROM audit_event AS audit "
            "WHERE audit.resource_type = 'governed_tool_version' "
            "AND audit.resource_id = CAST(tool_version.id AS text) "
            "AND audit.action = 'governed_tool.version.generated_by_agent' "
            "AND audit.details ->> 'source_kind' = 'VIEW' "
            "AND tool_version.source_routine_id IS NULL "
            "AND audit.details ->> 'table_id' ~* "
            "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
        )


def downgrade() -> None:
    op.drop_index(op.f(_INDEX), table_name=_TABLE)
    op.drop_column(_TABLE, "source_view_table_id")
