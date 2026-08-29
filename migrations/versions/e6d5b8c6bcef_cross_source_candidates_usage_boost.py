"""cross-source relationship candidates + usage-weighted scan priority (ADR-0017 phase 5)

Two independent additive changes that both close ADR-0017 SS8 ("discovery
cannot be scan everything, all the time"):

* relationship_candidate.target_datasource_id -- every existing row is a
  same-source candidate (relationship inference only ever ran within one
  datasource before phase 5), so it backfills equal to the existing
  datasource_id column and then becomes NOT NULL. A cross-source candidate
  discovered within a data_domain (never across one -- see
  intelligence_api.discover_cross_source_relationship_candidates) sets it to
  the *other* datasource, letting downstream traversal (unified_lineage_api)
  tell which datasource each end of the edge belongs to.

* scan_policy.usage_boost_enabled / computed_usage_boost / usage_boost_updated_at
  -- an opt-in, periodically-recomputed addend to the admin-set `priority`
  column the fleet scheduler already orders by (workflows/scheduler.py). All
  three default to their "no effect yet" values, so an existing policy's
  effective priority is unchanged until a steward opts it in.

Revision ID: e6d5b8c6bcef
Revises: 025cb1e553e9
Create Date: 2026-08-29 23:20:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6d5b8c6bcef"
down_revision: str | None = "025cb1e553e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- relationship_candidate.target_datasource_id ---
    op.add_column(
        "relationship_candidate", sa.Column("target_datasource_id", sa.Uuid(), nullable=True)
    )
    op.execute(
        sa.text(
            """
            UPDATE relationship_candidate
            SET target_datasource_id = datasource_id
            WHERE target_datasource_id IS NULL
            """
        )
    )
    op.alter_column("relationship_candidate", "target_datasource_id", nullable=False)
    op.create_foreign_key(
        op.f("fk_relationship_candidate_target_datasource_id_datasource"),
        "relationship_candidate",
        "datasource",
        ["target_datasource_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_relationship_candidate_target_datasource_id"),
        "relationship_candidate",
        ["target_datasource_id"],
        unique=False,
    )

    # --- scan_policy usage-weighted priority columns ---
    op.add_column(
        "scan_policy",
        sa.Column(
            "usage_boost_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    # base_priority backfills from the existing priority column -- every current
    # policy's admin-set value becomes its own baseline, so enabling usage boosting
    # later only ever adds on top of what the admin already chose.
    op.add_column("scan_policy", sa.Column("base_priority", sa.Integer(), nullable=True))
    op.execute(
        sa.text("UPDATE scan_policy SET base_priority = priority WHERE base_priority IS NULL")
    )
    op.alter_column("scan_policy", "base_priority", nullable=False, server_default="50")
    op.add_column(
        "scan_policy",
        sa.Column("computed_usage_boost", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "scan_policy", sa.Column("usage_boost_updated_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("scan_policy", "usage_boost_updated_at")
    op.drop_column("scan_policy", "computed_usage_boost")
    op.drop_column("scan_policy", "base_priority")
    op.drop_column("scan_policy", "usage_boost_enabled")

    op.drop_index(
        op.f("ix_relationship_candidate_target_datasource_id"),
        table_name="relationship_candidate",
    )
    op.drop_constraint(
        op.f("fk_relationship_candidate_target_datasource_id_datasource"),
        "relationship_candidate",
        type_="foreignkey",
    )
    op.drop_column("relationship_candidate", "target_datasource_id")
