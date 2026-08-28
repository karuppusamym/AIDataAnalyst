"""durable value-free data quality observations and incidents

Revision ID: 1b7e4c9a62d0
Revises: f2c8d5a93e71
Create Date: 2026-08-27 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1b7e4c9a62d0"
down_revision: str | Sequence[str] | None = "f2c8d5a93e71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("table_profile", sa.Column("schema_fingerprint", sa.String(64)))
    op.create_table(
        "data_quality_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid()),
        sa.Column("scope_key", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("volume_change_percent", sa.Float(), nullable=False),
        sa.Column("null_rate_change_percent", sa.Float(), nullable=False),
        sa.Column("schema_change_enabled", sa.Boolean(), nullable=False),
        sa.Column("metadata_scan_max_age_minutes", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("datasource_id", "scope_key"),
    )
    for column in ("organization_id", "datasource_id", "table_id"):
        op.create_index(op.f(f"ix_data_quality_policy_{column}"), "data_quality_policy", [column])
    op.create_index(
        "ix_data_quality_policy_org_enabled", "data_quality_policy", ["organization_id", "enabled"]
    )

    op.create_table(
        "data_quality_observation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_profile_id", sa.Uuid()),
        sa.Column("policy_id", sa.Uuid()),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("quality_score", sa.Integer(), nullable=False),
        sa.Column("anomaly_types", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("policy_snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["baseline_profile_id"], ["table_profile.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["policy_id"], ["data_quality_policy.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("analysis_run_id", "table_id"),
    )
    for column in (
        "organization_id",
        "datasource_id",
        "table_id",
        "analysis_run_id",
        "baseline_profile_id",
        "policy_id",
    ):
        op.create_index(
            op.f(f"ix_data_quality_observation_{column}"), "data_quality_observation", [column]
        )
    op.create_index(
        "ix_quality_observation_source_created",
        "data_quality_observation",
        ["datasource_id", "created_at"],
    )
    op.create_index(
        "ix_quality_observation_org_status",
        "data_quality_observation",
        ["organization_id", "status"],
    )

    op.create_table(
        "data_quality_incident",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid()),
        sa.Column("latest_observation_id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("anomaly_type", sa.String(50), nullable=False),
        sa.Column("severity", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_by", sa.String(255)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.String(255)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_reason", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["policy_id"], ["data_quality_policy.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["latest_observation_id"], ["data_quality_observation.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint"),
    )
    for column in (
        "organization_id",
        "datasource_id",
        "table_id",
        "policy_id",
        "latest_observation_id",
    ):
        op.create_index(
            op.f(f"ix_data_quality_incident_{column}"), "data_quality_incident", [column]
        )
    op.create_index(
        "ix_quality_incident_source_status", "data_quality_incident", ["datasource_id", "status"]
    )
    op.create_index(
        "ix_quality_incident_org_severity", "data_quality_incident", ["organization_id", "severity"]
    )


def downgrade() -> None:
    op.drop_table("data_quality_incident")
    op.drop_table("data_quality_observation")
    op.drop_table("data_quality_policy")
    op.drop_column("table_profile", "schema_fingerprint")
