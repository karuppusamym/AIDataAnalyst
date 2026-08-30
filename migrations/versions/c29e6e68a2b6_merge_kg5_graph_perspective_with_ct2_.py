"""merge kg5 graph perspective with ct2 ct3 index partition heads

Revision ID: c29e6e68a2b6
Revises: 2f9b7e13a4c6, c0e0c5c27e56
Create Date: 2026-08-30 18:12:00.000000
"""

from collections.abc import Sequence

revision: str = "c29e6e68a2b6"
down_revision: str | Sequence[str] | None = ("2f9b7e13a4c6", "c0e0c5c27e56")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
