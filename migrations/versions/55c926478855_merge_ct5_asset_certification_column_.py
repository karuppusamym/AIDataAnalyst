"""merge ct5 asset certification column scope with concurrent tracker heads

Revision ID: 55c926478855
Revises: 21a56d48976e, c4d8e6f0a1b3
Create Date: 2026-08-30 14:31:25.975986
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '55c926478855'
down_revision: Union[str, Sequence[str], None] = ('21a56d48976e', 'c4d8e6f0a1b3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

