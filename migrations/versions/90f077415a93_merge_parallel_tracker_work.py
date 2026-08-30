"""merge parallel tracker work (TL-1, LN-4, CT-1, GL-6)

Revision ID: 90f077415a93
Revises: 54ad108f9b8a, a67d5816d225, b3f7a1c94d62, dcb1fe4dfe6a
Create Date: 2026-08-30 13:42:41.331089
"""

from collections.abc import Sequence

revision: str = "90f077415a93"
down_revision: str | Sequence[str] | None = (
    "54ad108f9b8a",
    "a67d5816d225",
    "b3f7a1c94d62",
    "dcb1fe4dfe6a",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
