"""R11-GQL02: the caller-scoped record of one requested governed execution

Revision ID: a5d1c8e3f7b2
Revises: f4b8d2a6c913
Create Date: 2026-09-18

One table, declared in `aida.governed_execution_models`. A GraphQL execution mutation names its
request with an idempotency key; the first request claims the key and every later one with the
same key reads this record instead of executing again. An outcome the platform did not learn
stays PENDING and is never retried as a new execution.

**Value-free (INV-6).** The request is kept as an HMAC, never its parameter values, and no result
rows. `organization_id` is RESTRICT (INV-5), as is the tool version: the record is evidence of an
execution that happened, and must not vanish with the version it ran.

No backfill: the table starts empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a5d1c8e3f7b2"
down_revision: str | Sequence[str] | None = "f4b8d2a6c913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "governed_execution_request"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("principal_type", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("surface", sa.String(length=20), nullable=False),
        sa.Column("tool_version_id", sa.Uuid(), nullable=False),
        sa.Column("context_product_version_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("tool_execution_id", sa.Uuid(), nullable=True),
        sa.Column("query_execution_id", sa.Uuid(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("outcome_code", sa.String(length=100), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETED', 'REJECTED', 'FAILED')",
            name=op.f(f"ck_{_TABLE}_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f(f"fk_{_TABLE}_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tool_version_id"],
            ["governed_tool_version.id"],
            name=op.f(f"fk_{_TABLE}_tool_version_id_governed_tool_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
        sa.UniqueConstraint(
            "organization_id",
            "principal_type",
            "principal_id",
            "idempotency_key",
            name="uq_governed_execution_request_caller_key",
        ),
    )
    op.create_index(op.f(f"ix_{_TABLE}_organization_id"), _TABLE, ["organization_id"], unique=False)
    op.create_index(op.f(f"ix_{_TABLE}_tool_version_id"), _TABLE, ["tool_version_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{_TABLE}_tool_version_id"), table_name=_TABLE)
    op.drop_index(op.f(f"ix_{_TABLE}_organization_id"), table_name=_TABLE)
    op.drop_table(_TABLE)
