"""N17 (scoped half): exemplar promotion + benchmark replay.

Three things are exercised, matching the tracker row's own deliverables:

1. Promotion mechanism (`aida.exemplar_store.promote_confirmed_agent_run`):
   a real confirmed `AgentRun` (`QueryMemoryEvidence.status == "ELIGIBLE"`,
   the codebase's actual human-confirmation signal -- see
   `exemplar_store.py`'s module docstring) promotes deterministically into
   the correct `ExemplarCase`, and refuses anything not actually confirmed.
2. Benchmark replay (`tests/context_path_eval/exemplars.py`): a
   `STEWARD_AUTHORED` exemplar (the one source that carries a real question)
   converts losslessly into AT-8's own `ContextPathEvalCase` and replays
   through AT-8's own `run_eval_case` against a live orchestrator run,
   producing a real pass verdict -- the literal "gate change-set submission
   is achievable" proof. A `CONFIRMED_RUN` exemplar instead replays via
   `compare_exemplar_to_current` / `replay_confirmed_exemplar`, a
   stored-vs-current consistency check.
3. Consumption-based ranking (`rank_candidates_by_consumption`): the
   "BI/query history" signal the scoping doc names alongside confirmed runs.

Never asserts a final answer or business value anywhere in this file, per
INV-6/ADR-0014 -- matching AT-8's own discipline exactly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.consumption_lineage import ConsumptionEdge, record_consumption
from aida.context_path import ContextPath
from aida.db import Base
from aida.exemplar_store import (
    ExemplarCase,
    compare_exemplar_to_current,
    find_confirmed_agent_runs,
    promote_confirmed_agent_run,
    rank_candidates_by_consumption,
    steward_authored_exemplar,
)
from aida.models import AgentRun, QueryMemoryEvidence
from tests.context_path_eval.cases import ORDER_LOOKUP_REQUIRED_PARAMETER, ORDERS_QUESTION
from tests.context_path_eval.exemplars import (
    exemplar_to_eval_case,
    replay_confirmed_exemplar,
    replay_steward_exemplar,
)
from tests.context_path_eval.scenario import (
    ORDER_LOOKUP_TOOL_SLUG,
    ContextPathEvalScenario,
    build_scenario,
)

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_confirmed_run(
    db: AsyncSession,
    scenario: ContextPathEvalScenario | None = None,
    *,
    strategy: str = "CLARIFICATION",
    policy_status: str = "COMPLETED",
    policy_reason_code: str | None = None,
    memory_status: str = "ELIGIBLE",
) -> tuple[AgentRun, QueryMemoryEvidence]:
    """Build a real `AgentRun` + `QueryMemoryEvidence` pair against
    AT-8's own scenario, playing the role a live orchestrator run + a real
    `PUT /agent-runs/{id}/feedback` call would otherwise produce -- AT-8's
    scenario deliberately never reaches COMPLETED (no live SQL warehouse),
    so this builds the *outcome* state directly, the same "construct the DB
    row directly" boundary `tests/test_context_product_negative_knowledge.py`
    already uses for `NegativeAssertionRecord`.

    `scenario`, when given, is reused instead of building a fresh one -- so
    multiple confirmed runs can share one organization/datasource/tool where
    a test needs that (e.g. consumption-ranking across candidates).
    """
    if scenario is None:
        scenario = await build_scenario(db)
    tool_version = scenario.tool_version_by_slug[ORDER_LOOKUP_TOOL_SLUG]
    table_id = str(scenario.fact_orders.id)
    tool_id = str(tool_version.tool_id)
    agent_run = AgentRun(
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        principal_id="analyst@bank.com",
        status=policy_status,
        question_hash="q" * 64,
        generation_source="TOOL",
        semantic_version=f"technical-metadata:{scenario.datasource.id}",
        plan_evidence={
            "strategy": strategy,
            "selected_tool_version_id": str(tool_version.id),
            "tool_decisions": [{"tool_version_id": str(tool_version.id), "decision": "SELECTED"}],
        },
        retrieval_evidence=[
            {"object_type": "TABLE", "object_id": table_id},
            {"object_type": "GOVERNED_TOOL", "object_id": tool_id},
        ],
        recommended_tool_version_id=tool_version.id,
        failure_reason=policy_reason_code,
    )
    db.add(agent_run)
    await db.flush()
    memory = QueryMemoryEvidence(
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        agent_run_id=agent_run.id,
        query_execution_id=uuid4(),
        question_hash=agent_run.question_hash,
        sql_hash="s" * 64,
        semantic_version=agent_run.semantic_version,
        status=memory_status,
        positive_feedback_count=1 if memory_status == "ELIGIBLE" else 0,
    )
    db.add(memory)
    await db.flush()
    return agent_run, memory


# ---------------------------------------------------------------------------
# 1. Promotion mechanism
# ---------------------------------------------------------------------------


async def test_find_confirmed_agent_runs_returns_only_eligible_evidence(
    db: AsyncSession,
) -> None:
    confirmed_run, confirmed_memory = await _seed_confirmed_run(db)
    await _seed_confirmed_run(db, memory_status="OBSERVED")  # no feedback yet -- not confirmed
    await _seed_confirmed_run(db, memory_status="SUPPRESSED")  # negative feedback -- rejected

    candidates = await find_confirmed_agent_runs(db, confirmed_run.organization_id)

    assert [agent_run.id for agent_run, _ in candidates] == [confirmed_run.id]
    assert candidates[0][1].id == confirmed_memory.id


async def test_promote_confirmed_agent_run_produces_the_correct_exemplar(
    db: AsyncSession,
) -> None:
    agent_run, memory = await _seed_confirmed_run(
        db,
        strategy="CLARIFICATION",
        policy_reason_code=f"MISSING_TOOL_PARAMETERS:{ORDER_LOOKUP_REQUIRED_PARAMETER}",
    )

    exemplar = await promote_confirmed_agent_run(db, agent_run, memory)

    assert exemplar.case_id == f"confirmed-run-{agent_run.id}"
    assert exemplar.source == "CONFIRMED_RUN"
    # Never a live-replayable question -- AgentRun does not persist one.
    assert exemplar.question is None
    assert exemplar.is_replayable is False
    assert exemplar.question_hash == agent_run.question_hash
    assert exemplar.promoted_from_agent_run_id == str(agent_run.id)
    # The context-path facts, resolved correctly from real seeded state.
    assert exemplar.expected_strategy == "CLARIFICATION"
    assert exemplar.expected_selected_tool_slug == ORDER_LOOKUP_TOOL_SLUG
    assert exemplar.expected_resolved_object_types == frozenset({"TABLE", "GOVERNED_TOOL"})
    assert exemplar.expected_semantic_version_kind == "technical-metadata"
    assert exemplar.expected_policy_status == "COMPLETED"
    assert (
        exemplar.expected_policy_reason_code
        == f"MISSING_TOOL_PARAMETERS:{ORDER_LOOKUP_REQUIRED_PARAMETER}"
    )


async def test_promotion_is_deterministic(db: AsyncSession) -> None:
    agent_run, memory = await _seed_confirmed_run(db)

    first = await promote_confirmed_agent_run(db, agent_run, memory)
    second = await promote_confirmed_agent_run(db, agent_run, memory)

    assert first.artifact_hash == second.artifact_hash
    assert first == second


async def test_promotion_refuses_a_non_confirmed_run(db: AsyncSession) -> None:
    agent_run, memory = await _seed_confirmed_run(db, memory_status="OBSERVED")

    with pytest.raises(ValueError, match="not confirmed"):
        await promote_confirmed_agent_run(db, agent_run, memory)


async def test_promotion_refuses_mismatched_evidence(db: AsyncSession) -> None:
    agent_run, _ = await _seed_confirmed_run(db)
    _other_run, other_memory = await _seed_confirmed_run(db)

    with pytest.raises(ValueError, match="does not belong"):
        await promote_confirmed_agent_run(db, agent_run, other_memory)


# ---------------------------------------------------------------------------
# 2. Benchmark replay
# ---------------------------------------------------------------------------


def _steward_exemplar() -> ExemplarCase:
    return steward_authored_exemplar(
        case_id="steward-orders-clarification",
        description="Steward-authored: an Analyst asks about orders without a customer id.",
        question=ORDERS_QUESTION,
        roles=frozenset({"Analyst"}),
        preferred_tool_slug=ORDER_LOOKUP_TOOL_SLUG,
        tool_parameters={},
        expected_strategy="CLARIFICATION",
        expected_selected_tool_slug=ORDER_LOOKUP_TOOL_SLUG,
        expected_resolved_object_types=frozenset({"BUSINESS_ANNOTATION", "GOVERNED_TOOL", "TABLE"}),
        expected_semantic_version_kind="technical-metadata",
        expected_policy_status="REJECTED",
        expected_policy_reason_code=f"MISSING_TOOL_PARAMETERS:{ORDER_LOOKUP_REQUIRED_PARAMETER}",
    )


def test_steward_authored_exemplar_is_replayable_and_deterministic() -> None:
    first = _steward_exemplar()
    second = _steward_exemplar()

    assert first.is_replayable is True
    assert first.source == "STEWARD_AUTHORED"
    assert first.artifact_hash == second.artifact_hash


def test_exemplar_to_eval_case_refuses_a_non_replayable_exemplar() -> None:
    non_replayable = steward_authored_exemplar(
        case_id="x",
        description="d",
        question="never actually used",
        roles=frozenset(),
        preferred_tool_slug=None,
        expected_strategy="BLOCKED",
        expected_selected_tool_slug=None,
        expected_resolved_object_types=frozenset(),
        expected_semantic_version_kind="",
        expected_policy_status="REJECTED",
        expected_policy_reason_code=None,
    )
    # Simulate a CONFIRMED_RUN-shaped exemplar (question=None) via dataclasses.replace
    # to prove the guard fires on the field that actually matters (`question`),
    # not on `source`.
    questionless = replace(non_replayable, question=None)

    with pytest.raises(ValueError, match="carries no question"):
        exemplar_to_eval_case(questionless)


async def test_steward_authored_exemplar_replays_through_at8_runner(db: AsyncSession) -> None:
    """The literal benchmark-suite proof: a promoted (steward-authored)
    exemplar converts into AT-8's own case format and drives a real
    orchestrator run through AT-8's own `run_eval_case`, reaching a real
    pass verdict -- never a business-value assertion.
    """
    scenario = await build_scenario(db)
    exemplar = _steward_exemplar()

    result = await replay_steward_exemplar(db, scenario, exemplar)

    assert result.matched, result.drift
    assert result.actual.strategy == "CLARIFICATION"


async def test_confirmed_run_exemplar_replays_as_stored_vs_current_consistency(
    db: AsyncSession,
) -> None:
    """The `CONFIRMED_RUN` counterpart: no live re-run (no question to drive
    one with) -- instead the frozen exemplar snapshot is diffed against a
    fresh `derive_context_path` read of the same immutable `AgentRun` row.
    """
    agent_run, memory = await _seed_confirmed_run(db)
    exemplar = await promote_confirmed_agent_run(db, agent_run, memory)

    result = replay_confirmed_exemplar(exemplar, agent_run)

    assert result.matched
    assert result.drift == ()


def test_compare_exemplar_to_current_reports_drift_on_mismatch() -> None:
    """Pure unit test of the comparator itself (no DB): a differing context
    path is reported as named field-level drift, matching
    `runner.compare_to_expected`'s own shape -- proof the comparator is not
    a rubber stamp.
    """
    baseline = steward_authored_exemplar(
        case_id="c",
        description="d",
        question="q",
        roles=frozenset(),
        preferred_tool_slug=None,
        expected_strategy="CLARIFICATION",
        expected_selected_tool_slug=None,
        expected_resolved_object_types=frozenset({"TABLE"}),
        expected_semantic_version_kind="technical-metadata",
        expected_policy_status="REJECTED",
        expected_policy_reason_code="MISSING_TOOL_PARAMETERS:customer_id",
    )
    changed_run = AgentRun(
        organization_id=uuid4(),
        datasource_id=uuid4(),
        principal_id="p",
        status="COMPLETED",
        question_hash="h" * 64,
        generation_source="MODEL",
        semantic_version=None,
        plan_evidence={"strategy": "MODEL_GENERATION"},
        retrieval_evidence=[],
        failure_reason="MODEL_ROUTE_NOT_CONFIGURED",
    )

    drift = compare_exemplar_to_current(baseline, changed_run)

    drift_fields = {entry.split(":", 1)[0] for entry in drift}
    assert {
        "strategy",
        "resolved_object_types",
        "policy_status",
        "policy_reason_code",
    } <= drift_fields


# ---------------------------------------------------------------------------
# 3. Consumption-based ranking (the "BI/query history" signal)
# ---------------------------------------------------------------------------


async def test_rank_candidates_by_consumption_prioritizes_heavier_reuse(
    db: AsyncSession,
) -> None:
    # Both candidates must share one organization -- `rank_candidates_by_consumption`
    # is scoped by organization_id, matching every other scoped read in this
    # codebase (`query_negatives_for_scope`, `_load_exemplars`).
    scenario = await build_scenario(db)
    heavily_used, heavily_used_memory = await _seed_confirmed_run(db, scenario)
    rarely_used, rarely_used_memory = await _seed_confirmed_run(db, scenario)

    # Both runs recommend the same tool_version in this fixture's scenario
    # (order_lookup); to prove ranking actually discriminates, attach the
    # consumption evidence to only one run's underlying tool version by
    # overriding its `recommended_tool_version_id` to a distinct value.
    heavily_used.recommended_tool_version_id = uuid4()
    rarely_used.recommended_tool_version_id = uuid4()
    await db.flush()

    for _ in range(5):
        await record_consumption(
            db,
            organization_id=heavily_used.organization_id,
            edge=ConsumptionEdge(
                consumer_id="mcp-consumer",
                consumer_type="AGENT",
                resource_type="governed_tool_version",
                resource_id=str(heavily_used.recommended_tool_version_id),
                channel="MCP",
                correlation_id=str(uuid4()),
                policy_decision="ALLOW",
            ),
        )
    await record_consumption(
        db,
        organization_id=rarely_used.organization_id,
        edge=ConsumptionEdge(
            consumer_id="mcp-consumer",
            consumer_type="AGENT",
            resource_type="governed_tool_version",
            resource_id=str(rarely_used.recommended_tool_version_id),
            channel="MCP",
            correlation_id=str(uuid4()),
            policy_decision="ALLOW",
        ),
    )
    await db.flush()

    ranked = await rank_candidates_by_consumption(
        db,
        heavily_used.organization_id,
        [(heavily_used, heavily_used_memory), (rarely_used, rarely_used_memory)],
    )

    assert [agent_run.id for agent_run, _, _ in ranked] == [heavily_used.id, rarely_used.id]
    assert ranked[0][2] == 5
    assert ranked[1][2] == 1


def test_context_path_import_stays_stable_after_at8_relocation() -> None:
    """Regression guard for the AT-8 -> N17 relocation: `runner.py` still
    exposes `ContextPath`/`derive_context_path` at the same names (now
    re-exported from `aida.context_path`), so nothing importing them breaks.
    """
    from tests.context_path_eval.runner import ContextPath as RunnerContextPath
    from tests.context_path_eval.runner import derive_context_path as runner_derive

    assert RunnerContextPath is ContextPath
    from aida.context_path import derive_context_path as canonical_derive

    assert runner_derive is canonical_derive
