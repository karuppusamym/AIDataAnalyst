"""ADR-0029: the quality agent.

The runtime properties every task agent shares are exercised in
`tests/test_steward_agent.py`. What is specific to this agent, and tested here:

* the rules it derives from profile history, and the bars under which it
  derives none;
* that it proposes only for an uncovered rule key, and never again once a
  person rejected or disabled one;
* that every proposal is a T2 review a person decides -- not the agent, and not
  the reviewer agent at any ceiling -- and that approval creates a rule that
  runs;
* that the runtime lets a spec raise its proposal ceiling to T2 and no further.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import aida.semantic_api  # noqa: F401 -- registers the decision target adapters
from aida.custom_quality_rules import evaluate_rule
from aida.governance_decision_service import GovernanceDecisionRefused, decide_review
from aida.models import (
    AgentTask,
    AnalysisRun,
    ColumnProfile,
    DataSource,
    GovernanceReview,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    QualityRule,
    QualityRulePack,
    TableProfile,
)
from aida.quality_agent import (
    CAPABILITY_NULL_RATE_CEILING,
    CAPABILITY_ROW_COUNT_FLOOR,
    QUALITY_AGENT,
    QUALITY_WORK,
    run_quality_agent,
)
from aida.quality_agent_api import get_quality_agent_state
from aida.quality_rule_proposal_model import QualityRuleProposal
from aida.quality_rule_proposals import (
    AGENT_RULE_PACK_NAME,
    derive_null_rate_ceiling,
    derive_row_count_floor,
)
from aida.review_queue_read_model import compose_review_queue
from aida.review_risk_tiers import TIER_T2, TIER_T3, agent_decidable_object_types, risk_tier_for
from aida.security import SecurityContext
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_WOULD_PROPOSE,
    HARD_MAX_PROPOSAL_TIER,
    REASON_OBJECT_TYPE_ABOVE_CEILING,
    TaskAgentOutcome,
    TaskAgentRefused,
    TaskAgentRunRequest,
    TaskAgentSpec,
    run_task_agent,
)
from tests.support.task_agents import (
    agent_settings,
    count_rows,
    human,
    register_agent,
    seed_estate,
    seed_table,
    task_agent_session,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT = "agent:quality"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


async def _column(
    session: AsyncSession, org: Organization, table: MetadataTable, *, name: str, position: int
) -> MetadataColumn:
    column = MetadataColumn(
        organization_id=org.id,
        table_id=table.id,
        name=name,
        ordinal_position=position,
        physical_type="text",
        nullable=True,
        fingerprint="fp",
    )
    session.add(column)
    await session.flush()
    return column


async def _profiles(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    table: MetadataTable,
    *,
    row_counts: list[int],
    columns: list[tuple[MetadataColumn, list[int]]] = (),  # type: ignore[assignment]
) -> None:
    """One completed profile per entry of `row_counts`, oldest first; each
    column's list is its null count in each."""
    for index, rows in enumerate(row_counts):
        run = AnalysisRun(organization_id=org.id, datasource_id=datasource.id, status="COMPLETED")
        session.add(run)
        await session.flush()
        profile = TableProfile(
            organization_id=org.id,
            analysis_run_id=run.id,
            datasource_id=datasource.id,
            table_id=table.id,
            row_count_estimate=rows,
            sampled_row_count=min(rows, 1000),
            status="COMPLETED",
            created_at=NOW - timedelta(days=len(row_counts) - index),
        )
        session.add(profile)
        await session.flush()
        for column, nulls in columns:
            session.add(
                ColumnProfile(
                    organization_id=org.id,
                    table_profile_id=profile.id,
                    column_id=column.id,
                    null_count=nulls[index],
                    non_null_count=rows - nulls[index],
                    approximate_distinct_count=1,
                )
            )
    await session.flush()


