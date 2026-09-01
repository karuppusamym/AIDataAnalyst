"""merge pr2/pr5 profiling-exception/continue-as-new with pr3/pr4 head

Revision ID: 4a273bf0a890
Revises: 93c66b5d0837, 97d1d2013a35
Create Date: 2026-08-30 18:57:15.216373
"""
from collections.abc import Sequence

revision: str = '4a273bf0a890'
down_revision: str | Sequence[str] | None = ('93c66b5d0837', '97d1d2013a35')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

