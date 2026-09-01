"""merge AT-D2 lineage unique key and AT-7a support window heads

Revision ID: 9428bb91d084
Revises: 314f4d26a1d7, 0218088a10ff
Create Date: 2026-09-01 01:10:17.684527
"""

from collections.abc import Sequence

revision: str = "9428bb91d084"
down_revision: str | Sequence[str] | None = ("314f4d26a1d7", "0218088a10ff")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
