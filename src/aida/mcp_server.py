"""
Atlas MCP Server (Model Context Protocol)
==========================================

Exposes the Atlas governed metadata catalog and published governed SQL tools
to external AI agents (Claude Desktop, Cursor, Agentforce, custom LLM clients)
over the standard Model Context Protocol (MCP) JSON-RPC 2.0 transport.

Architecture contract
---------------------
* All tool calls are routed through QueryExecutionGateway — the same
  deterministic AST guard, cost check, masking, and audit trail that
  the rest-of-API uses. MCP is NOT a side-door.
* All requests require the same OIDC Bearer token as the REST API.
  SecurityContext is resolved before any MCP method is dispatched.
* Only PUBLISHED GovernedToolVersion records are visible to MCP callers.
* Resources expose value-free metadata only (schema, column names,
  classifications, descriptions). No raw source values are returned.

MCP Protocol reference
-----------------------
https://spec.modelcontextprotocol.io/specification/2025-03-26/

Mounted at: POST /mcp   (stateless HTTP-SSE transport)

JSON-RPC methods implemented
-----------------------------
  initialize              — capability negotiation
  tools/list              — list all published governed tools
  tools/call              — call a governed tool through the gateway
  resources/list          — list catalog assets as MCP resources
  resources/read          — read metadata for a specific resource URI
  ping                    — liveness

Error codes (MCP standard)
---------------------------
  -32700  Parse error
  -32600  Invalid request
  -32601  Method not found
  -32602  Invalid params
  -32603  Internal error
  -32001  Access denied (Atlas extension)
  -32002  Resource not found (Atlas extension)
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_orchestrator import (
    AgentClarificationRequired,
    AgentOrchestrationResult,
    AgentPolicyRejected,
    GovernedAgentOrchestrator,
    ModelRouteUnavailable,
)
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.models import (
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    TableProfile,
)
from aida.security import SecurityContext, get_security_context

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# MCP protocol constants
# ---------------------------------------------------------------------------

MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_SERVER_NAME = "atlas-governed-data-platform"
MCP_SERVER_VERSION = "1.0.0"

# JSON-RPC error codes
_ERR_PARSE = -32700
_ERR_INVALID_REQUEST = -32600
_ERR_METHOD_NOT_FOUND = -32601
_ERR_INVALID_PARAMS = -32602
_ERR_INTERNAL = -32603
_ERR_ACCESS_DENIED = -32001
_ERR_NOT_FOUND = -32002

# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/mcp", tags=["mcp"])


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


# ---------------------------------------------------------------------------
# MCP capability handlers
# ---------------------------------------------------------------------------


def _handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    """Capability negotiation — return server capabilities."""
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {},
        },
        "serverInfo": {
            "name": MCP_SERVER_NAME,
            "version": MCP_SERVER_VERSION,
            "description": (
                "Atlas governed data platform. All tool calls route through the "
                "deterministic SQL execution gateway with prompt-risk screening, "
                "AST validation, PII masking, and immutable audit evidence."
            ),
        },
    }


async def _handle_tools_list(
    session: AsyncSession,
    context: SecurityContext,
) -> dict[str, Any]:
    """
    Return all PUBLISHED governed tools visible to the caller's organization.

    Each tool is represented as an MCP ToolDefinition with:
      - name: slugified tool name
      - description: tool description + governance attestation
      - inputSchema: JSON Schema derived from parameter_schema
    """
    rows = (
        await session.execute(
            select(GovernedToolVersion, GovernedTool)
            .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
            .where(
                GovernedToolVersion.organization_id == context.organization_id,
                GovernedToolVersion.status == "PUBLISHED",
            )
            .order_by(GovernedTool.slug, GovernedToolVersion.version.desc())
        )
    ).all()

    tools = []
    seen_slugs: set[str] = set()
    for version, tool in rows:
        # Only surface the latest published version of each slug
        if tool.slug in seen_slugs:
            continue
        seen_slugs.add(tool.slug)

        # Build JSON Schema from parameter_schema
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in (version.parameter_schema or []):
            param_name = param.get("name", "")
            param_type = param.get("type", "string").lower()
            param_desc = param.get("description", "")
            properties[param_name] = {
                "type": param_type,
                "description": param_desc,
            }
            if not param.get("optional", False):
                required.append(param_name)

        tools.append(
            {
                "name": f"atlas__{tool.slug}",
                "description": (
                    f"{version.description or version.name}\n\n"
                    "⚠ Governed: This tool executes through the Atlas deterministic "
                    "SQL gateway. Results are masked for PII/PHI. Execution is "
                    "immutably audited."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
                "_atlas_meta": {
                    "tool_version_id": str(version.id),
                    "tool_id": str(tool.id),
                    "datasource_id": str(version.datasource_id),
                    "version": version.version,
                    "status": version.status,
                },
            }
        )

    return {"tools": tools}


async def _handle_tools_call(
    params: dict[str, Any],
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    correlation_id: str,
) -> dict[str, Any]:
    """
    Execute a governed tool by name through the full Atlas orchestration stack.

    The caller supplies:
      name        — e.g. "atlas__get_quarterly_revenue_by_region"
      arguments   — dict matching the tool's inputSchema

    Execution path:
      1. Resolve tool slug → GovernedToolVersion
      2. Resolve datasource for the caller's organization
      3. GovernedAgentOrchestrator.run() with strategy=GOVERNED_TOOL
         - prompt_risk screening
         - SQL template rendering with typed parameter substitution
         - QueryExecutionGateway: AST guard → cost check → execution → PII masking
         - Immutable audit record
      4. Return MCP content with rows + governance attestation
    """
    tool_name: str = params.get("name", "")
    arguments: dict[str, Any] = params.get("arguments", {})

    if not tool_name.startswith("atlas__"):
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "Unknown tool. All Atlas tools are prefixed 'atlas__'.",
                }
            ],
        }

    slug = tool_name.removeprefix("atlas__")

    # Resolve tool version
    row = (
        await session.execute(
            select(GovernedToolVersion, GovernedTool)
            .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
            .where(
                GovernedToolVersion.organization_id == context.organization_id,
                GovernedToolVersion.status == "PUBLISHED",
                GovernedTool.slug == slug,
            )
            .order_by(GovernedToolVersion.version.desc())
            .limit(1)
        )
    ).first()

    if row is None:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Tool '{slug}' not found or not published."}],
        }

    version, tool = row

    # Resolve datasource
    datasource = await session.get(DataSource, version.datasource_id)
    if datasource is None or datasource.organization_id != context.organization_id:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "Datasource not accessible."}],
        }

    # Execute through the full governed orchestration stack
    orchestrator = GovernedAgentOrchestrator(settings)
    try:
        result: AgentOrchestrationResult = await orchestrator.run(
            session,
            datasource=datasource,
            context=context,
            correlation_id=correlation_id,
            question=f"[MCP] Execute governed tool: {version.name}",
            candidate_sql=None,
            preferred_tool_version_id=version.id,
            tool_parameters=arguments,
            requested_limit=None,
        )
    except AgentPolicyRejected as exc:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Blocked by prompt safety policy: {exc}"}],
        }
    except AgentClarificationRequired as exc:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Missing required parameters: {exc}"}],
        }
    except ModelRouteUnavailable as exc:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Tool execution failed: {exc}"}],
        }

    gw = result.gateway_result
    rows_data = list(gw.rows)

    # Build MCP content blocks
    content = []

    # 1. Governance attestation
    content.append(
        {
            "type": "text",
            "text": (
                f"✅ **Governed Execution Complete**\n"
                f"- Tool: `{version.name}` v{version.version}\n"
                f"- Rows returned: {gw.execution.row_count or 0}\n"
                f"- Masked columns: {', '.join(gw.masked_columns) or 'none'}\n"
                f"- Execution ID: `{gw.execution.id}`\n"
                f"- SQL hash: `{gw.execution.sql_hash[:16]}...`\n"
                f"- Plan cost: {gw.execution.plan_cost}\n"
                f"- Tables accessed: {', '.join(gw.execution.referenced_tables or [])}"
            ),
        }
    )

    # 2. Data as JSON
    content.append(
        {
            "type": "text",
            "text": f"```json\n{json.dumps(rows_data, indent=2, default=str)}\n```",
        }
    )

    return {"content": content}


async def _handle_resources_list(
    session: AsyncSession,
    context: SecurityContext,
) -> dict[str, Any]:
    """
    List value-free catalog metadata assets as MCP resources.

    Each resource URI follows: atlas://catalog/{datasource_id}/{schema}/{table}
    """
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .join(DataSource, DataSource.id == MetadataCatalog.datasource_id)
            .where(
                DataSource.organization_id == context.organization_id,
                MetadataTable.organization_id == context.organization_id,
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataCatalog.name, MetadataSchema.name, MetadataTable.name)
            .limit(500)  # bounded — MCP clients page
        )
    ).all()

    resources = []
    for table, schema, catalog in rows:
        datasource_id = str(catalog.datasource_id)
        uri = f"atlas://catalog/{datasource_id}/{schema.name}/{table.name}"
        resources.append(
            {
                "uri": uri,
                "name": f"{schema.name}.{table.name}",
                "description": (
                    f"Catalog: {catalog.name} | Schema: {schema.name} | "
                    f"Table: {table.name} | Type: {table.object_type}"
                ),
                "mimeType": "application/json",
            }
        )

    return {"resources": resources}


async def _handle_resources_read(
    params: dict[str, Any],
    session: AsyncSession,
    context: SecurityContext,
) -> dict[str, Any]:
    """
    Return value-free metadata for a specific atlas:// resource URI.

    Returns: catalog name, schema name, table name, object type,
             column names, types, nullable flags, classifications.
    No raw source values are ever returned.
    """
    uri: str = params.get("uri", "")
    if not uri.startswith("atlas://catalog/"):
        return {"contents": [{"uri": uri, "text": "Unknown resource URI scheme."}]}

    # Parse: atlas://catalog/{datasource_id}/{schema}/{table}
    parts = uri.removeprefix("atlas://catalog/").split("/")
    if len(parts) != 3:
        return {"contents": [{"uri": uri, "text": "Malformed resource URI."}]}

    datasource_id_str, schema_name, table_name = parts

    row = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataCatalog)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .join(DataSource, DataSource.id == MetadataCatalog.datasource_id)
            .where(
                DataSource.organization_id == context.organization_id,
                MetadataTable.organization_id == context.organization_id,
                MetadataTable.name == table_name,
                MetadataSchema.name == schema_name,
                MetadataCatalog.datasource_id == datasource_id_str,
                MetadataTable.status == "ACTIVE",
            )
        )
    ).first()

    if row is None:
        return {"contents": [{"uri": uri, "text": "Resource not found or not accessible."}]}

    table, schema, catalog = row

    columns = (
        await session.scalars(
            select(MetadataColumn)
            .where(
                MetadataColumn.table_id == table.id,
                MetadataColumn.status == "ACTIVE",
            )
            .order_by(MetadataColumn.ordinal_position)
        )
    ).all()

    # MetadataTable itself carries no row count — row_count_estimate lives on
    # TableProfile (one per completed scan), linked back via table_id.
    latest_profile = await session.scalar(
        select(TableProfile)
        .where(
            TableProfile.organization_id == context.organization_id,
            TableProfile.table_id == table.id,
            TableProfile.status == "COMPLETED",
        )
        .order_by(TableProfile.created_at.desc())
        .limit(1)
    )

    metadata_payload = {
        "catalog": catalog.name,
        "schema": schema.name,
        "table": table.name,
        "object_type": table.object_type,
        "row_count_estimate": latest_profile.row_count_estimate if latest_profile else None,
        "columns": [
            {
                "name": col.name,
                "ordinal_position": col.ordinal_position,
                "physical_type": col.physical_type,
                "nullable": col.nullable,
                "classification": col.classification,
                "business_description": None,
            }
            for col in columns
        ],
        "_governance": {
            "note": (
                "This resource returns metadata only. No source row values are "
                "exposed. To query data, use tools/call with a published governed tool."
            )
        },
    }

    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(metadata_payload, indent=2, default=str),
            }
        ]
    }


# ---------------------------------------------------------------------------
# Main HTTP endpoint
# ---------------------------------------------------------------------------


@router.post(
    "",
    summary="MCP JSON-RPC 2.0 endpoint",
    description=(
        "Model Context Protocol (MCP) server. Accepts JSON-RPC 2.0 requests over HTTP POST. "
        "Requires standard Atlas OIDC Bearer token authentication. "
        "Tool calls route through the full governed execution gateway."
    ),
)
async def mcp_endpoint(
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: SecurityContext = Depends(get_security_context),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    correlation_id = get_correlation_id()

    # Parse JSON body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content=_err(None, _ERR_PARSE, "Parse error: request body is not valid JSON"),
            status_code=400,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            content=_err(None, _ERR_INVALID_REQUEST, "Invalid Request: body must be a JSON object"),
            status_code=400,
        )

    rpc_id = body.get("id")
    method: str = body.get("method", "")
    params: dict[str, Any] = body.get("params") or {}
    jsonrpc = body.get("jsonrpc")

    if jsonrpc != "2.0":
        return JSONResponse(
            content=_err(rpc_id, _ERR_INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'"),
            status_code=400,
        )

    logger.info(
        "mcp_request",
        method=method,
        correlation_id=correlation_id,
        principal_id=str(context.principal_id),
    )

    try:
        # Dispatch to the appropriate handler
        if method == "initialize":
            result = _handle_initialize(params)

        elif method == "ping":
            result = {}

        elif method == "tools/list":
            result = await _handle_tools_list(session, context)

        elif method == "tools/call":
            result = await _handle_tools_call(
                params, session, context, settings, correlation_id
            )

        elif method == "resources/list":
            result = await _handle_resources_list(session, context)

        elif method == "resources/read":
            result = await _handle_resources_read(params, session, context)

        else:
            return JSONResponse(
                content=_err(rpc_id, _ERR_METHOD_NOT_FOUND, f"Method not found: {method}"),
                status_code=404,
            )

    except PermissionError as exc:
        logger.warning("mcp_access_denied", method=method, error=str(exc))
        return JSONResponse(
            content=_err(rpc_id, _ERR_ACCESS_DENIED, f"Access denied: {exc}"),
            status_code=403,
        )
    except Exception as exc:
        logger.exception("mcp_internal_error", method=method, error=str(exc))
        return JSONResponse(
            content=_err(rpc_id, _ERR_INTERNAL, "Internal error"),
            status_code=500,
        )

    return JSONResponse(content=_ok(rpc_id, result))
