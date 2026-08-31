"""merge gl9 and token revocation heads

Revision ID: f2dff00748b8
Revises: b5249498ee93, e2f7f81de0a1
Create Date: 2026-08-30 23:47:27.045787
"""
from collections.abc import Sequence

revision: str = 'f2dff00748b8'
down_revision: str | Sequence[str] | None = ('b5249498ee93', 'e2f7f81de0a1')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

