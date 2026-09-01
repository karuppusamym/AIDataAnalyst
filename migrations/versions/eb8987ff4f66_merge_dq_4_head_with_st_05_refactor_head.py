"""merge DQ-4 head with ST-05 refactor head

Revision ID: eb8987ff4f66
Revises: 8f1e17ed2ba7, c1e64055ccdb
Create Date: 2026-09-01 04:24:17.668969
"""
from collections.abc import Sequence

revision: str = 'eb8987ff4f66'
down_revision: str | Sequence[str] | None = ('8f1e17ed2ba7', 'c1e64055ccdb')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