async def _estate(
    session: AsyncSession,
) -> tuple[Organization, DataSource, MetadataSchema, MetadataTable, dict[str, MetadataColumn]]:
    """`orders`, profiled three times at 1,000-1,200 rows: `id` never null,
    `status` at most 1% null, `notes` often empty."""
    org, datasource, schema = await seed_estate(session)
    orders = await seed_table(session, org, datasource, schema, name="orders")
    columns = {
        name: await _column(session, org, orders, name=name, position=position)
        for position, name in enumerate(("id", "status", "notes"), start=1)
    }
    await _profiles(
        session,
        org,
        datasource,
        orders,
        row_counts=[1000, 1200, 1100],
        columns=[
            (columns["id"], [0, 0, 0]),
            (columns["status"], [10, 0, 5]),
            (columns["notes"], [400, 600, 550]),
        ],
    )
    return org, datasource, schema, orders, columns


async def _run(
    session: AsyncSession, org: Organization, *, settings: Any = None, **request: Any
) -> TaskAgentOutcome:
    return await run_quality_agent(
        session,
        org.id,
        request=TaskAgentRunRequest(**request),
        settings=settings or agent_settings(),
        triggered_by=human(org),
    )


# ---------------------------------------------------------------------------
# What it derives
# ---------------------------------------------------------------------------


def test_a_floor_is_half_the_smallest_recent_row_count_and_needs_history() -> None:
    floor = derive_row_count_floor(profiles_used=3, smallest=1000, largest=1200)
    full = derive_row_count_floor(profiles_used=5, smallest=1001, largest=1001)

    assert floor is not None and (floor.threshold, floor.confidence) == (500.0, 0.6)
    assert full is not None and (full.threshold, full.confidence) == (500.0, 1.0)
    assert derive_row_count_floor(profiles_used=2, smallest=1000, largest=1000) is None
    assert derive_row_count_floor(profiles_used=5, smallest=99, largest=5000) is None


def test_a_ceiling_sits_just_above_a_normally_complete_columns_worst_null_rate() -> None:
    ceiling = derive_null_rate_ceiling(profiles_used=3, worst_null_rate=0.01)
    never_null = derive_null_rate_ceiling(profiles_used=4, worst_null_rate=0.0)

    assert ceiling is not None and ceiling.threshold == 0.03
    assert never_null is not None and never_null.threshold == 0.02
    assert derive_null_rate_ceiling(profiles_used=3, worst_null_rate=0.06) is None
    assert derive_null_rate_ceiling(profiles_used=2, worst_null_rate=0.0) is None


# ---------------------------------------------------------------------------
# What it proposes
# ---------------------------------------------------------------------------


async def test_a_profiled_table_gets_a_floor_and_its_complete_columns_ceilings(
    session: AsyncSession,
) -> None:
    org, _datasource, _schema, _orders, columns = await _estate(session)
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    assert {(item.capability, item.subject_name, item.action) for item in outcome.items} == {
        (CAPABILITY_ROW_COUNT_FLOOR, "public.orders", ACTION_PROPOSED),
        (CAPABILITY_NULL_RATE_CEILING, "public.orders.id", ACTION_PROPOSED),
        (CAPABILITY_NULL_RATE_CEILING, "public.orders.status", ACTION_PROPOSED),
    }
    proposals = {
        (proposal.rule_type, proposal.column_id): proposal
        for proposal in (await session.scalars(select(QualityRuleProposal))).all()
    }
    assert proposals[("TABLE_ROW_COUNT_MIN", None)].threshold == 500.0
    assert proposals[("COLUMN_NULL_RATE_MAX", columns["id"].id)].threshold == 0.02
    assert proposals[("COLUMN_NULL_RATE_MAX", columns["status"].id)].threshold == 0.03
    assert {p.status for p in proposals.values()} == {"PENDING_APPROVAL"}
    assert {p.created_by for p in proposals.values()} == {AGENT}
    reviews = (await session.scalars(select(GovernanceReview))).all()
    assert {(r.object_type, r.requested_by, r.requested_action) for r in reviews} == {
        ("QUALITY_RULE_PROPOSAL", AGENT, "APPROVE_QUALITY_RULE")
    }
    assert {p.governance_review_id for p in proposals.values()} == {r.id for r in reviews}
    assert await count_rows(session, QualityRule) == 0, "a proposal is not a rule"
    task = await session.get(AgentTask, outcome.items[0].task_id)
    assert task is not None
    assert set(task.evidence) == {"run_id", "review_id", "object_type", "object_id", "confidence"}


