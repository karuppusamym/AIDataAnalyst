"""merge QG-6 tokenization with MG-2 kill switch heads

Revision ID: 951caaf42f9c
Revises: 2fa45be65bf7, d09d6e42028d
Create Date: 2026-08-31 05:58:41.833872
"""
from collections.abc import Sequence

revision: str = '951caaf42f9c'
down_revision: str | Sequence[str] | None = ('2fa45be65bf7', 'd09d6e42028d')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
