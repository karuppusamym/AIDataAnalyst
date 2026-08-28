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

from aida.mcp_server import (
    MCP_PROTOCOL_VERSION,
    _err,
    _handle_initialize,
    _ok,
    _tool_role_eligible,
)

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