async def test_too_little_history_is_not_a_candidate(session: AsyncSession) -> None:
    org, datasource, schema = await seed_estate(session)
    young = await seed_table(session, org, datasource, schema, name="young")
    await _profiles(session, org, datasource, young, row_counts=[5000, 5000])
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    assert outcome.items == []


async def test_a_covered_rule_key_is_never_proposed(session: AsyncSession) -> None:
    """A person's rule -- even disabled -- and a rejected proposal are both
    answers already given."""
    org, datasource, _schema, orders, columns = await _estate(session)
    pack = QualityRulePack(
        organization_id=org.id, datasource_id=datasource.id, name="Steward rules", created_by="s"
    )
    session.add(pack)
    await session.flush()
    session.add(
        QualityRule(
            organization_id=org.id,
            rule_pack_id=pack.id,
            table_id=orders.id,
            name="floor",
            rule_type="TABLE_ROW_COUNT_MIN",
            threshold=10,
            enabled=False,
            created_by="s",
        )
    )
    session.add(
        QualityRuleProposal(
            organization_id=org.id,
            datasource_id=datasource.id,
            table_id=orders.id,
            column_id=columns["id"].id,
            rule_type="COLUMN_NULL_RATE_MAX",
            threshold=0.02,
            name="Null-rate ceiling on public.orders.id",
            confidence=0.6,
            evidence={},
            status="REJECTED",
            created_by=AGENT,
        )
    )
    await session.flush()
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    assert [(item.capability, item.subject_name) for item in outcome.items] == [
        (CAPABILITY_NULL_RATE_CEILING, "public.orders.status")
    ]


async def test_a_t0_contract_observes_and_writes_nothing(session: AsyncSession) -> None:
    org, *_ = await _estate(session)
    await register_agent(session, org, principal=AGENT, tier="T0")

    outcome = await _run(session, org)

    assert [item.action for item in outcome.items] == [ACTION_WOULD_PROPOSE] * 3
    assert await count_rows(session, QualityRuleProposal) == 0
    assert await count_rows(session, GovernanceReview) == 0
    assert await count_rows(session, AgentTask) == 0


# ---------------------------------------------------------------------------
# Who decides
# ---------------------------------------------------------------------------


async def test_a_person_approves_and_the_rule_runs_the_agent_cannot(
    session: AsyncSession,
) -> None:
    org, datasource, *_ = await _estate(session)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org, capabilities=(CAPABILITY_ROW_COUNT_FLOOR,))
    review = (await session.scalars(select(GovernanceReview))).one()
    agent_context = SecurityContext(
        principal_id=AGENT,
        principal_type="AGENT",
        organization_id=org.id,
        roles=frozenset({"Reviewer"}),
    )

    with pytest.raises(GovernanceDecisionRefused):
        await decide_review(
            session, review, decision="APPROVE", reason="self", context=agent_context, now=NOW
        )
    await decide_review(
        session, review, decision="APPROVE", reason="sensible floor", context=human(org), now=NOW
    )

    proposal = (await session.scalars(select(QualityRuleProposal))).one()
    rule = await session.get(QualityRule, proposal.applied_rule_id)
    assert rule is not None
    pack = await session.get(QualityRulePack, rule.rule_pack_id)
    assert pack is not None
    assert (pack.name, pack.datasource_id, pack.enabled) == (
        AGENT_RULE_PACK_NAME,
        datasource.id,
        True,
    )
    assert (rule.rule_type, rule.threshold, rule.enabled, rule.created_by) == (
        "TABLE_ROW_COUNT_MIN",
        500.0,
        True,
        "steward-1",
    )
    assert (proposal.status, proposal.reviewed_by) == ("APPROVED", "steward-1")
    latest = (
        await session.scalars(select(TableProfile).order_by(TableProfile.created_at.desc()))
    ).first()
    assert evaluate_rule(rule, profile=latest, column_profile=None).passed is True


