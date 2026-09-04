"""AG-10: the agent contract, its enforcement, and the agent task ledger.

Covers the three things the contract exists to guarantee, each against a
real in-memory database rather than a double:

* a run that names an agent version with **no** contract is refused, not
  run unconstrained;
* an engaged kill switch -- the agent's own, its tier's, or the
  organization's -- stops the run before it does any work;
* a governed tool outside the capability envelope is refused, and the
  refusal is attributable.

Plus the deterministic sampler, which has to replay for ADR-0027's audit
argument to hold, and the Tier-0-style property that an agent's identity can
never be a human's.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.agent_contracts import (
    REASON_ENVELOPE_VIOLATION,
    REASON_KILL_ENGAGED,
    AgentContractDefinition,
    AgentContractValidationError,
    CapabilityEnvelope,
    agent_kill_blocking_reason,
    envelope_violation,
    load_agent_asset_version,
    load_agent_contract,
    parse_capability_envelope,
    validate_contract_definition,
)
from aida.agent_tasks import (
    canonical_inputs_fingerprint,
    finish_agent_task,
    record_agent_task,
    record_audit_outcome,
    sampled_for_audit,
    task_for_agent_run,
)
from aida.db import Base
from aida.models import (
    AGENT_SAMPLING_RATE_FLOOR,
    AgentContract,
    AiAsset,
    AiAssetVersion,
    Organization,
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_agent(
    session: AsyncSession, *, org: Organization | None = None, name: str = "Steward agent"
) -> tuple[Organization, AiAsset, AiAssetVersion]:
    if org is None:
        org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
    asset = AiAsset(
        organization_id=org.id,
        asset_key=f"agent-{uuid4().hex[:8]}",
        asset_kind="AGENT",
        created_by="human-author",
    )
    session.add(asset)
    await session.flush()
    version = AiAssetVersion(
        organization_id=org.id,
        asset_id=asset.id,
        version=1,
        status="APPROVED",
        name=name,
        description="Drafts descriptions.",
        intended_use="Steward assistance.",
        owner_principal="steward-team",
        provider_type="INTERNAL",
        risk_tier="LOW",
        context_product_version_ids=[],
        model_route_ids=[],
        policy_control_ids=[],
        evaluation_evidence={},
        runtime_evidence={},
        fingerprint=uuid4().hex,
        created_by="human-author",
    )
    session.add(version)
    await session.flush()
    return org, asset, version


def _definition(**overrides: object) -> AgentContractDefinition:
    values: dict[str, object] = {
        "agent_principal_id": "agent:steward",
        "capability_envelope": CapabilityEnvelope(
            tool_slugs=("monthly-balance",), context_product_ids=(), write_lanes=()
        ),
        "autonomy_tier": "T1",
        "supervisor_persona": "STEWARD",
        "kill_scope": "AGENT",
        "sampling_rate": AGENT_SAMPLING_RATE_FLOOR,
    }
    values.update(overrides)
    return AgentContractDefinition(**values)  # type: ignore[arg-type]


async def _seed_contract(
    session: AsyncSession, org: Organization, version: AiAssetVersion, **overrides: object
) -> AgentContract:
    # `kill_engaged` is contract *state*, not part of the definition a caller
    # submits, so it is split out before the definition is built.
    kill_engaged = bool(overrides.pop("kill_engaged", False))
    definition = _definition(**overrides)
    contract = AgentContract(
        organization_id=org.id,
        ai_asset_version_id=version.id,
        agent_principal_id=definition.agent_principal_id,
        capability_envelope=definition.capability_envelope.as_json(),
        autonomy_tier=definition.autonomy_tier,
        supervisor_persona=definition.supervisor_persona,
        kill_scope=definition.kill_scope,
        kill_engaged=kill_engaged,
        sampling_rate=definition.sampling_rate,
        created_by="human-author",
    )
    session.add(contract)
    await session.flush()
    return contract


# ---------------------------------------------------------------------------
# 1. Contract validation -- the identity rules
# ---------------------------------------------------------------------------


def test_agent_principal_can_never_be_the_human_who_wrote_the_contract() -> None:
    """INV-8's premise: if the agent borrowed its supervisor's identity, the
    maker-checker distinction would collapse silently rather than loudly."""
    with pytest.raises(AgentContractValidationError) as excinfo:
        validate_contract_definition(
            _definition(agent_principal_id="alice"), actor_principal_id="alice"
        )
    assert excinfo.value.code == "agent_principal_is_human"


def test_agent_principal_can_never_be_another_named_human() -> None:
    with pytest.raises(AgentContractValidationError) as excinfo:
        validate_contract_definition(
            _definition(agent_principal_id="steward-team"),
            actor_principal_id="alice",
            human_principal_ids=frozenset({"steward-team"}),
        )
    assert excinfo.value.code == "agent_principal_is_human"


def test_agent_principal_must_be_a_workload_identity() -> None:
    with pytest.raises(AgentContractValidationError) as excinfo:
        validate_contract_definition(
            _definition(agent_principal_id="steward-bot"), actor_principal_id="alice"
        )
    assert excinfo.value.code == "agent_principal_not_workload_identity"


def test_sampling_rate_below_the_adr_0027_floor_is_refused() -> None:
    with pytest.raises(AgentContractValidationError) as excinfo:
        validate_contract_definition(
            _definition(sampling_rate=0.01), actor_principal_id="alice"
        )
    assert excinfo.value.code == "sampling_rate_below_floor"


def test_a_valid_contract_definition_passes() -> None:
    validate_contract_definition(_definition(), actor_principal_id="alice")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("autonomy_tier", "T9"),
        ("supervisor_persona", "EVERYONE"),
        ("kill_scope", "WORLD"),
    ],
)
def test_closed_enumerations_are_closed(field: str, value: str) -> None:
    with pytest.raises(AgentContractValidationError):
        validate_contract_definition(
            _definition(**{field: value}), actor_principal_id="alice"
        )


def test_an_envelope_key_the_platform_cannot_enforce_is_refused() -> None:
    """An envelope Atlas cannot fully interpret is not an envelope it can
    enforce, so it is refused rather than partially honoured."""
    with pytest.raises(AgentContractValidationError) as excinfo:
        parse_capability_envelope({"tool_slugs": [], "may_do_anything": True})
    assert excinfo.value.code == "envelope_unknown_key"


def test_an_unknown_write_lane_is_refused() -> None:
    with pytest.raises(AgentContractValidationError) as excinfo:
        parse_capability_envelope({"write_lanes": ["DIRECT_SOURCE_WRITE"]})
    assert excinfo.value.code == "envelope_write_lane_invalid"


# ---------------------------------------------------------------------------
# 2. Envelope enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_inside_the_envelope_is_allowed(session: AsyncSession) -> None:
    org, _asset, version = await _seed_agent(session)
    contract = await _seed_contract(session, org, version)
    assert envelope_violation(contract, tool_slug="monthly-balance") is None


@pytest.mark.asyncio
async def test_a_tool_outside_the_envelope_is_refused(session: AsyncSession) -> None:
    org, _asset, version = await _seed_agent(session)
    contract = await _seed_contract(session, org, version)
    assert envelope_violation(contract, tool_slug="wire-transfer") == REASON_ENVELOPE_VIOLATION


@pytest.mark.asyncio
async def test_an_unparseable_envelope_allows_nothing(session: AsyncSession) -> None:
    """Fail closed: a corrupt envelope is empty, never unrestricted. The
    opposite default would turn a bad migration into an unbounded agent."""
    org, _asset, version = await _seed_agent(session)
    contract = await _seed_contract(session, org, version)
    contract.capability_envelope = {"tool_slugs": ["x"], "nonsense": 1}
    assert envelope_violation(contract, tool_slug="x") == REASON_ENVELOPE_VIOLATION


# ---------------------------------------------------------------------------
# 3. Kill scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_agents_own_switch_blocks_it(session: AsyncSession) -> None:
    org, _asset, version = await _seed_agent(session)
    contract = await _seed_contract(session, org, version, kill_engaged=True)
    assert await agent_kill_blocking_reason(session, contract) == REASON_KILL_ENGAGED


@pytest.mark.asyncio
async def test_an_unengaged_switch_does_not_block(session: AsyncSession) -> None:
    org, _asset, version = await _seed_agent(session)
    contract = await _seed_contract(session, org, version)
    assert await agent_kill_blocking_reason(session, contract) is None


@pytest.mark.asyncio
async def test_a_tier_switch_blocks_every_agent_in_that_tier(session: AsyncSession) -> None:
    org, _asset, version_a = await _seed_agent(session, name="agent A")
    _org, _asset_b, version_b = await _seed_agent(session, org=org, name="agent B")
    target = await _seed_contract(session, org, version_a, agent_principal_id="agent:a")
    await _seed_contract(
        session,
        org,
        version_b,
        agent_principal_id="agent:b",
        kill_scope="TIER",
        kill_engaged=True,
    )
    assert await agent_kill_blocking_reason(session, target) == REASON_KILL_ENGAGED


@pytest.mark.asyncio
async def test_a_tier_switch_does_not_block_a_different_tier(session: AsyncSession) -> None:
    org, _asset, version_a = await _seed_agent(session, name="agent A")
    _org, _asset_b, version_b = await _seed_agent(session, org=org, name="agent B")
    target = await _seed_contract(
        session, org, version_a, agent_principal_id="agent:a", autonomy_tier="T0"
    )
    await _seed_contract(
        session,
        org,
        version_b,
        agent_principal_id="agent:b",
        autonomy_tier="T2",
        kill_scope="TIER",
        kill_engaged=True,
    )
    assert await agent_kill_blocking_reason(session, target) is None


@pytest.mark.asyncio
async def test_an_all_scope_switch_blocks_every_agent(session: AsyncSession) -> None:
    org, _asset, version_a = await _seed_agent(session, name="agent A")
    _org, _asset_b, version_b = await _seed_agent(session, org=org, name="agent B")
    target = await _seed_contract(
        session, org, version_a, agent_principal_id="agent:a", autonomy_tier="T0"
    )
    await _seed_contract(
        session,
        org,
        version_b,
        agent_principal_id="agent:b",
        autonomy_tier="T3",
        kill_scope="ALL",
        kill_engaged=True,
    )
    assert await agent_kill_blocking_reason(session, target) == REASON_KILL_ENGAGED


@pytest.mark.asyncio
async def test_a_switch_in_another_organization_does_not_reach_across(
    session: AsyncSession,
) -> None:
    """INV-5: kill state is organization-scoped like everything else."""
    org_a, _a, version_a = await _seed_agent(session)
    org_b, _b, version_b = await _seed_agent(session)
    target = await _seed_contract(session, org_a, version_a, agent_principal_id="agent:a")
    await _seed_contract(
        session,
        org_b,
        version_b,
        agent_principal_id="agent:b",
        kill_scope="ALL",
        kill_engaged=True,
    )
    assert org_a.id != org_b.id
    assert await agent_kill_blocking_reason(session, target) is None


# ---------------------------------------------------------------------------
# 4. Contract lookup is organization-scoped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contract_lookup_is_organization_scoped(session: AsyncSession) -> None:
    org_a, _a, version_a = await _seed_agent(session)
    org_b, _b, _vb = await _seed_agent(session)
    await _seed_contract(session, org_a, version_a)
    found = await load_agent_contract(
        session, organization_id=org_b.id, ai_asset_version_id=version_a.id
    )
    assert found is None


@pytest.mark.asyncio
async def test_only_agent_kind_assets_can_carry_a_contract(session: AsyncSession) -> None:
    org, asset, version = await _seed_agent(session)
    asset.asset_kind = "MODEL"
    await session.flush()
    found = await load_agent_asset_version(
        session, organization_id=org.id, ai_asset_version_id=version.id
    )
    assert found is None


# ---------------------------------------------------------------------------
# 5. The task ledger and its deterministic sampler
# ---------------------------------------------------------------------------


def test_sampling_is_deterministic_and_replays() -> None:
    """ADR-0027's audit argument depends on this: an auditor must be able to
    recompute which items should have been sampled."""
    fingerprint = canonical_inputs_fingerprint({"a": 1})
    first = sampled_for_audit(fingerprint, 0.5)
    for _ in range(10):
        assert sampled_for_audit(fingerprint, 0.5) is first


def test_sampling_honours_the_floor_even_when_the_rate_is_lower() -> None:
    """A rate below the floor -- from a hand-edited config or a row written
    before the constraint existed -- still samples at 5%."""
    sampled_at_floor = 0
    sampled_below = 0
    for index in range(400):
        fingerprint = canonical_inputs_fingerprint({"i": index})
        sampled_at_floor += sampled_for_audit(fingerprint, AGENT_SAMPLING_RATE_FLOOR)
        sampled_below += sampled_for_audit(fingerprint, 0.0)
    assert sampled_below == sampled_at_floor > 0


def test_a_fingerprint_is_stable_across_key_order() -> None:
    assert canonical_inputs_fingerprint({"a": 1, "b": 2}) == canonical_inputs_fingerprint(
        {"b": 2, "a": 1}
    )


@pytest.mark.asyncio
async def test_a_task_opens_proposed_and_closes_applied(session: AsyncSession) -> None:
    org, _asset, version = await _seed_agent(session)
    task = await record_agent_task(
        session,
        organization_id=org.id,
        agent_principal_id="agent:steward",
        intent="agent.analysis",
        inputs={"question_hash": "abc"},
        ai_asset_version_id=version.id,
        sampling_rate=0.0,  # floored to 5%; this row is very unlikely to sample
    )
    assert task.status == "PROPOSED"
    finish_agent_task(task, status="APPLIED", evidence={"ok": True})
    assert task.status in ("APPLIED", "SAMPLED")
    assert task.finished_at is not None


@pytest.mark.asyncio
async def test_a_sampled_applied_task_becomes_sampled_with_a_pending_audit(
    session: AsyncSession,
) -> None:
    org, _asset, version = await _seed_agent(session)
    task = await record_agent_task(
        session,
        organization_id=org.id,
        agent_principal_id="agent:steward",
        intent="agent.analysis",
        inputs={"question_hash": "abc"},
        ai_asset_version_id=version.id,
        sampling_rate=1.0,  # sample everything
    )
    assert task.sampled_for_audit is True
    finish_agent_task(task, status="APPLIED")
    assert task.status == "SAMPLED"
    assert task.audit_outcome == "PENDING"


def test_a_task_cannot_be_closed_back_into_proposed() -> None:
    from aida.models import AgentTask

    task = AgentTask(
        organization_id=uuid4(),
        agent_principal_id="agent:x",
        intent="i",
        inputs_fingerprint="f" * 64,
        status="PROPOSED",
        sampled_for_audit=False,
        started_at=datetime.now(UTC),
        evidence={},
    )
    with pytest.raises(ValueError, match="terminal status"):
        finish_agent_task(task, status="PROPOSED")


def test_an_unsampled_task_cannot_carry_an_audit_outcome() -> None:
    from aida.models import AgentTask

    task = AgentTask(
        organization_id=uuid4(),
        agent_principal_id="agent:x",
        intent="i",
        inputs_fingerprint="f" * 64,
        status="APPLIED",
        sampled_for_audit=False,
        started_at=datetime.now(UTC),
        evidence={},
    )
    with pytest.raises(ValueError, match="not sampled"):
        record_audit_outcome(task, outcome="AGREED")


@pytest.mark.asyncio
async def test_a_task_is_findable_by_its_run(session: AsyncSession) -> None:
    org, _asset, version = await _seed_agent(session)
    run_id = uuid4()
    await record_agent_task(
        session,
        organization_id=org.id,
        agent_principal_id="agent:steward",
        intent="agent.analysis",
        inputs={"question_hash": "abc"},
        ai_asset_version_id=version.id,
        agent_run_id=run_id,
    )
    found = await task_for_agent_run(session, agent_run_id=run_id)
    assert found is not None
    assert found.agent_run_id == run_id


@pytest.mark.asyncio
async def test_the_task_ledger_is_value_free(session: AsyncSession) -> None:
    """INV-6: only a fingerprint of the inputs is stored, never the inputs.

    The caller assembles a value-free payload; what this asserts is that the
    ledger keeps a digest of it rather than the payload itself, so even a
    caller that made a mistake cannot leak a value through this table.
    """
    org, _asset, version = await _seed_agent(session)
    task = await record_agent_task(
        session,
        organization_id=org.id,
        agent_principal_id="agent:steward",
        intent="agent.analysis",
        inputs={"question_hash": "SENTINEL-VALUE"},
        ai_asset_version_id=version.id,
    )
    stored = (await session.scalars(select(type(task)).where(type(task).id == task.id))).one()
    blob = f"{stored.inputs_fingerprint}{stored.evidence}{stored.intent}"
    assert "SENTINEL-VALUE" not in blob
    assert len(stored.inputs_fingerprint) == 64
    assert UUID(str(stored.organization_id)) == org.id
