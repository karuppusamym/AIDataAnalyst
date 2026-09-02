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

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from aida import mcp_server
from aida.asset_context import AssetContextSignals, ClassificationSummary
from aida.authorization_gate import AuthorizationDenied
from aida.config import Settings
from aida.mcp_server import (
    MCP_PROTOCOL_VERSION,
    NATIVE_LINEAGE_TOOL_SLUGS,
    _err,
    _handle_initialize,
    _handle_native_lineage_tool_call,
    _handle_resources_read,
    _handle_tools_call,
    _is_successful_consumption,
    _ok,
    _tool_role_eligible,
)
from aida.models import (
    AuditEvent,
    DataSource,
    DbtArtifactImport,
    DbtResource,
    GovernedTool,
    GovernedToolVersion,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    OutboxEvent,
    TableProfile,
)
from aida.schemas import (
    UnifiedLineageGraphRead,
    UnifiedLineageImpactNodeRead,
    UnifiedLineageImpactRead,
)
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


def test_consumption_evidence_excludes_denied_reads_and_failed_tools() -> None:
    assert not _is_successful_consumption(
        "resources/read", {"contents": [{"text": "Resource not found or not accessible."}]}
    )
    assert not _is_successful_consumption("prompts/get", {"messages": []})
    assert not _is_successful_consumption("tools/call", {"isError": True})
    assert _is_successful_consumption(
        "resources/read", {"contents": [{"mimeType": "application/json", "text": "{}"}]}
    )
    assert _is_successful_consumption("prompts/get", {"messages": [{"role": "user"}]})


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
    assert NATIVE_LINEAGE_TOOL_SLUGS == {
        "get_lineage_graph",
        "get_lineage_impact",
        "resolve_entity",
        "get_transformation_detail",
        "get_asset_context",
    }


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


