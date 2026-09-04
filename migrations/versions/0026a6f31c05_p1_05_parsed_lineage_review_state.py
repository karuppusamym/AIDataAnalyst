"""p1-05 parsed lineage-edge review state + merge parallel heads

Revision ID: 0026a6f31c05
Revises: f0c8a2e91b74, c5243ed13a18
Create Date: 2026-09-04 12:00:00.000000

P1-05 / ADR-0026: add the review lifecycle columns (review_status,
reviewed_by, reviewed_at, review_reason, previous_edge_id, created_by)
and the review_status index to each of the five non-governed
parser-produced lineage edge tables (view_lineage_edge,
procedure_lineage_edge, dbt_lineage_edge, openlineage_table_edge,
openlineage_column_edge). Also adds Datasource.trusted_for_lineage so
`require_review` mode can trust connector-pushed lineage from a datasource
the operator has explicitly vouched for.

The other Wave-2 branches named in the original draft are already ancestors
of these two current heads through the existing tracker merge migration, so
they must not be repeated here as additional Alembic parents.

The `review_status` default (`server_default="ACTIVE"`) is what makes
this migration safe to apply live: every existing row keeps its
pre-P1-05 meaning (an active fact), and every new row written under
the default `auto_active` config continues to land ACTIVE too. Only
when the operator flips `AIDA_LINEAGE_PARSED_EDGES_REVIEW_MODE` to
`require_review` does the new column start to matter.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026a6f31c05"
down_revision: str | Sequence[str] | None = (
    "f0c8a2e91b74",
    "c5243ed13a18",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The five edge tables under review. Kept as a plain tuple here so the
# upgrade/downgrade functions run one identical column set per table --
# see ADR-0026 for why they are NOT folded under a supertype.
_EDGE_TABLES: tuple[str, ...] = (
    "view_lineage_edge",
    "procedure_lineage_edge",
    "dbt_lineage_edge",
    "openlineage_table_edge",
    "openlineage_column_edge",
)


def upgrade() -> None:
    for table in _EDGE_TABLES:
        # `review_status` has a server_default so every EXISTING row keeps
        # the pre-P1-05 meaning (an active, projection-eligible fact) the
        # moment this migration lands, without a backfill. New rows also
        # default to ACTIVE, then the parser overrides that where the
        # deployment has flipped into `require_review` mode.
        op.add_column(
            table,
            sa.Column(
                "review_status",
                sa.String(length=20),
                nullable=False,
                server_default="ACTIVE",
            ),
        )
        op.add_column(
            table,
            sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        )
        op.add_column(
            table,
            sa.Column(
                "reviewed_at", sa.DateTime(timezone=True), nullable=True
            ),
        )
        op.add_column(
            table,
            sa.Column("review_reason", sa.String(length=2000), nullable=True),
        )
        # Self-FK to the superseded edge -- the pointer that lets a
        # future re-review chain (an APPROVED edge that a later re-parse
        # supersedes) be walked without rehydrating the natural key.
        op.add_column(
            table,
            sa.Column(
                "previous_edge_id",
                sa.UUID(as_uuid=True),
                sa.ForeignKey(f"{table}.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        # `created_by` is the maker in the maker-checker rule and is
        # nullable because every pre-P1-05 row was written by a parser
        # with no principal-attribution column. The review endpoint
        # simply skips the check when it's NULL (a legacy row cannot
        # have been "written by" the reviewer, so the check is inert).
        op.add_column(
            table,
            sa.Column("created_by", sa.String(length=255), nullable=True),
        )
        op.create_index(
            f"ix_{table}_review_status",
            table,
            ["review_status"],
        )

    # Datasource.trusted_for_lineage -- read only under `require_review`
    # mode. Default false keeps every existing datasource untrusted; the
    # default `auto_active` mode ignores the flag entirely, so nothing
    # about behavior changes on the flag being unset.
    op.add_column(
        "datasource",
        sa.Column(
            "trusted_for_lineage",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("datasource", "trusted_for_lineage")
    for table in _EDGE_TABLES:
        op.drop_index(f"ix_{table}_review_status", table_name=table)
        op.drop_column(table, "created_by")
        op.drop_column(table, "previous_edge_id")
        op.drop_column(table, "review_reason")
        op.drop_column(table, "reviewed_at")
        op.drop_column(table, "reviewed_by")
        op.drop_column(table, "review_status")
