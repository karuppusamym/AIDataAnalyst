"""R11-SQL01: the receipt a reviewed SQL statement runs on

Revision ID: f4b8d2a6c913
Revises: e3a9c7d51f02
Create Date: 2026-09-18

One table, declared in `aida.sql_workspace_models`. A generated or pasted SQL statement that the
query gateway validated -- without executing it -- gets a receipt binding a digest of the exact
statement, its row limit, its context product version and workspace to the caller and an expiry.
Run presents the statement again with the receipt and the gateway re-authorizes and re-validates
it in full, so the receipt is a precondition, never a bypass.

**No statement text (INV-6).** Only the digest and the redacted shape are stored; the caller
keeps the text. `organization_id` is RESTRICT, as on every other axis (INV-5); the datasource
foreign key CASCADEs, since a receipt for a deleted source can never run.

No backfill: the table starts empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4b8d2a6c913"
down_revision: str | Sequence[str] | None = "e3a9c7d51f02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "sql_draft_receipt"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("datasource_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("principal_type", sa.String(length=30), nullable=False),
        sa.Column("origin", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("statement_digest", sa.String(length=64), nullable=False),
        sa.Column("redacted_sql", sa.Text(), nullable=True),
        sa.Column("redaction_status", sa.String(length=20), nullable=False),
        sa.Column("context_product_version_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("max_rows", sa.Integer(), nullable=True),
        sa.Column("applied_row_limit", sa.Integer(), nullable=True),
        sa.Column("referenced_tables", sa.JSON(), nullable=False),
        sa.Column("finding_codes", sa.JSON(), nullable=False),
        sa.Column("estimate", sa.JSON(), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("query_execution_id", sa.Uuid(), nullable=True),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "origin IN ('GENERATED', 'PASTED')", name=op.f("ck_sql_draft_receipt_origin")
        ),
        sa.CheckConstraint(
            "status IN ('VALIDATED', 'EXECUTING', 'EXECUTED', 'FAILED')",
            name=op.f("ck_sql_draft_receipt_status"),
        ),
        sa.ForeignKeyConstraint(
            ["datasource_id"],
            ["datasource.id"],
            name=op.f(f"fk_{_TABLE}_datasource_id_datasource"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organization.id"],
            name=op.f(f"fk_{_TABLE}_organization_id_organization"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_TABLE}")),
    )
    op.create_index(
        "ix_sql_draft_receipt_principal",
        _TABLE,
        ["organization_id", "principal_id", "created_at"],
        unique=False,
    )
    op.create_index(op.f(f"ix_{_TABLE}_datasource_id"), _TABLE, ["datasource_id"], unique=False)
    op.create_index(op.f(f"ix_{_TABLE}_organization_id"), _TABLE, ["organization_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{_TABLE}_organization_id"), table_name=_TABLE)
    op.drop_index(op.f(f"ix_{_TABLE}_datasource_id"), table_name=_TABLE)
    op.drop_index("ix_sql_draft_receipt_principal", table_name=_TABLE)
    op.drop_table(_TABLE)
