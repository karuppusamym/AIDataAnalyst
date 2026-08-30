"""merge table family ct5 qg1 with sm2 term semantic binding heads

Revision ID: f8485009ff08
Revises: 2e97a984bc87, 85e22a90578c
Create Date: 2026-08-30 17:27:20.226461
"""

from collections.abc import Sequence

revision: str = "f8485009ff08"
down_revision: str | Sequence[str] | None = ("2e97a984bc87", "85e22a90578c")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
