"""R11-C6 finding 10: the native MCP tools get a per-agent allowlist.

The seven native tools gained the kill switch and contract existence on
2026-09-12, but nothing named *which* of them an agent may call -- and two are
not read-only: `validate_sql` reaches the source for a dry-run estimate, and
`request_data_product_access` opens a maker-checker request. `tool_slugs` could
not be reused, because it names `GovernedToolVersion` slugs and a native tool
has none. So the envelope gains its own dimension, `native_tools`.

What these pin, and why each matters:

* an envelope naming no native tool reaches none of them -- the allowlist is the
  control, not a label;
* a contract stored before the field existed reaches none either. An allowlist
  nobody wrote is empty, never unrestricted, which is how every other allowlist
  in the envelope already behaves;
* naming one tool reaches that tool and only that tool;
* granting a native tool is a widening, so it goes to review, while withdrawing
  one is not.

The gate is driven through the real `_handle_tools_call`, reusing the fixtures
of the kill-switch suite, and asserts on whether the native handler was
reached -- the same "before the dispatch" evidence that suite relies on.
"""

from typing import Any

import pytest

from aida.agent_contracts import (
    AgentContractDefinition,
    AgentContractValidationError,
    CapabilityEnvelope,
    contract_widening,
    parse_capability_envelope,
)
from aida.mcp_server import NATIVE_ALL_TOOL_SLUGS
from aida.models import AgentContract
from tests import test_r11c6_native_mcp_tool_contract as native

# The kill-switch suite's recorder of which native handler ran, bound as a module
# attribute so pytest finds it here too.
reached = native.reached


def _contract_with(native_tools: list[str] | None) -> AgentContract:
    contract = native._contract(kill_engaged=False)
    envelope: dict[str, Any] = dict(contract.capability_envelope)
    if native_tools is None:
        envelope.pop("native_tools", None)
    else:
        envelope["native_tools"] = native_tools
    contract.capability_envelope = envelope
    return contract


def _session_for(contract: AgentContract) -> Any:
    # Queue order: the contract lookup, the TIER/ALL kill scan, the org switch.
    return native._NativeSession([contract], [], [])


@pytest.mark.parametrize("slug", sorted(NATIVE_ALL_TOOL_SLUGS))
async def test_an_envelope_naming_no_native_tool_reaches_none(
    slug: str, reached: list[str]
) -> None:
    session = _session_for(_contract_with([]))

    result = await native._call(slug, session, native._agent())

    assert result["isError"] is True
    assert "agent_envelope_violation" in result["content"][0]["text"]
    assert reached == []
    assert "mcp.native_tool.envelope_denied" in native._audit_actions(session)


async def test_a_contract_stored_before_the_field_existed_reaches_no_native_tool(
    reached: list[str],
) -> None:
    """The fail-closed reading, pinned: an existing contract does not silently
    keep every native tool because it predates the allowlist. Granting them is
    an amendment, and an amendment that widens goes to review."""
    session = _session_for(_contract_with(None))

    result = await native._call("validate_sql", session, native._agent())

    assert result["isError"] is True
    assert reached == []


async def test_an_agent_reaches_exactly_the_native_tools_it_names(reached: list[str]) -> None:
    allowed = "get_lineage_graph"
    other = sorted(NATIVE_ALL_TOOL_SLUGS - {allowed})[0]

    served = await native._call(allowed, _session_for(_contract_with([allowed])), native._agent())
    refused = await native._call(other, _session_for(_contract_with([allowed])), native._agent())

    assert served == native.REACHED
    assert refused["isError"] is True
    assert reached == [allowed]


def test_the_envelope_parses_native_tools_and_still_refuses_unknown_keys() -> None:
    envelope = parse_capability_envelope({"native_tools": ["validate_sql", " validate_sql ", ""]})

    assert envelope.native_tools == ("validate_sql",)
    assert envelope.as_json()["native_tools"] == ["validate_sql"]
    with pytest.raises(AgentContractValidationError):
        parse_capability_envelope({"native_tool": ["validate_sql"]})


def _definition(native_tools: tuple[str, ...]) -> AgentContractDefinition:
    return AgentContractDefinition(
        agent_principal_id="agent:lineage-bot",
        capability_envelope=CapabilityEnvelope(
            tool_slugs=("quarterly_revenue",),
            context_product_ids=("revenue_context",),
            write_lanes=(),
            native_tools=native_tools,
        ),
        autonomy_tier="T1",
        supervisor_persona="STEWARD",
        kill_scope="AGENT",
        sampling_rate=0.05,
    )


def test_granting_a_native_tool_is_a_widening_and_withdrawing_one_is_not() -> None:
    existing = _contract_with(["get_lineage_graph"])

    granted = contract_widening(existing, _definition(("get_lineage_graph", "validate_sql")))
    withdrawn = contract_widening(existing, _definition(()))

    assert "capability_envelope.native_tools" in granted
    assert "capability_envelope.native_tools" not in withdrawn
