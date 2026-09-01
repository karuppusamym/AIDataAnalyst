"""gl6 tier2 escalation column

Adds `unowned_asset_escalation.escalated_tier2_at` (GL-6): a second escalation
tier for a backlog entry still unaddressed a further period after its tier-1
`escalated_at`, escalated unconditionally through ITSM regardless of what
channel tier 1 used. Distinct from `escalated_at`, which only ever records
the tier-1 timestamp -- both are needed to tell "escalated once" from
"escalated twice" and to compute the tier-2 deadline from the right anchor.

Revision ID: 75838f5c1cea
Revises: eb8987ff4f66
Create Date: 2026-09-01 10:21:53.324296
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "75838f5c1cea"
down_revision: str | Sequence[str] | None = "eb8987ff4f66"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "unowned_asset_escalation",
        sa.Column("escalated_tier2_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("unowned_asset_escalation", "escalated_tier2_at")
