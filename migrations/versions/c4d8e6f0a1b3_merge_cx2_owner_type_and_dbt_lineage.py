"""merge CX-2 owner type and dbt column lineage

Revision ID: c4d8e6f0a1b3
Revises: b3e7a5c19d02, f371492245ae
Create Date: 2026-08-30 00:00:01
"""

from collections.abc import Sequence

revision: str = "c4d8e6f0a1b3"
down_revision: str | Sequence[str] | None = ("b3e7a5c19d02", "f371492245ae")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
