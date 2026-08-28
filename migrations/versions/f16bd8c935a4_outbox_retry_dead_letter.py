"""outbox retry scheduling and dead-letter evidence

Revision ID: f16bd8c935a4
Revises: e95ac7b824f3
Create Date: 2026-08-25 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f16bd8c935a4"
down_revision: str | Sequence[str] | None = "e95ac7b824f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_event",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column("outbox_event", sa.Column("last_error", sa.String(length=1000), nullable=True))
    op.create_index(
        "ix_outbox_due",
        "outbox_event",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_due", table_name="outbox_event")
    op.drop_column("outbox_event", "last_error")
    op.drop_column("outbox_event", "next_attempt_at")
