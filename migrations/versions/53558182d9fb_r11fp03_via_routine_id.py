"""R11-FP03: a spliced edge's callee, as its own routine id

Revision ID: 53558182d9fb
Revises: 92b060bb2e10
Create Date: 2026-09-19

`deep_procedure_lineage_edge` and `trigger_lineage_edge` already carry `via_routine`
(R11-FP07): the callee's *qualified name*, on an edge spliced in from a call this
routine (or, for a package, one of its members) makes into another routine's body.
Neither table carried the callee's own captured routine id -- only its caller's,
`routine_id`, unchanged since the edge is still filed under whoever made the call.
A reader who wanted the callee's identity had to re-resolve `via_routine`'s display
text against the catalog themselves, which cannot tell two same-named routines in
different scopes apart the way the parser and `aida.routine_call_descent` already
do at splice time.

`via_routine_id` is that id, nullable (most edges are not spliced and carry no
via-routine at all), `SET NULL` if the callee routine is later removed -- the same
rule every other routine reference on these tables already follows
(`member_routine_id`). Added to both edge tables, since both already carry
`via_routine` and both are spliced by the same descent module; a trigger has no
packages of its own, so its `via_routine_id` is only ever a direct cross-routine
splice, never a package-member one.

No backfill: existing spliced rows keep `via_routine_id` NULL until re-parsed, the
same rule every other position/identity column added after the fact in this table
family follows (`b3e7f1a9c562`) -- a value here is a claim about what a specific
parse resolved, and stamping one onto a row no parse actually produced it for would
be a guess.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "53558182d9fb"
down_revision: str | Sequence[str] | None = "92b060bb2e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EDGE_TABLES = ("deep_procedure_lineage_edge", "trigger_lineage_edge")


def _index_name(table: str) -> str:
    return f"ix_{table}_via_routine_id"


def upgrade() -> None:
    for table in _EDGE_TABLES:
        op.add_column(
            table,
            sa.Column(
                "via_routine_id",
                sa.Uuid(),
                sa.ForeignKey("metadata_routine.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(_index_name(table), table, ["via_routine_id"])


def downgrade() -> None:
    for table in _EDGE_TABLES:
        op.drop_index(_index_name(table), table_name=table)
        op.drop_column(table, "via_routine_id")
