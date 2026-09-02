"""ST-15: enforce the lineage edge_kind vocabulary at the database level

Adds a CHECK constraint to every ``edge_kind`` column on the four lineage-edge tables, enforcing
the one agreed vocabulary documented in ``Docs/30-contracts/06-lineage-contract.md`` §2:
``QUERY | VIEW | PROCEDURE | ETL | DBT | BI | AI_DECISION``. The columns only ever hold their
defaults today (``ETL`` for the OpenLineage edges, ``BI`` for the BI edges), both of which are in
the vocabulary, so no data backfill or repair is required before the constraint can be added.

This is the *lineage* edge-kind axis only. The separate relationship/grant edge-kind vocabulary
(``DECLARED_FOREIGN_KEY`` / ``SUGGESTED_RELATIONSHIP`` / ``APPROVED_RELATIONSHIP_CANDIDATE``) used
by ``CrossBoundaryGrant.edge_kinds`` is intentionally not constrained here.

Revision ID: d7b1e5a9c204
Revises: ca56d6ce3f18
Create Date: 2026-09-01 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d7b1e5a9c204"
down_revision: str | Sequence[str] | None = "ca56d6ce3f18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EDGE_KIND_TABLES: tuple[str, ...] = (
    "openlineage_table_edge",
    "openlineage_column_edge",
    "bi_report_metric_edge",
    "bi_metric_column_edge",
)

# Keep this literal in lockstep with aida.models.LINEAGE_EDGE_KINDS.
_VOCAB = ("QUERY", "VIEW", "PROCEDURE", "ETL", "DBT", "BI", "AI_DECISION")
_CHECK_SQL = "edge_kind IN (" + ", ".join(f"'{k}'" for k in _VOCAB) + ")"


def upgrade() -> None:
    for table in _EDGE_KIND_TABLES:
        op.create_check_constraint(f"ck_{table}_edge_kind", table, _CHECK_SQL)


def downgrade() -> None:
    for table in _EDGE_KIND_TABLES:
        op.drop_constraint(f"ck_{table}_edge_kind", table, type_="check")
