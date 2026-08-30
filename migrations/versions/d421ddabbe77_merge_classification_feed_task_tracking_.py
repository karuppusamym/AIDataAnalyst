"""merge classification_feed_task_tracking with rl4567 heads

Revision ID: d421ddabbe77
Revises: 60ed76260ef6, 6e9e757413b2
Create Date: 2026-08-30 18:06:59.058771
"""
from collections.abc import Sequence

revision: str = 'd421ddabbe77'
down_revision: str | Sequence[str] | None = ('60ed76260ef6', '6e9e757413b2')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
