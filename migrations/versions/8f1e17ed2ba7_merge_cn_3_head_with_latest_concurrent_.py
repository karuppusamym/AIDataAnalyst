"""merge CN-3 head with latest concurrent migration head

Revision ID: 8f1e17ed2ba7
Revises: 9428bb91d084, d7d197ecef49
Create Date: 2026-09-01 01:13:48.358102
"""
from collections.abc import Sequence

revision: str = '8f1e17ed2ba7'
down_revision: str | Sequence[str] | None = ('9428bb91d084', 'd7d197ecef49')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

