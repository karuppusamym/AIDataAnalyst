"""PR-2: policy-approved range/top-value profiling by classification

Revision ID: 97d1d2013a35
Revises: c0e0c5c27e56
Create Date: 2026-08-30 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "97d1d2013a35"
down_revision: str | Sequence[str] | None = "c0e0c5c27e56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "profiling_exception_policy",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("classification", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("request_reason", sa.String(2000), nullable=False),
        sa.Column("decided_by", sa.String(255)),
        sa.Column("decision_reason", sa.String(2000)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by", sa.String(255)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("revocation_reason", sa.String(2000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "datasource_id"):
        op.create_index(
            op.f(f"ix_profiling_exception_policy_{column}"), "profiling_exception_policy", [column]
        )
    op.create_index(
        "ix_profiling_exception_policy_scope",
        "profiling_exception_policy",
        ["organization_id", "datasource_id", "classification", "status"],
    )

    op.create_table(
        "column_value_profile_artifact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=False),
        sa.Column("column_profile_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sa.Uuid(), nullable=False),
        sa.Column("classification", sa.String(30), nullable=False),
        sa.Column("min_value", sa.Text()),
        sa.Column("max_value", sa.Text()),
        sa.Column("top_values", sa.JSON(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["column_id"], ["metadata_column.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["column_profile_id"], ["column_profile.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["policy_id"], ["profiling_exception_policy.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("column_profile_id"),
    )
    for column in (
        "organization_id",
        "datasource_id",
        "table_id",
        "column_id",
        "column_profile_id",
        "policy_id",
    ):
        op.create_index(
            op.f(f"ix_column_value_profile_artifact_{column}"),
            "column_value_profile_artifact",
            [column],
        )
    op.create_index(
        "ix_column_value_profile_artifact_org_expires",
        "column_value_profile_artifact",
        ["organization_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("column_value_profile_artifact")
    op.drop_table("profiling_exception_policy")
