"""R11-MP10: upstream MCP servers and the tools they list

Revision ID: a8c4f1e2b7d9
Revises: e5b91c7d3a20
Create Date: 2026-09-24

Atlas can now read an upstream MCP server's tool list into the catalogue
(`aida.external_mcp`). Two tables: the servers an organization registered, and
the tools each listed at its last discovery. A tool row records what exists
upstream; nothing can invoke it. No backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c4f1e2b7d9"
down_revision: str | Sequence[str] | None = "e5b91c7d3a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_mcp_server",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("base_url", sa.String(length=1000), nullable=False),
        sa.Column("credential_reference", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("server_name", sa.String(length=255), nullable=True),
        sa.Column("protocol_version", sa.String(length=50), nullable=True),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_discovery_error", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "name", name="uq_external_mcp_server_org_name"),
    )
    op.create_index(
        "ix_external_mcp_server_org_status",
        "external_mcp_server",
        ["organization_id", "status"],
    )
    op.create_index(
        op.f("ix_external_mcp_server_organization_id"),
        "external_mcp_server",
        ["organization_id"],
    )
    op.create_table(
        "external_mcp_tool",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("server_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column("screening_status", sa.String(length=20), nullable=False),
        sa.Column("screening_reason_codes", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organization.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["server_id"], ["external_mcp_server.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("server_id", "name", name="uq_external_mcp_tool_server_name"),
    )
    op.create_index(
        "ix_external_mcp_tool_org_status", "external_mcp_tool", ["organization_id", "status"]
    )
    op.create_index(
        op.f("ix_external_mcp_tool_organization_id"), "external_mcp_tool", ["organization_id"]
    )
    op.create_index(op.f("ix_external_mcp_tool_server_id"), "external_mcp_tool", ["server_id"])


def downgrade() -> None:
    op.drop_table("external_mcp_tool")
    op.drop_table("external_mcp_server")
