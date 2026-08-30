"""merge SM-2 term semantic binding and CX-2/dbt lineage heads

Revision ID: 06870567d835
Revises: 01cb9967cbf2, c4d8e6f0a1b3
Create Date: 2026-08-30 00:00:02
"""

from collections.abc import Sequence

revision: str = "06870567d835"
down_revision: str | Sequence[str] | None = ("01cb9967cbf2", "c4d8e6f0a1b3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
