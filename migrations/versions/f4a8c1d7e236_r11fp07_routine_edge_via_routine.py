"""R11-FP07: the called routine a routine lineage edge was read through

Revision ID: f4a8c1d7e236
Revises: b7e2d9c4f158
Create Date: 2026-09-15

`deep_procedure_lineage_edge.via_routine` names the routine an edge was read from when the lineage
of a nested call is read through (`aida.routine_call_descent`). Nullable, and NULL for every edge
from a routine's own statements, so existing rows need no backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a8c1d7e236"
down_revision: str | Sequence[str] | None = "b7e2d9c4f158"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deep_procedure_lineage_edge",
        sa.Column("via_routine", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deep_procedure_lineage_edge", "via_routine")
