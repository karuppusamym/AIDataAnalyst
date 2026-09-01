"""merge CN-3 head with AT-6 context-receipts head

Revision ID: d7d197ecef49
Revises: 9f8d1e8e0134, c947905af952
Create Date: 2026-09-01 01:07:30.060903
"""
from collections.abc import Sequence

revision: str = 'd7d197ecef49'
down_revision: str | Sequence[str] | None = ('9f8d1e8e0134', 'c947905af952')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

