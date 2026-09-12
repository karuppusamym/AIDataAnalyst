"""AG-10: the agent contract -- deterministic validation and enforcement.

`Docs/00-product/08-market-deep-dive-and-target-architecture-2026-09.md`
section 4.2. This module holds the *authority* half of the contract (INV-3:
deterministic services decide, models only propose): what a contract may
declare, whether an agent's kill switch currently blocks it, and whether a
plan's selected governed tool is inside the agent's capability envelope.
`aida.agent_contract_api` is the HTTP surface over it;
`aida.agent_orchestrator.GovernedAgentOrchestrator.run` is the enforcement
point that calls `agent_kill_blocking_reason` and `envelope_violation`
before a linked run does any work.

Every check here fails closed: a run that names an agent version with no
contract is refused (`agent_contract_missing`), not run unconstrained, and
an unknown enum value is a validation error, never a silent default.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.model_gateway import kill_switch_blocking_state
from aida.models import (
    AGENT_AUTONOMY_TIERS,
    AGENT_KILL_SCOPES,
    AGENT_SAMPLING_RATE_FLOOR,
    AGENT_SUPERVISOR_PERSONAS,
    AGENT_WRITE_LANES,
    AgentContract,
    AiAsset,
    AiAssetVersion,
)

#: Reason codes carried by `AgentPolicyRejected.reason_code` and written
#: verbatim into the DENIED audit row's `details.reason`.
REASON_CONTRACT_MISSING = "agent_contract_missing"
REASON_KILL_ENGAGED = "agent_kill_switch_engaged"
REASON_ENVELOPE_VIOLATION = "agent_envelope_violation"
REASON_CONTEXT_PRODUCT_VIOLATION = "agent_context_product_violation"


class AgentContractValidationError(ValueError):
    """A contract definition that the deterministic rules refuse. `code` is
    a stable machine-readable reason; the message never carries a value
    other than the offending field name.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CapabilityEnvelope:
    tool_slugs: tuple[str, ...]
    context_product_ids: tuple[str, ...]
    write_lanes: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "tool_slugs": list(self.tool_slugs),
            "context_product_ids": list(self.context_product_ids),
            "write_lanes": list(self.write_lanes),
        }


@dataclass(frozen=True, slots=True)
class AgentContractDefinition:
    """Everything a caller may set on a contract, already shaped as the
    service expects it. `aida.agent_contract_api.AgentContractWrite` is the
    Pydantic edge of this dataclass.
    """

    agent_principal_id: str
    capability_envelope: CapabilityEnvelope
    autonomy_tier: str
    supervisor_persona: str
    kill_scope: str
    sampling_rate: float
    daily_token_cap: int | None = None
    per_run_token_cap: int | None = None
    wall_clock_seconds_cap: int | None = None
    eval_gate_threshold: float | None = None


