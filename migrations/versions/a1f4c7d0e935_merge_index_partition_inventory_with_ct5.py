"""merge index/partition inventory (CT-2/CT-3) head with CT-5/catalog-scale-indexes head

Revision ID: a1f4c7d0e935
Revises: d5e8a2c4f691, 12aa5b4dd87d
Create Date: 2026-08-30 18:00:00
"""

from collections.abc import Sequence

revision: str = "a1f4c7d0e935"
down_revision: str | Sequence[str] | None = ("d5e8a2c4f691", "12aa5b4dd87d")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
