"""R11-MP08: PROMPT as an AI asset kind

Revision ID: e5b91c7d3a20
Revises: c4a7e2d9b815
Create Date: 2026-09-24

The SQL-generation instruction becomes a governed, versioned AI asset
(`aida.prompt_registry`): a PROMPT-kind `ai_asset` whose versions are approved
through the ordinary AI asset review. `ck_ai_asset_kind` admitted only
AI_USE_CASE, MODEL and AGENT; it now admits PROMPT too. No data changes, and the
downgrade refuses nothing that could exist before this revision.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5b91c7d3a20"
down_revision: str | Sequence[str] | None = "c4a7e2d9b815"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_ai_asset_kind", "ai_asset", type_="check")
    op.create_check_constraint(
        "ck_ai_asset_kind",
        "ai_asset",
        "asset_kind IN ('AI_USE_CASE', 'MODEL', 'AGENT', 'PROMPT')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_ai_asset_kind", "ai_asset", type_="check")
    op.create_check_constraint(
        "ck_ai_asset_kind",
        "ai_asset",
        "asset_kind IN ('AI_USE_CASE', 'MODEL', 'AGENT')",
    )
