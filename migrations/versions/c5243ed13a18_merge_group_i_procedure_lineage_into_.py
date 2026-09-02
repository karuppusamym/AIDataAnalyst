"""merge Group I procedure-lineage into Wave-2 head

Revision ID: c5243ed13a18
Revises: 466f21849789, 7825174fa6c3
Create Date: 2026-09-02 18:43:48.549877
"""

from collections.abc import Sequence

revision: str = "c5243ed13a18"
down_revision: str | Sequence[str] | None = ("466f21849789", "7825174fa6c3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
