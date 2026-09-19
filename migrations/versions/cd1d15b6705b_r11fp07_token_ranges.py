"""R11-FP07 token-grain ranges on routine and trigger lineage edges

Revision ID: cd1d15b6705b
Revises: b3e7f1a9c562
Create Date: 2026-09-19

`b3e7f1a9c562` located each edge at its *statement* in the stored, redacted body.
This narrows it to the *token*: inside that statement, where the edge's source is
named (the column reference it reads, or for a table-grain edge the table
reference) and where its target is (the column it writes, or the write target).
`deep_procedure_lineage_edge` and `trigger_lineage_edge` -- one mixin, so the two
cannot drift -- each gain, per end of the edge, half-open character offsets into
the same stored body the statement range indexes (and `statement_text_digest`
pins) and the token's kind (`COLUMN` or `TABLE`). Integers and a ten-character
code: nothing wide enough to hold body text (INV-6). Lines and columns are not
stored; they follow from an offset and the digest-pinned body.

**Nullable, no backfill, no server default.** NULL is the answer wherever a token
is not exactly one reference of its statement (the same table named twice, a
column read twice in one expression, a transitive edge's source, an edge read
from a called routine, an unparsed statement) and for every row written before
this revision: a token range is a property of a parse against a specific body,
and the next parse of the routine or trigger records it.

Downgrade drops the twelve columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cd1d15b6705b"
down_revision: str | Sequence[str] | None = "b3e7f1a9c562"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EDGE_TABLES = ("deep_procedure_lineage_edge", "trigger_lineage_edge")
_SIDES = ("source", "target")


def upgrade() -> None:
    for table in _EDGE_TABLES:
        for side in _SIDES:
            op.add_column(
                table, sa.Column(f"{side}_token_start_offset", sa.Integer(), nullable=True)
            )
            op.add_column(
                table, sa.Column(f"{side}_token_end_offset", sa.Integer(), nullable=True)
            )
            op.add_column(
                table, sa.Column(f"{side}_token_kind", sa.String(length=10), nullable=True)
            )


def downgrade() -> None:
    for table in _EDGE_TABLES:
        for side in reversed(_SIDES):
            op.drop_column(table, f"{side}_token_kind")
            op.drop_column(table, f"{side}_token_end_offset")
            op.drop_column(table, f"{side}_token_start_offset")
