"""N3/N12: deep procedure lineage edges and tool generation provenance (Group I)

Two new, routine-identity-aware tables backing `procedure_lineage.py` (N3,
procedure-aware SQL lineage extraction) and `procedure_tool_blueprint.py`
(N12, procedure -> governed tool generation, gated on N3's read-only proof):

* `deep_procedure_lineage_edge`: one column-level (or, for a statement the
  parser could not resolve, statement-level `UNPARSED`) lineage fact per
  `MetadataRoutine`, carrying the statement it came from, whether that
  statement was a write, whether either side is a temp table/variable local
  to the procedure body, and -- INV-9/AT-C4's explicit-degradation
  invariant -- the named reason for any construct the parser gave up on.
  Deliberately a new table rather than an extension of the existing
  `procedure_lineage_edge` (AT-D2/AT-D5): that table has no routine identity
  at all (the gap AT-19 documents) and reusing it would mean editing an
  already-declared class body in `models.py`, the highest-collision-risk
  kind of edit for a module under concurrent edit (ST-05/06/07).
* `procedure_tool_generation_record`: provenance for one N12-generated
  `GovernedToolVersion` draft -- which routine it came from, and the exact
  redacted-body hash and statement count the read-only proof was computed
  against.

Revision ID: 466f21849789
Revises: 7e6460d905fe
Create Date: 2026-09-02 15:29:07.130551
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "466f21849789"
down_revision: str | None = "7e6460d905fe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
            ["datasource_id"], ["datasource.id"],
            name=op.f("fk_procedure_tool_generation_record_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"],
            name=op.f("fk_procedure_tool_generation_record_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"], ["metadata_routine.id"],
            name=op.f("fk_procedure_tool_generation_record_routine_id_metadata_routine"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tool_version_id"], ["governed_tool_version.id"],
            name=op.f(
                "fk_procedure_tool_generation_record_tool_version_id_governed_tool_version"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_procedure_tool_generation_record")),
    )
    op.create_index(
        op.f("ix_procedure_tool_generation_record_datasource_id"),
        "procedure_tool_generation_record", ["datasource_id"], unique=False,
    )
    op.create_index(
        op.f("ix_procedure_tool_generation_record_organization_id"),
        "procedure_tool_generation_record", ["organization_id"], unique=False,
    )
    op.create_index(
        "ix_procedure_tool_generation_record_routine",
        "procedure_tool_generation_record", ["routine_id"], unique=False,
    )
    op.create_index(
        op.f("ix_procedure_tool_generation_record_routine_id"),
        "procedure_tool_generation_record", ["routine_id"], unique=False,
    )
    op.create_index(
        "ix_procedure_tool_generation_record_tool_version",
        "procedure_tool_generation_record", ["tool_version_id"], unique=False,
    )
    op.create_index(
        op.f("ix_procedure_tool_generation_record_tool_version_id"),
        "procedure_tool_generation_record", ["tool_version_id"], unique=False,
    )
    op.create_table(
        "deep_procedure_lineage_edge",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("routine_id", sa.Uuid(), nullable=False),
        sa.Column("statement_ordinal", sa.Integer(), nullable=False),
        sa.Column("source_table", sa.String(length=500), nullable=False),
        sa.Column("source_column", sa.String(length=255), nullable=False),
        sa.Column("target_table", sa.String(length=500), nullable=False),
        sa.Column("target_column", sa.String(length=255), nullable=False),
        sa.Column("source_resolved", sa.Boolean(), nullable=False),
        sa.Column("source_table_id", sa.Uuid(), nullable=True),
        sa.Column("source_column_id", sa.Uuid(), nullable=True),
        sa.Column("target_table_id", sa.Uuid(), nullable=True),
        sa.Column("target_column_id", sa.Uuid(), nullable=True),
        sa.Column("transformation_type", sa.String(length=30), nullable=False),
        sa.Column("confidence", sa.String(length=30), nullable=False),
        sa.Column("dialect", sa.String(length=50), nullable=False),
        sa.Column("is_write", sa.Boolean(), nullable=False),
        sa.Column("is_intermediate", sa.Boolean(), nullable=False),
        sa.Column("control_flow_context", sa.String(length=30), nullable=True),
        sa.Column("unparsed_reason", sa.String(length=400), nullable=True),
        sa.Column("via_temp_table", sa.String(length=500), nullable=True),
        sa.Column("sql_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["datasource_id"], ["datasource.id"],
            name=op.f("fk_deep_procedure_lineage_edge_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"],
            name=op.f("fk_deep_procedure_lineage_edge_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["routine_id"], ["metadata_routine.id"],
            name=op.f("fk_deep_procedure_lineage_edge_routine_id_metadata_routine"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_column_id"], ["metadata_column.id"],
            name=op.f("fk_deep_procedure_lineage_edge_source_column_id_metadata_column"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_table_id"], ["metadata_table.id"],
            name=op.f("fk_deep_procedure_lineage_edge_source_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_column_id"], ["metadata_column.id"],
            name=op.f("fk_deep_procedure_lineage_edge_target_column_id_metadata_column"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_table_id"], ["metadata_table.id"],
            name=op.f("fk_deep_procedure_lineage_edge_target_table_id_metadata_table"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deep_procedure_lineage_edge")),
        sa.UniqueConstraint(
            "datasource_id", "routine_id", "statement_ordinal", "source_table", "source_column",
            "target_table", "target_column", "transformation_type", "via_temp_table",
            name="uq_deep_procedure_lineage_edge_natural_key",
        ),
    )
    op.create_index(
        "ix_deep_procedure_lineage_edge_datasource",
        "deep_procedure_lineage_edge", ["datasource_id"], unique=False,
    )
    op.create_index(
        op.f("ix_deep_procedure_lineage_edge_datasource_id"),
        "deep_procedure_lineage_edge", ["datasource_id"], unique=False,
    )
    op.create_index(
        "ix_deep_procedure_lineage_edge_org_target",
        "deep_procedure_lineage_edge", ["organization_id", "target_table_id"], unique=False,
    )
    op.create_index(
        op.f("ix_deep_procedure_lineage_edge_organization_id"),
        "deep_procedure_lineage_edge", ["organization_id"], unique=False,
    )
    op.create_index(
        "ix_deep_procedure_lineage_edge_routine",
        "deep_procedure_lineage_edge", ["routine_id"], unique=False,
    )
    op.create_index(
        op.f("ix_deep_procedure_lineage_edge_routine_id"),
        "deep_procedure_lineage_edge", ["routine_id"], unique=False,
    )
    op.create_index(
        op.f("ix_deep_procedure_lineage_edge_source_column_id"),
        "deep_procedure_lineage_edge", ["source_column_id"], unique=False,
    )
    op.create_index(
        op.f("ix_deep_procedure_lineage_edge_source_table_id"),
        "deep_procedure_lineage_edge", ["source_table_id"], unique=False,
    )
    op.create_index(
        op.f("ix_deep_procedure_lineage_edge_target_column_id"),
        "deep_procedure_lineage_edge", ["target_column_id"], unique=False,
    )
    op.create_index(
        op.f("ix_deep_procedure_lineage_edge_target_table_id"),
        "deep_procedure_lineage_edge", ["target_table_id"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_deep_procedure_lineage_edge_target_table_id"),
        table_name="deep_procedure_lineage_edge",
    )
    op.drop_index(
        op.f("ix_deep_procedure_lineage_edge_target_column_id"),
        table_name="deep_procedure_lineage_edge",
    )
    op.drop_index(
        op.f("ix_deep_procedure_lineage_edge_source_table_id"),
        table_name="deep_procedure_lineage_edge",
    )
    op.drop_index(
        op.f("ix_deep_procedure_lineage_edge_source_column_id"),
        table_name="deep_procedure_lineage_edge",
    )
    op.drop_index(
        op.f("ix_deep_procedure_lineage_edge_routine_id"),
        table_name="deep_procedure_lineage_edge",
    )
    op.drop_index(
        "ix_deep_procedure_lineage_edge_routine", table_name="deep_procedure_lineage_edge"
    )
    op.drop_index(
        op.f("ix_deep_procedure_lineage_edge_organization_id"),
        table_name="deep_procedure_lineage_edge",
    )
    op.drop_index(
        "ix_deep_procedure_lineage_edge_org_target", table_name="deep_procedure_lineage_edge"
    )
    op.drop_index(
        op.f("ix_deep_procedure_lineage_edge_datasource_id"),
        table_name="deep_procedure_lineage_edge",
    )
    op.drop_index(
        "ix_deep_procedure_lineage_edge_datasource", table_name="deep_procedure_lineage_edge"
    )
    op.drop_table("deep_procedure_lineage_edge")
    op.drop_index(
        op.f("ix_procedure_tool_generation_record_tool_version_id"),
        table_name="procedure_tool_generation_record",
    )
    op.drop_index(
        "ix_procedure_tool_generation_record_tool_version",
        table_name="procedure_tool_generation_record",
    )
    op.drop_index(
        op.f("ix_procedure_tool_generation_record_routine_id"),
        table_name="procedure_tool_generation_record",
    )
    op.drop_index(
        "ix_procedure_tool_generation_record_routine",
        table_name="procedure_tool_generation_record",
    )
    op.drop_index(
        op.f("ix_procedure_tool_generation_record_organization_id"),
        table_name="procedure_tool_generation_record",
    )
    op.drop_index(
        op.f("ix_procedure_tool_generation_record_datasource_id"),
        table_name="procedure_tool_generation_record",
    )
    op.drop_table("procedure_tool_generation_record")
