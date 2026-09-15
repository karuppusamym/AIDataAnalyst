"""R11-FP01: a datasource's discovery selection, and the scope each run applied

Revision ID: 4c9e2a7f5b13
Revises: b7e3d1f9a254
Create Date: 2026-09-15

`datasource.discovery_selection` holds which object kinds, schemas and qualified names a
discovery run takes in (`aida.discovery_selection.DiscoverySelection`). NULL is unrestricted,
which is what every existing datasource keeps: nothing is backfilled.

`analysis_run.discovery_selection_fingerprint` and `analysis_run.excluded_objects` are the run's
own scope receipt -- the selection it applied and how many objects the source returned that the
selection left out -- so a preview and the run that followed it can be matched. Existing runs
applied no selection: NULL and 0.

`downgrade` drops all three columns, and with them every stored selection; a datasource then
discovers everything again, and its next FULL run retires what the dropped selection had kept
out of scope but the source no longer holds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c9e2a7f5b13"
down_revision: str | Sequence[str] | None = "b7e3d1f9a254"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("datasource", sa.Column("discovery_selection", sa.JSON(), nullable=True))
    op.add_column(
        "analysis_run",
        sa.Column("discovery_selection_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "analysis_run",
        sa.Column("excluded_objects", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("analysis_run", "excluded_objects")
    op.drop_column("analysis_run", "discovery_selection_fingerprint")
    op.drop_column("datasource", "discovery_selection")
