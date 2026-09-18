"""R11-FP01: per-trigger parse coverage

Revision ID: c5e9a2f7d314
Revises: b7c2f4d9e315
Create Date: 2026-09-17

`trigger_parse_coverage` (`aida.procedure_lineage_models.TriggerParseCoverage`) is
`routine_parse_coverage` on the trigger axis: whether the body's parse completed,
whether it was proven read-only, how many statements it held, how many ended in an
UNPARSED marker, and the distinct `procedure_lineage.UnparsedReason` codes.

**Why a table and not a query.** Before this, "was this trigger fully understood?"
could only be re-derived from `trigger_lineage_edge`, and a trigger whose body
writes nothing leaves no row there at all -- so the gap register counted every
such trigger as waiting on the lineage agent forever, fully read or not.

**Why it carries `routine_id`.** On PostgreSQL a trigger's body is the function
`action_routine` names. When that function is redefined the trigger row does not
change, and `metadata_change_signal` records the change against the *routine* --
correctly, because that is the object that changed. This column is the join from
that signal to every trigger whose measurement it makes stale, which is why no
TRIGGER subject kind was added to that table's CHECK-constrained vocabulary: the
signal already exists, once, for the object that actually changed. `ondelete`
follows the edge table -- dropping the trigger CASCADEs its measurement away,
dropping the routine only SET NULLs the reference.

No backfill: a coverage figure is a property of a parse that ran, so a trigger
gains its row the next time the lineage agent examines it, and its absence reads
honestly as "not measured".

`organization_id` is RESTRICT and `datasource_id` CASCADE, as on every other axis
(INV-5). `downgrade` drops the table and the measurements in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e9a2f7d314"
down_revision: str | Sequence[str] | None = "b7c2f4d9e315"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "trigger_parse_coverage"
#: The columns the ORM declares `index=True` on, in the order Alembic emits them.
_INDEXED_COLUMNS = ("datasource_id", "organization_id", "routine_id", "trigger_id")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("trigger_id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=True),
        sa.Column("parse_completed", sa.Boolean(), nullable=False),
        sa.Column("is_read_only", sa.Boolean(), nullable=False),
        sa.Column("statement_count", sa.Integer(), nullable=False),
        sa.Column("unparsed_statement_count", sa.Integer(), nullable=False),
        sa.Column(
            "unparsed_reason_codes",
            sa.String(length=400),
            nullable=False,
            server_default="",
        ),
        sa.Column("dialect", sa.String(length=50), nullable=False),
        sa.Column("confidence", sa.String(length=30), nullable=False),
        sa.Column("sql_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "source_mapping_granularity",
            sa.String(length=40),
            nullable=False,
            server_default="STATEMENT_ORDINAL",
        ),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("measured_by", sa.String(length=255), nullable=True),
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
            ["routine_id"],
            ["metadata_routine.id"],
            name=op.f(f"fk_{_TABLE}_routine_id_metadata_routine"),
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
            name="uq_trigger_parse_coverage_trigger",
        ),
    )
    for column in _INDEXED_COLUMNS:
        op.create_index(op.f(f"ix_{_TABLE}_{column}"), _TABLE, [column], unique=False)
    op.create_index(
        "ix_trigger_parse_coverage_org_completed",
        _TABLE,
        ["organization_id", "parse_completed"],
        unique=False,
    )
    op.create_index(
        "ix_trigger_parse_coverage_datasource",
        _TABLE,
        ["datasource_id", "parse_completed"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_trigger_parse_coverage_datasource", table_name=_TABLE)
    op.drop_index("ix_trigger_parse_coverage_org_completed", table_name=_TABLE)
    for column in reversed(_INDEXED_COLUMNS):
        op.drop_index(op.f(f"ix_{_TABLE}_{column}"), table_name=_TABLE)
    op.drop_table(_TABLE)
