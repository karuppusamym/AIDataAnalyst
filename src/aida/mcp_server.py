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
from collections.abc import Sequence
from typing import Any
from uuid import UUID

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
from aida.context_product_policy import (
    ContextProductQualityDecision,
    evaluate_context_product_quality_from_db,
)
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductVersion,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
)
from aida.schemas import UnifiedLineageGraphRead, UnifiedLineageImpactRead
from aida.security import SecurityContext, get_security_context
from aida.unified_lineage_api import (
    UNIFIED_LINEAGE_READER_ROLES,
    LineageNodeNotFoundError,
    build_unified_lineage_graph_payload,
    build_unified_lineage_impact_payload,
)

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
# Native platform tools (not GovernedToolVersion-backed)
# ---------------------------------------------------------------------------
# CP-6 / EE.10 ("Lineage MCP"): Collibra's MCP server exposes upstream/
# downstream lineage and impact as callable tools, not only as a REST API.
# These wrap the exact same unified-lineage payload builders the REST routes
# in `unified_lineage_api.py` use (EA.14), so the graph an agent gets here is
# never allowed to drift from the one a human gets in the product. They are
# read-only and value-free: table/column/dbt-resource names only, never row
# values -- the same guarantee `resources/read` gives.
#
# Native lineage reads emit the same audit/outbox evidence as other MCP reads.

NATIVE_LINEAGE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "slug": "get_lineage_graph",
        "description": (
            "Return the unified lineage graph for a datasource: declared foreign keys, "
            "approved/candidate relationship suggestions, dbt manifest dependencies, and "
            "OpenLineage table edges merged into one node/edge set. Read-only and "
            "value-free -- table, column, and dbt-resource names only, never row values."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource_id": {"type": "string", "description": "Datasource UUID"},
                "node_limit": {
                    "type": "integer",
                    "description": "Max nodes to return, 5-2000 (default 300)",
                },
                "edge_limit": {
                    "type": "integer",
                    "description": "Max edges to return, 5-10000 (default 1500)",
                },
            },
            "required": ["datasource_id"],
            "additionalProperties": False,
        },
    },
    {
        "slug": "get_lineage_impact",
        "description": (
            "Compute transitive upstream and downstream lineage impact for one node -- a "
            "table, an unmatched dbt model/source, or an unresolved OpenLineage dataset -- "
            "across the unified lineage graph. Bounded multi-hop traversal, not a "
            "direct-reference count: answers 'what would break, N hops out, if this "
            "changed'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "datasource_id": {"type": "string", "description": "Datasource UUID"},
                "node_id": {
                    "type": "string",
                    "description": (
                        "A node id from get_lineage_graph: a table UUID, or a synthetic id "
                        "such as 'dbt:<uuid>' or 'openlineage:<namespace>:<name>'"
                    ),
                },
                "depth": {
                    "type": "integer",
                    "description": "Max traversal hops, 1-8 (default 5)",
                },
            },
            "required": ["datasource_id", "node_id"],
            "additionalProperties": False,
        },
    },
]
NATIVE_LINEAGE_TOOL_SLUGS = frozenset(item["slug"] for item in NATIVE_LINEAGE_TOOL_DEFINITIONS)


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
# Tool eligibility
# ---------------------------------------------------------------------------


def _tool_role_eligible(roles: frozenset[str], allowed_roles: Sequence[str]) -> bool:
    """
    Mirror the native governed-tool execution role binding
    (see POST /v1/tool-versions/{id}/execute in tool_api.py) so that an MCP
    client is offered, and may invoke, exactly the tools its identity is
    bound to -- no more, no less. ``PlatformAdmin`` is exempt, matching the
    same carve-out used by the native execution path.
    """
    if "PlatformAdmin" in roles:
        return True
    return not roles.isdisjoint(allowed_roles)


def _context_product_role_eligible(roles: frozenset[str], allowed_roles: Sequence[str]) -> bool:
    """Apply the same fail-closed role binding used by governed tools."""
    return _tool_role_eligible(roles, allowed_roles)


def _parse_context_product_uri(uri: str) -> tuple[str, int] | None:
    prefix = "atlas://context-products/"
    if not uri.startswith(prefix):
        return None
    parts = uri.removeprefix(prefix).split("/")
    if len(parts) != 3 or parts[1] != "versions":
        return None
    try:
        version_number = int(parts[2])
    except ValueError:
        return None
    if not parts[0] or version_number < 1:
        return None
    return parts[0], version_number