def _string_list(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AgentContractValidationError(
            "envelope_field_invalid", f"capability_envelope.{field} must be a list of strings"
        )
    cleaned = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
    return cleaned


def parse_capability_envelope(raw: dict[str, Any]) -> CapabilityEnvelope:
    """Parse and normalize a stored/submitted envelope. Unknown keys and
    unknown write lanes are refused rather than ignored -- an envelope the
    platform cannot fully interpret is not an envelope it can enforce.
    """
    unknown = set(raw) - {"tool_slugs", "context_product_ids", "write_lanes"}
    if unknown:
        raise AgentContractValidationError(
            "envelope_unknown_key",
            "capability_envelope carries a key the platform does not enforce",
        )
    write_lanes = _string_list(raw.get("write_lanes", []), field="write_lanes")
    if any(lane not in AGENT_WRITE_LANES for lane in write_lanes):
        raise AgentContractValidationError(
            "envelope_write_lane_invalid",
            "capability_envelope.write_lanes contains an unknown lane",
        )
    return CapabilityEnvelope(
        tool_slugs=_string_list(raw.get("tool_slugs", []), field="tool_slugs"),
        context_product_ids=_string_list(
            raw.get("context_product_ids", []), field="context_product_ids"
        ),
        write_lanes=write_lanes,
    )


def validate_contract_definition(
    definition: AgentContractDefinition,
    *,
    actor_principal_id: str,
    human_principal_ids: frozenset[str] = frozenset(),
) -> None:
    """The deterministic rules every contract write must clear.

    - `agent_principal_id` is a distinct, non-human identity: never the
      human writing the contract (INV-8 -- otherwise the supervisor would be
      approving their own agent's work under one identity), never any other
      principal the caller names as human (the asset version's owner, for
      instance), and never a bare human-looking id with no `agent:` prefix.
    - Enumerations are closed (`autonomy_tier`, `supervisor_persona`,
      `kill_scope`, `capability_envelope.write_lanes`).
    - `sampling_rate` never drops below `AGENT_SAMPLING_RATE_FLOOR` (ADR-0027's
      5% floor) and never exceeds 1.0.
    - Every cap and threshold is either absent or positive/in range.
    """
    principal = definition.agent_principal_id.strip()
    if not principal:
        raise AgentContractValidationError(
            "agent_principal_missing", "agent_principal_id is required"
        )
    if principal == actor_principal_id or principal in human_principal_ids:
        raise AgentContractValidationError(
            "agent_principal_is_human",
            "agent_principal_id must be a distinct non-human identity, never a human principal",
        )
    if not principal.startswith("agent:"):
        raise AgentContractValidationError(
            "agent_principal_not_workload_identity",
            "agent_principal_id must be a workload identity of the form 'agent:<name>'",
        )
    if definition.autonomy_tier not in AGENT_AUTONOMY_TIERS:
        raise AgentContractValidationError("autonomy_tier_invalid", "autonomy_tier is invalid")
    if definition.supervisor_persona not in AGENT_SUPERVISOR_PERSONAS:
        raise AgentContractValidationError(
            "supervisor_persona_invalid", "supervisor_persona is invalid"
        )
    if definition.kill_scope not in AGENT_KILL_SCOPES:
        raise AgentContractValidationError("kill_scope_invalid", "kill_scope is invalid")
    if not (AGENT_SAMPLING_RATE_FLOOR <= definition.sampling_rate <= 1.0):
        raise AgentContractValidationError(
            "sampling_rate_below_floor",
            f"sampling_rate must be between {AGENT_SAMPLING_RATE_FLOOR} and 1.0",
        )
    for field_name in ("daily_token_cap", "per_run_token_cap", "wall_clock_seconds_cap"):
        cap = getattr(definition, field_name)
        if cap is not None and cap <= 0:
            raise AgentContractValidationError(
                "cap_not_positive", f"{field_name} must be a positive integer when set"
            )
    threshold = definition.eval_gate_threshold
    if threshold is not None and not (0.0 <= threshold <= 1.0):
        raise AgentContractValidationError(
            "eval_gate_threshold_out_of_range", "eval_gate_threshold must be within [0, 1]"
        )
    if any(lane not in AGENT_WRITE_LANES for lane in definition.capability_envelope.write_lanes):
        raise AgentContractValidationError(
            "envelope_write_lane_invalid",
            "capability_envelope.write_lanes contains an unknown lane",
        )
    if definition.capability_envelope.write_lanes:
        # AR-06, deliberately fail-closed. `write_lanes` has been parsed,
        # validated and stored since AG-10 shipped, and no write path in this
        # codebase reads it -- there is no agent write that names a lane, so
        # there is nothing for a lane to bound. Accepting the declaration would
        # put an unenforced control into a governance dossier, which is the
        # specific failure AR-06 names. Refusing it means whoever builds the
        # first lane-bearing write path has to build the check in the same
        # change: this branch is what will fail their contract fixture.
        raise AgentContractValidationError(
            "envelope_write_lane_unenforceable",
            "capability_envelope.write_lanes cannot be declared until a write "
            "path enforces it; declaring an unenforced lane is refused",
        )


def apply_definition(contract: AgentContract, definition: AgentContractDefinition) -> None:
    contract.agent_principal_id = definition.agent_principal_id.strip()
    contract.capability_envelope = definition.capability_envelope.as_json()
    contract.autonomy_tier = definition.autonomy_tier
    contract.supervisor_persona = definition.supervisor_persona
    contract.kill_scope = definition.kill_scope
    contract.sampling_rate = definition.sampling_rate
    contract.daily_token_cap = definition.daily_token_cap
    contract.per_run_token_cap = definition.per_run_token_cap
    contract.wall_clock_seconds_cap = definition.wall_clock_seconds_cap
    contract.eval_gate_threshold = definition.eval_gate_threshold


async def load_agent_contract(
    session: AsyncSession, *, organization_id: UUID, ai_asset_version_id: UUID
) -> AgentContract | None:
    """The contract for one agent version, organization-scoped (INV-5)."""
    contract: AgentContract | None = await session.scalar(
        select(AgentContract).where(
            AgentContract.organization_id == organization_id,
            AgentContract.ai_asset_version_id == ai_asset_version_id,
        )
    )
    return contract


async def load_agent_asset_version(
    session: AsyncSession, *, organization_id: UUID, ai_asset_version_id: UUID
) -> tuple[AiAsset, AiAssetVersion] | None:
    """The `AGENT`-kind asset and version a contract may attach to, or
    `None` when the version does not exist in this organization or its
    asset is not an agent.
    """
    row = (
        await session.execute(
            select(AiAsset, AiAssetVersion)
            .join(AiAssetVersion, AiAssetVersion.asset_id == AiAsset.id)
            .where(
                AiAssetVersion.id == ai_asset_version_id,
                AiAssetVersion.organization_id == organization_id,
                AiAsset.organization_id == organization_id,
                AiAsset.asset_kind == "AGENT",
            )
        )
    ).first()
    if row is None:
        return None
    asset, version = row
    return asset, version


async def agent_kill_blocking_reason(
    session: AsyncSession, contract: AgentContract
) -> str | None:
    """Why this agent's runs are blocked right now, or `None`.

    Extends `aida.model_gateway.kill_switch_blocking_state` (the organization-
    wide / per-route switch every generation request already checks) with
    the contract's own `kill_scope`:

    - `AGENT` -- only this contract's `kill_engaged` flag stops it.
    - `TIER`  -- any engaged contract in the organization with
      `kill_scope == "TIER"` and the same `autonomy_tier` stops it.
    - `ALL`   -- any engaged contract in the organization with
      `kill_scope == "ALL"` stops it, as does the organization-wide model
      kill switch (`GLOBAL_KILL_SWITCH_SCOPE`).

    A live query on every call, same as the model-gateway check: a switch
    engaged a moment ago blocks the very next run.
    """
    if contract.kill_engaged:
        return REASON_KILL_ENGAGED
    engaged_rows = (
        await session.scalars(
            select(AgentContract).where(
                AgentContract.organization_id == contract.organization_id,
                AgentContract.kill_engaged.is_(True),
                AgentContract.kill_scope.in_(["TIER", "ALL"]),
            )
        )
    ).all()
    for row in engaged_rows:
        if row.kill_scope == "ALL":
            return REASON_KILL_ENGAGED
        if row.kill_scope == "TIER" and row.autonomy_tier == contract.autonomy_tier:
            return REASON_KILL_ENGAGED
    global_switch = await kill_switch_blocking_state(session, contract.organization_id, None)
    if global_switch is not None:
        return REASON_KILL_ENGAGED
    return None


def envelope_violation(contract: AgentContract, *, tool_slug: str) -> str | None:
    """`REASON_ENVELOPE_VIOLATION` when a planned governed tool's slug is
    outside the contract's `capability_envelope.tool_slugs`; `None` when it
    is inside. An envelope that cannot be parsed is treated as empty --
    nothing is allowed -- never as unrestricted.
    """
    try:
        envelope = parse_capability_envelope(dict(contract.capability_envelope or {}))
    except AgentContractValidationError:
        return REASON_ENVELOPE_VIOLATION
    if tool_slug not in envelope.tool_slugs:
        return REASON_ENVELOPE_VIOLATION
    return None


def context_product_violation(
    contract: AgentContract, *, product_key: str, product_id: str
) -> str | None:
    """`REASON_CONTEXT_PRODUCT_VIOLATION` when a contracted agent reads a
    context product its envelope does not name (AR-06).

    `capability_envelope.context_product_ids` was parsed and stored from the
    day AG-10 shipped and read by nothing -- the 2026-09-09 review found the
    declaration but no enforcement. This is the enforcement, at the MCP
    server's `tools/list` and `tools/call`, when the caller scopes a request
    to a product with `contextProductUri`. An earlier version of this note
    also claimed the resource path; `resources/read` and `prompts/get` do not
    call it, and neither do the REST context-product routes -- see
    `Docs/10-architecture/18-agent-capability-enforcement-matrix.md`.

    Either identifier matches, because a contract is written by a human who
    may reasonably name the product by its stable `product_key` or by its
    UUID, and refusing one of the two would be a usability trap rather than a
    control. An empty list allows nothing, exactly as `tool_slugs` does: an
    envelope is an allowlist, and an empty allowlist is empty.
    """
    try:
        envelope = parse_capability_envelope(dict(contract.capability_envelope or {}))
    except AgentContractValidationError:
        return REASON_CONTEXT_PRODUCT_VIOLATION
    allowed = set(envelope.context_product_ids)
    if product_key in allowed or product_id in allowed:
        return None
    return REASON_CONTEXT_PRODUCT_VIOLATION


async def load_contract_for_principal(
    session: AsyncSession,
    *,
    organization_id: UUID,
    agent_principal_id: str,
    principal_type: str | None = None,
) -> AgentContract | None:
    """The contract for a workload identity, or `None` for a human principal.

    The lookup a *boundary* needs, as opposed to `load_agent_contract`'s:
    a request arriving at the MCP server carries a principal, not an asset
    version id, and the question is "is this caller a contracted agent, and if
    so what may it touch". `ix_agent_contract_org_principal` exists for this.

    Humans without a contract return None. A workload identity with no
    contract, or any identity with multiple matching contracts, is refused.
    Ambiguity must never disable the caller's capability restriction.

    `principal_type` is the *authenticated* kind of the caller -- the value
    `aida.oidc.context_from_claims` takes from a verified token claim against a
    closed enum, not anything the caller spells for itself. Passing it closes a
    boundary confusion this function had while it keyed the decision on the
    name alone: the `agent:` prefix is required of every contract's
    `agent_principal_id` (`validate_contract_definition`) but of nothing at the
    door, so an identity the identity provider asserts *is* an `AGENT`, issued
    with a subject that simply lacks the prefix, found no contract, fell
    through this function's `return None` and was served as an uncontracted
    human -- no kill switch, no `tool_slugs` envelope, no token caps, because
    the boundary passed `agent_asset_version_id=None` to the orchestrator. An
    agent identity could therefore shed its entire contract by being named
    without a prefix, which is one boundary's identity acting as another's.
    The authenticated type now decides, and the prefix is kept only as the
    additional signal it always was.

    `SERVICE_ACCOUNT` and `WORKER` are deliberately *not* held to this rule:
    they name the platform's own internal callers, which legitimately have no
    agent contract. `AGENT` is the type that means "a contracted agent", and it
    is the one that must resolve exactly one contract.
    """
    if not agent_principal_id:
        return None
    rows = (
        await session.scalars(
            select(AgentContract)
            .where(
                AgentContract.organization_id == organization_id,
                AgentContract.agent_principal_id == agent_principal_id,
            )
            .limit(2)
        )
    ).all()
    if len(rows) != 1:
        if rows or agent_principal_id.startswith("agent:") or principal_type == "AGENT":
            raise AgentContractValidationError(
                "agent_contract_unresolved",
                "agent identity requires exactly one unambiguous contract",
            )
        return None
    return rows[0]
