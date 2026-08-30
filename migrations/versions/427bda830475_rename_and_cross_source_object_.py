"""rename detection and cross-source object resolution candidates (CT-4, CT-6)

* CT-4: `rename_candidate` -- proposes that a table tombstoned in an analysis run
  is really a table just created in the same run, renamed. Detected by
  `aida.workflows.activities.detect_rename_candidates`; only merged into
  `metadata_table.superseded_by_table_id` (new column, added here) and the
  downstream-link reassignment in `aida.identity_merge` when a steward
  approves it via the review endpoint -- never automatically.

* CT-6: `cross_source_resolution_candidate` -- proposes that a table in one
  datasource is the same logical asset as a table in another, via
  deterministic metadata-only structural matching. The catalog-identity
  analogue of `relationship_candidate`'s cross-source pairing; same
  maker-checker review shape.

Revision ID: 427bda830475
Revises: f371492245ae
Create Date: 2026-08-30 18:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "427bda830475"
down_revision: str | None = "f371492245ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- metadata_table.superseded_by_table_id (CT-4) ---
    op.add_column(
        "metadata_table", sa.Column("superseded_by_table_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_metadata_table_superseded_by_table_id_metadata_table"),
        "metadata_table",
        "metadata_table",
        ["superseded_by_table_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_metadata_table_superseded_by_table_id"),
        "metadata_table",
        ["superseded_by_table_id"],
        unique=False,
    )

    # --- rename_candidate (CT-4) ---
    op.create_table(
        "rename_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("schema_id", sa.Uuid(), nullable=False),
        sa.Column("old_table_id", sa.Uuid(), nullable=False),
        sa.Column("new_table_id", sa.Uuid(), nullable=False),
        sa.Column("detection_rule", sa.String(length=100), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("review_reason", sa.String(length=2000), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_run_id"], ["analysis_run.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["new_table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["old_table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["schema_id"], ["metadata_schema.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "old_table_id", "new_table_id", name="uq_rename_candidate_pair"
        ),
    )
    for column in (
        "organization_id",
        "datasource_id",
        "analysis_run_id",
        "schema_id",
        "old_table_id",
        "new_table_id",
    ):
        op.create_index(
            op.f(f"ix_rename_candidate_{column}"), "rename_candidate", [column], unique=False
        )
    op.create_index(
        "ix_rename_candidate_org_status",
        "rename_candidate",
        ["organization_id", "status"],
        unique=False,
    )

    # --- cross_source_resolution_candidate (CT-6) ---
    op.create_table(
        "cross_source_resolution_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("source_datasource_id", sa.Uuid(), nullable=False),
        sa.Column("source_table_id", sa.Uuid(), nullable=False),
        sa.Column("target_datasource_id", sa.Uuid(), nullable=False),
        sa.Column("target_table_id", sa.Uuid(), nullable=False),
        sa.Column("detection_rule", sa.String(length=100), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("reviewed_by", sa.String(length=255), nullable=True),
        sa.Column("review_reason", sa.String(length=2000), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organization.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["source_datasource_id"], ["datasource.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_table_id"], ["metadata_table.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_datasource_id"], ["datasource.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["target_table_id"], ["metadata_table.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_table_id", "target_table_id", name="uq_cross_source_resolution_pair"
        ),
    )
    for column in (
        "organization_id",
        "source_datasource_id",
        "source_table_id",
        "target_datasource_id",
        "target_table_id",
    ):
        op.create_index(
            op.f(f"ix_cross_source_resolution_candidate_{column}"),
            "cross_source_resolution_candidate",
            [column],
            unique=False,
        )
    op.create_index(
        "ix_cross_source_resolution_org_status",
        "cross_source_resolution_candidate",
        ["organization_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cross_source_resolution_org_status",
        table_name="cross_source_resolution_candidate",
    )
    for column in (
        "organization_id",
        "source_datasource_id",
        "source_table_id",
        "target_datasource_id",
        "target_table_id",
    ):
        op.drop_index(
            op.f(f"ix_cross_source_resolution_candidate_{column}"),
            table_name="cross_source_resolution_candidate",
        )
    op.drop_table("cross_source_resolution_candidate")

    op.drop_index("ix_rename_candidate_org_status", table_name="rename_candidate")
    for column in (
        "organization_id",
        "datasource_id",
        "analysis_run_id",
        "schema_id",
        "old_table_id",
        "new_table_id",
    ):
        op.drop_index(op.f(f"ix_rename_candidate_{column}"), table_name="rename_candidate")
    op.drop_table("rename_candidate")

    op.drop_index(
        op.f("ix_metadata_table_superseded_by_table_id"), table_name="metadata_table"
    )
    op.drop_constraint(
        op.f("fk_metadata_table_superseded_by_table_id_metadata_table"),
        "metadata_table",
        type_="foreignkey",
    )
    op.drop_column("metadata_table", "superseded_by_table_id")
