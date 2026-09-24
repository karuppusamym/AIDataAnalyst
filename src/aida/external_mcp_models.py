"""R11-MP10: upstream MCP servers and the tools they offer, as catalogue records.

Two tables, registered on the same `Base` so Alembic and `create_all` see them:
one row per upstream server an organization registered, one row per tool that
server listed at its last discovery. A tool row is a record of what exists
upstream -- nothing in this platform can invoke it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from aida.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ExternalMcpServer(Base):
    __tablename__ = "external_mcp_server"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_external_mcp_server_org_name"),
        Index("ix_external_mcp_server_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    #: A secret reference (`vault://...`), never a secret. Nullable: an in-network
    #: server may need none.
    credential_reference: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    server_name: Mapped[str | None] = mapped_column(String(255))
    protocol_version: Mapped[str | None] = mapped_column(String(50))
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The class of the last discovery failure, never its message (which can carry data).
    last_discovery_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )


class ExternalMcpTool(Base):
    __tablename__ = "external_mcp_tool"
    __table_args__ = (
        UniqueConstraint("server_id", "name", name="uq_external_mcp_tool_server_name"),
        Index("ix_external_mcp_tool_org_status", "organization_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    server_id: Mapped[UUID] = mapped_column(
        ForeignKey("external_mcp_server.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: The upstream description, or NULL when screening withheld it.
    description: Mapped[str | None] = mapped_column(Text)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    screening_status: Mapped[str] = mapped_column(String(20), default="CLEAN", nullable=False)
    screening_reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    #: SHA-256 of the name, description and schema as listed -- a change upstream is visible.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: DISCOVERED while the server lists it; WITHDRAWN once a discovery no longer does.
    status: Mapped[str] = mapped_column(String(20), default="DISCOVERED", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False
    )
