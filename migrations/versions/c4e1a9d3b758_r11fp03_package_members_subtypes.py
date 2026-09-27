"""R11-FP03: package members and native routine subtypes

Revision ID: c4e1a9d3b758
Revises: b2d6e8f0a417
Create Date: 2026-09-15

* `metadata_routine.package_name` -- the package a member subprogram belongs to, empty for a
  standalone routine -- joins the routine's identity: the unique key becomes
  `(schema_id, package_name, name, signature)`, so a standalone `SCORE(NUMBER)` and the packaged
  `RISK_PKG.SCORE(NUMBER)` are two rows. Existing rows get `''`.
* `metadata_routine.native_subtype` keeps the engine's finer kind beside the portable
  `routine_type`. BigQuery stored its native kinds (`SCALAR_FUNCTION`, `TABLE_FUNCTION`) *as* the
  routine type, which matched no selectable kind; those rows become `FUNCTION` with the native kind
  as subtype.

`downgrade` restores BigQuery's stored kinds, deletes member rows (the old key cannot hold a member
beside a same-named standalone routine), and drops both columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e1a9d3b758"
down_revision: str | Sequence[str] | None = "b2d6e8f0a417"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNIQUE = "uq_metadata_routine_schema_id"


def upgrade() -> None:
    op.add_column(
        "metadata_routine",
        sa.Column("package_name", sa.String(length=255), server_default="", nullable=False),
    )
    op.add_column(
        "metadata_routine", sa.Column("native_subtype", sa.String(length=30), nullable=True)
    )
    op.drop_constraint(op.f(_UNIQUE), "metadata_routine", type_="unique")
    op.create_unique_constraint(
        op.f(_UNIQUE), "metadata_routine", ["schema_id", "package_name", "name", "signature"]
    )
    op.execute(
        "UPDATE metadata_routine SET native_subtype = routine_type, routine_type = 'FUNCTION' "
        "WHERE routine_type LIKE '%_FUNCTION'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE metadata_routine SET routine_type = native_subtype "
        "WHERE routine_type = 'FUNCTION' AND native_subtype LIKE '%_FUNCTION'"
    )
    op.execute("DELETE FROM metadata_routine WHERE package_name <> ''")
    op.drop_constraint(op.f(_UNIQUE), "metadata_routine", type_="unique")
    op.create_unique_constraint(
        op.f(_UNIQUE), "metadata_routine", ["schema_id", "name", "signature"]
    )
    op.drop_column("metadata_routine", "native_subtype")
    op.drop_column("metadata_routine", "package_name")
