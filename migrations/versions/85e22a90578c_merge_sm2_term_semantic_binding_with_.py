"""merge sm2 term semantic binding with ct5 asset certification

Revision ID: 85e22a90578c
Revises: 12aa5b4dd87d, b1d443e43fc8
Create Date: 2026-08-30 17:22:34.097410
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '85e22a90578c'
down_revision: Union[str, Sequence[str], None] = ('12aa5b4dd87d', 'b1d443e43fc8')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

