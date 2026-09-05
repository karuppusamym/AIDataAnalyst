"""Column-level business description of record.

`MetadataColumn.source_description` holds the *source system's* own comment
and is overwritten by rediscovery. Until now there was no place at all for an
authored, reviewed column description: `DocumentClaim`'s docstring records
that an APPROVED column `DESCRIBES` claim's terminal state was the claim row
itself, because no column-level description surface existed to consume it.

This migration adds that store, as the column-level counterpart to the
existing `asset_documentation` / `asset_documentation_version` pair:

1. `column_documentation` -- identity/pointer row, one per column.
2. `column_documentation_version` -- append-only authored content, with
   `source_claim_id` tracing a published version back to the claim (and
   through it, the uploaded source text) that asserted it.

No backfill: there is no prior column-description content anywhere in the
schema to carry forward. Approved-but-unpublished `document_claim` rows
predating this migration stay where they are -- replaying them would be
publishing content under an approval that was granted when approval meant
something narrower.

Revision ID: b4e1c7a90d33
Revises: e6b1c390d7a2
Create Date: 2026-09-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4e1c7a90d33"
down_revision: str | Sequence[str] | None = "e6b1c390d7a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "column_documentation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("column_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["column_id"], ["metadata_column.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("column_id", name="uq_column_documentation_column_id"),
    )
    op.create_index(
        "ix_column_documentation_organization_id", "column_documentation", ["organization_id"]
    )
    op.create_index("ix_column_documentation_table_id", "column_documentation", ["table_id"])

    op.create_table(
        "column_documentation_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("documentation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("source_claim_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["documentation_id"], ["column_documentation.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["source_claim_id"], ["document_claim.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("documentation_id", "version"),
    )
    op.create_index(
        "ix_column_documentation_version_organization_id",
        "column_documentation_version",
        ["organization_id"],
    )
    op.create_index(
        "ix_column_documentation_version_documentation_id",
        "column_documentation_version",
        ["documentation_id"],
    )
    op.create_index(
        "ix_column_documentation_version_source_claim_id",
        "column_documentation_version",
        ["source_claim_id"],
    )
    op.create_index(
        "ix_column_documentation_version_org_status",
        "column_documentation_version",
        ["organization_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_column_documentation_version_org_status", table_name="column_documentation_version"
    )
    op.drop_index(
        "ix_column_documentation_version_source_claim_id",
        table_name="column_documentation_version",
    )
    op.drop_index(
        "ix_column_documentation_version_documentation_id",
        table_name="column_documentation_version",
    )
    op.drop_index(
        "ix_column_documentation_version_organization_id",
        table_name="column_documentation_version",
    )
    op.drop_table("column_documentation_version")
    op.drop_index("ix_column_documentation_table_id", table_name="column_documentation")
    op.drop_index("ix_column_documentation_organization_id", table_name="column_documentation")
    op.drop_table("column_documentation")