async def test_a_rejected_rule_is_never_proposed_again(session: AsyncSession) -> None:
    org, *_ = await _estate(session)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org, capabilities=(CAPABILITY_ROW_COUNT_FLOOR,))
    review = (await session.scalars(select(GovernanceReview))).one()
    await decide_review(
        session,
        review,
        decision="REJECT",
        reason="this table is truncated nightly",
        context=human(org),
        now=NOW,
    )

    again = await _run(session, org, capabilities=(CAPABILITY_ROW_COUNT_FLOOR,))

    assert again.items == []
    proposal = (await session.scalars(select(QualityRuleProposal))).one()
    assert proposal.status == "REJECTED"
    assert await count_rows(session, QualityRule) == 0


def test_no_agent_can_decide_a_quality_rule_proposal() -> None:
    assert risk_tier_for("QUALITY_RULE_PROPOSAL") == TIER_T2
    for ceiling in ("T0", "T1", "T2", "T3"):
        assert "QUALITY_RULE_PROPOSAL" not in agent_decidable_object_types(ceiling)
    assert QUALITY_AGENT.max_proposal_tier == TIER_T2 == HARD_MAX_PROPOSAL_TIER


# ---------------------------------------------------------------------------
# The runtime's proposal ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ceiling", [TIER_T3, "T9", ""])
def test_no_spec_may_propose_above_t2(ceiling: str) -> None:
    with pytest.raises(ValueError):
        TaskAgentSpec(
            key="quality", audit_roles=frozenset(), capabilities=(), max_proposal_tier=ceiling
        )


async def test_an_agent_at_the_default_ceiling_is_refused_a_t2_proposal(
    session: AsyncSession,
) -> None:
    """The same work under a spec that did not declare T2: refused at the first
    proposal, and a refusal ends the run."""
    org, *_ = await _estate(session)
    await register_agent(session, org, principal=AGENT)
    t1_spec = TaskAgentSpec(
        key="quality",
        audit_roles=frozenset({"DataSteward"}),
        capabilities=QUALITY_AGENT.capabilities,
    )

    with pytest.raises(TaskAgentRefused) as excinfo:
        await run_task_agent(
            session,
            org.id,
            spec=t1_spec,
            work=QUALITY_WORK,
            request=TaskAgentRunRequest(),
            settings=agent_settings(),
            triggered_by=human(org),
        )

    assert excinfo.value.reason_code == REASON_OBJECT_TYPE_ABOVE_CEILING


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------


async def test_the_state_endpoint_reports_both_capabilities_at_t2(session: AsyncSession) -> None:
    org, *_ = await _estate(session)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org)

    state = await get_quality_agent_state(
        org.id, context=human(org), session=session, settings=agent_settings()
    )

    assert (state.agent_key, state.registered, state.pending_proposals) == ("quality", True, 3)
    assert {(c.capability, c.review_queue, c.risk_tier) for c in state.capabilities} == {
        (CAPABILITY_ROW_COUNT_FLOOR, "GOVERNANCE_REVIEW", "T2"),
        (CAPABILITY_NULL_RATE_CEILING, "GOVERNANCE_REVIEW", "T2"),
    }
    [outcome] = state.outcomes
    assert (outcome.object_type, outcome.pending, outcome.acceptance_rate) == (
        "QUALITY_RULE_PROPOSAL",
        3,
        None,
    )


async def test_the_review_queue_shows_the_proposed_rule_first(session: AsyncSession) -> None:
    org, *_ = await _estate(session)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org, capabilities=(CAPABILITY_ROW_COUNT_FLOOR,))
    review = (await session.scalars(select(GovernanceReview))).one()

    [row] = await compose_review_queue(session, [review])

    assert row.confidence == 0.6
    assert row.evidence[0].claim == (
        "proposed rule: Row-count floor on public.orders (TABLE_ROW_COUNT_MIN 500)"
    )


def test_the_agent_module_never_creates_or_applies_a_rule() -> None:
    source = (REPO_ROOT / "src" / "aida" / "quality_agent.py").read_text(encoding="utf-8")
    assert "QualityRule(" not in source
    assert "apply_quality_rule_proposal" not in source
    assert "decide_review" not in source
