"""R11-C6 hole 4: the REST tool-execution route ignored the agent contract.

`POST /v1/tool-versions/{id}/execute` checks a great deal -- the caller's
role, the organization, that the version is PUBLISHED, the version's own role
binding, the datasource's admission state, and open quality incidents against
the tool's declared dependencies. It did not check the caller's agent
contract. So an agent identity holding any of the four execution roles could
execute a governed tool **with its kill switch engaged** and **outside the
`tool_slugs` its contract names**, by addressing the tool version by id
instead of asking through Ask.

The orchestrator has gated both since AG-10. Same authority, same governed
object, different transport -- and the transport is not the control.

The asymmetry had already been reasoned about here in the other direction:
`agent_orchestrator` carries a comment requiring parity with *this* route's
quality gate, on the grounds that a tool version must not "answer differently
depending on which surface asked for it". That argument does not run one way
only, and the contract half of it had no such note.

These tests drive the real `execute_tool_version`, not the gate helper,
because the defect was the gate's *absence at this call site*: a test of the
helper alone would have passed throughout the bug's life. Delete the
`_enforce_agent_contract` call and `test_a_killed_agent_cannot_execute_over_rest`
fails.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from aida.agent_contracts import REASON_ENVELOPE_VIOLATION, REASON_KILL_ENGAGED
from aida.config import Settings
from aida.models import (
    AgentContract,
    AuditEvent,
    DataSource,
    GovernedTool,
    GovernedToolVersion,
)
from aida.schemas import ToolExecutionRequest
from aida.security import SecurityContext
from aida.tool_api import execute_tool_version

ORG = uuid4()
VERSION_ID = uuid4()
TOOL_ID = uuid4()
DATASOURCE_ID = uuid4()

#: The one slug the contract below allows.
ALLOWED_SLUG = "quarterly_revenue"


def _settings() -> Settings:
    return Settings(_env_file=None, environment="test")


def _version() -> GovernedToolVersion:
    return GovernedToolVersion(
        id=VERSION_ID,
        organization_id=ORG,
        tool_id=TOOL_ID,
        datasource_id=DATASOURCE_ID,
        version=1,
        status="PUBLISHED",
        sql_template="SELECT 1",
        parameter_schema=[],
        referenced_tables=["public.accounts"],
        allowed_roles=["Analyst"],
        created_by="steward",
    )


def _tool(slug: str = ALLOWED_SLUG) -> GovernedTool:
    return GovernedTool(
        id=TOOL_ID,
        organization_id=ORG,
        project_id=uuid4(),
        slug=slug,
    )


def _datasource(*, enabled: bool = True) -> DataSource:
    return DataSource(
        id=DATASOURCE_ID,
        organization_id=ORG,
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="warehouse",
        connector_type="POSTGRESQL",
        dialect="postgresql",
        environment="test",
        credential_reference="env://NONE",
        # `ensure_datasource_enabled` reads `status`; DISABLED is how the
        # ordering test proves the contract gate runs first.
        status="ACTIVE" if enabled else "DISABLED",
    )


def _contract(
    *, kill_engaged: bool = False, slugs: list[str] | None = None
) -> AgentContract:
    return AgentContract(
        id=uuid4(),
        organization_id=ORG,
        ai_asset_version_id=uuid4(),
        agent_principal_id="agent:revenue-bot",
        capability_envelope={
            "tool_slugs": [ALLOWED_SLUG] if slugs is None else slugs,
            "context_product_ids": [],
            "write_lanes": [],
        },
        autonomy_tier="T1",
        supervisor_persona="STEWARD",
        kill_scope="AGENT",
        kill_engaged=kill_engaged,
        sampling_rate=0.05,
    )


class _ToolSession:
    """The session surface this path touches, up to and past the gate.

    `get` is served by type, because the route fetches the version, then the
    tool, then the datasource. `scalars` is a queue: the contract lookup
    first, then `agent_kill_blocking_reason`'s scan for engaged TIER/ALL
    contracts, so a test states exactly what the database holds at each step.
    """

    def __init__(
        self,
        *,
        version: GovernedToolVersion | None,
        tool: GovernedTool | None,
        datasource: DataSource | None,
        contracts: list[object] | None = None,
        tier_scan: list[object] | None = None,
    ) -> None:
        self._by_type: dict[Any, Any] = {
            GovernedToolVersion: version,
            GovernedTool: tool,
            DataSource: datasource,
        }
        self._queue: list[list[object]] = [
            list(contracts or []),
            list(tier_scan or []),
        ]
        self.added: list[object] = []
        self.committed = False

    async def get(self, model: Any, _id: UUID, **_kwargs: Any) -> Any:
        return self._by_type.get(model)

    async def scalars(self, _statement: object) -> object:
        rows = self._queue.pop(0) if self._queue else []

        class _Scalars:
            def all(self_inner) -> list[object]:
                return list(rows)

        return _Scalars()

    async def scalar(self, _statement: object) -> object:
        return None

    async def execute(self, _statement: object) -> object:
        class _Result:
            def first(self_inner) -> object:
                return None

            def all(self_inner) -> list[object]:
                return []

            def scalars(self_inner) -> object:
                class _S:
                    def all(self_innermost) -> list[object]:
                        return []

                return _S()

        return _Result()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True

    async def flush(self) -> None:
        return None


def _agent(principal_id: str = "agent:revenue-bot") -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="AGENT",
        organization_id=ORG,
        roles=frozenset({"Analyst"}),
    )


def _human() -> SecurityContext:
    return SecurityContext(
        principal_id="analyst@bank.example",
        principal_type="USER",
        organization_id=ORG,
        roles=frozenset({"Analyst"}),
    )


async def _execute(session: _ToolSession, context: SecurityContext) -> Any:
    return await execute_tool_version(
        VERSION_ID,
        ToolExecutionRequest(parameters={}),
        context,
        session,  # type: ignore[arg-type]
        _settings(),
    )


def _denials(session: _ToolSession) -> list[AuditEvent]:
    return [
        value
        for value in session.added
        if isinstance(value, AuditEvent) and value.outcome == "DENIED"
    ]


# --------------------------------------------------------------------------- #
# The two controls that were missing
# --------------------------------------------------------------------------- #


async def test_a_killed_agent_cannot_execute_over_rest() -> None:
    """The defect, stated as a test. Remove the gate and this passes a tool
    execution through for an agent an operator has just stopped."""
    session = _ToolSession(
        version=_version(),
        tool=_tool(),
        datasource=_datasource(),
        contracts=[_contract(kill_engaged=True)],
    )

    with pytest.raises(HTTPException) as excinfo:
        await _execute(session, _agent())

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == REASON_KILL_ENGAGED


async def test_an_agent_cannot_execute_a_tool_outside_its_envelope() -> None:
    """`capability_envelope.tool_slugs` is the per-agent allowlist over
    governed tools, and this route never consulted it -- so an agent contracted
    for one tool could execute any published tool in its organization."""
    session = _ToolSession(
        version=_version(),
        tool=_tool(slug="payroll_export"),
        datasource=_datasource(),
        contracts=[_contract(slugs=[ALLOWED_SLUG])],
    )

    with pytest.raises(HTTPException) as excinfo:
        await _execute(session, _agent())

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == REASON_ENVELOPE_VIOLATION


async def test_the_denial_is_audited_with_its_reason() -> None:
    """A refusal nobody can attribute later is half a control (INV-7)."""
    session = _ToolSession(
        version=_version(),
        tool=_tool(),
        datasource=_datasource(),
        contracts=[_contract(kill_engaged=True)],
    )

    with pytest.raises(HTTPException):
        await _execute(session, _agent())

    denials = _denials(session)
    assert len(denials) == 1
    assert denials[0].action == "tool.execute"
    assert denials[0].details["reason"] == REASON_KILL_ENGAGED
    assert denials[0].details["agent_principal_id"] == "agent:revenue-bot"
    assert session.committed, "the denial was not durable"


# --------------------------------------------------------------------------- #
# Ordering: a refusal that discloses is still a disclosure
# --------------------------------------------------------------------------- #


async def test_a_killed_agent_learns_nothing_about_the_datasource() -> None:
    """The gate sits above `ensure_datasource_enabled` on purpose.

    With the order reversed, a stopped agent probing tool versions would read
    which datasources are admitting work off the difference between a 409 and
    a 403 -- a small leak, but one granted to exactly the caller an operator
    has decided should be doing nothing at all.
    """
    session = _ToolSession(
        version=_version(),
        tool=_tool(),
        datasource=_datasource(enabled=False),
        contracts=[_contract(kill_engaged=True)],
    )

    with pytest.raises(HTTPException) as excinfo:
        await _execute(session, _agent())

    assert excinfo.value.status_code == 403, "the datasource 409 answered first"
    assert excinfo.value.detail == REASON_KILL_ENGAGED


# --------------------------------------------------------------------------- #
# Who this must not affect
# --------------------------------------------------------------------------- #


async def test_a_human_caller_passes_the_gate_untouched() -> None:
    """A human has no contract, and this control must be invisible to them.

    Proven by where the request gets to: past the contract gate and into the
    datasource admission check, which this fixture fails deliberately. A test
    asserting "no exception" could not tell "allowed through" from "never
    reached the gate".
    """
    session = _ToolSession(
        version=_version(),
        tool=_tool(),
        datasource=_datasource(enabled=False),
        contracts=[],
    )

    with pytest.raises(HTTPException) as excinfo:
        await _execute(session, _human())

    assert excinfo.value.status_code == 409
    assert not _denials(session), "a human caller was recorded as a contract denial"


async def test_a_contracted_agent_within_its_envelope_passes_the_gate() -> None:
    """The gate must let the legitimate case through, or it is not a control
    but an outage. Same proof by position as the human case above."""
    session = _ToolSession(
        version=_version(),
        tool=_tool(),
        datasource=_datasource(enabled=False),
        contracts=[_contract(kill_engaged=False)],
        tier_scan=[],
    )

    with pytest.raises(HTTPException) as excinfo:
        await _execute(session, _agent())

    assert excinfo.value.status_code == 409
    assert not _denials(session)


async def test_an_agent_identity_with_no_contract_is_refused() -> None:
    """An `AGENT`-typed caller with no contract must not be served as an
    uncontracted human -- that is the boundary confusion
    `load_contract_for_principal` exists to close, and this route now inherits
    the closure rather than reimplementing it."""
    session = _ToolSession(
        version=_version(),
        tool=_tool(),
        datasource=_datasource(),
        contracts=[],
    )

    with pytest.raises(HTTPException) as excinfo:
        await _execute(session, _agent(principal_id="revenue-bot-without-prefix"))

    assert excinfo.value.status_code == 403
