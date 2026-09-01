"""merge table family intelligence and cx2 owner type heads

Revision ID: ec945d36b495
Revises: 68a9ada00969, c4d8e6f0a1b3
Create Date: 2026-08-30 14:34:23.840752
"""

from collections.abc import Sequence

revision: str = "ec945d36b495"
down_revision: str | Sequence[str] | None = ("68a9ada00969", "c4d8e6f0a1b3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
