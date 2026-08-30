"""merge composite key candidate with table family ct5 qg1 sm2 heads

Revision ID: 99823f633c68
Revises: 6500275e1d36, f8485009ff08
Create Date: 2026-08-30 17:31:51.703349
"""

from collections.abc import Sequence

revision: str = "99823f633c68"
down_revision: str | Sequence[str] | None = ("6500275e1d36", "f8485009ff08")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
