"""merge AT-7 context-product support window with SM-4/PG-4/OB-7 merge point

Revision ID: fd8e5859d17f
Revises: c1a4d7e9f062, 626211c0e077
Create Date: 2026-09-01 00:45:37.732790
"""

from collections.abc import Sequence

revision: str = "fd8e5859d17f"
down_revision: str | Sequence[str] | None = ("c1a4d7e9f062", "626211c0e077")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
