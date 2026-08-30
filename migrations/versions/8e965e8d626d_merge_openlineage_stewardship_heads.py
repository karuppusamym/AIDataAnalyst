"""merge openlineage and stewardship/integration-policy heads

Revision ID: 8e965e8d626d
Revises: 8a7f3c1d4b22, d81e6c0f2a14
Create Date: 2026-08-30 14:06:00
"""

from collections.abc import Sequence

revision: str = "8e965e8d626d"
down_revision: str | Sequence[str] | None = ("8a7f3c1d4b22", "d81e6c0f2a14")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
