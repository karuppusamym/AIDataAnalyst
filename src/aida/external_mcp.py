"""R11-MP10: read an upstream MCP server's tool list into the catalogue.

Atlas served MCP and could not read it. This is the client half, deliberately
narrow: it **discovers** -- `initialize` then `tools/list` -- and records what it
found. It has no `tools/call`. An upstream tool becomes a catalogue record a
person can see, never something an agent here can invoke.

The outbound connection is the risk, so it is fenced:

* **Allowlisted hosts only.** A server's host must appear in
  `Settings.mcp_client_allowed_hosts`, which is deployment configuration: the
  list is empty by default, and an empty list means no server can be
  registered or discovered at all.
* **HTTPS**, except in development and test.
* **No redirects**, a timeout, and a cap on the response size.
* **Credentials by reference.** A server's credential is a secret reference
  resolved at call time and sent as a bearer token; it is never stored or logged.
* **Screened descriptions.** A tool description is text a third party wrote,
  bound for people and, one day, models: each passes the same screen as source
  metadata, and a quarantined one is stored withheld.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.external_mcp_models import ExternalMcpServer, ExternalMcpTool
from aida.ingest_screening import screen_text
from aida.secrets import SecretResolver

MCP_PROTOCOL_VERSION: Final = "2025-03-26"
MAX_RESPONSE_BYTES: Final = 1_000_000
MAX_TOOLS: Final = 500
MAX_PAGES: Final = 20


class ExternalMcpRefused(ValueError):
    """The server may not be reached, or answered in a way that is not accepted.
    `code` is stable and value-free."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def server_url_problem(url: str, settings: Settings) -> str | None:
    """Why this URL may not be registered or reached, or None."""
    parts = urlsplit(url)
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        return "URL_INVALID"
    if parts.scheme == "http" and settings.environment not in {"development", "test"}:
        return "HTTPS_REQUIRED"
    if parts.username or parts.password:
        return "CREDENTIALS_IN_URL"
    allowed = {host.strip().lower() for host in settings.mcp_client_allowed_hosts if host.strip()}
    if parts.hostname.lower() not in allowed:
        return "HOST_NOT_ALLOWED"
    return None


def _decode(response: httpx.Response) -> dict[str, Any]:
    """A JSON-RPC response from a JSON body or a server-sent-events body."""
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ExternalMcpRefused("RESPONSE_TOO_LARGE", "the server's response was too large")
    content_type = response.headers.get("content-type", "")
    try:
        if "text/event-stream" in content_type:
            for block in response.text.split("\n\n"):
                data = "\n".join(
                    line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
                )
                if data:
                    message = json.loads(data)
                    if isinstance(message, dict) and ("result" in message or "error" in message):
                        return message
            raise ExternalMcpRefused("NO_RESPONSE", "the event stream held no response")
        body = response.json()
    except ValueError as exc:
        raise ExternalMcpRefused("INVALID_JSON", "the server did not answer JSON-RPC") from exc
    if not isinstance(body, dict):
        raise ExternalMcpRefused("INVALID_JSON", "the server did not answer JSON-RPC")
    return body


