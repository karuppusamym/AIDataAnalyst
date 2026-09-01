"""merge SM-4 metric-proposal head with PG-4/OB-7 merge point

Revision ID: 626211c0e077
Revises: 2c7ea84c000f, b799d3cd61f6
Create Date: 2026-08-31 08:20:08.447336
"""
from collections.abc import Sequence

revision: str = '626211c0e077'
down_revision: str | Sequence[str] | None = ('2c7ea84c000f', 'b799d3cd61f6')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

