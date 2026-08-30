"""merge sm2 term semantic binding and catalog scale indexes

Revision ID: b1d443e43fc8
Revises: 06870567d835, f9a2b3c4d5e6
Create Date: 2026-08-30 17:19:47.441350
"""
from collections.abc import Sequence

revision: str = 'b1d443e43fc8'
down_revision: str | Sequence[str] | None = ('06870567d835', 'f9a2b3c4d5e6')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

