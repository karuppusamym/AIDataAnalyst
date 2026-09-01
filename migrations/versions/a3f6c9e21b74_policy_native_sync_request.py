"""QG-2: source-native row/column policy sync requests (maker-checker gate)

Revision ID: a3f6c9e21b74
Revises: 4f730e96ee9b
Create Date: 2026-08-31 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3f6c9e21b74"
down_revision: str | Sequence[str] | None = "4f730e96ee9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_native_sync_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("connector_type", sa.String(50), nullable=False),
        sa.Column("schema_name", sa.String(255), nullable=False),
        sa.Column("table_name", sa.String(255), nullable=False),
        sa.Column("statements", sa.JSON(), nullable=False),
        sa.Column("row_policy_count", sa.Integer(), nullable=False),
        sa.Column("column_policy_count", sa.Integer(), nullable=False),
        sa.Column("unsupported", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("request_reason", sa.String(2000), nullable=False),
        sa.Column("decided_by", sa.String(255)),
        sa.Column("decision_reason", sa.String(2000)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("apply_error", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["datasource_id"], ["datasource.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "datasource_id"):
        op.create_index(
            op.f(f"ix_policy_native_sync_request_{column}"),
            "policy_native_sync_request",
            [column],
        )
    op.create_index(
        "ix_policy_native_sync_request_org_status",
        "policy_native_sync_request",
        ["organization_id", "status"],
    )
    op.create_index(
        "ix_policy_native_sync_request_scope",
        "policy_native_sync_request",
        ["organization_id", "datasource_id", "schema_name", "table_name"],
    )


def downgrade() -> None:
    op.drop_table("policy_native_sync_request")
