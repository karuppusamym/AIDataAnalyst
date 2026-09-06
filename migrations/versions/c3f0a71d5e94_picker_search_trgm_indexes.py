"""Index the columns F15's picker search filters on.

Revision ID: c3f0a71d5e94
Revises: b6c4f1d80a27

F15 (`Docs/review-2026-09-05/REVIEW.md`) adds an optional `q=` substring
filter to the four shared-picker list routes. A substring match is a
leading-wildcard predicate: no b-tree index can serve it, so without these
the filter degrades to a full scan of the tenant's projects/workspaces/
sources on every keystroke -- which is not a fix, it is the same problem
moved to the server.

`pg_trgm` GIN indexes on the same `lower(<column>)` expression the query
uses, which is exactly the strategy `f9a2b3c4d5e6_catalog_scale_indexes.py`
already established for the catalog's `q=` search. The extension is created
`IF NOT EXISTS` because that migration may already have created it, and is
deliberately *not* dropped on downgrade for the same reason -- it is shared.

**Both columns of each OR, on purpose.** The project and workspace searches
match `name OR slug`. Postgres can only use indexes for an `OR` when every
branch is indexable (it bitmap-ORs them); indexing `name` alone would leave
the whole predicate unindexable and the index unused. `datasource` gets one
index because a datasource has no slug -- its search is name only.

**`organization` is deliberately not indexed here.** `list_organizations`
also gained `q=`, but that table holds one row per tenant: it is bounded by
how many organizations exist, not by the size of any estate, and a scan of
it is measured in microseconds. Two GIN indexes there would be write cost
and review surface bought with no read benefit. If tenant count ever
reaches a scale where that stops being true, this is the migration to
copy.

`CONCURRENTLY` is not used: the repository's migrations run in a
transaction (see `migrations/env.py`), and `CREATE INDEX CONCURRENTLY`
cannot. These tables are small enough relative to `metadata_table` that the
brief write lock is acceptable on the same terms the catalog indexes were.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3f0a71d5e94"
down_revision: str | None = "b6c4f1d80a27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Support case-insensitive contains search on the picker resources."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_project_name_trgm "
        "ON project USING gin (lower(name) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_project_slug_trgm "
        "ON project USING gin (lower(slug) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workspace_name_trgm "
        "ON workspace USING gin (lower(name) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_workspace_slug_trgm "
        "ON workspace USING gin (lower(slug) gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_datasource_name_trgm "
        "ON datasource USING gin (lower(name) gin_trgm_ops)"
    )


def downgrade() -> None:
    """Remove only indexes owned by this migration; preserve the shared extension."""
    op.execute("DROP INDEX IF EXISTS ix_datasource_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_workspace_slug_trgm")
    op.execute("DROP INDEX IF EXISTS ix_workspace_name_trgm")
    op.execute("DROP INDEX IF EXISTS ix_project_slug_trgm")
    op.execute("DROP INDEX IF EXISTS ix_project_name_trgm")
