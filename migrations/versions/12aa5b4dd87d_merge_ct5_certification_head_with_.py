"""merge ct5 certification head with catalog scale indexes / control plane heads

Revision ID: 12aa5b4dd87d
Revises: 55c926478855, f9a2b3c4d5e6
Create Date: 2026-08-30 17:20:32.552884
"""
from collections.abc import Sequence

revision: str = '12aa5b4dd87d'
down_revision: str | Sequence[str] | None = ('55c926478855', 'f9a2b3c4d5e6')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

