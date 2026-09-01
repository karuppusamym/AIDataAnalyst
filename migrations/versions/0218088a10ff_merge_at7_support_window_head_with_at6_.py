"""merge AT-7 support-window head with AT-6 context-receipts head

Revision ID: 0218088a10ff
Revises: fd8e5859d17f, c947905af952
Create Date: 2026-09-01 01:00:34.266943
"""

from collections.abc import Sequence

revision: str = "0218088a10ff"
down_revision: str | Sequence[str] | None = ("fd8e5859d17f", "c947905af952")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