@dataclass(slots=True)
class _Session:
    client: httpx.AsyncClient
    url: str
    headers: dict[str, str]
    next_id: int = 1

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}
        self.next_id += 1
        response = await self.client.post(self.url, json=body, headers=self.headers)
        if response.status_code >= 300:
            raise ExternalMcpRefused(
                f"HTTP_{response.status_code}", f"the server answered HTTP {response.status_code}"
            )
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id
        message = _decode(response)
        if "error" in message:
            raise ExternalMcpRefused("RPC_ERROR", f"the server refused {method}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise ExternalMcpRefused("INVALID_RESULT", f"{method} returned no result object")
        return result

    async def notify(self, method: str) -> None:
        await self.client.post(
            self.url, json={"jsonrpc": "2.0", "method": method}, headers=self.headers
        )


@dataclass(frozen=True, slots=True)
class DiscoveredTool:
    name: str
    description: str | None
    input_schema: dict[str, Any]


@dataclass(slots=True)
class Discovery:
    server_name: str | None = None
    protocol_version: str | None = None
    tools: list[DiscoveredTool] = field(default_factory=list)


async def discover_tools(
    url: str,
    settings: Settings,
    *,
    credential: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> Discovery:
    """`initialize`, then `tools/list` page by page. Raises `ExternalMcpRefused`."""
    problem = server_url_problem(url, settings)
    if problem is not None:
        raise ExternalMcpRefused(problem, "the server may not be reached")
    headers = {"Accept": "application/json, text/event-stream"}
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    owned = client is None
    http = client or httpx.AsyncClient(
        timeout=settings.mcp_client_timeout_seconds, follow_redirects=False
    )
    try:
        session = _Session(http, url, headers)
        init = await session.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "atlas-catalogue", "version": "1"},
            },
        )
        await session.notify("notifications/initialized")
        info = init.get("serverInfo")
        discovery = Discovery(
            server_name=str(info.get("name"))[:255] if isinstance(info, dict) else None,
            protocol_version=str(init.get("protocolVersion"))[:50]
            if init.get("protocolVersion")
            else None,
        )
        cursor: str | None = None
        for _page in range(MAX_PAGES):
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            listed = await session.request("tools/list", params)
            for tool in listed.get("tools") or []:
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                    continue
                schema = tool.get("inputSchema")
                description = tool.get("description")
                discovery.tools.append(
                    DiscoveredTool(
                        name=tool["name"][:255],
                        description=description if isinstance(description, str) else None,
                        input_schema=schema if isinstance(schema, dict) else {},
                    )
                )
                if len(discovery.tools) >= MAX_TOOLS:
                    return discovery
            next_cursor = listed.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return discovery
    except httpx.HTTPError as exc:
        raise ExternalMcpRefused("UNREACHABLE", "the server could not be reached") from exc
    finally:
        if owned:
            await http.aclose()


def tool_fingerprint(tool: DiscoveredTool) -> str:
    payload = json.dumps(
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    listed: int
    new: int
    changed: int
    withdrawn: int
    quarantined: int


async def record_discovery(
    session: AsyncSession, server: ExternalMcpServer, discovery: Discovery
) -> DiscoveryOutcome:
    """Upsert what the server listed; mark what it no longer lists WITHDRAWN."""
    now = datetime.now(UTC)
    existing = {
        tool.name: tool
        for tool in (
            await session.scalars(
                select(ExternalMcpTool).where(ExternalMcpTool.server_id == server.id)
            )
        ).all()
    }
    new = changed = quarantined = 0
    seen: set[str] = set()
    for listed in discovery.tools:
        seen.add(listed.name)
        verdict = screen_text(listed.description, content_origin=f"external_mcp_tool:{listed.name}")
        description = listed.description if verdict.is_clean else None
        if not verdict.is_clean:
            quarantined += 1
        fingerprint = tool_fingerprint(listed)
        row = existing.get(listed.name)
        if row is None:
            session.add(
                ExternalMcpTool(
                    organization_id=server.organization_id,
                    server_id=server.id,
                    name=listed.name,
                    description=description,
                    input_schema=listed.input_schema,
                    screening_status=verdict.status,
                    screening_reason_codes=verdict.reason_codes,
                    fingerprint=fingerprint,
                    status="DISCOVERED",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            new += 1
            continue
        if row.fingerprint != fingerprint or row.status != "DISCOVERED":
            changed += 1
        row.description = description
        row.input_schema = listed.input_schema
        row.screening_status = verdict.status
        row.screening_reason_codes = verdict.reason_codes
        row.fingerprint = fingerprint
        row.status = "DISCOVERED"
        row.last_seen_at = now
    withdrawn = 0
    for name, row in existing.items():
        if name not in seen and row.status != "WITHDRAWN":
            row.status = "WITHDRAWN"
            withdrawn += 1
    server.server_name = discovery.server_name
    server.protocol_version = discovery.protocol_version
    server.last_discovered_at = now
    server.last_discovery_error = None
    await session.flush()
    return DiscoveryOutcome(
        listed=len(discovery.tools),
        new=new,
        changed=changed,
        withdrawn=withdrawn,
        quarantined=quarantined,
    )


def resolve_server_credential(server: ExternalMcpServer, settings: Settings) -> str | None:
    if not server.credential_reference:
        return None
    return SecretResolver(settings).resolve(server.credential_reference)
