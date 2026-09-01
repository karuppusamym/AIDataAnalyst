"""Column-level dbt manifest lineage (LN-5)

`dbt_lineage_edge` only carried table-level `DEPENDS_ON` dependency edges.
This adds four columns so the same table can also carry `COLUMN_DEPENDS_ON`
edges extracted from a resource's `compiled_sql_redacted` where the manifest
provides parseable SQL: `source_column` / `target_column` (empty string,
never NULL, on existing/table-level rows -- see the model docstring for why)
and `transformation_type` / `confidence` (NULL on table-level rows, where
they carry no meaning).

The existing unique constraint is widened to include the two new column
fields so a table-level edge and any column-level edges between the same
two resources can coexist without colliding, and so multiple column-level
edges between the same pair (one per column) each stay unique.

Revision ID: 25c51ca82a9b
Revises: d5f8b21c4a03
Create Date: 2026-08-30 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "25c51ca82a9b"
down_revision: str | None = "d5f8b21c4a03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CONSTRAINT = "uq_dbt_lineage_edge_import_source_target"
_NEW_CONSTRAINT = "uq_dbt_lineage_edge_import_source_target_column"


def upgrade() -> None:
    op.drop_constraint(_OLD_CONSTRAINT, "dbt_lineage_edge", type_="unique")
    op.add_column(
        "dbt_lineage_edge",
        sa.Column("source_column", sa.String(length=255), nullable=True, server_default=""),
    )
    op.add_column(
        "dbt_lineage_edge",
        sa.Column("target_column", sa.String(length=255), nullable=True, server_default=""),
    )
    op.add_column(
        "dbt_lineage_edge", sa.Column("transformation_type", sa.String(length=30), nullable=True)
    )
    op.add_column(
        "dbt_lineage_edge", sa.Column("confidence", sa.String(length=30), nullable=True)
    )
    # The server_default already backfills these on existing rows, but make it
    # explicit and unconditional so the widened unique constraint below is
    # never built over a NULL in source_column/target_column.
    op.execute(
        sa.text(
            "UPDATE dbt_lineage_edge SET source_column = '', target_column = '' "
            "WHERE source_column IS NULL OR target_column IS NULL"
        )
    )
    op.create_unique_constraint(
        _NEW_CONSTRAINT,
        "dbt_lineage_edge",
        [
            "artifact_import_id",
            "source_resource_id",
            "target_resource_id",
            "source_column",
            "target_column",
        ],
    )


def downgrade() -> None:
    op.drop_constraint(_NEW_CONSTRAINT, "dbt_lineage_edge", type_="unique")
    op.drop_column("dbt_lineage_edge", "confidence")
    op.drop_column("dbt_lineage_edge", "transformation_type")
    op.drop_column("dbt_lineage_edge", "target_column")
    op.drop_column("dbt_lineage_edge", "source_column")
    op.create_unique_constraint(
        _OLD_CONSTRAINT,
        "dbt_lineage_edge",
        ["artifact_import_id", "source_resource_id", "target_resource_id"],
    )
