"""merge pr3/pr4 classification-feed/task-tracking with rl2/rl3/kg5 head

Revision ID: 93c66b5d0837
Revises: d421ddabbe77, e78be16fdeaf
Create Date: 2026-08-30 18:29:08.451169
"""
from collections.abc import Sequence

revision: str = '93c66b5d0837'
down_revision: str | Sequence[str] | None = ('d421ddabbe77', 'e78be16fdeaf')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

