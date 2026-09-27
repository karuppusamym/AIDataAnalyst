"""A task agent cannot run until it is registered, and nothing registered one.

`AIDA_STEWARD_AGENT_INTERVAL_MINUTES=1` does not make the steward agent run.
`task_agent_schedule.registered_organizations` starts an agent only where its
workload identity holds an `AgentContract` whose `AiAssetVersion` is APPROVED,
and a freshly seeded estate has neither -- so all three task agents were
correctly and silently inert, and the acceptance guide told a reader to set an
interval and watch one work.

`scripts/seed_task_agent.py` is the missing path.
`test_a_seeded_agent_becomes_schedulable` is the test that matters: it asserts
against the same function the scheduler calls, so "registered" means
"the scheduler would start it" rather than "some rows exist".

The rest pin the controls the script exists to go *through* rather than
around. A seeding script that wrote an APPROVED row directly would produce a
working agent and a false audit trail, and every later claim about
maker-checker would rest on rows nobody checked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.models import AgentContract, AiAssetVersion, Organization
from aida.task_agent_schedule import registered_organizations
from scripts.seed_task_agent import seed

SLUG = "sample-bank"


@pytest_asyncio.fixture
async def estate(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker]:
    """One organization, which is all the script's own lookups need.

    The handlers it drives create everything else. StaticPool because the
    script opens its own session from the factory, and each new connection to
    `:memory:` would otherwise be a different empty database.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.add(Organization(name="Sample Bank", slug=SLUG))
        await session.commit()

    # The script binds `session_factory` and `get_settings` at module scope, so
    # those are the names to replace -- patching `aida.db` leaves the script
    # pointed at the real database. (That mistake, made in a sibling test file,
    # silently read the developer's live Postgres.)
    import scripts.seed_task_agent as script

    monkeypatch.setattr(script, "session_factory", maker)
    monkeypatch.setattr(
        script, "get_settings", lambda: Settings(_env_file=None, environment="test")
    )
    yield maker
    await engine.dispose()


async def _seed(**overrides: object) -> int:
    kwargs: dict[str, object] = {
        "organization_slug": SLUG,
        "agent_key": "steward",
        "tier": "T1",
        "tool_slugs": [],
        "exemplars": 3,
        "fail_one": False,
        "same_identity": False,
    }
    kwargs.update(overrides)
    return await seed(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The defect this closes
# --------------------------------------------------------------------------- #


async def test_a_seeded_agent_becomes_schedulable(
    estate: async_sessionmaker,
) -> None:
    """Asserted against the scheduler's own query, not against row counts.

    Before this script, `registered_organizations` returned nothing for every
    task agent on every estate, so the interval setting had no observable
    effect at all.
    """
    async with estate() as session:
        before = await registered_organizations(session, principal_id="agent:steward")
    assert before == [], "the fixture estate already had a registered agent"

    assert await _seed() == 0

    async with estate() as session:
        after = await registered_organizations(session, principal_id="agent:steward")
    assert len(after) == 1, "the scheduler still would not start this agent"


async def test_the_version_reaches_approved_through_the_review(
    estate: async_sessionmaker,
) -> None:
    """APPROVED is the state `registered_organizations` requires, and it is
    reached as the *effect* of a decision rather than written directly."""
    await _seed()

    async with estate() as session:
        version = await session.scalar(select(AiAssetVersion))
    assert version is not None
    assert version.status == "APPROVED"


async def test_the_contract_records_the_agents_workload_identity(
    estate: async_sessionmaker,
) -> None:
    await _seed(tier="T1")

    async with estate() as session:
        contract = await session.scalar(select(AgentContract))
    assert contract is not None
    assert contract.agent_principal_id == "agent:steward"
    assert contract.autonomy_tier == "T1"


async def test_the_envelope_is_closed_by_default(estate: async_sessionmaker) -> None:
    """A contract seeded with no `--tool-slug` must authorise no governed tool.

    `envelope_violation` treats an envelope's `tool_slugs` as a closed
    allowlist, so an empty list is the safe default -- and a seeding script
    that quietly granted every tool would hand out an authority nobody asked
    for.
    """
    await _seed(tool_slugs=[])

    async with estate() as session:
        contract = await session.scalar(select(AgentContract))
    assert contract is not None
    assert contract.capability_envelope["tool_slugs"] == []


async def test_a_named_tool_slug_is_the_only_one_granted(
    estate: async_sessionmaker,
) -> None:
    await _seed(tool_slugs=["accounts_by_branch"])

    async with estate() as session:
        contract = await session.scalar(select(AgentContract))
    assert contract is not None
    assert contract.capability_envelope["tool_slugs"] == ["accounts_by_branch"]


# --------------------------------------------------------------------------- #
# The controls it goes through rather than around
# --------------------------------------------------------------------------- #


async def test_the_author_cannot_approve_their_own_agent(
    estate: async_sessionmaker,
) -> None:
    """INV-8. `--same-identity` exists so this refusal can be *watched*, and
    this asserts the platform still produces it: no contract is written and the
    version does not reach APPROVED."""
    assert await _seed(same_identity=True) == 0

    async with estate() as session:
        version = await session.scalar(select(AiAssetVersion))
        contract = await session.scalar(select(AgentContract))
    assert version is not None
    assert version.status != "APPROVED"
    assert contract is None, "a contract was written for an unapproved version"


async def test_a_failing_evaluation_gate_holds_the_approval_back(
    estate: async_sessionmaker,
) -> None:
    """N15. The gate is recomputed live on every APPROVE and stored evidence is
    never trusted, so a publish cannot be manufactured. One unmatched exemplar
    out of two is below the 0.8 threshold.
    """
    assert await _seed(exemplars=2, fail_one=True) == 1

    async with estate() as session:
        version = await session.scalar(select(AiAssetVersion))
        contract = await session.scalar(select(AgentContract))
    assert version is not None
    assert version.status != "APPROVED"
    assert contract is None


# --------------------------------------------------------------------------- #
# Re-running
# --------------------------------------------------------------------------- #


async def test_running_twice_does_not_create_a_rival_authority(
    estate: async_sessionmaker,
) -> None:
    """Idempotent by necessity, not convenience: a run that fails the gate has
    already committed the asset, so an operator's second attempt must continue
    rather than collide with its own first one."""
    await _seed()
    await _seed()

    async with estate() as session:
        contracts = list((await session.scalars(select(AgentContract))).all())
    assert len(contracts) == 1


async def test_an_unknown_agent_key_is_refused(estate: async_sessionmaker) -> None:
    """The registry is the authority on which agents exist, so a typo names
    the three that do rather than half-seeding a fourth."""
    with pytest.raises(SystemExit) as excinfo:
        await _seed(agent_key="not-an-agent")

    assert "steward" in str(excinfo.value)


async def test_an_unknown_organization_is_refused(
    estate: async_sessionmaker,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        await _seed(organization_slug=f"absent-{uuid4().hex[:6]}")

    assert "seed_sample_estate" in str(excinfo.value)
