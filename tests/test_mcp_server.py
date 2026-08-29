"""
Unit tests for the Atlas MCP server (src/aida/mcp_server.py).

This module has no database-integration test harness in this repository
(there is no conftest.py and no sqlite/test-database fixture anywhere in
tests/ -- every existing test either exercises pure functions directly, as
in tests/test_agent_intelligence.py's GovernedPlanner tests, or mocks its
collaborators, as in tests/test_model_gateway.py). These tests follow that
same established convention: they exercise the MCP server's pure,
DB-free decision logic directly rather than standing up a Postgres-backed
FastAPI TestClient.

Coverage before this file existed: none. src/aida/mcp_server.py is wired
into src/aida/main.py (mounted at POST /mcp) but had zero references from
any test in this suite.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from aida.config import Settings
from aida.mcp_server import (
    MCP_PROTOCOL_VERSION,
    _err,
    _handle_initialize,
    _handle_tools_call,
    _ok,
    _tool_role_eligible,
)
from aida.models import AuditEvent, GovernedTool, GovernedToolVersion
from aida.security import SecurityContext

# ---------------------------------------------------------------------------
# _tool_role_eligible -- mirrors the native role-binding check in
# tool_api.py's execute_tool (`"PlatformAdmin" not in roles and
# roles.isdisjoint(version.allowed_roles)`), which is the check this
# module's tools/list and tools/call handlers now also enforce.
# ---------------------------------------------------------------------------


def test_tool_role_eligible_platform_admin_is_always_exempt() -> None:
    # PlatformAdmin can invoke a tool bound to completely unrelated roles.
    assert _tool_role_eligible(frozenset({"PlatformAdmin"}), ["RiskAnalyst"]) is True


def test_tool_role_eligible_platform_admin_exempt_even_with_empty_binding() -> None:
    assert _tool_role_eligible(frozenset({"PlatformAdmin"}), []) is True


def test_tool_role_eligible_true_when_roles_intersect() -> None:
    assert _tool_role_eligible(frozenset({"Analyst", "Viewer"}), ["Analyst"]) is True


def test_tool_role_eligible_false_when_roles_are_disjoint() -> None:
    assert _tool_role_eligible(frozenset({"Viewer"}), ["RiskAnalyst", "RiskReviewer"]) is False


def test_tool_role_eligible_false_for_non_admin_with_no_roles() -> None:
    assert _tool_role_eligible(frozenset(), ["Analyst"]) is False


def test_tool_role_eligible_false_when_tool_has_no_allowed_roles() -> None:
    # An (invalid, but defensively handled) tool with an empty allowed_roles
    # binding is invocable by nobody except PlatformAdmin -- never silently
    # open to every caller.
    assert _tool_role_eligible(frozenset({"Analyst"}), []) is False


def test_tool_role_eligible_multiple_overlapping_roles() -> None:
    assert (
        _tool_role_eligible(
            frozenset({"RiskAnalyst", "Viewer"}),
            ["RiskAnalyst", "RiskReviewer"],
        )
        is True
    )


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 envelope helpers
# ---------------------------------------------------------------------------


def test_ok_envelope_is_valid_jsonrpc_2_0() -> None:
    envelope = _ok("req-1", {"tools": []})

    assert envelope == {"jsonrpc": "2.0", "id": "req-1", "result": {"tools": []}}


def test_err_envelope_omits_data_when_not_supplied() -> None:
    envelope = _err("req-2", -32601, "Method not found: bogus")

    assert envelope == {
        "jsonrpc": "2.0",
        "id": "req-2",
        "error": {"code": -32601, "message": "Method not found: bogus"},
    }
    assert "data" not in envelope["error"]


def test_err_envelope_includes_data_when_supplied() -> None:
    envelope = _err("req-3", -32602, "Invalid params", data={"field": "name"})

    assert envelope["error"]["data"] == {"field": "name"}


# ---------------------------------------------------------------------------
# initialize -- capability negotiation
# ---------------------------------------------------------------------------


def test_handle_initialize_declares_protocol_version_and_capabilities() -> None:
    result = _handle_initialize({})

    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["capabilities"]["resources"] == {"subscribe": False, "listChanged": False}
    assert result["serverInfo"]["name"] == "atlas-governed-data-platform"


def test_handle_initialize_result_is_independent_of_params() -> None:
    # initialize is capability negotiation, not per-caller state -- the
    # server's declared capabilities must not vary with client-supplied
    # params.
    assert _handle_initialize({}) == _handle_initialize({"clientInfo": {"name": "anything"}})


# ---------------------------------------------------------------------------
# tools/call -- anti-enumeration (CX-5): a caller must not be able to tell,
# from the response shape, whether a tool name doesn't exist at all or
# exists but is bound to roles the caller doesn't hold.
# ---------------------------------------------------------------------------


class ToolCallSession:
    """A fake session answering `_handle_tools_call`'s lookup-then-decide shape."""

    def __init__(self, row: object) -> None:
        self.row = row
        self.added: list[object] = []
        self.timeline: list[str] = []

    async def execute(self, _statement: object) -> object:
        row = self.row

        class _Result:
            def first(self_inner) -> object:
                return row

        return _Result()

    async def get(self, _model: type[object], _identity: object) -> object:
        return None

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.timeline.append("commit")


