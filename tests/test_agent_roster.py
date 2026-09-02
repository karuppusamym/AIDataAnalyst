"""UX-19: agent roster with published purpose, task plan and live results.

Two halves, mirroring `test_consumer_footer.py`'s own split:

1. ``test_compose_agent_roster_assembles_purpose_method_and_results`` --
   seeds one registered `AGENT`-kind AI asset and a handful of real
   `AgentRun` rows, then proves `compose_agent_roster` assembles the
   published purpose (verbatim from the AI registry), an aggregated method
   summary (strategy/confidence/tool-first split correctly derived from
   `plan_evidence`/`generation_source`), and a bounded recent-results
   window -- all from real data, nothing invented.

2. ``test_agent_without_a_real_auto_apply_branch_gets_no_fabricated_threshold``
   -- the row's own stop-condition test: seeds an agent version whose
   registry evidence *looks* like it might carry an auto-apply threshold
   (a steward-entered `runtime_evidence` value shaped like one) and proves
   the roster does not treat that as a genuine, code-backed threshold --
   `has_auto_apply_branch` stays `False` and `threshold` stays `None`,
   because no real auto-apply branch exists anywhere in this codebase (see
   `aida.agent_roster`'s module docstring for the evidence).

Plus a handful of smaller compositional checks: an organization with no
registered `AGENT`-kind asset gets an honestly empty roster (not an error),
a `MODEL`-kind asset is excluded from an `AGENT` roster, and the wired-in
route enforces the same cross-organization boundary the rest of the AI
registry does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.agent_roster import compose_agent_roster
from aida.agent_roster_api import get_agent_roster
from aida.db import Base
from aida.models import (
    AgentRun,
    AiAsset,
    AiAssetVersion,
    DataDomain,
    DataSource,
    LineOfBusiness,
    Organization,
    Project,
)
from aida.security_types import SecurityContext

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _context(org_id: UUID, *, roles: frozenset[str] | None = None) -> SecurityContext:
    return SecurityContext(
        principal_id="steward-1",
        principal_type="USER",
        organization_id=org_id,
        roles=roles or frozenset({"DataSteward"}),
    )


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


async def _seed_agent_asset(
    session: AsyncSession,
    *,
    organization_id: UUID,
    asset_key: str = "governed-answer-agent",
    asset_kind: str = "AGENT",
    runtime_evidence: dict[str, object] | None = None,
    evaluation_evidence: dict[str, object] | None = None,
) -> tuple[AiAsset, AiAssetVersion]:
    asset = AiAsset(
        id=uuid4(),
        organization_id=organization_id,
        asset_key=asset_key,
        asset_kind=asset_kind,
        created_by="steward-1",
    )
    version = AiAssetVersion(
        id=uuid4(),
        organization_id=organization_id,
        asset_id=asset.id,
        version=1,
        status="APPROVED",
        name="Governed answer agent",
        description="Answers analyst questions over governed metadata and tools.",
        intended_use="Assist analysts; every write path still requires human review.",
        owner_principal="platform-team",
        provider_type="INTERNAL",
        risk_tier="MEDIUM",
        documentation_url="https://internal.example/docs/answer-agent",
        policy_control_ids=[],
        evaluation_evidence=evaluation_evidence or {},
        runtime_evidence=runtime_evidence or {},
        fingerprint="fp-1",
        created_by="steward-1",
    )
    session.add_all([asset, version])
    await session.flush()
    return asset, version


def _agent_run(
    *,
    organization_id: UUID,
    datasource_id: UUID,
    generation_source: str,
    status: str = "COMPLETED",
    strategy: str | None = None,
    confidence: float | None = None,
    created_at: datetime,
    failure_reason: str | None = None,
) -> AgentRun:
    plan_evidence: dict[str, object] = {}
    if strategy is not None:
        plan_evidence["strategy"] = strategy
    if confidence is not None:
        plan_evidence["confidence"] = confidence
    run = AgentRun(
        id=uuid4(),
        organization_id=organization_id,
        datasource_id=datasource_id,
        principal_id="analyst-1",
        status=status,
        question_hash=uuid4().hex,
        generation_source=generation_source,
        plan_evidence=plan_evidence,
        failure_reason=failure_reason,
    )
    run.created_at = created_at
    return run


# ---------------------------------------------------------------------------
# 1. Composition: purpose + method summary + recent results.
# ---------------------------------------------------------------------------


async def test_compose_agent_roster_assembles_purpose_method_and_results(
    session: AsyncSession,
) -> None:
    org, datasource = await _seed_org_and_datasource(session)
    asset, version = await _seed_agent_asset(session, organization_id=org.id)

    runs = [
        _agent_run(
            organization_id=org.id,
            datasource_id=datasource.id,
            generation_source="GOVERNED_TOOL",
            strategy="GOVERNED_TOOL",
            confidence=0.95,
            created_at=_NOW - timedelta(hours=1),
        ),
        _agent_run(
            organization_id=org.id,
            datasource_id=datasource.id,
            generation_source="GOVERNED_TOOL",
            strategy="GOVERNED_TOOL",
            confidence=0.9,
            created_at=_NOW - timedelta(hours=2),
        ),
        _agent_run(
            organization_id=org.id,
            datasource_id=datasource.id,
            generation_source="MODEL_GATEWAY",
            strategy="MODEL_GENERATION",
            confidence=0.6,
            created_at=_NOW - timedelta(hours=3),
        ),
        # A failed run should surface in recent_results but is excluded from
        # the COMPLETED-only method aggregation (mirrors TL-6's own rule).
        _agent_run(
            organization_id=org.id,
            datasource_id=datasource.id,
            generation_source="MODEL_GATEWAY",
            status="FAILED",
            strategy="MODEL_GENERATION",
            confidence=0.4,
            created_at=_NOW - timedelta(hours=4),
            failure_reason="SQL_GUARD_REJECTED",
        ),
    ]
    session.add_all(runs)
    await session.flush()

    roster = await compose_agent_roster(session, organization_id=org.id, now=_NOW)

    assert roster.total_agents == 1
    entry = roster.agents[0]

    # Purpose is verbatim from the AI registry.
    assert entry.purpose.asset_id == asset.id
    assert entry.purpose.asset_key == "governed-answer-agent"
    assert entry.purpose.name == version.name
    assert entry.purpose.description == version.description
    assert entry.purpose.intended_use == version.intended_use
    assert entry.purpose.owner_principal == "platform-team"
    assert entry.purpose.risk_tier == "MEDIUM"

    # Method: aggregated from plan_evidence/generation_source over COMPLETED
    # runs only -- 2 GOVERNED_TOOL + 1 MODEL_GENERATION, the FAILED run
    # excluded.
    assert entry.method.scope == "ORGANIZATION_WIDE"
    assert entry.method.sampled_runs == 3
    assert entry.method.by_strategy == {"GOVERNED_TOOL": 2, "MODEL_GENERATION": 1}
    assert entry.method.average_confidence == pytest.approx(round((0.95 + 0.9 + 0.6) / 3, 4))
    assert entry.method.tool_first.tool_first_executions == 2
    assert entry.method.tool_first.freeform_executions == 1
    assert entry.method.tool_first.total_executions == 3
    assert entry.method.tool_first.rate == pytest.approx(round(2 / 3, 4))

    # Recent results: all four runs, newest first, including the FAILED one.
    assert entry.recent_results_total == 4
    assert [result.status for result in entry.recent_results] == [
        "COMPLETED",
        "COMPLETED",
        "COMPLETED",
        "FAILED",
    ]
    assert entry.recent_results[-1].failure_reason == "SQL_GUARD_REJECTED"
    assert entry.recent_results[0].confidence == pytest.approx(0.95)


async def test_empty_organization_gets_an_honestly_empty_roster(session: AsyncSession) -> None:
    org, _datasource = await _seed_org_and_datasource(session)

    roster = await compose_agent_roster(session, organization_id=org.id, now=_NOW)

    assert roster.agents == []
    assert roster.total_agents == 0


async def test_a_model_kind_asset_is_excluded_from_the_agent_roster(
    session: AsyncSession,
) -> None:
    org, _datasource = await _seed_org_and_datasource(session)
    await _seed_agent_asset(
        session, organization_id=org.id, asset_key="scoring-model", asset_kind="MODEL"
    )

    roster = await compose_agent_roster(session, organization_id=org.id, now=_NOW)

    assert roster.agents == []


# ---------------------------------------------------------------------------
# 2. No fabricated auto-apply threshold.
# ---------------------------------------------------------------------------


async def test_agent_without_a_real_auto_apply_branch_gets_no_fabricated_threshold(
    session: AsyncSession,
) -> None:
    org, _datasource = await _seed_org_and_datasource(session)
    # A steward could enter anything here via `sync_ai_provider_evidence` --
    # this value is shaped exactly like a real auto-apply threshold, but it
    # is not backed by any code path that actually applies without review,
    # so the roster must not surface it as one.
    await _seed_agent_asset(
        session,
        organization_id=org.id,
        runtime_evidence={"auto_apply_threshold": 0.9, "success_rate": 0.97},
        evaluation_evidence={"auto_apply_threshold": 0.85, "pass_rate": 0.92},
    )

    roster = await compose_agent_roster(session, organization_id=org.id, now=_NOW)

    assert roster.total_agents == 1
    auto_apply = roster.agents[0].auto_apply
    assert auto_apply.has_auto_apply_branch is False
    assert auto_apply.threshold is None
    assert auto_apply.threshold_source is None
    assert auto_apply.evidence  # a real, non-empty explanation, not a blank field
    assert "GovernanceReview" in auto_apply.evidence


async def test_two_agents_in_the_same_org_both_report_no_auto_apply_branch(
    session: AsyncSession,
) -> None:
    org, _datasource = await _seed_org_and_datasource(session)
    await _seed_agent_asset(session, organization_id=org.id, asset_key="agent-a")
    await _seed_agent_asset(session, organization_id=org.id, asset_key="agent-b")

    roster = await compose_agent_roster(session, organization_id=org.id, now=_NOW)

    assert roster.total_agents == 2
    assert all(not entry.auto_apply.has_auto_apply_branch for entry in roster.agents)
    assert all(entry.auto_apply.threshold is None for entry in roster.agents)


# ---------------------------------------------------------------------------
# Route wiring: same cross-organization boundary the rest of the AI
# registry enforces.
# ---------------------------------------------------------------------------


async def test_a_foreign_organization_is_denied_before_the_database_is_touched(
    session: AsyncSession,
) -> None:
    org, _datasource = await _seed_org_and_datasource(session)
    other_org_id = uuid4()

    with pytest.raises(HTTPException) as excinfo:
        await get_agent_roster(
            org.id,
            window_days=30,
            recent_results_limit=20,
            context=_context(other_org_id),
            session=session,
        )
    assert excinfo.value.status_code == 403


async def test_the_wired_in_route_returns_the_composed_roster(session: AsyncSession) -> None:
    org, datasource = await _seed_org_and_datasource(session)
    await _seed_agent_asset(session, organization_id=org.id)
    session.add(
        _agent_run(
            organization_id=org.id,
            datasource_id=datasource.id,
            generation_source="GOVERNED_TOOL",
            strategy="GOVERNED_TOOL",
            confidence=0.9,
            created_at=_NOW,
        )
    )
    await session.flush()

    roster = await get_agent_roster(
        org.id,
        window_days=30,
        recent_results_limit=20,
        context=_context(org.id),
        session=session,
    )

    assert roster.organization_id == org.id
    assert roster.total_agents == 1
