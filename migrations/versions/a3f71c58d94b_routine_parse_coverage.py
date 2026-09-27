"""Review 2026-09-16 F06.4: per-object routine parse coverage

Revision ID: a3f71c58d94b
Revises: b7d24e1c8f03
Create Date: 2026-09-16

`routine_parse_coverage` (`aida.procedure_lineage_models.RoutineParseCoverage`) stores how
completely one routine's body was understood: whether the parse completed, whether the body was
proven read-only, how many statements it held, how many ended in an UNPARSED marker, and the
distinct `procedure_lineage.UnparsedReason` codes it produced.

Those facts already existed -- `ProcedureParseResult.is_fully_parsed` and `.is_read_only` -- and
lived only in memory: they reached the parse endpoint's response and the lineage agent's ledger
entry and were then gone, so "which routines are not fully understood?" had to be re-derived by
scanning `deep_procedure_lineage_edge` for UNPARSED rows, which answers a different question once
a re-parse under review mode has replaced them.

No backfill. The measurement is a property of a parse, not of a stored body, so inventing rows
here would mean claiming a coverage figure for a parse that never ran -- exactly the finding this
table exists to answer. Existing routines gain a row the next time a person or the lineage agent
parses them, and their absence reads honestly as "not measured".

`downgrade` drops the table and the measurements in it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f71c58d94b"
down_revision: str | Sequence[str] | None = "b7d24e1c8f03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "routine_parse_coverage"
_INDEXED_COLUMNS = ("organization_id", "datasource_id", "routine_id")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=False),
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
            name=op.f("fk_routine_parse_coverage_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_routine_parse_coverage_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"],
            ["metadata_routine.id"],
            name=op.f("fk_routine_parse_coverage_routine_id_metadata_routine"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routine_parse_coverage")),
        sa.UniqueConstraint(
            "datasource_id",
            "routine_id",
            name="uq_routine_parse_coverage_routine",
        ),
    )
    for column in _INDEXED_COLUMNS:
        op.create_index(op.f(f"ix_{_TABLE}_{column}"), _TABLE, [column], unique=False)
    op.create_index(
        "ix_routine_parse_coverage_org_completed",
        _TABLE,
        ["organization_id", "parse_completed"],
        unique=False,
    )
    op.create_index(
        "ix_routine_parse_coverage_datasource",
        _TABLE,
        ["datasource_id", "parse_completed"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_routine_parse_coverage_datasource", table_name=_TABLE)
    op.drop_index("ix_routine_parse_coverage_org_completed", table_name=_TABLE)
    for column in reversed(_INDEXED_COLUMNS):
        op.drop_index(op.f(f"ix_{_TABLE}_{column}"), table_name=_TABLE)
    op.drop_table(_TABLE)
