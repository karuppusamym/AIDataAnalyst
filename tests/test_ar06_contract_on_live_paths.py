"""AR-06: a contracted agent's contract applies on the paths it actually uses.

AR-06's enforcement matrix found the orchestrator's contract enforcement --
contract existence, the kill switch, the `tool_slugs` allowlist and the token
caps -- unreachable. `GovernedAgentOrchestrator.run` applies it only when told
which contract, and neither live path into it (MCP `tools/call`, REST
`agent-analyses`) told it. A contracted agent was served as any holder of its
roles.

Both paths now resolve the caller's contract (`load_contract_for_principal`)
and pass it through. A human has no contract and is unaffected. An `agent:`
identity with none, or with several, is refused before anything runs.

The orchestrator and the contract lookup are replaced here so the test pins
exactly what each boundary hands over. What the orchestrator then does with a
contract is covered where it lives (`test_agent_orchestrator_*`).
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from aida import api, mcp_server
from aida.agent_contracts import AgentContractValidationError
from aida.agent_orchestrator import AgentPolicyRejected
from aida.config import Settings
from aida.mcp_server import _handle_tools_call
from aida.models import AuditEvent, DataSource, GovernedTool, GovernedToolVersion
from aida.schemas import AgentAnalysisRequest
from aida.security import SecurityContext
from tests.test_mcp_server import ToolCallSession, _published_tool_version

SLUG = "quarterly_revenue_by_region"
_UNRESOLVED = AgentContractValidationError(
    "agent_contract_unresolved", "agent identity requires exactly one unambiguous contract"
)


class _Contract:
    def __init__(self, ai_asset_version_id: UUID) -> None:
        self.ai_asset_version_id = ai_asset_version_id


class _Session(ToolCallSession):
    """`ToolCallSession`, plus the datasource the tool runs against."""

    def __init__(self, row: object, datasource: DataSource) -> None:
        super().__init__(row)
        self.datasource = datasource

    async def get(self, _model: type[object], _identity: object) -> object:
        return self.datasource


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every `GovernedAgentOrchestrator.run` either boundary asks for."""
    calls: list[dict[str, Any]] = []

    class Recording:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def run(self, _session: object, **kwargs: Any) -> Any:
            calls.append(kwargs)
            raise AgentPolicyRejected("stopped_by_test")

    monkeypatch.setattr(mcp_server, "GovernedAgentOrchestrator", Recording)
    monkeypatch.setattr(api, "GovernedAgentOrchestrator", Recording)
    return calls


def _contract_lookup(monkeypatch: pytest.MonkeyPatch, outcome: object) -> None:
    async def lookup(
        _session: object,
        *,
        organization_id: UUID,
        agent_principal_id: str,
        principal_type: str | None = None,
    ) -> object:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(mcp_server, "load_contract_for_principal", lookup)
    monkeypatch.setattr(api, "load_contract_for_principal", lookup)


def _scene(principal_id: str) -> tuple[SecurityContext, _Session, GovernedToolVersion]:
    version, tool = _published_tool_version(slug=SLUG, allowed_roles=["Analyst"])
    datasource = DataSource(
        id=version.datasource_id,
        organization_id=version.organization_id,
        name="core-warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="TEST",
        credential_reference="env://AIDA_SAMPLE_SOURCE_DSN",
        status="ACTIVE",
    )
    context = SecurityContext(
        principal_id=principal_id,
        principal_type="AGENT" if principal_id.startswith("agent:") else "USER",
        organization_id=version.organization_id,
        roles=frozenset({"Analyst"}),
    )
    row: tuple[GovernedToolVersion, GovernedTool] = (version, tool)
    return context, _Session(row, datasource), version


async def _call_tool(context: SecurityContext, session: _Session) -> dict[str, Any]:
    return await _handle_tools_call(
        {"name": f"atlas__{SLUG}", "arguments": {}},
        session,  # type: ignore[arg-type]
        context,
        Settings(_env_file=None),
        "corr-ar06",
    )


# --- MCP tools/call -----------------------------------------------------------


async def test_a_contracted_agent_calls_a_governed_tool_under_its_contract(
    monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]]
) -> None:
    contract_version = uuid4()
    _contract_lookup(monkeypatch, _Contract(contract_version))
    context, session, _version = _scene("agent:revenue-bot")

    await _call_tool(context, session)

    [run] = runs
    assert run["agent_asset_version_id"] == contract_version


async def test_a_human_calling_a_governed_tool_is_unaffected(
    monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]]
) -> None:
    _contract_lookup(monkeypatch, None)
    context, session, _version = _scene("analyst")

    await _call_tool(context, session)

    [run] = runs
    assert run["agent_asset_version_id"] is None


async def test_an_agent_identity_without_one_contract_is_refused_and_audited(
    monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]]
) -> None:
    _contract_lookup(monkeypatch, _UNRESOLVED)
    context, session, version = _scene("agent:unknown-bot")

    result = await _call_tool(context, session)

    assert result == {
        "isError": True,
        "content": [
            {"type": "text", "text": "Blocked by agent contract: agent_contract_unresolved"}
        ],
    }
    assert runs == []
    audit = next(value for value in session.added if isinstance(value, AuditEvent))
    assert (audit.action, audit.outcome) == ("mcp.tool_call.agent_contract_denied", "DENIED")
    assert audit.resource_id == str(version.id)
    assert session.timeline == ["commit"]


# --- REST agent-analyses --------------------------------------------------------


async def _analyse(context: SecurityContext, session: _Session) -> None:
    await api.run_agent_analysis(
        session.datasource.id,
        AgentAnalysisRequest(question="total revenue by region"),
        context=context,
        session=session,  # type: ignore[arg-type]
        settings=Settings(_env_file=None),
    )


async def test_a_contracted_agent_runs_an_analysis_under_its_contract(
    monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]]
) -> None:
    contract_version = uuid4()
    _contract_lookup(monkeypatch, _Contract(contract_version))
    context, session, _version = _scene("agent:revenue-bot")

    with pytest.raises(HTTPException) as stopped:
        await _analyse(context, session)

    assert stopped.value.status_code == 422  # the recording orchestrator's stop
    [run] = runs
    assert run["agent_asset_version_id"] == contract_version


async def test_a_human_running_an_analysis_is_unaffected(
    monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]]
) -> None:
    _contract_lookup(monkeypatch, None)
    context, session, _version = _scene("analyst")

    with pytest.raises(HTTPException):
        await _analyse(context, session)

    [run] = runs
    assert run["agent_asset_version_id"] is None


async def test_an_analysis_for_an_agent_identity_without_one_contract_is_refused(
    monkeypatch: pytest.MonkeyPatch, runs: list[dict[str, Any]]
) -> None:
    _contract_lookup(monkeypatch, _UNRESOLVED)
    context, session, _version = _scene("agent:unknown-bot")

    with pytest.raises(HTTPException) as refused:
        await _analyse(context, session)

    assert (refused.value.status_code, refused.value.detail) == (403, "agent_contract_unresolved")
    assert runs == []
