"""R11-X2: retire five tables nothing used

Revision ID: c4e7b2d9a613
Revises: e5c8a1f4b927
Create Date: 2026-09-13

Two were never written and three were never read. Each is retired rather than
given a caller, on the product owner's delegation (2026-09-13):

* `isolation_boundary`, with `workspace.isolation_boundary_id` -- ADR-0018's hard
  wall. Nothing ever created a boundary and nothing enforced one, so the only
  reachable behaviour was a request refused because its boundary could not
  exist. ADR-0018's addendum records that a hard wall is built together with
  its enforcement.
* `business_assignment_rule`, with `business_assignment.rule_id` -- ADR-0018's
  rule-driven assignment. No rule was ever constructed and no caller supplied a
  rule id. Deferred by the same addendum, to be built with its evaluator.
* `contract_sla_record` -- inserted by every read of the SLA status endpoint and
  never read back. The status is a function of the violation ledger, so it is
  computed on read.
* `studio_test_run` -- a copy of a change-set test result that nothing read. The
  outcome and its counts are in the `studio.change_set.test` audit record, and
  each eval question's verdict stays in `studio_eval_result`.
* `procedure_tool_generation_record` -- provenance for a draft tool generated
  from a stored procedure, never read. It moves to the
  `governed_tool.version.generated_from_procedure` audit record.

All five were empty on the development estate when this was written, with no
workspace naming a boundary and no assignment naming a rule.

`downgrade` recreates the tables, keys and indexes as their original migrations
made them (`f1a2b3c4d5e6`, `e8f1a2b3c4d5`, `466f21849789`), empty. The two
restored columns come back last in their tables rather than in their original
position, which nothing reads by position.
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "c4e7b2d9a613"
down_revision: str | Sequence[str] | None = "e5c8a1f4b927"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def _timestamps() -> tuple[sa.Column[datetime], sa.Column[datetime]]:
    return (
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.func.now()),
    )


def upgrade() -> None:
    # Dropping a column drops the foreign key and the single-column index on it.
    op.drop_column("workspace", "isolation_boundary_id")
    op.drop_table("isolation_boundary")
    op.drop_column("business_assignment", "rule_id")
    op.drop_table("business_assignment_rule")
    op.drop_table("contract_sla_record")
    op.drop_table("studio_test_run")
    op.drop_table("procedure_tool_generation_record")


def downgrade() -> None:
    # --- procedure_tool_generation_record (466f21849789) ----------------------
    op.create_table(
        "procedure_tool_generation_record",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=False),
        sa.Column("tool_version_id", sa.Uuid(), nullable=False),
        sa.Column("sql_hash", sa.String(length=64), nullable=False),
        sa.Column("statement_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f("fk_procedure_tool_generation_record_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f("fk_procedure_tool_generation_record_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"],
            ["metadata_routine.id"],
            name=op.f("fk_procedure_tool_generation_record_routine_id_metadata_routine"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_version_id"],
            ["governed_tool_version.id"],
            name=op.f(
                "fk_procedure_tool_generation_record_tool_version_id_governed_tool_version"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_procedure_tool_generation_record")),
    )
    for name, column in (
        (op.f("ix_procedure_tool_generation_record_datasource_id"), "datasource_id"),
        (op.f("ix_procedure_tool_generation_record_organization_id"), "organization_id"),
        ("ix_procedure_tool_generation_record_routine", "routine_id"),
        (op.f("ix_procedure_tool_generation_record_routine_id"), "routine_id"),
        ("ix_procedure_tool_generation_record_tool_version", "tool_version_id"),
        (op.f("ix_procedure_tool_generation_record_tool_version_id"), "tool_version_id"),
    ):
        op.create_index(name, "procedure_tool_generation_record", [column], unique=False)

    # --- studio_test_run and contract_sla_record (e8f1a2b3c4d5, raw DDL) ------
    op.execute(
        """
        CREATE TABLE studio_test_run (
          id UUID PRIMARY KEY,
          organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
          change_set_id UUID NOT NULL REFERENCES studio_change_set(id) ON DELETE CASCADE,
          started_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ, passed BOOLEAN NOT NULL,
          evidence JSON NOT NULL, created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_studio_test_run_organization_id ON studio_test_run (organization_id)"
    )
    op.execute("CREATE INDEX ix_studio_test_run_change_set ON studio_test_run (change_set_id)")
    op.execute(
        "CREATE INDEX ix_studio_test_run_change_set_id ON studio_test_run (change_set_id)"
    )
    op.execute(
        """
        CREATE TABLE contract_sla_record (
          id UUID PRIMARY KEY,
          organization_id UUID NOT NULL REFERENCES organization(id) ON DELETE RESTRICT,
          contract_id UUID NOT NULL REFERENCES data_contract_version(id) ON DELETE CASCADE,
          period_start TIMESTAMPTZ NOT NULL, period_end TIMESTAMPTZ NOT NULL,
          uptime_percent FLOAT NOT NULL,
          violations_count INTEGER NOT NULL, breach_minutes INTEGER NOT NULL,
          created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_contract_sla_period UNIQUE (contract_id, period_start)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_contract_sla_org_contract "
        "ON contract_sla_record (organization_id, contract_id)"
    )
    op.execute(
        "CREATE INDEX ix_contract_sla_record_organization_id "
        "ON contract_sla_record (organization_id)"
    )
    op.execute(
        "CREATE INDEX ix_contract_sla_record_contract_id ON contract_sla_record (contract_id)"
    )

    # --- business_assignment_rule and its column (f1a2b3c4d5e6) ---------------
    op.create_table(
        "business_assignment_rule",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("business_node_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(80), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=False),
        sa.Column("match", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("auto_confirm", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", sa.String(255), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["business_node_id"], ["business_node.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "code"),
    )
    op.create_index(
        "ix_business_assignment_rule_organization_id",
        "business_assignment_rule",
        ["organization_id"],
    )
    op.create_index(
        "ix_business_assignment_rule_business_node_id",
        "business_assignment_rule",
        ["business_node_id"],
    )
    op.add_column("business_assignment", sa.Column("rule_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "business_assignment_rule_id_fkey",
        "business_assignment",
        "business_assignment_rule",
        ["rule_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- isolation_boundary and its column (f1a2b3c4d5e6) ---------------------
    op.create_table(
        "isolation_boundary",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False, server_default="STRICT"),
        sa.Column("description", sa.String(1000), nullable=False, server_default=""),
        sa.Column("status", sa.String(30), nullable=False, server_default="ACTIVE"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("organization_id", "code"),
    )
    op.create_index(
        "ix_isolation_boundary_organization_id", "isolation_boundary", ["organization_id"]
    )
    op.add_column("workspace", sa.Column("isolation_boundary_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "workspace_isolation_boundary_id_fkey",
        "workspace",
        "isolation_boundary",
        ["isolation_boundary_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_workspace_isolation_boundary_id", "workspace", ["isolation_boundary_id"])
