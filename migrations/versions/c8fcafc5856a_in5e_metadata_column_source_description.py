"""in5e metadata column source description

Adds `metadata_column.source_description` (IN-5e): a source-side column
comment lives directly on the object now, mirroring
`metadata_table.source_description`, instead of as a row in
`metadata_object_description` keyed by `column_id`. Column comments only ever
lived in the row-based table because `models.py` was off-limits to the N1
workstream that built envelope 1.1's description axis -- not for any
architectural reason.

Backfills the new column from every ACTIVE `object_type = 'COLUMN'` row in
`metadata_object_description`, then deletes those rows (both ACTIVE and
DEPRECATED -- `COLUMN` is no longer a valid `object_type` there) and drops
`column_id` and its constraints/index. `object_type_is_describable` and
`exactly_one_subject` are recreated without the `COLUMN`/`column_id` case.

Revision ID: c8fcafc5856a
Revises: eaf120430212
Create Date: 2026-09-01 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8fcafc5856a"
down_revision: str | Sequence[str] | None = "eaf120430212"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("metadata_column", sa.Column("source_description", sa.Text(), nullable=True))

    # Backfill from the row-based mechanism before it is narrowed below. Only
    # ACTIVE rows -- a DEPRECATED description is a source fact that has since
    # been retracted or superseded and must not resurrect on migration.
    op.execute(
        """
        UPDATE metadata_column
        SET source_description = mod.description
        FROM metadata_object_description AS mod
        WHERE mod.column_id = metadata_column.id
          AND mod.object_type = 'COLUMN'
          AND mod.status = 'ACTIVE'
        """
    )

    # COLUMN-type rows are now fully superseded by the column above, both
    # ACTIVE and DEPRECATED -- DESCRIBABLE_OBJECT_TYPES no longer accepts
    # COLUMN, so leaving old rows around with no writer would just be dead data.
    op.execute("DELETE FROM metadata_object_description WHERE object_type = 'COLUMN'")

    op.drop_constraint(
        op.f("ck_metadata_object_description_exactly_one_subject"),
        "metadata_object_description",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_metadata_object_description_object_type_is_describable"),
        "metadata_object_description",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_metadata_object_description_column_id_metadata_column"),
        "metadata_object_description",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("uq_metadata_object_description_column_id"),
        "metadata_object_description",
        type_="unique",
    )
    op.drop_index(
        op.f("ix_metadata_object_description_column_id"),
        table_name="metadata_object_description",
    )
    op.drop_column("metadata_object_description", "column_id")

    op.create_check_constraint(
        op.f("ck_metadata_object_description_object_type_is_describable"),
        "metadata_object_description",
        "object_type IN ('CATALOG', 'SCHEMA')",
    )
    op.create_check_constraint(
        op.f("ck_metadata_object_description_exactly_one_subject"),
        "metadata_object_description",
        "(CASE WHEN catalog_id IS NULL THEN 0 ELSE 1 END) "
        "+ (CASE WHEN schema_id IS NULL THEN 0 ELSE 1 END) = 1",
    )


def downgrade() -> None:
    """Restores the schema shape, not the data.

    A COLUMN description moved back to `metadata_column.source_description` by
    `upgrade()` is not reconstructed as a `metadata_object_description` row --
    the same one-directional-backfill convention every other envelope-axis
    migration in this codebase follows for a column-to-row (or row-to-column)
    move. `metadata_column.source_description` itself is left in place rather
    than dropped, so no data is destroyed by downgrading.
    """
    op.drop_constraint(
        op.f("ck_metadata_object_description_exactly_one_subject"),
        "metadata_object_description",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_metadata_object_description_object_type_is_describable"),
        "metadata_object_description",
        type_="check",
    )

    op.add_column(
        "metadata_object_description",
        sa.Column("column_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        op.f("ix_metadata_object_description_column_id"),
        "metadata_object_description",
        ["column_id"],
        unique=False,
    )
    op.create_unique_constraint(
        op.f("uq_metadata_object_description_column_id"),
        "metadata_object_description",
        ["column_id"],
    )
    op.create_foreign_key(
        op.f("fk_metadata_object_description_column_id_metadata_column"),
        "metadata_object_description",
        "metadata_column",
        ["column_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.create_check_constraint(
        op.f("ck_metadata_object_description_object_type_is_describable"),
        "metadata_object_description",
        "object_type IN ('CATALOG', 'SCHEMA', 'COLUMN')",
    )
    op.create_check_constraint(
        op.f("ck_metadata_object_description_exactly_one_subject"),
        "metadata_object_description",
        "(CASE WHEN catalog_id IS NULL THEN 0 ELSE 1 END) "
        "+ (CASE WHEN schema_id IS NULL THEN 0 ELSE 1 END) "
        "+ (CASE WHEN column_id IS NULL THEN 0 ELSE 1 END) = 1",
    )

    op.drop_column("metadata_column", "source_description")
