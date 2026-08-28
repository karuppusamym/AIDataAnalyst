"""merge stewardship and integration policy heads

Revision ID: d81e6c0f2a14
Revises: 6f4c1d2e9a10, 9284d3ee7c0e
Create Date: 2026-08-28 10:35:00
"""

from collections.abc import Sequence

revision: str = "d81e6c0f2a14"
down_revision: str | Sequence[str] | None = ("6f4c1d2e9a10", "9284d3ee7c0e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
