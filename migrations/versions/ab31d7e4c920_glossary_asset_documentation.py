"""governed glossary and versioned asset documentation

Revision ID: ab31d7e4c920
Revises: 9e4c7a12b5f8
Create Date: 2026-08-28 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ab31d7e4c920"
down_revision: str | Sequence[str] | None = "9e4c7a12b5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "glossary_term",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("term_key", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "term_key"),
    )
    op.create_index(op.f("ix_glossary_term_organization_id"), "glossary_term", ["organization_id"])

    op.create_table(
        "glossary_term_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("term_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("owner_principal", sa.String(255)),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["term_id"], ["glossary_term.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("term_id", "version"),
    )
    op.create_index(
        op.f("ix_glossary_term_version_organization_id"),
        "glossary_term_version",
        ["organization_id"],
    )
    op.create_index(op.f("ix_glossary_term_version_term_id"), "glossary_term_version", ["term_id"])
    op.create_index(
        "ix_glossary_term_version_org_status",
        "glossary_term_version",
        ["organization_id", "status"],
    )

    op.create_table(
        "asset_documentation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("table_id"),
    )
    op.create_index(
        op.f("ix_asset_documentation_organization_id"),
        "asset_documentation",
        ["organization_id"],
    )
    op.create_index(op.f("ix_asset_documentation_table_id"), "asset_documentation", ["table_id"])

    op.create_table(
        "asset_documentation_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("documentation_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("readme", sa.Text(), nullable=False),
        sa.Column("owner_principal", sa.String(255)),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.Column("approved_by", sa.String(255)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["documentation_id"], ["asset_documentation.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("documentation_id", "version"),
    )
    op.create_index(
        op.f("ix_asset_documentation_version_organization_id"),
        "asset_documentation_version",
        ["organization_id"],
    )
    op.create_index(
        op.f("ix_asset_documentation_version_documentation_id"),
        "asset_documentation_version",
        ["documentation_id"],
    )
    op.create_index(
        "ix_asset_documentation_version_org_status",
        "asset_documentation_version",
        ["organization_id", "status"],
    )

    op.create_table(
        "asset_term_link",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("table_id", sa.Uuid(), nullable=False),
        sa.Column("term_id", sa.Uuid(), nullable=False),
        sa.Column("linked_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["table_id"], ["metadata_table.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["term_id"], ["glossary_term.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("table_id", "term_id"),
    )
    op.create_index(
        op.f("ix_asset_term_link_organization_id"),
        "asset_term_link",
        ["organization_id"],
    )
    op.create_index(op.f("ix_asset_term_link_table_id"), "asset_term_link", ["table_id"])
    op.create_index(op.f("ix_asset_term_link_term_id"), "asset_term_link", ["term_id"])


def downgrade() -> None:
    op.drop_table("asset_term_link")
    op.drop_table("asset_documentation_version")
    op.drop_table("asset_documentation")
    op.drop_table("glossary_term_version")
    op.drop_table("glossary_term")
