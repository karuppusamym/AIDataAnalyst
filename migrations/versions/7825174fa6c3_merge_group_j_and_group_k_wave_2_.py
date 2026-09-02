"""merge Group J and Group K Wave-2 migrations

Revision ID: 7825174fa6c3
Revises: 8396592b30e0, c3f7a1b9e2d4
Create Date: 2026-09-02 17:32:20.429813
"""

from collections.abc import Sequence

revision: str = "7825174fa6c3"
down_revision: str | Sequence[str] | None = ("8396592b30e0", "c3f7a1b9e2d4")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
