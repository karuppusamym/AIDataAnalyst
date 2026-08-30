"""merge id4 token revocation with pr2 pr5 profiling exception work

Revision ID: b5249498ee93
Revises: 4a273bf0a890, 8735a8693458
Create Date: 2026-08-30 19:30:24.835661
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5249498ee93'
down_revision: Union[str, Sequence[str], None] = ('4a273bf0a890', '8735a8693458')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

