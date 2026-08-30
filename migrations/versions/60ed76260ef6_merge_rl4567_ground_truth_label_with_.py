"""merge rl4567 ground truth label with ct4/ct6 identity resolution heads

Revision ID: 60ed76260ef6
Revises: 89a6d7c1dcbb, e773f6e3e4e3
Create Date: 2026-08-30 17:57:04.671264
"""
from collections.abc import Sequence

revision: str = '60ed76260ef6'
down_revision: str | Sequence[str] | None = ('89a6d7c1dcbb', 'e773f6e3e4e3')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

