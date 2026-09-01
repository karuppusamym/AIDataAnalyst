"""merge table family and ct5 qg1 heads

Revision ID: 2e97a984bc87
Revises: 12aa5b4dd87d, ec945d36b495
Create Date: 2026-08-30 17:23:45.423178
"""

from collections.abc import Sequence

revision: str = "2e97a984bc87"
down_revision: str | Sequence[str] | None = ("12aa5b4dd87d", "ec945d36b495")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
