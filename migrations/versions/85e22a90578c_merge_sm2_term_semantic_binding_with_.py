"""merge sm2 term semantic binding with ct5 asset certification

Revision ID: 85e22a90578c
Revises: 12aa5b4dd87d, b1d443e43fc8
Create Date: 2026-08-30 17:22:34.097410
"""
from collections.abc import Sequence

revision: str = '85e22a90578c'
down_revision: str | Sequence[str] | None = ('12aa5b4dd87d', 'b1d443e43fc8')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

