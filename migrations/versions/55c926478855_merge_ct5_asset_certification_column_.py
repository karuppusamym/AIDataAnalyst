"""merge ct5 asset certification column scope with concurrent tracker heads

Revision ID: 55c926478855
Revises: 21a56d48976e, c4d8e6f0a1b3
Create Date: 2026-08-30 14:31:25.975986
"""
from collections.abc import Sequence

revision: str = '55c926478855'
down_revision: str | Sequence[str] | None = ('21a56d48976e', 'c4d8e6f0a1b3')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

