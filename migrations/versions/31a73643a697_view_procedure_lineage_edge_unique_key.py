"""View/procedure lineage edge natural-key uniqueness (AT-D2 defect 5)

`view_lineage_edge` / `procedure_lineage_edge` had no unique constraint at
all, so re-parsing the same view or procedure definition through
`POST .../view-lineage/parse` (or `.../procedure-lineage/parse`) blindly
inserted a fresh copy of every edge on top of the previous parse's rows,
doubling the graph on each re-parse.

The natural key for one edge is the (datasource, source, target,
transformation) tuple -- deliberately *not* `sql_hash`, so a genuinely
unchanged re-parse collides with its own prior row (the bug this closes)
while a changed view definition still collides on any column pair that
survived the edit, letting the paired application-level delete-then-insert
in `view_lineage_api.py` replace stale rows for that target table cleanly
rather than accumulate them.

Revision ID: 31a73643a697
Revises: 09be3ab5b008
Create Date: 2026-09-01 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "31a73643a697"
down_revision: str | None = "09be3ab5b008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VIEW_CONSTRAINT = "uq_view_lineage_edge_natural_key"
_PROCEDURE_CONSTRAINT = "uq_procedure_lineage_edge_natural_key"

_NATURAL_KEY_COLUMNS = [
    "datasource_id",
    "source_table",
    "source_column",
    "target_table",
    "target_column",
    "transformation_type",
]


def upgrade() -> None:
    op.create_unique_constraint(_VIEW_CONSTRAINT, "view_lineage_edge", _NATURAL_KEY_COLUMNS)
    op.create_unique_constraint(
        _PROCEDURE_CONSTRAINT, "procedure_lineage_edge", _NATURAL_KEY_COLUMNS
    )


def downgrade() -> None:
    op.drop_constraint(_PROCEDURE_CONSTRAINT, "procedure_lineage_edge", type_="unique")
    op.drop_constraint(_VIEW_CONSTRAINT, "view_lineage_edge", type_="unique")
