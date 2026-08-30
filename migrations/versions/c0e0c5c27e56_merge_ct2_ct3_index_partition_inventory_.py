"""merge ct2/ct3 index-partition-inventory with rl4567 ground-truth head

Revision ID: c0e0c5c27e56
Revises: 60ed76260ef6, a1f4c7d0e935
Create Date: 2026-08-30 18:03:29.865608
"""
from collections.abc import Sequence

revision: str = 'c0e0c5c27e56'
down_revision: str | Sequence[str] | None = ('60ed76260ef6', 'a1f4c7d0e935')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

