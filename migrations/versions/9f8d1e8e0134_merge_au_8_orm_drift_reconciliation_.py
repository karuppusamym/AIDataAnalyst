"""merge AU-8 ORM drift reconciliation head with SM-4/PG-4/OB-7 merge point

Revision ID: 9f8d1e8e0134
Revises: 09be3ab5b008, 626211c0e077
Create Date: 2026-09-01 00:55:08.065477
"""
from collections.abc import Sequence

revision: str = '9f8d1e8e0134'
down_revision: str | Sequence[str] | None = ('09be3ab5b008', '626211c0e077')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

