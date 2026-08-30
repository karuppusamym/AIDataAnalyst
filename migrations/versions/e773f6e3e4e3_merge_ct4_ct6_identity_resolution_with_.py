"""merge ct4/ct6 identity resolution with concurrent rl1/pr1 heads

Revision ID: e773f6e3e4e3
Revises: 427bda830475, 99823f633c68
Create Date: 2026-08-30 17:47:26.994490
"""
from collections.abc import Sequence

revision: str = 'e773f6e3e4e3'
down_revision: str | Sequence[str] | None = ('427bda830475', '99823f633c68')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

