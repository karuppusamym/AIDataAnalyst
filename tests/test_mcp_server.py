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

from aida import mcp_server
from aida.config import Settings
from aida.mcp_server import (
    MCP_PROTOCOL_VERSION,
    NATIVE_LINEAGE_TOOL_SLUGS,
    _err,
    _handle_initialize,
    _handle_native_lineage_tool_call,
    _handle_tools_call,
    _ok,
    _tool_role_eligible,
)
from aida.models import AuditEvent, DataSource, GovernedTool, GovernedToolVersion
from aida.schemas import UnifiedLineageGraphRead
from aida.security import SecurityContext
from aida.unified_lineage_api import LineageNodeNotFoundError

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


# ---------------------------------------------------------------------------
# Native lineage tools (CP-6 / EE.10) -- atlas__get_lineage_graph and
# atlas__get_lineage_impact, wired through _handle_native_lineage_tool_call.
# These wrap unified_lineage_api.build_unified_lineage_*_payload, which do
# real ORM queries; rather than stand up a database (this suite's
# established convention avoids that -- see the module docstring), success
# paths monkeypatch the payload builders and only exercise this function's
# own decision logic: role gating, argument parsing, and datasource scoping.
# ---------------------------------------------------------------------------


class LineageToolSession:
    """Fake session: only `.get()` is used by _handle_native_lineage_tool_call
    itself (the payload builders are monkeypatched in success-path tests, so
    they never see this session)."""

    def __init__(self, datasource: object) -> None:
        self.datasource = datasource
        self.added: list[object] = []
        self.committed = False

    async def get(self, _model: type[object], _identity: object) -> object:
        return self.datasource

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


def test_native_lineage_tool_slugs_match_declared_definitions() -> None:
    assert NATIVE_LINEAGE_TOOL_SLUGS == {"get_lineage_graph", "get_lineage_impact"}


async def test_native_lineage_tool_denies_ineligible_caller_like_an_unknown_tool() -> None:
    caller = SecurityContext(
        principal_id="viewer-with-no-lineage-role",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset(),  # disjoint from UNIFIED_LINEAGE_READER_ROLES
    )
    session = LineageToolSession(None)

    result = await _handle_native_lineage_tool_call(
        "get_lineage_graph",
        {"datasource_id": str(uuid4())},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result["isError"] is True
    assert result["content"] == [
        {"type": "text", "text": "Tool 'get_lineage_graph' not found or not published."}
    ]


async def test_native_lineage_tool_rejects_a_non_uuid_datasource_id() -> None:
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"Analyst"}),
    )
    session = LineageToolSession(None)

    result = await _handle_native_lineage_tool_call(
        "get_lineage_graph",
        {"datasource_id": "not-a-uuid"},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result == {
        "isError": True,
        "content": [{"type": "text", "text": "datasource_id must be a UUID."}],
    }


async def test_native_lineage_tool_rejects_a_datasource_in_another_organization() -> None:
    caller_org = uuid4()
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=caller_org,
        roles=frozenset({"Analyst"}),
    )
    foreign_datasource = DataSource(
        id=uuid4(),
        organization_id=uuid4(),  # different org
        project_id=uuid4(),
        connector_type="postgresql",
        name="someone else's warehouse",
        status="ACTIVE",
    )
    session = LineageToolSession(foreign_datasource)

    result = await _handle_native_lineage_tool_call(
        "get_lineage_graph",
        {"datasource_id": str(foreign_datasource.id)},
        session,  # type: ignore[arg-type]
        caller,
    )

    assert result["content"] == [{"type": "text", "text": "Datasource not accessible."}]


async def test_native_lineage_tool_requires_node_id_for_impact() -> None:
    org = uuid4()
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=org,
        roles=frozenset({"Analyst"}),
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org,
        project_id=uuid4(),
        connector_type="postgresql",
        name="warehouse",
        status="ACTIVE",
    )
    session = LineageToolSession(datasource)

    result = await _handle_native_lineage_tool_call(
        "get_lineage_impact",
        {"datasource_id": str(datasource.id)},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result == {
        "isError": True,
        "content": [{"type": "text", "text": "node_id is required."}],
    }


async def test_native_lineage_tool_get_lineage_graph_returns_the_payload_as_json(
    monkeypatch: object,
) -> None:
    org = uuid4()
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=org,
        roles=frozenset({"Analyst"}),
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org,
        project_id=uuid4(),
        connector_type="postgresql",
        name="warehouse",
        status="ACTIVE",
    )
    session = LineageToolSession(datasource)
    canned = UnifiedLineageGraphRead(
        datasource_id=datasource.id,
        nodes=[],
        edges=[],
        counts_by_source={
            "FOREIGN_KEY": 0,
            "SUGGESTED_RELATIONSHIP": 0,
            "DBT_DEPENDENCY": 0,
            "OPENLINEAGE_ETL": 0,
        },
        returned_node_count=0,
        returned_edge_count=0,
        node_limit=300,
        edge_limit=1_500,
        truncated=False,
    )

    async def _fake_builder(*_args: object, **_kwargs: object) -> UnifiedLineageGraphRead:
        return canned

    monkeypatch.setattr(mcp_server, "build_unified_lineage_graph_payload", _fake_builder)  # type: ignore[attr-defined]

    result = await _handle_native_lineage_tool_call(
        "get_lineage_graph",
        {"datasource_id": str(datasource.id)},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result["content"][0]["text"].startswith("\u2705 Unified lineage read")
    assert str(datasource.id) in result["content"][1]["text"]
    assert '"returned_node_count": 0' in result["content"][1]["text"]


async def test_native_lineage_tool_get_lineage_impact_surfaces_node_not_found(
    monkeypatch: object,
) -> None:
    org = uuid4()
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=org,
        roles=frozenset({"Analyst"}),
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org,
        project_id=uuid4(),
        connector_type="postgresql",
        name="warehouse",
        status="ACTIVE",
    )
    session = LineageToolSession(datasource)

    async def _fake_builder(*_args: object, **_kwargs: object) -> None:
        raise LineageNodeNotFoundError("lineage node 'bogus' not found in this datasource's graph")

    monkeypatch.setattr(mcp_server, "build_unified_lineage_impact_payload", _fake_builder)  # type: ignore[attr-defined]

    result = await _handle_native_lineage_tool_call(
        "get_lineage_impact",
        {"datasource_id": str(datasource.id), "node_id": "bogus"},
        session,  # type: ignore[arg-type]
        caller,
    )

    assert result == {
        "isError": True,
        "content": [
            {"type": "text", "text": "lineage node 'bogus' not found in this datasource's graph"}
        ],
    }
