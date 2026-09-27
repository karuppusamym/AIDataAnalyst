"""R11-FP09: a context product version can bind approved ontology versions

Revision ID: 7b4e2d9a6c13
Revises: 5d7a3c9e1f24
Create Date: 2026-09-15

`context_product_version.ontology_version_ids` is the sixth governed reference group, stored like
the others: a JSON list of ids, validated by the API against APPROVED ontology versions of the
product's own organization. It pins a version, not an ontology, so a later publication never
changes what an existing product says. Existing versions get an empty list, which the fingerprint
treats exactly as a definition written before the field existed. The server default only fills
those rows and is dropped, matching the ORM. `downgrade` drops the column and every binding.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7b4e2d9a6c13"
down_revision: str | Sequence[str] | None = "5d7a3c9e1f24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "context_product_version",
        sa.Column(
            "ontology_version_ids", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False
        ),
    )
    op.alter_column("context_product_version", "ontology_version_ids", server_default=None)


def downgrade() -> None:
    op.drop_column("context_product_version", "ontology_version_ids")
