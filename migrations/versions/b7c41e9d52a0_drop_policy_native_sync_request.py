"""drop policy_native_sync_request with the apply path it gated

Revision ID: b7c41e9d52a0
Revises: d81f5a2c9e47
Create Date: 2026-09-11 21:30:00.000000

Review 2026-09-11, defect D1. `policy_native_sync` used to open its own
asyncpg/pytds connection to a customer source and execute the row/column policy
DDL it generates -- a second SQL execution path, where INV-2 / ADR-0004 reserve
source execution for `aida.query_gateway`. Neither the import-linter contract
(which protects `connectors.execution_access`) nor the tier-0 method scan could
see it, because it imported a driver directly.

The execution half and the maker-checker request/decision endpoints that drove
it are gone; `POST .../native-policy-sync/preview` still generates the
statements for whoever owns DDL change control on that source. This table only
ever held those requests, so it has no remaining reader or writer.

Dropping rather than keeping: the rows record intents whose outcome column
(`APPLIED`/`APPLY_FAILED`) describes a capability the platform no longer has,
and the audit trail of what was applied lives in `audit_event`, which this
migration does not touch. `downgrade` recreates the table and its indexes, not
its rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7c41e9d52a0"
down_revision: str | Sequence[str] | None = "d81f5a2c9e47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "policy_native_sync_request"


def upgrade() -> None:
    op.drop_index("ix_policy_native_sync_request_scope", table_name=_TABLE)
    op.drop_index("ix_policy_native_sync_request_org_status", table_name=_TABLE)
    op.drop_table(_TABLE)


def downgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "datasource_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("data_source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connector_type", sa.String(length=50), nullable=False),
        sa.Column("schema_name", sa.String(length=255), nullable=False),
        sa.Column("table_name", sa.String(length=255), nullable=False),
        sa.Column("statements", sa.JSON(), nullable=False),
        sa.Column("row_policy_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("column_policy_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unsupported", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="PENDING"),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("request_reason", sa.String(length=2000), nullable=False),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("decision_reason", sa.String(length=2000), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apply_error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_policy_native_sync_request_org_status", _TABLE, ["organization_id", "status"]
    )
    op.create_index(
        "ix_policy_native_sync_request_scope", _TABLE, ["organization_id", "datasource_id"]
    )
    op.create_index(
        op.f("ix_policy_native_sync_request_organization_id"), _TABLE, ["organization_id"]
    )
