"""merge rl2/rl3 canonical-resolution-composite with ct2/ct3 index-partition head

Revision ID: 6b38b3be42f4
Revises: 354a60c31083, c0e0c5c27e56
Create Date: 2026-08-30 18:14:04.077639
"""
from collections.abc import Sequence

revision: str = '6b38b3be42f4'
down_revision: str | Sequence[str] | None = ('354a60c31083', 'c0e0c5c27e56')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

