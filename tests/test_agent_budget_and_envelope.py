"""AR-05 and AR-06: contract caps and the capability envelope, enforced.

The 2026-09-09 architecture review
(`Docs/10-architecture/15-agent-architecture-critical-review.md`) found that
`AgentContract` validated and stored `daily_token_cap`, `per_run_token_cap`,
`wall_clock_seconds_cap`, `context_product_ids` and `write_lanes`, and that
*no runtime path read any of them* except `tool_slugs`. A declared control
nothing enforces is worse than an absent one, because a governance dossier
quotes it as though it bounds something.

Each test here fails against the implementation as it stood at `15f29cd`.

The daily-cap tests are the ones that matter most, and the interleaving they
use is deliberate: reservations are taken without reconciling between them,
which is the shape a read-then-check implementation gets wrong. SQLite
serializes writers, so none of this proves behaviour under genuine
multi-connection contention -- the PostgreSQL reproduction F05 got is still
outstanding for `agent_budget_window`, and AR-05 is not closed on these tests
alone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.agent_budget import (
    REASON_DAILY_TOKEN_CAP,
    REASON_PER_RUN_TOKEN_CAP,
    REASON_WALL_CLOCK_CAP,
    AgentBudgetExceeded,
    daily_reserved_tokens,
    per_run_violation,
    reconcile_run_budget,
    reserve_run_budget,
    wall_clock_violation,
)
from aida.agent_contracts import (
    REASON_CONTEXT_PRODUCT_VIOLATION,
    AgentContractDefinition,
    AgentContractValidationError,
    CapabilityEnvelope,
    context_product_violation,
    load_contract_for_principal,
    validate_contract_definition,
)
from aida.db import Base
from aida.model_gateway import estimate_payload_tokens, estimate_serialized_tokens
from aida.models import AgentContract, AiAsset, AiAssetVersion, Organization

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_contract(
    session: AsyncSession,
    *,
    daily_token_cap: int | None = None,
    per_run_token_cap: int | None = None,
    wall_clock_seconds_cap: int | None = None,
    context_product_ids: list[str] | None = None,
    agent_principal_id: str | None = None,
) -> AgentContract:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    asset = AiAsset(
        organization_id=org.id,
        asset_key=f"agent-{uuid4().hex[:6]}",
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
        name="Steward agent",
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
    contract = AgentContract(
        organization_id=org.id,
        ai_asset_version_id=version.id,
        agent_principal_id=agent_principal_id or f"agent:steward-{uuid4().hex[:6]}",
        capability_envelope={
            "tool_slugs": [],
            "context_product_ids": context_product_ids or [],
            "write_lanes": [],
        },
        autonomy_tier="T1",
        supervisor_persona="STEWARD",
        kill_scope="AGENT",
        kill_engaged=False,
        sampling_rate=0.05,
        daily_token_cap=daily_token_cap,
        per_run_token_cap=per_run_token_cap,
        wall_clock_seconds_cap=wall_clock_seconds_cap,
        created_by="human-author",
    )
    session.add(contract)
    await session.flush()
    return contract


# ---------------------------------------------------------------------------
# AR-05: the caps have a runtime consumer at all
# ---------------------------------------------------------------------------


async def test_ar05_a_contract_with_no_caps_reserves_nothing(session: AsyncSession) -> None:
    """The overwhelmingly common case must stay free: no caps declared, no
    ledger row created, no query issued on the hot path."""
    contract = await _seed_contract(session)
    reservation = await reserve_run_budget(
        session, contract, estimated_input_tokens=500, now=_NOW
    )
    assert reservation.is_reserved is False
    assert (
        await daily_reserved_tokens(
            session,
            organization_id=contract.organization_id,
            ai_asset_version_id=contract.ai_asset_version_id,
            window_date=_NOW.date(),
        )
        == 0
    )


async def test_ar05_an_uncontracted_run_reserves_nothing(session: AsyncSession) -> None:
    """An analyst asking a question has no contract and therefore no cap."""
    reservation = await reserve_run_budget(
        session, None, estimated_input_tokens=10_000, now=_NOW
    )
    assert reservation.is_reserved is False


async def test_ar05_a_reservation_is_recorded_and_reconciled(session: AsyncSession) -> None:
    contract = await _seed_contract(session, daily_token_cap=10_000, per_run_token_cap=1_000)
    reservation = await reserve_run_budget(
        session, contract, estimated_input_tokens=120, now=_NOW
    )

    # The per-run cap is what is held, not the input estimate: the output has
    # not been generated yet and could still cost the rest of it.
    assert reservation.amount == 1_000
    assert (
        await daily_reserved_tokens(
            session,
            organization_id=contract.organization_id,
            ai_asset_version_id=contract.ai_asset_version_id,
            window_date=_NOW.date(),
        )
        == 1_000
    )

    await reconcile_run_budget(session, reservation, actual_tokens=300)
    assert (
        await daily_reserved_tokens(
            session,
            organization_id=contract.organization_id,
            ai_asset_version_id=contract.ai_asset_version_id,
            window_date=_NOW.date(),
        )
        == 300
    )


async def test_ar05_a_failed_run_gives_its_reservation_back(session: AsyncSession) -> None:
    """A run of provider failures must not exhaust a day's budget without a
    single answer being produced."""
    contract = await _seed_contract(session, daily_token_cap=1_000, per_run_token_cap=400)
    for _ in range(5):
        reservation = await reserve_run_budget(
            session, contract, estimated_input_tokens=100, now=_NOW
        )
        await reconcile_run_budget(session, reservation, actual_tokens=0)
    assert (
        await daily_reserved_tokens(
            session,
            organization_id=contract.organization_id,
            ai_asset_version_id=contract.ai_asset_version_id,
            window_date=_NOW.date(),
        )
        == 0
    )


async def test_ar05_the_daily_cap_refuses_once_the_window_is_full(
    session: AsyncSession,
) -> None:
    contract = await _seed_contract(session, daily_token_cap=1_000, per_run_token_cap=400)
    first = await reserve_run_budget(session, contract, estimated_input_tokens=1, now=_NOW)
    second = await reserve_run_budget(session, contract, estimated_input_tokens=1, now=_NOW)
    await reconcile_run_budget(session, first, actual_tokens=400)
    await reconcile_run_budget(session, second, actual_tokens=400)

    with pytest.raises(AgentBudgetExceeded) as excinfo:
        await reserve_run_budget(session, contract, estimated_input_tokens=1, now=_NOW)
    assert excinfo.value.reason_code == REASON_DAILY_TOKEN_CAP


async def test_ar05_a_run_that_cannot_fit_the_whole_day_is_refused_outright(
    session: AsyncSession,
) -> None:
    """A per-run cap above the daily cap is a misconfiguration; refusing before
    touching the ledger keeps it from parking a row at its limit."""
    contract = await _seed_contract(session, daily_token_cap=100, per_run_token_cap=500)
    with pytest.raises(AgentBudgetExceeded):
        await reserve_run_budget(session, contract, estimated_input_tokens=1, now=_NOW)
    assert (
        await daily_reserved_tokens(
            session,
            organization_id=contract.organization_id,
            ai_asset_version_id=contract.ai_asset_version_id,
            window_date=_NOW.date(),
        )
        == 0
    )


async def test_ar05_interleaved_reservations_cannot_jointly_break_the_cap(
    session: AsyncSession,
) -> None:
    """The property a read-then-check implementation does not have.

    Five runs each want a fifth of the day and *none* of them reconciles
    before the next starts -- the interleaving in which a read-then-check
    implementation lets every one of them see "0 spent" and proceed. Here the
    cap lives in the conditional UPDATE's own `WHERE`, so the fifth run is
    refused by the database rather than by a value Python read earlier.

    **What this does not prove.** SQLite serializes writers, so this exercises
    the predicate, not genuine multi-connection contention. The equivalent of
    F05's PostgreSQL reproduction -- N racers released from a shared barrier on
    separate connections, at both isolation levels -- is still outstanding for
    this table, and AR-05 is not closed on this test alone.
    """
    contract = await _seed_contract(session, daily_token_cap=1_000, per_run_token_cap=250)

    granted = 0
    for _ in range(5):
        try:
            await reserve_run_budget(session, contract, estimated_input_tokens=1, now=_NOW)
        except AgentBudgetExceeded:
            continue
        granted += 1

    assert granted == 4
    assert (
        await daily_reserved_tokens(
            session,
            organization_id=contract.organization_id,
            ai_asset_version_id=contract.ai_asset_version_id,
            window_date=_NOW.date(),
        )
        == 1_000
    )


async def test_ar05_the_window_rolls_over_at_the_utc_day(session: AsyncSession) -> None:
    contract = await _seed_contract(session, daily_token_cap=500, per_run_token_cap=500)
    await reserve_run_budget(session, contract, estimated_input_tokens=1, now=_NOW)
    with pytest.raises(AgentBudgetExceeded):
        await reserve_run_budget(session, contract, estimated_input_tokens=1, now=_NOW)

    tomorrow = _NOW + timedelta(days=1)
    granted = await reserve_run_budget(
        session, contract, estimated_input_tokens=1, now=tomorrow
    )
    assert granted.is_reserved is True


def test_ar05_the_per_run_cap_is_a_pure_comparison() -> None:
    contract = AgentContract(per_run_token_cap=1_000)
    assert per_run_violation(contract, tokens=999) is None
    assert per_run_violation(contract, tokens=1_000) is None
    assert per_run_violation(contract, tokens=1_001) == REASON_PER_RUN_TOKEN_CAP
    assert per_run_violation(AgentContract(per_run_token_cap=None), tokens=10**9) is None
    assert per_run_violation(None, tokens=10**9) is None


def test_ar05_the_wall_clock_cap_is_enforced_from_the_run_start() -> None:
    contract = AgentContract(wall_clock_seconds_cap=30)
    started = _NOW
    assert wall_clock_violation(contract, started_at=started, now=started) is None
    assert (
        wall_clock_violation(contract, started_at=started, now=started + timedelta(seconds=30))
        is None
    )
    assert (
        wall_clock_violation(contract, started_at=started, now=started + timedelta(seconds=31))
        == REASON_WALL_CLOCK_CAP
    )


def test_ar05_a_naive_start_timestamp_does_not_disable_the_wall_clock_cap() -> None:
    """Some drivers return `AgentRun.created_at` naive. Reading that as "no cap
    applies" would make a governance control depend on a driver."""
    contract = AgentContract(wall_clock_seconds_cap=10)
    naive_start = _NOW.replace(tzinfo=None)
    assert (
        wall_clock_violation(contract, started_at=naive_start, now=_NOW + timedelta(seconds=60))
        == REASON_WALL_CLOCK_CAP
    )


