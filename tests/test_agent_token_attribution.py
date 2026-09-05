"""AG-10: per-agent budget attribution.

The agent inbox used to render a cap with "usage not tracked" under it,
because nothing attributed model consumption to the agent that caused it.

The number this closes it with is an **estimate**, and every test here holds
that line. No provider adapter in `build_model_providers` returns a usage
block, so the only figure available is the 4-bytes-per-token heuristic
`ProviderNeutralModelGateway` already enforces the approved input cap
against. That is also the *right* figure to show: consumption measured any
other way would not be comparable to the cap it is drawn beside.

Three properties:

* the estimate the gateway reports is exactly the one it enforced the cap
  against -- otherwise an agent could be shown as inside a budget it was
  refused by;
* a run with no model call reports NULL, not zero -- "nothing ran" and "ran
  and cost nothing" are different answers to "is this agent inside budget";
* the inbox daily figure counts today only, because the cap is daily.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.agent_contract_api import get_agent_inbox
from aida.config import Settings
from aida.db import Base
from aida.model_gateway import (
    ApprovedModelRoute,
    DeterministicTestProvider,
    ModelCallEvidence,
    ProviderNeutralModelGateway,
    SqlGenerationOutput,
)
from aida.models import (
    AGENT_SAMPLING_RATE_FLOOR,
    AgentContract,
    AgentRun,
    AiAsset,
    AiAssetVersion,
    DataDomain,
    DataSource,
    LineOfBusiness,
    Organization,
    Project,
)
from aida.security import SecurityContext

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_org_and_datasource(session: AsyncSession) -> tuple[Organization, DataSource]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=f"src-{uuid4().hex[:8]}",
        connector_type="snowflake",
        dialect="snowflake",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        status="ACTIVE",
        capabilities={},
    )
    session.add_all([org, lob, domain, project, datasource])
    await session.flush()
    return org, datasource


async def _seed_agent_with_cap(
    session: AsyncSession, org: Organization, *, cap: int | None
) -> AiAssetVersion:
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
    session.add(
        AgentContract(
            organization_id=org.id,
            ai_asset_version_id=version.id,
            agent_principal_id=f"agent:steward-{uuid4().hex[:6]}",
            capability_envelope={"tool_slugs": [], "context_product_ids": [], "write_lanes": []},
            autonomy_tier="T1",
            supervisor_persona="STEWARD",
            kill_scope="AGENT",
            kill_engaged=False,
            sampling_rate=AGENT_SAMPLING_RATE_FLOOR,
            daily_token_cap=cap,
            created_by="human-author",
        )
    )
    await session.flush()
    return version


def _run(
    org: Organization,
    datasource: DataSource,
    version_id: UUID | None,
    *,
    created_at: datetime,
    tokens: tuple[int, int] | None,
) -> AgentRun:
    return AgentRun(
        organization_id=org.id,
        datasource_id=datasource.id,
        principal_id="agent:steward",
        status="COMPLETED",
        question_hash=uuid4().hex,
        generation_source="MODEL_GATEWAY",
        ai_asset_version_id=version_id,
        created_at=created_at,
        estimated_input_tokens=None if tokens is None else tokens[0],
        estimated_output_tokens=None if tokens is None else tokens[1],
    )


# ---------------------------------------------------------------------------
# The gateway reports the estimate it enforced the cap against
# ---------------------------------------------------------------------------


def _route(**overrides: object) -> ApprovedModelRoute:
    values: dict[str, Any] = {
        "route_key": "test-route",
        "provider_type": "deterministic",
        "model_id": "test-model",
        "endpoint_alias": "test",
        "credential_reference": "env://TEST_MODEL_KEY",
        "timeout_seconds": 10,
        "max_input_tokens": 100_000,
        "max_output_tokens": 4_000,
    }
    values.update(overrides)
    return ApprovedModelRoute(**values)


async def _complete(
    session: AsyncSession,
    organization_id: UUID,
    *,
    payload: dict[str, Any],
) -> tuple[SqlGenerationOutput, ModelCallEvidence]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        model_route="test-route",
        model_generation_enabled=True,
    )
    gateway = ProviderNeutralModelGateway(
        settings,
        providers={
            "deterministic": DeterministicTestProvider(
                {"sql": "SELECT 1", "confidence": 0.9, "rationale_codes": ["TEST"]}
            )
        },
    )
    return await gateway.structured_completion(
        session=session,
        organization_id=organization_id,
        route=_route(),
        system_instruction="be governed",
        payload=payload,
        output_schema=SqlGenerationOutput,
    )


async def test_the_reported_estimate_is_the_one_the_cap_was_enforced_against(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If these two numbers could differ, an agent could be shown as
    comfortably inside a budget the very same call was refused by."""
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    org, _datasource = await _seed_org_and_datasource(session)
    payload: dict[str, Any] = {"question": "x" * 400}

    _output, evidence = await _complete(session, org.id, payload=payload)

    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert evidence.estimated_input_tokens == max(1, len(serialized) // 4)
    assert evidence.estimated_output_tokens > 0


async def test_a_bigger_payload_estimates_more(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_MODEL_KEY", "secret")
    org, _datasource = await _seed_org_and_datasource(session)

    _o1, small = await _complete(session, org.id, payload={"question": "x" * 40})
    _o2, large = await _complete(session, org.id, payload={"question": "x" * 4000})

    assert large.estimated_input_tokens > small.estimated_input_tokens


# ---------------------------------------------------------------------------
# The inbox daily figure
# ---------------------------------------------------------------------------


async def _inbox(session: AsyncSession, org: Organization) -> Any:
    return await get_agent_inbox(
        org.id,
        persona="STEWARD",
        limit=50,
        since_hours=168,
        context=SecurityContext(
            principal_id="steward-1",
            principal_type="USER",
            organization_id=org.id,
            roles=frozenset({"DataSteward"}),
        ),
        session=session,
        settings=Settings(_env_file=None, environment="test"),  # type: ignore[call-arg]
    )


async def test_the_inbox_sums_todays_estimate_against_a_daily_cap(
    session: AsyncSession,
) -> None:
    org, datasource = await _seed_org_and_datasource(session)
    version = await _seed_agent_with_cap(session, org, cap=100_000)
    now = datetime.now(UTC)
    session.add_all(
        [
            _run(org, datasource, version.id, created_at=now, tokens=(1_000, 200)),
            _run(
                org,
                datasource,
                version.id,
                created_at=now - timedelta(hours=2),
                tokens=(500, 100),
            ),
        ]
    )
    await session.flush()

    inbox = await _inbox(session, org)

    agent = next(a for a in inbox.agents if a.version_id == version.id)
    assert agent.budget.daily_token_cap == 100_000
    assert agent.budget.daily_tokens_estimated == 1_800


async def test_a_run_outside_the_day_counts_toward_runs_but_not_the_daily_budget(
    session: AsyncSession,
) -> None:
    """The window the inbox lists activity over is a week; the cap is a day.
    Mixing them would show an agent over budget on last Tuesday traffic."""
    org, datasource = await _seed_org_and_datasource(session)
    version = await _seed_agent_with_cap(session, org, cap=100_000)
    now = datetime.now(UTC)
    session.add_all(
        [
            _run(org, datasource, version.id, created_at=now, tokens=(1_000, 200)),
            _run(
                org,
                datasource,
                version.id,
                created_at=now - timedelta(days=3),
                tokens=(90_000, 5_000),
            ),
        ]
    )
    await session.flush()

    inbox = await _inbox(session, org)

    agent = next(a for a in inbox.agents if a.version_id == version.id)
    assert agent.runs_recent == 2, "both runs are inside the activity window"
    assert agent.budget.daily_tokens_estimated == 1_200, "only today counts against the cap"


async def test_no_model_call_today_reports_none_rather_than_zero(
    session: AsyncSession,
) -> None:
    """A query-memory hit answers without a model call. Reporting 0 there
    reads as an idle agent; it was not idle."""
    org, datasource = await _seed_org_and_datasource(session)
    version = await _seed_agent_with_cap(session, org, cap=100_000)
    session.add(_run(org, datasource, version.id, created_at=datetime.now(UTC), tokens=None))
    await session.flush()

    inbox = await _inbox(session, org)

    agent = next(a for a in inbox.agents if a.version_id == version.id)
    assert agent.runs_recent == 1
    assert agent.budget.daily_tokens_estimated is None


async def test_a_run_by_no_agent_is_not_attributed_to_one(session: AsyncSession) -> None:
    """A direct human run has no agent identity, so it must not land on any
    agent budget -- attribution is to the named version or nowhere."""
    org, datasource = await _seed_org_and_datasource(session)
    version = await _seed_agent_with_cap(session, org, cap=100_000)
    session.add(_run(org, datasource, None, created_at=datetime.now(UTC), tokens=(50_000, 9_000)))
    await session.flush()

    inbox = await _inbox(session, org)

    agent = next(a for a in inbox.agents if a.version_id == version.id)
    assert agent.runs_recent == 0
    assert agent.budget.daily_tokens_estimated is None


async def test_an_agent_with_no_cap_still_reports_its_consumption(
    session: AsyncSession,
) -> None:
    org, datasource = await _seed_org_and_datasource(session)
    version = await _seed_agent_with_cap(session, org, cap=None)
    session.add(_run(org, datasource, version.id, created_at=datetime.now(UTC), tokens=(700, 300)))
    await session.flush()

    inbox = await _inbox(session, org)

    agent = next(a for a in inbox.agents if a.version_id == version.id)
    assert agent.budget.daily_token_cap is None
    assert agent.budget.daily_tokens_estimated == 1_000
