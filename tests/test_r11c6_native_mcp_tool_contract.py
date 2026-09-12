"""R11-C6 hole 3: the native MCP tools ran outside the contract entirely.

`_handle_tools_call` dispatched the seven native platform tools in three
near-identical branches that all `return`ed *above* the contract resolution
the governed-tool path does further down. So they ran with no contract: an
agent whose kill switch an operator had just engaged kept answering on this
door.

The audit asked whether this was a documentation fix -- whether these are
read-only metadata genuinely outside the contract's scope. It is not, and
`test_the_native_tool_inventory_is_what_this_fix_assumes` below is the
evidence rather than the assumption:

- five lineage tools are read-only, value-free metadata;
- `request_data_product_access` **mutates** (`request_marketplace_access`
  inserts a `DataProductAccessRequest`, opens a `GovernanceReview` and
  commits);
- `validate_sql` **reaches the data plane** (`QueryExecutionGateway.validate`
  runs as far as `estimate_read_query`, a dry-run against the customer's
  warehouse).

Two of seven mutate or leave the platform, and an engaged kill switch means
stop on every door -- not only the doors that read rows. So the fix is a
check, and the check is the kill switch plus contract existence.

What is *not* enforced here is recorded honestly and pinned by
`test_the_envelope_tool_slug_list_is_deliberately_not_read_here`:
`capability_envelope.tool_slugs` names `GovernedToolVersion` slugs, and a
native tool has none, so reading the field here would silently redefine it
for every stored contract.

These tests drive the real `_handle_tools_call` and assert on whether the
native *handler* was reached, because "returns before the contract
resolution" was the bug -- an assertion about the gate alone would not see
it. Move the `_native_tool_contract_denial` call back below the dispatch and
`test_a_killed_agent_cannot_call_*` fails: the handler runs.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest

from aida import mcp_server
from aida.mcp_server import (
    NATIVE_ALL_TOOL_SLUGS,
    NATIVE_LINEAGE_TOOL_SLUGS,
    NATIVE_MARKETPLACE_TOOL_SLUGS,
    NATIVE_VALIDATION_TOOL_SLUGS,
    _handle_tools_call,
    _native_tool_contract_denial,
)
from aida.models import AgentContract, AuditEvent
from aida.security import SecurityContext

ORG = uuid4()
REACHED = {"content": [{"type": "text", "text": "native handler reached"}]}


class _NativeSession:
    """The session surface `_native_tool_contract_denial` touches.

    `scalars` serves the agent-contract lookup first, then
    `agent_kill_blocking_reason`'s scan for engaged TIER/ALL contracts, then
    `kill_switch_blocking_state`'s organization-wide switch -- each from a
    queue, so a test says exactly what the database holds at each step.
    """

    def __init__(self, *scalars_queue: list[object]) -> None:
        self._queue = [list(rows) for rows in scalars_queue]
        self.added: list[object] = []
        self.committed = False

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

        return _Result()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.committed = True


def _contract(*, kill_engaged: bool, kill_scope: str = "AGENT") -> AgentContract:
    return AgentContract(
        id=uuid4(),
        organization_id=ORG,
        ai_asset_version_id=uuid4(),
        agent_principal_id="agent:lineage-bot",
        capability_envelope={
            "tool_slugs": ["quarterly_revenue"],
            "context_product_ids": ["revenue_context"],
            "write_lanes": [],
        },
        autonomy_tier="T1",
        supervisor_persona="STEWARD",
        kill_scope=kill_scope,
        kill_engaged=kill_engaged,
        sampling_rate=0.05,
    )


def _agent(principal_id: str = "agent:lineage-bot") -> SecurityContext:
    return SecurityContext(
        principal_id=principal_id,
        principal_type="AGENT",
        organization_id=ORG,
        roles=frozenset({"Analyst", "DataSteward"}),
    )


def _human() -> SecurityContext:
    return SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=ORG,
        roles=frozenset({"Analyst", "DataSteward"}),
    )


def _audit_actions(session: _NativeSession) -> list[str]:
    return [value.action for value in session.added if isinstance(value, AuditEvent)]


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace all three native handlers with a recorder.

    The point under test is *where* the gate sits, not what the handlers do,
    and the handlers themselves need a real catalog. Recording which one was
    reached is the assertion the bug would have failed.
    """
    calls: list[str] = []

    async def _lineage(slug: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(slug)
        return REACHED

    async def _marketplace(slug: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(slug)
        return REACHED

    async def _validation(slug: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(slug)
        return REACHED

    monkeypatch.setattr(mcp_server, "_handle_native_lineage_tool_call", _lineage)
    monkeypatch.setattr(mcp_server, "_handle_native_marketplace_tool_call", _marketplace)
    monkeypatch.setattr(mcp_server, "_handle_native_validation_tool_call", _validation)
    return calls


async def _call(
    slug: str, session: _NativeSession, context: SecurityContext
) -> dict[str, Any]:
    return await _handle_tools_call(
        {"name": f"atlas__{slug}", "arguments": {}},
        session,  # type: ignore[arg-type]
        context,
        None,  # type: ignore[arg-type]
        f"corr-{slug}",
    )


# --- the inventory this fix rests on ----------------------------------------


def test_the_native_tool_inventory_is_what_this_fix_assumes() -> None:
    """The finding, asserted rather than assumed.

    Seven native tools in three families. If a family gains a tool, or a
    family is added, this fails and whoever added it has to re-answer "does
    the contract reach it" -- which is the question this whole row exists
    because nobody asked.
    """
    assert NATIVE_LINEAGE_TOOL_SLUGS == {
        "get_lineage_graph",
        "get_lineage_impact",
        "resolve_entity",
        "get_transformation_detail",
        "get_asset_context",
    }
    assert NATIVE_MARKETPLACE_TOOL_SLUGS == {"request_data_product_access"}
    assert NATIVE_VALIDATION_TOOL_SLUGS == {"validate_sql"}
    assert NATIVE_ALL_TOOL_SLUGS == (
        NATIVE_LINEAGE_TOOL_SLUGS | NATIVE_MARKETPLACE_TOOL_SLUGS | NATIVE_VALIDATION_TOOL_SLUGS
    )
    assert len(NATIVE_ALL_TOOL_SLUGS) == 7


def test_the_marketplace_native_tool_really_does_write() -> None:
    """Why this was not a documentation fix. `request_data_product_access`
    reaches `request_marketplace_access`, which inserts a
    `DataProductAccessRequest`, opens a `GovernanceReview` and commits --
    a mutation an engaged kill switch must stop, whatever its maker-checker
    posture afterwards.
    """
    import inspect

    from aida.product_marketplace_api import request_marketplace_access

    body = inspect.getsource(request_marketplace_access)
    assert "session.add(access_request)" in body
    assert "session.add(review)" in body
    assert "await session.commit()" in body


def test_the_validation_native_tool_really_does_reach_the_source() -> None:
    """And why the other one was not either. `validate_sql` runs the real
    gateway pipeline as far as the dry-run estimate against the customer's
    warehouse -- value-free, but a call that leaves the platform and costs
    the customer money.
    """
    import inspect

    from aida.query_gateway import QueryExecutionGateway

    # `validate` threads the parse through `_run_validation`, which is where
    # the one reachable connector call lives (INV-2: `estimate_read_query`
    # only -- execution stays in `execute`).
    assert "_run_validation" in inspect.getsource(QueryExecutionGateway.validate)
    assert "estimate_read_query" in inspect.getsource(
        QueryExecutionGateway._run_validation
    )


# --- the kill switch now reaches every native tool --------------------------


@pytest.mark.parametrize("slug", sorted(NATIVE_ALL_TOOL_SLUGS))
async def test_a_killed_agent_cannot_call_any_native_tool(
    slug: str, reached: list[str]
) -> None:
    session = _NativeSession([_contract(kill_engaged=True)])

    result = await _call(slug, session, _agent())

    assert result["isError"] is True
    assert "agent_kill_switch_engaged" in result["content"][0]["text"]
    # The gate is *before* the dispatch, which is the whole fix.
    assert reached == []
    assert "mcp.native_tool.kill_switch_denied" in _audit_actions(session)
    assert session.committed is True


@pytest.mark.parametrize("slug", sorted(NATIVE_ALL_TOOL_SLUGS))
async def test_a_live_contract_still_reaches_every_native_tool(
    slug: str, reached: list[str]
) -> None:
    """The control must not become a second role check. `scalars` order:
    the contract, then `agent_kill_blocking_reason`'s TIER/ALL scan, then
    the organization-wide model switch -- none engaged.
    """
    session = _NativeSession([_contract(kill_engaged=False)], [], [])

    result = await _call(slug, session, _agent())

    assert result == REACHED
    assert reached == [slug]
    assert _audit_actions(session) == []


async def test_an_organization_wide_kill_scope_stops_a_native_tool(
    reached: list[str],
) -> None:
    """`kill_scope="ALL"` on *another* agent's engaged contract stops this
    one, exactly as `agent_kill_blocking_reason` defines it on the
    orchestration paths. Reusing that function rather than reading
    `kill_engaged` here is what makes the two agree by construction.
    """
    session = _NativeSession(
        [_contract(kill_engaged=False)],
        [_contract(kill_engaged=True, kill_scope="ALL")],
    )

    result = await _call("get_lineage_graph", session, _agent())

    assert result["isError"] is True
    assert reached == []
    assert "mcp.native_tool.kill_switch_denied" in _audit_actions(session)


async def test_an_uncontracted_human_reaches_the_native_tools_unchanged(
    reached: list[str],
) -> None:
    session = _NativeSession([])

    result = await _call("get_lineage_graph", session, _human())

    assert result == REACHED
    assert reached == ["get_lineage_graph"]


@pytest.mark.parametrize("slug", sorted(NATIVE_ALL_TOOL_SLUGS))
async def test_an_agent_identity_without_one_contract_is_refused(
    slug: str, reached: list[str]
) -> None:
    """Finding #3 of the enforcement matrix, on this door: an `agent:`
    principal with no contract was served like any role holder.
    """
    session = _NativeSession([])

    result = await _call(slug, session, _agent("agent:unknown-bot"))

    assert result["isError"] is True
    assert "agent_contract_unresolved" in result["content"][0]["text"]
    assert reached == []
    assert "mcp.native_tool.agent_contract_denied" in _audit_actions(session)


async def test_two_contracts_for_one_identity_are_refused_not_guessed(
    reached: list[str],
) -> None:
    """Ambiguity must never disable the restriction -- picking either row
    would be choosing which kill switch to honour.
    """
    session = _NativeSession([_contract(kill_engaged=True), _contract(kill_engaged=False)])

    result = await _call("validate_sql", session, _agent())

    assert result["isError"] is True
    assert "agent_contract_unresolved" in result["content"][0]["text"]
    assert reached == []


async def test_an_agent_without_a_tenant_cannot_shed_its_contract(
    reached: list[str],
) -> None:
    """`AgentContract.organization_id` is non-nullable, so a caller with no
    tenant can hold no contract. For a human that is simply "uncontracted";
    for an `AGENT` identity it is the unresolved case, and it must refuse
    here or arriving without a tenant would be a way to shed the switch.
    """
    session = _NativeSession()
    tenantless = SecurityContext(
        principal_id="agent:lineage-bot",
        principal_type="AGENT",
        organization_id=None,
        roles=frozenset({"Analyst"}),
    )

    result = await _call("get_asset_context", session, tenantless)

    assert result["isError"] is True
    assert "agent_contract_unresolved" in result["content"][0]["text"]
    assert reached == []


async def test_a_tenantless_human_is_unaffected(reached: list[str]) -> None:
    session = _NativeSession()
    tenantless = SecurityContext(
        principal_id="analyst",
        principal_type="USER",
        organization_id=None,
        roles=frozenset({"Analyst"}),
    )

    result = await _call("get_asset_context", session, tenantless)

    assert result == REACHED


# --- what is deliberately left unenforced, stated as such -------------------


async def test_the_envelope_tool_slug_list_is_deliberately_not_read_here() -> None:
    """The honest residual, pinned so it is a decision rather than an
    oversight.

    `capability_envelope.tool_slugs` names `GovernedToolVersion` slugs --
    that is what `envelope_violation` is called with on every other path. A
    native tool has no governed-tool version, so an envelope cannot name one
    in the sense the field means, and reading it here would silently
    redefine the field for every contract already stored. This agent's
    envelope names only `quarterly_revenue`, and it reaches the native
    tools anyway. What bounds them instead: roles, per-object gateway
    authorization, maker-checker on the one write, and now the kill switch.
    """
    contract = _contract(kill_engaged=False)
    assert "get_lineage_graph" not in contract.capability_envelope["tool_slugs"]
    session = _NativeSession([contract], [], [])

    assert (
        await _native_tool_contract_denial(
            "get_lineage_graph", session, _agent(), "corr-residual"  # type: ignore[arg-type]
        )
        is None
    )


async def test_a_product_scoped_native_call_is_still_refused_outright() -> None:
    """`context_product_ids` needs no new check on this door: a native call
    that arrives with a `contextProductUri` is refused before the gate, so
    there is no product-scoped native path for an envelope to bound.
    """
    session = _NativeSession([_contract(kill_engaged=False)], [], [])

    result = await _handle_tools_call(
        {
            "name": "atlas__get_lineage_graph",
            "arguments": {},
            "contextProductUri": "atlas://context-products/revenue_context/versions/1",
        },
        session,  # type: ignore[arg-type]
        _agent(),
        None,  # type: ignore[arg-type]
        "corr-scoped",
    )

    assert result["isError"] is True
    assert "not found or not published" in result["content"][0]["text"]


def test_the_fixture_agent_is_shaped_like_a_real_contracted_caller() -> None:
    contract = _contract(kill_engaged=False)
    assert contract.agent_principal_id.startswith("agent:")
    assert isinstance(contract.organization_id, UUID)
    assert _agent().principal_type == "AGENT"
    assert _agent().principal_id == contract.agent_principal_id