def test_ar05_the_budget_estimate_is_the_gateway_s_own() -> None:
    """The number a run is refused on and the number recorded against it have
    to be the same one, or an agent can be shown inside a budget it was
    refused by."""
    payload = {"question": "how many customers", "context": ["a" * 400]}
    import json

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert estimate_payload_tokens(payload) == estimate_serialized_tokens(serialized)
    assert estimate_payload_tokens({}) >= 1


# ---------------------------------------------------------------------------
# AR-06: the capability envelope beyond tool_slugs
# ---------------------------------------------------------------------------


def test_ar06_a_context_product_outside_the_envelope_is_refused() -> None:
    contract = AgentContract(
        capability_envelope={
            "tool_slugs": [],
            "context_product_ids": ["retail-risk"],
            "write_lanes": [],
        }
    )
    assert (
        context_product_violation(
            contract, product_key="retail-risk", product_id=str(uuid4())
        )
        is None
    )
    assert (
        context_product_violation(contract, product_key="fraud-ops", product_id=str(uuid4()))
        == REASON_CONTEXT_PRODUCT_VIOLATION
    )


def test_ar06_either_identifier_matches() -> None:
    """A contract is written by a human who may name the product by its stable
    key or by its UUID; refusing one of the two is a trap, not a control."""
    product_id = str(uuid4())
    contract = AgentContract(
        capability_envelope={
            "tool_slugs": [],
            "context_product_ids": [product_id],
            "write_lanes": [],
        }
    )
    assert (
        context_product_violation(contract, product_key="retail-risk", product_id=product_id)
        is None
    )


