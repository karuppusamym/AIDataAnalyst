"""merge AT-D2 lineage unique key and AT-6 context receipts heads

Revision ID: 314f4d26a1d7
Revises: 31a73643a697, c947905af952
Create Date: 2026-09-01 00:58:06.022816
"""

from collections.abc import Sequence

revision: str = "314f4d26a1d7"
down_revision: str | Sequence[str] | None = ("31a73643a697", "c947905af952")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
