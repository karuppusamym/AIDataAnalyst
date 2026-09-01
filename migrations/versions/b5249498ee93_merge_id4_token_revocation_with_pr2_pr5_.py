"""merge id4 token revocation with pr2 pr5 profiling exception work

Revision ID: b5249498ee93
Revises: 4a273bf0a890, 8735a8693458
Create Date: 2026-08-30 19:30:24.835661
"""
from collections.abc import Sequence

revision: str = 'b5249498ee93'
down_revision: str | Sequence[str] | None = ('4a273bf0a890', '8735a8693458')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

