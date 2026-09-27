"""R11-MP10: register upstream MCP servers and read their tool lists into the catalogue.

Four routes. Registration and discovery are platform-admin and agent-developer
acts, recorded in the audit trail; reading is open to the governance readers.
There is no route that invokes an upstream tool -- see `aida.external_mcp`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.external_mcp import (
    ExternalMcpRefused,
    discover_tools,
    record_discovery,
    resolve_server_credential,
    server_url_problem,
)
from aida.external_mcp_models import ExternalMcpServer, ExternalMcpTool
from aida.schemas import ApiModel
from aida.secrets import SecretResolutionError
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["external-mcp"])

_REGISTRARS = ("PlatformAdmin",)
_DISCOVERERS = ("PlatformAdmin", "AgentDeveloper")
_READERS = ("PlatformAdmin", "AgentDeveloper", "DataSteward", "Auditor")


class ExternalMcpServerCreate(ApiModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]{1,99}$")
    base_url: str = Field(min_length=8, max_length=1000)
    credential_reference: str | None = Field(default=None, max_length=1000)


class ExternalMcpServerRead(ApiModel):
    id: UUID
    organization_id: UUID
    name: str
    base_url: str
    uses_credential_reference: bool
    status: str
    server_name: str | None
    protocol_version: str | None
    last_discovered_at: datetime | None
    last_discovery_error: str | None
    discovered_tool_count: int


class ExternalMcpToolRead(ApiModel):
    id: UUID
    server_id: UUID
    name: str
    description: str | None
    input_schema: dict[str, Any]
    screening_status: str
    screening_reason_codes: list[str]
    status: str
    first_seen_at: datetime
    last_seen_at: datetime


class ExternalMcpDiscoveryRead(ApiModel):
    server_id: UUID
    listed: int
    new: int
    changed: int
    withdrawn: int
    quarantined: int


def _server_read(server: ExternalMcpServer, tool_count: int) -> ExternalMcpServerRead:
    return ExternalMcpServerRead(
        id=server.id,
        organization_id=server.organization_id,
        name=server.name,
        base_url=server.base_url,
        uses_credential_reference=server.credential_reference is not None,
        status=server.status,
        server_name=server.server_name,
        protocol_version=server.protocol_version,
        last_discovered_at=server.last_discovered_at,
        last_discovery_error=server.last_discovery_error,
        discovered_tool_count=tool_count,
    )


@router.post(
    "/organizations/{organization_id}/external-mcp-servers",
    response_model=ExternalMcpServerRead,
    status_code=status.HTTP_201_CREATED,
)
async def register_external_mcp_server(
    organization_id: UUID,
    body: ExternalMcpServerCreate,
    context: SecurityContext = Depends(require_roles(*_REGISTRARS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ExternalMcpServerRead:
    """Register an upstream server on an allowlisted host. Discovers nothing yet."""
    enforce_organization(context, organization_id)
    problem = server_url_problem(body.base_url, settings)
    if problem is not None:
        raise HTTPException(status_code=422, detail=f"server URL refused: {problem}")
    server = ExternalMcpServer(
        organization_id=organization_id,
        name=body.name,
        base_url=body.base_url,
        credential_reference=body.credential_reference,
        created_by=context.principal_id,
    )
    session.add(server)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="a server with that name exists") from exc
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="external_mcp.server.registered",
        resource_type="external_mcp_server",
        resource_id=str(server.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"name": server.name, "host_allowlisted": True},
    )
    await session.commit()
    return _server_read(server, 0)


@router.get(
    "/organizations/{organization_id}/external-mcp-servers",
    response_model=list[ExternalMcpServerRead],
)
async def list_external_mcp_servers(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*_READERS)),
    session: AsyncSession = Depends(get_session),
) -> list[ExternalMcpServerRead]:
    enforce_organization(context, organization_id)
    servers = (
        await session.scalars(
            select(ExternalMcpServer)
            .where(ExternalMcpServer.organization_id == organization_id)
            .order_by(ExternalMcpServer.name)
        )
    ).all()
    count_rows = (
        await session.execute(
            select(ExternalMcpTool.server_id, func.count())
            .where(
                ExternalMcpTool.organization_id == organization_id,
                ExternalMcpTool.status == "DISCOVERED",
            )
            .group_by(ExternalMcpTool.server_id)
        )
    ).all()
    counts: dict[UUID, int] = {server_id: int(count) for server_id, count in count_rows}
    return [_server_read(server, int(counts.get(server.id, 0))) for server in servers]


async def _server_in_scope(
    session: AsyncSession, server_id: UUID, context: SecurityContext
) -> ExternalMcpServer:
    server = await session.get(ExternalMcpServer, server_id)
    if server is None:
        raise HTTPException(status_code=404, detail="external MCP server not found")
    enforce_organization(context, server.organization_id)
    return server


@router.post(
    "/external-mcp-servers/{server_id}/discover",
    response_model=ExternalMcpDiscoveryRead,
)
async def discover_external_mcp_tools(
    server_id: UUID,
    context: SecurityContext = Depends(require_roles(*_DISCOVERERS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ExternalMcpDiscoveryRead:
    """Read the server's tool list now and record it. Invokes no tool."""
    server = await _server_in_scope(session, server_id, context)
    if server.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="the server is not active")
    audit_context = replace(context, organization_id=server.organization_id)
    try:
        credential = resolve_server_credential(server, settings)
        discovery = await discover_tools(server.base_url, settings, credential=credential)
    except (ExternalMcpRefused, SecretResolutionError) as exc:
        code = exc.code if isinstance(exc, ExternalMcpRefused) else "CREDENTIAL_UNAVAILABLE"
        server.last_discovery_error = code
        record_audit(
            session,
            audit_context,
            action="external_mcp.discovery.failed",
            resource_type="external_mcp_server",
            resource_id=str(server.id),
            outcome="FAILURE",
            correlation_id=get_correlation_id(),
            details={"code": code},
        )
        await session.commit()
        raise HTTPException(status_code=502, detail=f"discovery failed: {code}") from exc
    outcome = await record_discovery(session, server, discovery)
    record_audit(
        session,
        audit_context,
        action="external_mcp.discovery.completed",
        resource_type="external_mcp_server",
        resource_id=str(server.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "listed": outcome.listed,
            "new": outcome.new,
            "changed": outcome.changed,
            "withdrawn": outcome.withdrawn,
            "quarantined": outcome.quarantined,
        },
    )
    await session.commit()
    return ExternalMcpDiscoveryRead(
        server_id=server.id,
        listed=outcome.listed,
        new=outcome.new,
        changed=outcome.changed,
        withdrawn=outcome.withdrawn,
        quarantined=outcome.quarantined,
    )


@router.get(
    "/external-mcp-servers/{server_id}/tools",
    response_model=list[ExternalMcpToolRead],
)
async def list_external_mcp_tools(
    server_id: UUID,
    context: SecurityContext = Depends(require_roles(*_READERS)),
    session: AsyncSession = Depends(get_session),
) -> list[ExternalMcpToolRead]:
    server = await _server_in_scope(session, server_id, context)
    tools = (
        await session.scalars(
            select(ExternalMcpTool)
            .where(ExternalMcpTool.server_id == server.id)
            .order_by(ExternalMcpTool.name)
        )
    ).all()
    return [
        ExternalMcpToolRead(
            id=tool.id,
            server_id=tool.server_id,
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
            screening_status=tool.screening_status,
            screening_reason_codes=list(tool.screening_reason_codes),
            status=tool.status,
            first_seen_at=tool.first_seen_at,
            last_seen_at=tool.last_seen_at,
        )
        for tool in tools
    ]
