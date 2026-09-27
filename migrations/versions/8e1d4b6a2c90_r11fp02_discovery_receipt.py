"""R11-FP02: each discovery run's scope receipt

Revision ID: 8e1d4b6a2c90
Revises: 4c9e2a7f5b13
Create Date: 2026-09-15

`analysis_run.discovery_receipt` holds what one run took in: per object kind how many the source
returned into scope and how many the selection left out, per facet whether the connector collects
it and -- for view definitions and routine bodies -- how much arrived, was withheld or was
truncated, whether the stream finished, and what reconciliation did (`aida.discovery_receipt`).
Value-free. Nullable, and not backfilled: a run before this has no receipt, which is different
from a run that found nothing. `downgrade` drops the column and every receipt with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8e1d4b6a2c90"
down_revision: str | Sequence[str] | None = "4c9e2a7f5b13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_run", sa.Column("discovery_receipt", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_run", "discovery_receipt")
