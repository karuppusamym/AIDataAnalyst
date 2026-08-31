"""merge QG-2 native policy sync with QG-6/MG-2 heads

Revision ID: bb909675ad3c
Revises: 951caaf42f9c, a3f6c9e21b74
Create Date: 2026-08-31 06:03:38.219232
"""
from collections.abc import Sequence

revision: str = 'bb909675ad3c'
down_revision: str | Sequence[str] | None = ('951caaf42f9c', 'a3f6c9e21b74')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
