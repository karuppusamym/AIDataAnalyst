"""merge openlineage and stewardship-integration heads

Revision ID: c3a9f1d5b6e2
Revises: 8a7f3c1d4b22, d81e6c0f2a14
Create Date: 2026-08-30 15:00:00
"""

from collections.abc import Sequence

revision: str = "c3a9f1d5b6e2"
down_revision: str | Sequence[str] | None = ("8a7f3c1d4b22", "d81e6c0f2a14")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