def test_ar06_an_empty_envelope_allows_no_context_product() -> None:
    """Same fail-closed semantics `tool_slugs` already has: an envelope is an
    allowlist, and an empty allowlist is empty."""
    contract = AgentContract(
        capability_envelope={
            "tool_slugs": [],
            "context_product_ids": [],
            "write_lanes": [],
        }
    )
    assert (
        context_product_violation(contract, product_key="anything", product_id=str(uuid4()))
        == REASON_CONTEXT_PRODUCT_VIOLATION
    )


def test_ar06_an_unparseable_envelope_allows_no_context_product() -> None:
    contract = AgentContract(capability_envelope={"nonsense_key": ["x"]})
    assert (
        context_product_violation(contract, product_key="retail-risk", product_id=str(uuid4()))
        == REASON_CONTEXT_PRODUCT_VIOLATION
    )


def _definition(**overrides: object) -> AgentContractDefinition:
    values: dict[str, object] = {
        "agent_principal_id": "agent:steward",
        "capability_envelope": CapabilityEnvelope(
            tool_slugs=(), context_product_ids=(), write_lanes=()
        ),
        "autonomy_tier": "T1",
        "supervisor_persona": "STEWARD",
        "kill_scope": "AGENT",
        "sampling_rate": 0.05,
    }
    values.update(overrides)
    return AgentContractDefinition(**values)  # type: ignore[arg-type]


