"""R11-FP12: a context product version can name routines

Revision ID: 5d7a3c9e1f24
Revises: 8e1d4b6a2c90
Create Date: 2026-09-15

`context_product_version.routine_ids` is the fifth governed reference group, beside `table_ids`,
`semantic_model_version_ids`, `glossary_term_version_ids` and `eligible_tool_version_ids`, and is
stored the same way: a JSON list of ids, validated by the API against ACTIVE routines in the
product's own project. Existing versions get an empty list, which the fingerprint treats exactly as
a definition written before the field existed, so no stored fingerprint goes stale. The server
default exists only to fill those rows and is dropped, matching the ORM. `downgrade` drops the
column, and with it every routine reference a version named.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5d7a3c9e1f24"
down_revision: str | Sequence[str] | None = "8e1d4b6a2c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "context_product_version",
        sa.Column("routine_ids", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
    )
    op.alter_column("context_product_version", "routine_ids", server_default=None)


def downgrade() -> None:
    op.drop_column("context_product_version", "routine_ids")