def _published_tool_version(
    *, slug: str, allowed_roles: list[str]
) -> tuple[GovernedToolVersion, GovernedTool]:
    organization_id = uuid4()
    tool = GovernedTool(id=uuid4(), organization_id=organization_id, project_id=uuid4(), slug=slug)
    version = GovernedToolVersion(
        id=uuid4(),
        organization_id=organization_id,
        tool_id=tool.id,
        version=1,
        status="PUBLISHED",
        name="Quarterly revenue by region",
        description="A sample governed tool.",
        datasource_id=uuid4(),
        sql_template="SELECT 1",
        referenced_tables=[],
        parameter_schema=[],
        allowed_roles=allowed_roles,
        fingerprint="tool-fingerprint",
        created_by="admin",
    )
    return version, tool


async def test_tools_call_reports_identical_response_for_unknown_and_denied_tool_names() -> None:
    slug = "quarterly_revenue_by_region"
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"Viewer"}),
    )
    settings = Settings(_env_file=None)

    unknown_session = ToolCallSession(None)
    unknown_result = await _handle_tools_call(
        {"name": f"atlas__{slug}", "arguments": {}},
        unknown_session,  # type: ignore[arg-type]
        caller,
        settings,
        "corr-unknown",
    )

    version, tool = _published_tool_version(slug=slug, allowed_roles=["RiskAnalyst"])
    denied_session = ToolCallSession((version, tool))
    denied_result = await _handle_tools_call(
        {"name": f"atlas__{slug}", "arguments": {}},
        denied_session,  # type: ignore[arg-type]
        caller,
        settings,
        "corr-denied",
    )

    # Byte-for-byte identical envelopes for the same requested name -- a
    # caller cannot distinguish "doesn't exist" from "exists, not for you".
    assert unknown_result == denied_result
    assert unknown_result["isError"] is True
    assert unknown_result["content"] == [
        {"type": "text", "text": f"Tool '{slug}' not found or not published."}
    ]

    # But the two cases are NOT actually identical server-side: the denial
    # is still recorded as operator evidence, unlike the true-unknown case.
    assert unknown_session.timeline == []
    assert unknown_session.added == []
    assert denied_session.timeline == ["commit"]
    audit = next(value for value in denied_session.added if isinstance(value, AuditEvent))
    assert audit.action == "mcp.tool_call.role_binding_denied"
    assert audit.outcome == "DENIED"
    assert audit.details["tool_slug"] == slug


async def test_tools_call_reaches_datasource_resolution_when_role_is_eligible() -> None:
    # Confirms the collapsed "not found" response above is specific to the
    # denial path, not a blanket bug that returns it for every request.
    slug = "quarterly_revenue_by_region"
    version, tool = _published_tool_version(slug=slug, allowed_roles=["Viewer"])
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=version.organization_id,
        roles=frozenset({"Viewer"}),
    )
    session = ToolCallSession((version, tool))  # .get() returns None => datasource missing

    result: dict[str, Any] = await _handle_tools_call(
        {"name": f"atlas__{slug}", "arguments": {}},
        session,  # type: ignore[arg-type]
        caller,
        Settings(_env_file=None),
        "corr-eligible",
    )

    assert result["content"] == [{"type": "text", "text": "Datasource not accessible."}]
