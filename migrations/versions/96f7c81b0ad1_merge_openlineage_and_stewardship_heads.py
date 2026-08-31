"""merge openlineage and stewardship heads

Revision ID: 96f7c81b0ad1
Revises: 8a7f3c1d4b22, d81e6c0f2a14
Create Date: 2026-08-30 23:29:16
"""

from collections.abc import Sequence

revision: str = "96f7c81b0ad1"
down_revision: str | Sequence[str] | None = ("8a7f3c1d4b22", "d81e6c0f2a14")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
