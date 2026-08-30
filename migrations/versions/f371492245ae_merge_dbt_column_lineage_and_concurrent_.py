"""merge dbt column lineage and concurrent tracker work heads

Revision ID: f371492245ae
Revises: 25c51ca82a9b, f3a8c62d9e17
Create Date: 2026-08-30 13:59:52.731271
"""

from collections.abc import Sequence

revision: str = "f371492245ae"
down_revision: str | Sequence[str] | None = ("25c51ca82a9b", "f3a8c62d9e17")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