async def test_newest_native_lineage_tools_deny_ineligible_caller_like_unknown_tools() -> None:
    caller = SecurityContext(
        principal_id="viewer-with-no-lineage-role",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset(),
    )
    session = LineageToolSession(None)
    scenarios = [
        ("resolve_entity", {"datasource_id": str(uuid4()), "query": "customers"}),
        (
            "get_transformation_detail",
            {"datasource_id": str(uuid4()), "entity_id": str(uuid4())},
        ),
        ("get_asset_context", {"table_id": str(uuid4())}),
    ]

    for slug, arguments in scenarios:
        result = await _handle_native_lineage_tool_call(
            slug,
            arguments,
            session,
            caller,  # type: ignore[arg-type]
        )

        assert result["isError"] is True
        assert result["content"] == [
            {"type": "text", "text": f"Tool '{slug}' not found or not published."}
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


async def test_native_lineage_tool_resolve_entity_validates_query_length() -> None:
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
        "resolve_entity",
        {"datasource_id": str(datasource.id), "query": "x"},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result == {
        "isError": True,
        "content": [{"type": "text", "text": "query must contain 2-200 characters."}],
    }


async def test_native_lineage_tool_resolve_entity_validates_entity_type() -> None:
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
        "resolve_entity",
        {
            "datasource_id": str(datasource.id),
            "query": "customers",
            "entity_type": "procedure",
        },
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result == {
        "isError": True,
        "content": [{"type": "text", "text": "entity_type is invalid."}],
    }


async def test_native_lineage_tool_resolve_entity_returns_the_payload_as_json(
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
    canned = {
        "query": "customers",
        "matches": [
            {
                "entity_id": str(uuid4()),
                "entity_type": "TABLE",
                "name": "customers",
                "qualified_name": "analytics.public.customers",
                "score": 1.0,
            }
        ],
        "candidate_scan_limit": 1_000,
        "truncated": False,
    }

    async def _fake_resolver(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return canned

    monkeypatch.setattr(mcp_server, "_resolve_governed_entities", _fake_resolver)  # type: ignore[attr-defined]

    result = await _handle_native_lineage_tool_call(
        "resolve_entity",
        {"datasource_id": str(datasource.id), "query": "customers"},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result["content"][0]["text"].startswith("\u2705 Unified lineage read")
    assert '"entity_type": "TABLE"' in result["content"][1]["text"]
    assert '"qualified_name": "analytics.public.customers"' in result["content"][1]["text"]
    assert session.committed is True
    assert len(session.added) == 2


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


async def test_native_lineage_tool_get_transformation_detail_rejects_a_non_uuid_entity_id() -> None:
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
        "get_transformation_detail",
        {"datasource_id": str(datasource.id), "entity_id": "not-a-uuid"},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result == {
        "isError": True,
        "content": [{"type": "text", "text": "entity_id must be a UUID."}],
    }


async def test_native_lineage_tool_get_transformation_detail_returns_the_payload_as_json(
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
    dbt_resource_id = uuid4()
    table_id = uuid4()
    canned = {
        "dbt_resource_id": str(dbt_resource_id),
        "lineage_node_id": str(table_id),
        "unique_id": "model.analytics.customers",
        "resource_type": "model",
        "name": "customers",
        "relation_name": "analytics.public.customers",
        "materialization": "table",
        "description": "Customer dimension",
        "compiled_sql_hash": "sha256:abc123",
        "compiled_sql_redacted": "select * from redacted_source",
        "sql_parse_status": "REDACTED",
        "depends_on_unique_ids": ["source.analytics.raw_customers"],
        "test_status": "PASS",
        "test_failures": 0,
        "artifact": {
            "artifact_import_id": str(uuid4()),
            "manifest_fingerprint": "manifest-123",
            "dbt_version": "1.9.0",
        },
        "governance": {
            "value_free": True,
            "compiled_sql_literals_redacted": True,
            "raw_artifact_persisted": False,
        },
    }

    async def _fake_detail(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return canned

    monkeypatch.setattr(mcp_server, "_transformation_detail", _fake_detail)  # type: ignore[attr-defined]

    result = await _handle_native_lineage_tool_call(
        "get_transformation_detail",
        {"datasource_id": str(datasource.id), "entity_id": str(dbt_resource_id)},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result["content"][0]["text"].startswith("\u2705 Unified lineage read")
    assert (
        '"compiled_sql_redacted": "select * from redacted_source"'
        in result["content"][1]["text"]
    )
    assert '"value_free": true' in result["content"][1]["text"]
    assert session.committed is True
    assert len(session.added) == 2


async def test_native_lineage_tool_get_transformation_detail_surfaces_not_found(
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

    async def _fake_detail(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(mcp_server, "_transformation_detail", _fake_detail)  # type: ignore[attr-defined]

    result = await _handle_native_lineage_tool_call(
        "get_transformation_detail",
        {"datasource_id": str(datasource.id), "entity_id": str(uuid4())},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result == {
        "isError": True,
        "content": [{"type": "text", "text": "Transformation not found or accessible."}],
    }


# ---------------------------------------------------------------------------
# EE.10 leak test -- for `resolve_entity` and `get_transformation_detail`
# specifically, the role-eligibility check in
# `_handle_native_lineage_tool_call` must run and return *before* any
# traversal/lookup work (datasource resolution, `_resolve_governed_entities`,
# `_transformation_detail`). A caller who lacks a lineage-reader role must be
# unable to distinguish "this entity exists but you can't see it" from "this
# entity doesn't exist" -- proven two ways: (1) byte-for-byte identical
# responses for an entity standing in for "real" versus one standing in for
# "missing", for the same denied caller, mirroring
# `test_tools_call_reports_identical_response_for_unknown_and_denied_tool_names`
# above; and (2) the traversal/lookup collaborator and the session are never
# touched at all -- a spy session raises if `.get()` is called, and the
# monkeypatched resolver/detail function raises if invoked, so either
# assertion would fail loudly if the check moved after traversal.
# ---------------------------------------------------------------------------


class DeniedCallerSpySession:
    """Fake session for a denied caller: any DB access at all -- even the
    datasource lookup that would precede real traversal -- is a bug, so
    `.get()` raises rather than silently returning None."""

    def __init__(self) -> None:
        self.get_called = False

    async def get(self, _model: type[object], _identity: object) -> object:
        self.get_called = True
        raise AssertionError(
            "session.get() must not run before the role-eligibility check returns"
        )

    def add(self, _value: object) -> None:
        raise AssertionError("no audit/outbox evidence should be recorded for a denied caller")

    async def commit(self) -> None:
        raise AssertionError("commit must not happen for a denied caller")


async def test_leak_resolve_entity_denied_caller_cannot_distinguish_existing_from_missing_entity(
    monkeypatch: object,
) -> None:
    caller = SecurityContext(
        principal_id="viewer-with-no-lineage-role",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset(),  # disjoint from UNIFIED_LINEAGE_READER_ROLES
    )

    def _forbidden_resolver(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("_resolve_governed_entities must not run before the role check denies")

    monkeypatch.setattr(mcp_server, "_resolve_governed_entities", _forbidden_resolver)  # type: ignore[attr-defined]

    # `session_for_real_entity`'s query stands in for a name that resolves to
    # a real, existing entity; `session_for_missing_entity`'s stands in for
    # one that resolves to nothing. Both are denied identically, before
    # either query is ever evaluated against real data.
    session_for_real_entity = DeniedCallerSpySession()
    result_for_real_entity = await _handle_native_lineage_tool_call(
        "resolve_entity",
        {"datasource_id": str(uuid4()), "query": "a real customers table"},
        session_for_real_entity,
        caller,  # type: ignore[arg-type]
    )

    session_for_missing_entity = DeniedCallerSpySession()
    result_for_missing_entity = await _handle_native_lineage_tool_call(
        "resolve_entity",
        {"datasource_id": str(uuid4()), "query": "zzz-definitely-absent-entity-zzz"},
        session_for_missing_entity,
        caller,  # type: ignore[arg-type]
    )

    assert result_for_real_entity == result_for_missing_entity
    assert result_for_real_entity == {
        "isError": True,
        "content": [{"type": "text", "text": "Tool 'resolve_entity' not found or not published."}],
    }
    assert session_for_real_entity.get_called is False
    assert session_for_missing_entity.get_called is False


async def test_leak_get_transformation_detail_denied_caller_cannot_distinguish_existing_from_missing_entity(  # noqa: E501
    monkeypatch: object,
) -> None:
    caller = SecurityContext(
        principal_id="viewer-with-no-lineage-role",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset(),  # disjoint from UNIFIED_LINEAGE_READER_ROLES
    )

    async def _forbidden_detail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("_transformation_detail must not run before the role check denies")

    monkeypatch.setattr(mcp_server, "_transformation_detail", _forbidden_detail)  # type: ignore[attr-defined]

    real_entity_id = uuid4()  # stands in for a dbt-resource/table id that genuinely exists
    missing_entity_id = uuid4()  # stands in for one that does not

    session_for_real_entity = DeniedCallerSpySession()
    result_for_real_entity = await _handle_native_lineage_tool_call(
        "get_transformation_detail",
        {"datasource_id": str(uuid4()), "entity_id": str(real_entity_id)},
        session_for_real_entity,
        caller,  # type: ignore[arg-type]
    )

    session_for_missing_entity = DeniedCallerSpySession()
    result_for_missing_entity = await _handle_native_lineage_tool_call(
        "get_transformation_detail",
        {"datasource_id": str(uuid4()), "entity_id": str(missing_entity_id)},
        session_for_missing_entity,
        caller,  # type: ignore[arg-type]
    )

    assert result_for_real_entity == result_for_missing_entity
    assert result_for_real_entity == {
        "isError": True,
        "content": [
            {"type": "text", "text": "Tool 'get_transformation_detail' not found or not published."}
        ],
    }
    assert session_for_real_entity.get_called is False
    assert session_for_missing_entity.get_called is False


# ---------------------------------------------------------------------------
# AG-1/AG-2/TS-6 -- `_transformation_detail` is the live model-context builder for a
# dbt resource's `description` (a manifest `description:` field, source-controlled,
# with no stored `screening_status` of its own the way `MetadataViewDefinition`/
# `MetadataRoutine` have -- see envelope_models.py). Unlike the tests above,
# `_transformation_detail` is deliberately NOT monkeypatched here: these drive the real
# function, through the real `_handle_native_lineage_tool_call` dispatch
# (`_handle_tools_call` uses the same path for the live `/mcp` endpoint), so the
# screening wired into it actually runs.
# ---------------------------------------------------------------------------


class TransformationDetailSession:
    """Fake session for `_transformation_detail`'s single-row DbtResource lookup, plus
    the `.add()`/`.commit()` calls `_handle_native_lineage_tool_call` makes for the audit
    and outbox records on a successful read."""

    def __init__(self, datasource: object, resource: object, artifact: object) -> None:
        self.datasource = datasource
        self.resource = resource
        self.artifact = artifact
        self.added: list[object] = []
        self.committed = False

    async def get(self, model: type[object], _identity: object) -> object:
        if model is DbtArtifactImport:
            return self.artifact
        return self.datasource

    async def scalar(self, _stmt: object) -> object:
        return self.resource

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


def _dbt_resource_scenario(description: str) -> tuple[DataSource, DbtResource, DbtArtifactImport]:
    org = uuid4()
    datasource = DataSource(
        id=uuid4(),
        organization_id=org,
        project_id=uuid4(),
        connector_type="postgresql",
        name="warehouse",
        status="ACTIVE",
    )
    artifact_id = uuid4()
    resource = DbtResource(
        id=uuid4(),
        organization_id=org,
        artifact_import_id=artifact_id,
        unique_id="model.bank.customers",
        resource_type="model",
        package_name="bank",
        name="customers",
        relation_name="analytics.public.customers",
        materialization="table",
        description=description,
        sql_parse_status="REDACTED",
    )
    artifact = DbtArtifactImport(
        id=artifact_id,
        organization_id=org,
        dbt_project_id=uuid4(),
        manifest_fingerprint="fp-1",
        dbt_schema_version="v12",
        dbt_version="1.9.0",
        resource_count=1,
        model_count=1,
        source_count=0,
        test_count=0,
        lineage_edge_count=0,
        matched_resource_count=1,
        unmatched_resource_count=0,
        imported_by="tester",
    )
    return datasource, resource, artifact


async def test_transformation_detail_quarantines_a_multilingual_injection_description() -> None:
    """AG-1/AG-2's corpus (`injection_corpus.py`), reached through the live MCP path.

    A Chinese "ignore all previous instructions" payload -- flagged by
    `injection_defense.screen_metadata`'s multilingual detector, not by
    `prompt_risk.py`'s English-only classifier alone -- proves the richer detector, not
    just some detector, is what `_transformation_detail` now runs before this text
    reaches the calling LLM's context.
    """
    from aida.injection_corpus import MULTILINGUAL_INJECTIONS

    hostile_description, expected_threat, _label = next(
        item for item in MULTILINGUAL_INJECTIONS if "Chinese" in item[2]
    )
    assert expected_threat == "MULTILINGUAL_INJECTION"

    datasource, resource, artifact = _dbt_resource_scenario(hostile_description)
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session = TransformationDetailSession(datasource, resource, artifact)

    result = await _handle_native_lineage_tool_call(
        "get_transformation_detail",
        {"datasource_id": str(datasource.id), "entity_id": str(resource.id)},
        session,
        caller,  # type: ignore[arg-type]
    )

    body_text = result["content"][1]["text"]
    payload = json.loads(body_text.removeprefix("```json\n").removesuffix("\n```"))
    assert payload["description"] is None
    assert payload["description_screening"]["status"] == "QUARANTINED"
    assert (
        "INJECTION_DEFENSE:MULTILINGUAL_INJECTION"
        in payload["description_screening"]["reason_codes"]
    )
    assert hostile_description not in body_text
    assert session.committed is True


async def test_transformation_detail_passes_through_a_benign_description() -> None:
    """The live screen must not become a false-positive tax on an ordinary manifest."""
    benign = "One row per active customer, refreshed nightly by the customers model."
    datasource, resource, artifact = _dbt_resource_scenario(benign)
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session = TransformationDetailSession(datasource, resource, artifact)

    result = await _handle_native_lineage_tool_call(
        "get_transformation_detail",
        {"datasource_id": str(datasource.id), "entity_id": str(resource.id)},
        session,
        caller,  # type: ignore[arg-type]
    )

    body_text = result["content"][1]["text"]
    payload = json.loads(body_text.removeprefix("```json\n").removesuffix("\n```"))
    assert payload["description"] == benign
    assert payload["description_screening"]["status"] == "CLEAN"


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


# ---------------------------------------------------------------------------
# resources/read -- row_count_estimate must come from the latest completed
# TableProfile, not from MetadataTable (which has no such column and raised
# AttributeError on every catalog resource read before this fix).
# ---------------------------------------------------------------------------


class _FakeReadResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FakeReadScalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class ResourcesReadSession:
    """Stands in for AsyncSession: returns canned results regardless of the
    statement passed in, since these tests only need to exercise handler
    logic, not real query construction."""

    def __init__(self, *, catalog_row, columns, latest_profile):
        self._catalog_row = catalog_row
        self._columns = columns
        self._latest_profile = latest_profile
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    async def execute(self, _statement):
        return _FakeReadResult(self._catalog_row)

    async def scalars(self, _statement):
        return _FakeReadScalars(self._columns)

    async def scalar(self, _statement):
        return self._latest_profile


def _build_catalog_row(organization_id):
    datasource_id = uuid4()
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=datasource_id,
        name="prod-warehouse",
        status="ACTIVE",
        fingerprint="cat-fp",
    )
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=organization_id,
        catalog_id=catalog.id,
        name="analytics",
        status="ACTIVE",
        fingerprint="schema-fp",
    )
    table = MetadataTable(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=datasource_id,
        schema_id=schema.id,
        name="orders",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="table-fp",
    )
    return table, schema, catalog, datasource_id


def _read_resource(context, session, datasource_id, schema_name, table_name):
    uri = f"atlas://catalog/{datasource_id}/{schema_name}/{table_name}"
    result = asyncio.run(_handle_resources_read({"uri": uri}, session, context, "corr-test"))
    return json.loads(result["contents"][0]["text"])


def test_catalog_read_reports_row_count_estimate_from_table_profile() -> None:
    organization_id = uuid4()
    context = SecurityContext(
        principal_id="test-principal",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Viewer"}),
    )
    table, schema, catalog, datasource_id = _build_catalog_row(organization_id)
    profile = TableProfile(
        id=uuid4(),
        organization_id=organization_id,
        analysis_run_id=uuid4(),
        datasource_id=datasource_id,
        table_id=table.id,
        row_count_estimate=48213,
        sampled_row_count=5000,
        status="COMPLETED",
        created_at=datetime.now(UTC),
    )
    session = ResourcesReadSession(
        catalog_row=(table, schema, catalog),
        columns=[],
        latest_profile=profile,
    )

    payload = _read_resource(context, session, datasource_id, schema.name, table.name)

    assert payload["row_count_estimate"] == 48213


def test_catalog_read_reports_null_row_count_when_no_profile_exists() -> None:
    organization_id = uuid4()
    context = SecurityContext(
        principal_id="test-principal",
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"Viewer"}),
    )
    table, schema, catalog, datasource_id = _build_catalog_row(organization_id)
    session = ResourcesReadSession(
        catalog_row=(table, schema, catalog),
        columns=[],
        latest_profile=None,
    )

    payload = _read_resource(context, session, datasource_id, schema.name, table.name)

    assert payload["row_count_estimate"] is None


# ---------------------------------------------------------------------------
# AT-13 -- atlas__get_asset_context: one composite call, one policy
# evaluation, one audit record. Success-path tests monkeypatch
# `compose_asset_context_signals` and `build_unified_lineage_impact_payload`
# (real ORM-backed collaborators, same convention every other native lineage
# tool's tests use, see the module docstring) and `gate` (so no real
# workspace/policy machinery is needed), and only exercise this handler's own
# decision logic: role gating, table/datasource resolution, the exactly-one-
# gate-call / exactly-one-audit-record shape, and response composition. The
# `usage_decision` embedded in the response is produced by the REAL, un-mocked
# `compute_usage_decision` -- so these tests also prove the wiring between the
# composed signals and the pure decision function, not just that some
# decision object comes back.
# ---------------------------------------------------------------------------


class AssetContextSession:
    """Fake session for `_handle_get_asset_context`: `.get()` answers the
    MetadataTable and DataSource lookups (the only direct session use --
    signal composition and lineage traversal are monkeypatched collaborators,
    same convention as `LineageToolSession` above); `.add()`/`.commit()`
    record what the audit/outbox path did."""

    def __init__(self, *, table: object, datasource: object) -> None:
        self.table = table
        self.datasource = datasource
        self.added: list[object] = []
        self.committed = False

    async def get(self, model: type[object], _identity: object) -> object:
        if model is MetadataTable:
            return self.table
        return self.datasource

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


def _asset_context_scenario() -> tuple[DataSource, MetadataTable]:
    org = uuid4()
    datasource = DataSource(
        id=uuid4(),
        organization_id=org,
        project_id=uuid4(),
        connector_type="postgresql",
        name="warehouse",
        status="ACTIVE",
    )
    table = MetadataTable(
        id=uuid4(),
        organization_id=org,
        datasource_id=datasource.id,
        schema_id=uuid4(),
        name="customers",
        object_type="TABLE",
        status="ACTIVE",
        fingerprint="table-fp",
    )
    return datasource, table


def _canned_signals(
    *,
    certification_state: str = "CERTIFIED",
    quality_state: str = "PASSING",
    has_open_critical_incident: bool = False,
    owner: str | None = "steward@bank.example",
    has_sensitive_classification: bool = False,
) -> AssetContextSignals:
    return AssetContextSignals(
        owner=owner,
        owner_source="ownership_assignment (GL-2, ACTIVE)" if owner else None,
        certification_state=certification_state,
        certification_expires_at=None,
        quality_state=quality_state,
        open_incident_count=1 if has_open_critical_incident else 0,
        has_open_critical_incident=has_open_critical_incident,
        classification=ClassificationSummary(
            total_columns=10,
            classified_columns=3,
            distinct_classifications=(
                ("INTERNAL", "PII") if has_sensitive_classification else ("INTERNAL",)
            ),
            has_sensitive_classification=has_sensitive_classification,
        ),
    )


def _canned_impact(datasource: DataSource, table: MetadataTable) -> UnifiedLineageImpactRead:
    return UnifiedLineageImpactRead(
        datasource_id=datasource.id,
        focus_node_id=str(table.id),
        focus_node_kind="TABLE",
        focus_label=table.name,
        upstream=[
            UnifiedLineageImpactNodeRead(
                node_id=str(uuid4()),
                node_kind="TABLE",
                label="raw_customers",
                qualified_name="raw.public.raw_customers",
                depth=2,
                contributing_edge_sources=["FOREIGN_KEY"],
            )
        ],
        downstream=[],
        requested_depth=5,
        node_limit=200,
        upstream_truncated=False,
        downstream_truncated=False,
    )


async def test_get_asset_context_denies_ineligible_caller_like_an_unknown_tool() -> None:
    caller = SecurityContext(
        principal_id="viewer-with-no-lineage-role",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset(),
    )
    session = AssetContextSession(table=None, datasource=None)

    result = await _handle_native_lineage_tool_call(
        "get_asset_context",
        {"table_id": str(uuid4())},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result == {
        "isError": True,
        "content": [
            {"type": "text", "text": "Tool 'get_asset_context' not found or not published."}
        ],
    }


async def test_leak_get_asset_context_denied_caller_cannot_distinguish_existing_from_missing_table(
    monkeypatch: object,
) -> None:
    caller = SecurityContext(
        principal_id="viewer-with-no-lineage-role",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset(),  # disjoint from UNIFIED_LINEAGE_READER_ROLES
    )

    async def _forbidden_signals(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "compose_asset_context_signals must not run before the role check denies"
        )

    monkeypatch.setattr(mcp_server, "compose_asset_context_signals", _forbidden_signals)  # type: ignore[attr-defined]

    real_table_id = uuid4()  # stands in for a table id that genuinely exists
    missing_table_id = uuid4()  # stands in for one that does not

    session_for_real_table = DeniedCallerSpySession()
    result_for_real_table = await _handle_native_lineage_tool_call(
        "get_asset_context",
        {"table_id": str(real_table_id)},
        session_for_real_table,
        caller,  # type: ignore[arg-type]
    )

    session_for_missing_table = DeniedCallerSpySession()
    result_for_missing_table = await _handle_native_lineage_tool_call(
        "get_asset_context",
        {"table_id": str(missing_table_id)},
        session_for_missing_table,
        caller,  # type: ignore[arg-type]
    )

    assert result_for_real_table == result_for_missing_table
    assert result_for_real_table == {
        "isError": True,
        "content": [
            {"type": "text", "text": "Tool 'get_asset_context' not found or not published."}
        ],
    }
    assert session_for_real_table.get_called is False
    assert session_for_missing_table.get_called is False


async def test_get_asset_context_rejects_a_non_uuid_table_id() -> None:
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"Analyst"}),
    )
    session = AssetContextSession(table=None, datasource=None)

    result = await _handle_native_lineage_tool_call(
        "get_asset_context",
        {"table_id": "not-a-uuid"},
        session,
        caller,  # type: ignore[arg-type]
    )

    assert result == {
        "isError": True,
        "content": [{"type": "text", "text": "table_id must be a UUID."}],
    }


async def test_get_asset_context_reports_identical_not_found_for_missing_and_wrong_org() -> None:
    org = uuid4()
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=org,
        roles=frozenset({"Analyst"}),
    )

    missing_session = AssetContextSession(table=None, datasource=None)
    missing_result = await _handle_native_lineage_tool_call(
        "get_asset_context",
        {"table_id": str(uuid4())},
        missing_session,  # type: ignore[arg-type]
        caller,
    )

    foreign_datasource, foreign_table = _asset_context_scenario()  # different org than `caller`
    wrong_org_session = AssetContextSession(table=foreign_table, datasource=foreign_datasource)
    wrong_org_result = await _handle_native_lineage_tool_call(
        "get_asset_context",
        {"table_id": str(foreign_table.id)},
        wrong_org_session,  # type: ignore[arg-type]
        caller,
    )

    assert missing_result == wrong_org_result
    assert missing_result == {
        "isError": True,
        "content": [{"type": "text", "text": "Asset not found or not accessible."}],
    }


async def test_get_asset_context_returns_not_found_when_the_gate_denies(
    monkeypatch: object,
) -> None:
    datasource, table = _asset_context_scenario()
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session = AssetContextSession(table=table, datasource=datasource)

    gate_calls: list[dict[str, object]] = []

    async def _denying_gate(_session: object, _context: object, **kwargs: object) -> object:
        gate_calls.append(kwargs)
        raise AuthorizationDenied("WORKSPACE_ENFORCED_DENY")

    async def _forbidden_signals(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("compose_asset_context_signals must not run once the gate denies")

    monkeypatch.setattr(mcp_server, "gate", _denying_gate)  # type: ignore[attr-defined]
    monkeypatch.setattr(mcp_server, "compose_asset_context_signals", _forbidden_signals)  # type: ignore[attr-defined]

    result = await _handle_native_lineage_tool_call(
        "get_asset_context",
        {"table_id": str(table.id)},
        session,  # type: ignore[arg-type]
        caller,
        "corr-denied",
        Settings(_env_file=None),
    )

    # Same anti-enumeration wording as a table that doesn't exist at all --
    # a policy denial is never distinguishable from "not found".
    assert result == {
        "isError": True,
        "content": [{"type": "text", "text": "Asset not found or not accessible."}],
    }
    assert len(gate_calls) == 1
    assert session.added == []
    assert session.committed is False


async def test_get_asset_context_makes_exactly_one_policy_evaluation_and_one_audit_record(
    monkeypatch: object,
) -> None:
    """AT-13's explicit anti-pattern: a composite read must not perform one
    policy evaluation or one audit record per composed fact. This proves the
    whole call -- certification + quality + classification + lineage depth +
    owner + usage_decision -- makes exactly ONE `gate()` call and leaves
    exactly ONE `AuditEvent` and ONE `OutboxEvent` behind, not five (or two)
    of either.
    """
    datasource, table = _asset_context_scenario()
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session = AssetContextSession(table=table, datasource=datasource)

    gate_calls: list[dict[str, object]] = []

    async def _allowing_gate(_session: object, _context: object, **kwargs: object) -> object:
        gate_calls.append(kwargs)
        return None

    signals_calls = 0

    async def _fake_signals(*_args: object, **_kwargs: object) -> AssetContextSignals:
        nonlocal signals_calls
        signals_calls += 1
        return _canned_signals(has_open_critical_incident=True, owner=None)

    async def _fake_impact(*_args: object, **_kwargs: object) -> UnifiedLineageImpactRead:
        return _canned_impact(datasource, table)

    monkeypatch.setattr(mcp_server, "gate", _allowing_gate)  # type: ignore[attr-defined]
    monkeypatch.setattr(mcp_server, "compose_asset_context_signals", _fake_signals)  # type: ignore[attr-defined]
    monkeypatch.setattr(mcp_server, "build_unified_lineage_impact_payload", _fake_impact)  # type: ignore[attr-defined]

    result = await _handle_native_lineage_tool_call(
        "get_asset_context",
        {"table_id": str(table.id)},
        session,  # type: ignore[arg-type]
        caller,
        "corr-asset-context",
        Settings(_env_file=None),
    )

    assert "isError" not in result
    assert result["content"][0]["text"].startswith("✅ Asset context composed")

    # Exactly one policy evaluation, reusing asset_evidence_api.py's own
    # gate() call shape verbatim -- not a second/different evaluation.
    assert len(gate_calls) == 1
    assert gate_calls[0]["action"] == "READ_METADATA"
    assert gate_calls[0]["resource_type"] == "datasource"
    assert gate_calls[0]["resource_id"] == str(datasource.id)
    assert gate_calls[0]["datasource_id"] == datasource.id

    # Signals composed exactly once (not per-fact).
    assert signals_calls == 1

    # Exactly one audit record and one outbox event for the WHOLE composite
    # call -- not one per composed fact.
    audit_events = [value for value in session.added if isinstance(value, AuditEvent)]
    outbox_events = [value for value in session.added if isinstance(value, OutboxEvent)]
    assert len(audit_events) == 1
    assert len(outbox_events) == 1
    assert len(session.added) == 2
    assert audit_events[0].action == "mcp.asset_context.read"
    assert audit_events[0].outcome == "SUCCESS"
    assert audit_events[0].resource_type == "metadata_table"
    assert audit_events[0].resource_id == str(table.id)
    assert session.committed is True

    body_text = result["content"][1]["text"]
    payload = json.loads(body_text.removeprefix("```json\n").removesuffix("\n```"))

    # Every composed fact is present, and the usage_decision was computed by
    # the REAL compute_usage_decision from the (mocked) signals above: an
    # open critical incident with no owner assigned -- BLOCKED wins over
    # CAUTION, and both factors are visible, not a bare label.
    assert payload["owner"] is None
    assert payload["certification"]["state"] == "CERTIFIED"
    assert payload["quality"]["has_open_critical_incident"] is True
    assert payload["classification"]["has_sensitive_classification"] is False
    assert payload["lineage"]["available"] is True
    assert payload["lineage"]["max_upstream_depth"] == 2
    assert payload["usage_decision"]["decision"] == "BLOCKED"
    factor_names = {factor["name"] for factor in payload["usage_decision"]["factors"]}
    assert factor_names == {
        "certification_state",
        "open_critical_quality_incident",
        "quality_state",
        "has_owner",
        "has_sensitive_classification",
    }


async def test_get_asset_context_surfaces_lineage_unavailable_without_failing_the_call(
    monkeypatch: object,
) -> None:
    """A table the unified graph never registered as a node (e.g.
    deprecated) still gets every other composed fact -- lineage
    unavailability is reported honestly, not an error that sinks the whole
    composite call."""
    datasource, table = _asset_context_scenario()
    caller = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=datasource.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session = AssetContextSession(table=table, datasource=datasource)

    async def _allowing_gate(_session: object, _context: object, **_kwargs: object) -> object:
        return None

    async def _fake_signals(*_args: object, **_kwargs: object) -> AssetContextSignals:
        return _canned_signals()

    async def _raising_impact(*_args: object, **_kwargs: object) -> None:
        raise LineageNodeNotFoundError("lineage node not found in this datasource's graph")

    monkeypatch.setattr(mcp_server, "gate", _allowing_gate)  # type: ignore[attr-defined]
    monkeypatch.setattr(mcp_server, "compose_asset_context_signals", _fake_signals)  # type: ignore[attr-defined]
    monkeypatch.setattr(mcp_server, "build_unified_lineage_impact_payload", _raising_impact)  # type: ignore[attr-defined]

    result = await _handle_native_lineage_tool_call(
        "get_asset_context",
        {"table_id": str(table.id)},
        session,  # type: ignore[arg-type]
        caller,
        "corr-no-lineage",
        Settings(_env_file=None),
    )

    assert "isError" not in result
    body_text = result["content"][1]["text"]
    payload = json.loads(body_text.removeprefix("```json\n").removesuffix("\n```"))
    assert payload["lineage"] == {
        "available": False,
        "reason": "table is not a node in this datasource's unified lineage graph",
        "source": "unified_lineage_api.build_unified_lineage_impact_payload (EA.14)",
    }
    # Certification, quality, classification, owner and usage_decision are
    # still present -- lineage unavailability didn't take down the rest.
    assert payload["usage_decision"]["decision"] == "ALLOWED"
    assert session.committed is True
