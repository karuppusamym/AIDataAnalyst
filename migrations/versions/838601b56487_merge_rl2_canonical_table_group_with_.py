"""merge rl2 canonical table group with kg5 ct2 ct3 heads

Revision ID: 838601b56487
Revises: 8948cba0f342, c29e6e68a2b6
Create Date: 2026-08-30 18:25:00.000000
"""

from collections.abc import Sequence

revision: str = "838601b56487"
down_revision: str | Sequence[str] | None = ("8948cba0f342", "c29e6e68a2b6")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
