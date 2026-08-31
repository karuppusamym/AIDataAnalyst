"""merge PG-4 delegation head with OB-7 access-review head

Revision ID: 2c7ea84c000f
Revises: 3683caa58f58, da74c4fd9def
Create Date: 2026-08-31 07:52:52.863216
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '2c7ea84c000f'
down_revision: Union[str, Sequence[str], None] = ('3683caa58f58', 'da74c4fd9def')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

