"""merge at6 context receipts and concurrent migration heads

Revision ID: c947905af952
Revises: 626211c0e077, f8a3c1d97e42
Create Date: 2026-09-01 00:46:57.953651
"""

from collections.abc import Sequence

revision: str = "c947905af952"
down_revision: str | Sequence[str] | None = ("626211c0e077", "f8a3c1d97e42")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
