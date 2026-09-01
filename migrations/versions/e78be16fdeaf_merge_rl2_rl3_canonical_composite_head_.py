"""merge rl2/rl3 canonical-composite head with kg5 graph-perspective head

Revision ID: e78be16fdeaf
Revises: 6b38b3be42f4, c29e6e68a2b6
Create Date: 2026-08-30 18:16:14.453691
"""
from collections.abc import Sequence

revision: str = 'e78be16fdeaf'
down_revision: str | Sequence[str] | None = ('6b38b3be42f4', 'c29e6e68a2b6')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

