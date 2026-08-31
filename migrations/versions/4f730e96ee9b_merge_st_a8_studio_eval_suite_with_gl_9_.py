"""merge ST-A8 studio eval suite with GL-9 heads

Revision ID: 4f730e96ee9b
Revises: d3f8a1c56e90, f2dff00748b8
Create Date: 2026-08-31 00:04:21.202002
"""
from collections.abc import Sequence

revision: str = '4f730e96ee9b'
down_revision: str | Sequence[str] | None = ('d3f8a1c56e90', 'f2dff00748b8')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