async def _resolve_context_product_scope(
    uri: str,
    session: AsyncSession,
    context: SecurityContext,
) -> tuple[ContextProductVersion, ContextProduct, ContextProductQualityDecision] | None:
    parsed = _parse_context_product_uri(uri)
    if parsed is None:
        return None
    product_key, version_number = parsed
    row = (
        await session.execute(
            select(ContextProductVersion, ContextProduct)
            .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
            .where(
                ContextProductVersion.organization_id == context.organization_id,
                ContextProduct.organization_id == context.organization_id,
                ContextProduct.product_key == product_key,
                ContextProduct.lifecycle_status == "ACTIVE",
                ContextProductVersion.version == version_number,
                ContextProductVersion.status == "PUBLISHED",
            )
        )
    ).first()
    if row is None:
        return None
    product_version, product = row
    if not _context_product_role_eligible(context.roles, product_version.allowed_consumer_roles):
        return None
    quality = await evaluate_context_product_quality_from_db(
        session,
        organization_id=product_version.organization_id,
        table_id_values=product_version.table_ids,
        requirements=product_version.quality_requirements,
    )
    if not quality.allowed:
        return None
    return product_version, product, quality


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
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Return all PUBLISHED governed tools visible to the caller's organization.

    Each tool is represented as an MCP ToolDefinition with:
      - name: slugified tool name
      - description: tool description + governance attestation
      - inputSchema: JSON Schema derived from parameter_schema
    """
    context_uri = str((params or {}).get("contextProductUri") or "")
    scoped_product = (
        await _resolve_context_product_scope(context_uri, session, context)
        if context_uri
        else None
    )
    if context_uri and scoped_product is None:
        return {"tools": []}
    eligible_version_ids = (
        {UUID(value) for value in scoped_product[0].eligible_tool_version_ids}
        if scoped_product is not None
        else None
    )
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

        # Eligible-tool exposure: an MCP client only ever sees tools its
        # identity is role-bound to invoke (CX-5). A tool the caller cannot
        # invoke is not merely hidden at call time -- it never appears in
        # the catalog it's offered, matching the module 12 principle that
        # policy filtering happens before the candidate set is built, not
        # after.
        if not _tool_role_eligible(context.roles, version.allowed_roles):
            continue
        if eligible_version_ids is not None and version.id not in eligible_version_ids:
            continue

        # Build JSON Schema from parameter_schema
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in version.parameter_schema or []:
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
                    "allowed_roles": sorted(version.allowed_roles),
                },
            }
        )

    # Native platform tools (CP-6 / EE.10): eligible-tool exposure applies
    # here exactly as it does above -- a caller whose roles are not bound to
    # read lineage never sees these tools offered, mirroring the governed-
    # tool role gate rather than introducing a second exposure rule.
    if eligible_version_ids is None and context.roles & set(UNIFIED_LINEAGE_READER_ROLES):
        for native in NATIVE_LINEAGE_TOOL_DEFINITIONS:
            tools.append(
                {
                    "name": f"atlas__{native['slug']}",
                    "description": native["description"],
                    "inputSchema": native["inputSchema"],
                    "_atlas_meta": {"kind": "NATIVE_PLATFORM_TOOL"},
                }
            )

    return {"tools": tools}


async def _handle_native_lineage_tool_call(
    slug: str,
    arguments: dict[str, Any],
    session: AsyncSession,
    context: SecurityContext,
    correlation_id: str = "mcp-lineage",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Execute one of the native lineage tools (CP-6 / EE.10).

    Same eligibility rule as `_handle_tools_list`, re-checked here because a
    tool call can arrive without a preceding `tools/list` in the same
    session; same anti-enumeration shape as the governed-tool path (an
    ineligible or unresolvable request gets the same wording either way, so
    role eligibility is never distinguishable from a bad datasource id).
    """

    if not (context.roles & set(UNIFIED_LINEAGE_READER_ROLES)):
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Tool '{slug}' not found or not published."}],
        }

    raw_datasource_id = arguments.get("datasource_id")
    try:
        datasource_id = UUID(str(raw_datasource_id))
    except (TypeError, ValueError):
        return {
            "isError": True,
            "content": [{"type": "text", "text": "datasource_id must be a UUID."}],
        }

    datasource = await session.get(DataSource, datasource_id)
    if datasource is None or datasource.organization_id != context.organization_id:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "Datasource not accessible."}],
        }

    payload: UnifiedLineageGraphRead | UnifiedLineageImpactRead
    try:
        if slug == "get_lineage_graph":
            node_limit = min(max(int(arguments.get("node_limit", 300)), 5), 2_000)
            edge_limit = min(max(int(arguments.get("edge_limit", 1_500)), 5), 10_000)
            payload = await build_unified_lineage_graph_payload(
                session,
                datasource,
                node_limit=node_limit,
                edge_limit=edge_limit,
                suggestion_status="APPROVED",
                settings=settings,
            )
        else:
            node_id = str(arguments.get("node_id") or "")
            if not node_id:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": "node_id is required."}],
                }
            depth = min(max(int(arguments.get("depth", 5)), 1), 8)
            payload = await build_unified_lineage_impact_payload(
                session,
                datasource,
                node_id,
                depth=depth,
                node_limit=200,
                settings=settings,
            )
    except LineageNodeNotFoundError as exc:
        return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    except (TypeError, ValueError) as exc:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Invalid arguments: {exc}"}],
        }

    body = payload.model_dump(mode="json")
    record_audit(
        session,
        context,
        action="mcp.lineage.read",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={"tool_slug": slug, "value_free": True},
    )
    record_outbox(
        session,
        organization_id=context.organization_id,
        aggregate_type="datasource",
        aggregate_id=str(datasource.id),
        event_type="lineage.consumed.v1",
        payload={
            "tool_slug": slug,
            "principal_id": context.principal_id,
            "channel": "MCP",
        },
    )
    await session.commit()
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    "\u2705 Unified lineage read (value-free: table/column names only, "
                    "no row values).\n"
                    f"- Tool: `atlas__{slug}`\n"
                    f"- Datasource: `{datasource.id}`"
                ),
            },
            {"type": "text", "text": f"```json\n{json.dumps(body, indent=2, default=str)}\n```"},
        ]
    }


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
    context_uri = str(params.get("contextProductUri") or "")
    scoped_product = (
        await _resolve_context_product_scope(context_uri, session, context)
        if context_uri
        else None
    )
    if context_uri and scoped_product is None:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Tool '{slug}' not found or not published."}],
        }

    if slug in NATIVE_LINEAGE_TOOL_SLUGS:
        if scoped_product is not None:
            return {
                "isError": True,
                "content": [
                    {"type": "text", "text": f"Tool '{slug}' not found or not published."}
                ],
            }
        return await _handle_native_lineage_tool_call(
            slug, arguments, session, context, correlation_id, settings
        )

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
    if scoped_product is not None:
        eligible_ids = {UUID(value) for value in scoped_product[0].eligible_tool_version_ids}
        if version.id not in eligible_ids:
            return {
                "isError": True,
                "content": [
                    {"type": "text", "text": f"Tool '{slug}' not found or not published."}
                ],
            }

    # Role-binding enforcement (CX-5, mirrors tool_api.py execute_tool).
    # A caller whose roles do not intersect the tool's allowed_roles gets
    # the identical "not found or not published" response used above --
    # never a distinguishable access-denied message. Telling an
    # unauthorized caller that a tool *does* exist, just not for them,
    # leaks its existence through a side channel exactly like ranking an
    # object the caller cannot see (module 12, section 6). The denial is
    # still recorded as evidence for operators.
    if not _tool_role_eligible(context.roles, version.allowed_roles):
        record_audit(
            session,
            context,
            action="mcp.tool_call.role_binding_denied",
            resource_type="governed_tool_version",
            resource_id=str(version.id),
            outcome="DENIED",
            correlation_id=correlation_id,
            details={
                "tool_slug": slug,
                "allowed_roles": sorted(version.allowed_roles),
                "principal_roles": sorted(context.roles),
            },
        )
        record_outbox(
            session,
            organization_id=context.organization_id,
            aggregate_type="governed_tool_version",
            aggregate_id=str(version.id),
            event_type="mcp.tool_invocation_denied.v1",
            payload={"tool_slug": slug, "principal_id": context.principal_id},
        )
        await session.commit()
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Tool '{slug}' not found or not published."}],
        }

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

    if scoped_product is not None:
        product_version, product, quality_decision = scoped_product
        session.add(
            ContextProductConsumptionEdge(
                organization_id=context.organization_id,
                context_product_version_id=product_version.id,
                principal_id=context.principal_id,
                principal_type=context.principal_type,
                channel="MCP_TOOL",
                correlation_id=correlation_id,
                product_fingerprint=product_version.fingerprint,
                policy_decision="ALLOW",
                quality_snapshot=quality_decision.snapshot(),
            )
        )
        record_outbox(
            session,
            organization_id=context.organization_id,
            aggregate_type="context_product_version",
            aggregate_id=str(product_version.id),
            event_type="context.product_tool_consumed.v1",
            payload={
                "product_key": product.product_key,
                "version": product_version.version,
                "tool_version_id": str(version.id),
                "principal_id": context.principal_id,
            },
        )
        await session.commit()

    return {"content": content}


