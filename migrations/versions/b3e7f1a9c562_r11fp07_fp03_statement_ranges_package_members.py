"""R11-FP07 statement ranges, and R11-FP03 package-member attribution

Revision ID: b3e7f1a9c562
Revises: c6e2a9f41d37
Create Date: 2026-09-18

**Statement ranges (R11-FP07).** `deep_procedure_lineage_edge` and
`trigger_lineage_edge` gain where each edge's statement is in the body that was
parsed: half-open character offsets, 1-based start/end lines and columns, what
the range is the range *of* (`statement_range_status`: STATEMENT, GAP_STATEMENT,
CALL_SITE or NOT_LOCATED) and the SHA-256 of the text the offsets index
(`statement_text_digest`). That text is the stored, redacted body -- the only
text the parser is ever handed -- so the digest is what lets a reader prove a
range still points into the body as it now stands. Positions only: no excerpt,
no statement text (INV-6).

**No backfill, on purpose.** Existing rows keep NULL positions and read
`NOT_LOCATED` through the server default. A range is a property of a parse that
ran against a specific body; stamping one onto a row written before would claim a
location nobody measured. The next parse of the routine (or trigger) locates it,
and a decided edge that parse finds again is re-pointed in place.

**Package members (R11-FP03).** `deep_procedure_lineage_edge` gains
`package_member` (the member subprogram of an Oracle package the edge belongs to,
as the package body names it), `member_attribution` (MEMBER, PACKAGE_LEVEL or
PACKAGE_FALLBACK) and `member_routine_id` (the captured member routine, when
exactly one matches; SET NULL if that routine goes, like every other routine
reference on this table). The edge's owner stays `routine_id`, the package.

`routine_parse_coverage` and `trigger_parse_coverage` -- one shape by design,
which `tests/test_trigger_lineage_decidable.py` pins -- gain `member_attribution`
and `member_fallback_reason`, so "which packages are only understood as a
whole, and why" is a stored answer.

Downgrade drops every column added here and the index on `member_routine_id`.

**Chaining.** Authored against the working tree's single head `a5d1c8e3f7b2`, a
peer's uncommitted migration; the parent session re-chains at commit time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3e7f1a9c562"
down_revision: str | Sequence[str] | None = "c6e2a9f41d37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EDGE_TABLES = ("deep_procedure_lineage_edge", "trigger_lineage_edge")
_COVERAGE_TABLES = ("routine_parse_coverage", "trigger_parse_coverage")
_ROUTINE_EDGES = "deep_procedure_lineage_edge"
_POSITION_COLUMNS = (
    "statement_start_offset",
    "statement_end_offset",
    "statement_start_line",
    "statement_start_column",
    "statement_end_line",
    "statement_end_column",
)
_MEMBER_ROUTINE_INDEX = "ix_deep_procedure_lineage_edge_member_routine_id"


def upgrade() -> None:
    for table in _EDGE_TABLES:
        for column in _POSITION_COLUMNS:
            op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
        op.add_column(
            table,
            sa.Column(
                "statement_range_status",
                sa.String(length=20),
                nullable=False,
                server_default="NOT_LOCATED",
            ),
        )
        op.add_column(
            table, sa.Column("statement_text_digest", sa.String(length=64), nullable=True)
        )

    op.add_column(
        _ROUTINE_EDGES, sa.Column("package_member", sa.String(length=255), nullable=True)
    )
    op.add_column(
        _ROUTINE_EDGES, sa.Column("member_attribution", sa.String(length=30), nullable=True)
    )
    op.add_column(
        _ROUTINE_EDGES,
        sa.Column(
            "member_routine_id",
            sa.Uuid(),
            sa.ForeignKey("metadata_routine.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(_MEMBER_ROUTINE_INDEX, _ROUTINE_EDGES, ["member_routine_id"])

    for table in _COVERAGE_TABLES:
        op.add_column(
            table, sa.Column("member_attribution", sa.String(length=30), nullable=True)
        )
        op.add_column(
            table, sa.Column("member_fallback_reason", sa.String(length=40), nullable=True)
        )


def downgrade() -> None:
    for table in _COVERAGE_TABLES:
        op.drop_column(table, "member_fallback_reason")
        op.drop_column(table, "member_attribution")

    op.drop_index(_MEMBER_ROUTINE_INDEX, table_name=_ROUTINE_EDGES)
    op.drop_column(_ROUTINE_EDGES, "member_routine_id")
    op.drop_column(_ROUTINE_EDGES, "member_attribution")
    op.drop_column(_ROUTINE_EDGES, "package_member")

    for table in _EDGE_TABLES:
        op.drop_column(table, "statement_text_digest")
        op.drop_column(table, "statement_range_status")
        for column in reversed(_POSITION_COLUMNS):
            op.drop_column(table, column)
