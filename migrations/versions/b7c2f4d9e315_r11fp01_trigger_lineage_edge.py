"""R11-FP01: what a trigger body reads and writes

Revision ID: b7c2f4d9e315
Revises: d4a8c1e6b520
Create Date: 2026-09-17

One table. `d4a8c1e6b520` captured triggers as native objects, including the
firing table -- so the table-to-trigger half of the path was known -- but nothing
handed a trigger body to the procedure parser, so what a trigger *writes* was
not. That was recorded as a declared gap; this closes it, and this table is where
the answer lands.

**Why not `deep_procedure_lineage_edge`.** That table's `routine_id` is NOT NULL
and is its identity: `footprint_gaps` counts distinct routines through it, the
lineage agent treats any row for a routine as "already parsed", and
`persist_routine_edges` replaces a routine's rows by it. A SQL Server or Oracle
trigger has no routine at all, so its edges would need that column NULL and would
drop out of every one of those counts; a PostgreSQL trigger's code lives in a
function that the agent must remain free to parse on its own axis, so filing the
trigger's edges under that function's id would make the function look done. The
same argument `aida.procedure_lineage_models` already makes against overloading
`procedure_lineage_edge`, one table along.

**`trigger_id` is NOT NULL and `routine_id` is nullable**, which is exactly the
inverse of that other table and is the point: the trigger owns the edge, and the
routine, where there is one, is only where the text was read from. PostgreSQL is
the engine with one -- `pg_trigger` keeps no body, the action is
`EXECUTE FUNCTION f()`, and `metadata_trigger.action_routine` names `f` -- so
`routine_id` records which body was actually parsed, and `via_routine` carries
its qualified name onto the edge. `ondelete` differs with that ownership:
dropping the trigger CASCADEs its edges away, while dropping the routine only
SET NULLs the reference, because the edges still describe the trigger.

**No body text and no column that could hold any.** The parse reads the stored
literal-redacted text and keeps names, ordinals and reason codes; `sql_hash` ties
a row back to the body version it came from without storing the body (INV-6).
`unparsed_reason` is 400 characters, matching the routine table, and carries a
reason *code* plus at most an identifier -- never a driver message and never a
value.

The review columns are the six ADR-0026 gives every parser-produced edge table,
with the same `server_default="ACTIVE"`: only ACTIVE edges steer retrieval and
tool generation, so an agent's undecided proposal influences nothing, and an
UNPARSED marker is ACTIVE because it records a gap rather than an edge and is
never put in front of a reviewer.

The natural key is `deep_procedure_lineage_edge`'s with `trigger_id` in place of
`routine_id`, for the reasons that one lists: a source->target pair may
legitimately recur at different statement ordinals within one body, and a direct
hop and its synthesised transitive edge share every other column.

No backfill. The table starts empty on every deployment and stays empty until the
lineage agent's TRIGGER_LINEAGE pass runs; there is nothing to migrate, because
nothing anywhere held this fact before.

`organization_id` is RESTRICT and `datasource_id` CASCADE, as on every other axis
(INV-5).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c2f4d9e315"
down_revision: str | Sequence[str] | None = "d4a8c1e6b520"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "trigger_lineage_edge"

#: The columns that carry their own single-column index because the ORM declares
#: `index=True` on them, in the order Alembic would emit them.
_INDEXED_COLUMNS = (
    "datasource_id",
    "organization_id",
    "routine_id",
    "source_table_id",
    "target_table_id",
    "trigger_id",
)


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=True),
        sa.Column("statement_ordinal", sa.Integer(), nullable=False),
        sa.Column("source_table", sa.String(length=500), nullable=False),
        sa.Column("source_column", sa.String(length=255), nullable=False),
        sa.Column("target_table", sa.String(length=500), nullable=False),
        sa.Column("target_column", sa.String(length=255), nullable=False),
        sa.Column("source_resolved", sa.Boolean(), nullable=False),
        sa.Column("source_table_id", sa.Uuid(), nullable=True),
        sa.Column("target_table_id", sa.Uuid(), nullable=True),
        sa.Column("transformation_type", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.String(length=30), nullable=False),
        sa.Column("dialect", sa.String(length=50), nullable=False),
        sa.Column("is_write", sa.Boolean(), nullable=False),
        sa.Column("is_intermediate", sa.Boolean(), nullable=False),
        sa.Column("control_flow_context", sa.String(length=30), nullable=True),
        sa.Column("unparsed_reason", sa.String(length=400), nullable=True),
        sa.Column("via_temp_table", sa.String(length=500), nullable=True),
        sa.Column("via_routine", sa.String(length=500), nullable=True),
        sa.Column("sql_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "review_status",
            sa.String(length=20),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.String(length=2000), nullable=True),
        sa.Column("previous_edge_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f(f"fk_{_TABLE}_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f(f"fk_{_TABLE}_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["previous_edge_id"],
            [f"{_TABLE}.id"],
            name=op.f(f"fk_{_TABLE}_previous_edge_id_{_TABLE}"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"],
            ["metadata_routine.id"],
            name=op.f(f"fk_{_TABLE}_routine_id_metadata_routine"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_table_id"],
            ["metadata_table.id"],
            name=op.f(f"fk_{_TABLE}_source_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_table_id"],
            ["metadata_table.id"],
            name=op.f(f"fk_{_TABLE}_target_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["trigger_id"],
            ["metadata_trigger.id"],
            name=op.f(f"fk_{_TABLE}_trigger_id_metadata_trigger"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
        sa.UniqueConstraint(
            "datasource_id",
            "trigger_id",
            "statement_ordinal",
            "source_table",
            "source_column",
            "target_table",
            "target_column",
            "transformation_type",
            "via_temp_table",
            name=f"uq_{_TABLE}_natural_key",
        ),
    )
    op.create_index(f"ix_{_TABLE}_datasource", _TABLE, ["datasource_id"], unique=False)
    op.create_index(
        f"ix_{_TABLE}_org_target", _TABLE, ["organization_id", "target_table_id"], unique=False
    )
    op.create_index(f"ix_{_TABLE}_review_status", _TABLE, ["review_status"], unique=False)
    op.create_index(f"ix_{_TABLE}_trigger", _TABLE, ["trigger_id"], unique=False)
    for column in _INDEXED_COLUMNS:
        op.create_index(op.f(f"ix_{_TABLE}_{column}"), _TABLE, [column], unique=False)


def downgrade() -> None:
    for column in reversed(_INDEXED_COLUMNS):
        op.drop_index(op.f(f"ix_{_TABLE}_{column}"), table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_trigger", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_review_status", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_org_target", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_datasource", table_name=_TABLE)
    op.drop_table(_TABLE)