async def _handle_resources_list(
    session: AsyncSession,
    context: SecurityContext,
) -> dict[str, Any]:
    """
    List value-free catalog metadata assets as MCP resources.

    Catalog URIs follow: atlas://catalog/{datasource_id}/{schema}/{table}
    Context Product URIs follow:
    atlas://context-products/{product_key}/versions/{version}
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

    product_rows = (
        await session.execute(
            select(ContextProductVersion, ContextProduct)
            .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
            .where(
                ContextProductVersion.organization_id == context.organization_id,
                ContextProductVersion.status == "PUBLISHED",
                ContextProduct.lifecycle_status == "ACTIVE",
            )
            .order_by(ContextProduct.product_key, ContextProductVersion.version.desc())
            .limit(200)
        )
    ).all()
    for version, product in product_rows:
        if not _context_product_role_eligible(context.roles, version.allowed_consumer_roles):
            continue
        resources.append(
            {
                "uri": (
                    f"atlas://context-products/{product.product_key}/versions/{version.version}"
                ),
                "name": f"{version.name} v{version.version}",
                "description": (
                    f"Governed Context Product: {version.description} | "
                    f"Owner: {version.owner_principal} | Fingerprint: {version.fingerprint}"
                ),
                "mimeType": "application/json",
            }
        )

    return {"resources": resources}


async def _handle_resources_read(
    params: dict[str, Any],
    session: AsyncSession,
    context: SecurityContext,
    correlation_id: str,
) -> dict[str, Any]:
    """
    Return value-free metadata for a specific atlas:// resource URI.

    Returns: catalog name, schema name, table name, object type,
             column names, types, nullable flags, classifications.
    No raw source values are ever returned.
    """
    uri: str = params.get("uri", "")
    if uri.startswith("atlas://context-products/"):
        return await _read_context_product_resource(uri, session, context, correlation_id)
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

    metadata_payload = {
        "catalog": catalog.name,
        "schema": schema.name,
        "table": table.name,
        "object_type": table.object_type,
        "row_count_estimate": table.row_count_estimate,
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


async def _read_context_product_resource(
    uri: str,
    session: AsyncSession,
    context: SecurityContext,
    correlation_id: str,
) -> dict[str, Any]:
    """Return a published, version-pinned, value-free Context Product."""
    inaccessible = {"contents": [{"uri": uri, "text": "Resource not found or not accessible."}]}
    parts = uri.removeprefix("atlas://context-products/").split("/")
    if len(parts) != 3 or parts[1] != "versions":
        return {"contents": [{"uri": uri, "text": "Malformed resource URI."}]}
    product_key, _, version_text = parts
    try:
        version_number = int(version_text)
    except ValueError:
        return {"contents": [{"uri": uri, "text": "Malformed resource URI."}]}
    if version_number < 1:
        return {"contents": [{"uri": uri, "text": "Malformed resource URI."}]}

    row = (
        await session.execute(
            select(ContextProductVersion, ContextProduct)
            .join(ContextProduct, ContextProduct.id == ContextProductVersion.product_id)
            .where(
                ContextProductVersion.organization_id == context.organization_id,
                ContextProduct.organization_id == context.organization_id,
                ContextProduct.product_key == product_key,
                ContextProduct.lifecycle_status == "ACTIVE",
                ContextProductVersion.version == version_number,
                ContextProductVersion.status == "PUBLISHED",
            )
        )
    ).first()
    if row is None:
        return inaccessible

    product_version, product = row
    if not _context_product_role_eligible(context.roles, product_version.allowed_consumer_roles):
        record_audit(
            session,
            context,
            action="mcp.context_product.role_binding_denied",
            resource_type="context_product_version",
            resource_id=str(product_version.id),
            outcome="DENIED",
            correlation_id=correlation_id,
            details={
                "product_key": product.product_key,
                "version": product_version.version,
                "allowed_roles": sorted(product_version.allowed_consumer_roles),
                "principal_roles": sorted(context.roles),
            },
        )
        record_outbox(
            session,
            organization_id=context.organization_id,
            aggregate_type="context_product_version",
            aggregate_id=str(product_version.id),
            event_type="context.product_consumption_denied.v1",
            payload={
                "product_key": product.product_key,
                "version": product_version.version,
                "principal_id": context.principal_id,
            },
        )
        await session.commit()
        return inaccessible

    quality_decision = await evaluate_context_product_quality_from_db(
        session,
        organization_id=product_version.organization_id,
        table_id_values=product_version.table_ids,
        requirements=product_version.quality_requirements,
    )
    if not quality_decision.allowed:
        record_audit(
            session,
            context,
            action="mcp.context_product.quality_denied",
            resource_type="context_product_version",
            resource_id=str(product_version.id),
            outcome="DENIED",
            correlation_id=correlation_id,
            details={
                "product_key": product.product_key,
                "version": product_version.version,
                "quality": quality_decision.snapshot(),
            },
        )
        record_outbox(
            session,
            organization_id=context.organization_id,
            aggregate_type="context_product_version",
            aggregate_id=str(product_version.id),
            event_type="context.product_consumption_denied.v1",
            payload={
                "product_key": product.product_key,
                "version": product_version.version,
                "principal_id": context.principal_id,
                "reasons": list(quality_decision.reasons),
            },
        )
        await session.commit()
        return inaccessible

    payload = {
        "product_key": product.product_key,
        "version": product_version.version,
        "name": product_version.name,
        "description": product_version.description,
        "purpose": product_version.purpose,
        "owner_principal": product_version.owner_principal,
        "fingerprint": product_version.fingerprint,
        "governed_references": {
            "table_ids": product_version.table_ids,
            "semantic_model_version_ids": product_version.semantic_model_version_ids,
            "glossary_term_version_ids": product_version.glossary_term_version_ids,
            "eligible_tool_version_ids": product_version.eligible_tool_version_ids,
        },
        "allowed_consumer_roles": product_version.allowed_consumer_roles,
        "lineage_depth": product_version.lineage_depth,
        "quality_requirements": product_version.quality_requirements,
        "quality_evaluation": quality_decision.snapshot(),
        "policy_summary": product_version.policy_summary,
        "_governance": {
            "status": product_version.status,
            "published_at": product_version.published_at,
            "note": (
                "This immutable resource contains governed metadata references only. "
                "Source values are available only through eligible governed tools."
            ),
        },
    }
    record_audit(
        session,
        context,
        action="mcp.context_product.read",
        resource_type="context_product_version",
        resource_id=str(product_version.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={
            "product_key": product.product_key,
            "version": product_version.version,
            "fingerprint": product_version.fingerprint,
        },
    )
    record_outbox(
        session,
        organization_id=context.organization_id,
        aggregate_type="context_product_version",
        aggregate_id=str(product_version.id),
        event_type="context.product_consumed.v1",
        payload={
            "product_key": product.product_key,
            "version": product_version.version,
            "fingerprint": product_version.fingerprint,
            "principal_id": context.principal_id,
        },
    )
    session.add(
        ContextProductConsumptionEdge(
            organization_id=context.organization_id,
            context_product_version_id=product_version.id,
            principal_id=context.principal_id,
            principal_type=context.principal_type,
            channel="MCP",
            correlation_id=correlation_id,
            product_fingerprint=product_version.fingerprint,
            policy_decision="ALLOW",
            quality_snapshot=quality_decision.snapshot(),
        )
    )
    await session.commit()
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(payload, indent=2, default=str),
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
            result = await _handle_tools_list(session, context, params)

        elif method == "tools/call":
            result = await _handle_tools_call(params, session, context, settings, correlation_id)

        elif method == "resources/list":
            result = await _handle_resources_list(session, context)

        elif method == "resources/read":
            result = await _handle_resources_read(params, session, context, correlation_id)

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
