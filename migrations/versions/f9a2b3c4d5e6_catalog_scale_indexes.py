"""Add catalog indexes for bounded, server-side discovery.

Revision ID: f9a2b3c4d5e6
Revises: e8f1a2b3c4d5
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f9a2b3c4d5e6"
down_revision: str | None = "e8f1a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Support source/status paging and case-insensitive contains search."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_metadata_table_catalog_page "
        "ON metadata_table (datasource_id, status, object_type, name, id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_metadata_table_name_trgm "
        "ON metadata_table USING gin (lower(name) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_metadata_table_description_trgm "
        "ON metadata_table USING gin (lower(coalesce(source_description, '')) gin_trgm_ops)"
    )


def downgrade() -> None:
    """Remove only indexes owned by this migration; preserve the shared extension."""
    op.execute("DROP INDEX IF EXISTS ix_metadata_table_description_trgm")
    op.execute("DROP INDEX IF EXISTS ix_metadata_table_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_metadata_table_catalog_page")