def test_ar06_a_write_lane_the_platform_cannot_enforce_is_refused() -> None:
    """No write path in this codebase reads `write_lanes`. Accepting the
    declaration would put an unenforced control into a governance dossier,
    which is the failure AR-06 names. Whoever builds the first lane-bearing
    write path has to build the check in the same change: this is the branch
    that will fail their fixture."""
    with pytest.raises(AgentContractValidationError) as excinfo:
        validate_contract_definition(
            _definition(
                capability_envelope=CapabilityEnvelope(
                    tool_slugs=(), context_product_ids=(), write_lanes=("MEASURED_FACT",)
                )
            ),
            actor_principal_id="human-author",
        )
    assert excinfo.value.code == "envelope_write_lane_unenforceable"


def test_ar06_an_empty_write_lane_list_is_still_accepted() -> None:
    validate_contract_definition(_definition(), actor_principal_id="human-author")


async def test_ar06_a_principal_resolves_to_its_contract(session: AsyncSession) -> None:
    contract = await _seed_contract(session, agent_principal_id="agent:reviewer")
    found = await load_contract_for_principal(
        session,
        organization_id=contract.organization_id,
        agent_principal_id="agent:reviewer",
    )
    assert found is not None
    assert found.id == contract.id


async def test_ar06_a_human_principal_has_no_contract(session: AsyncSession) -> None:
    contract = await _seed_contract(session, agent_principal_id="agent:reviewer")
    assert (
        await load_contract_for_principal(
            session,
            organization_id=contract.organization_id,
            agent_principal_id="steward-a",
        )
        is None
    )
    assert (
        await load_contract_for_principal(
            session, organization_id=contract.organization_id, agent_principal_id=""
        )
        is None
    )


async def test_ar06_an_ambiguous_principal_resolves_to_no_contract(
    session: AsyncSession,
) -> None:
    """Two registered versions sharing one workload identity give an envelope
    the platform cannot pick between. Treat the caller as uncontracted rather
    than guessing which envelope binds it."""
    first = await _seed_contract(session, agent_principal_id="agent:shared")
    second = await _seed_contract(session, agent_principal_id="agent:shared")
    # Same organization, two versions, one identity.
    second.organization_id = first.organization_id
    await session.flush()

    assert (
        await load_contract_for_principal(
            session,
            organization_id=first.organization_id,
            agent_principal_id="agent:shared",
        )
        is None
    )


async def test_ar06_the_contract_lookup_is_organization_scoped(
    session: AsyncSession,
) -> None:
    await _seed_contract(session, agent_principal_id="agent:reviewer")
    other = Organization(name="Other", slug=f"other-{uuid4().hex[:8]}")
    session.add(other)
    await session.flush()
    assert (
        await load_contract_for_principal(
            session, organization_id=other.id, agent_principal_id="agent:reviewer"
        )
        is None
    )
