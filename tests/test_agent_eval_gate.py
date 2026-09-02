"""N15: evaluation-gated agent publication.

Three layers, matching the tracker row's own deliverables:

1. Pure gate-evaluation-logic tests (`evaluate_agent_eval_gate`) -- no
   database: enough passing exemplars is a PASS, too many failures is a FAIL
   naming the failing exemplars, and zero (or too few) exemplars is
   INSUFFICIENT_DATA, never a silent pass.
2. `replay_confirmed_run_corpus`/`stored_steward_verdicts`/
   `record_agent_eval_gate_evidence` against a real in-memory SQLite
   database -- N17's own real confirmed-run promotion machinery, not a
   fake/mocked replay.
3. A real integration proof that an actual `AiAssetVersion` publish attempt
   (`semantic_api.decide_governance_review`, the shared maker-checker
   dispatcher every AI asset version APPROVE decision goes through) is
   blocked or allowed based on real exemplar replay results -- including one
   case that drives a real `GovernedAgentOrchestrator.run()` through N17's
   own `replay_steward_exemplar` for its verdict, the literal
   "based on real exemplar replay results" proof.

Never asserts a final answer or business value anywhere in this file
(INV-6/ADR-0014), matching AT-8/N17's own discipline exactly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.agent_eval_gate import (
    AgentEvalGateEvaluateRequest,
    AgentEvalGateVerdictInput,
    ExemplarVerdict,
    ExemplarVerdictSource,
    compute_agent_eval_gate,
    evaluate_agent_eval_gate,
    record_agent_eval_gate_evidence,
    replay_confirmed_run_corpus,
    stored_steward_verdicts,
)
from aida.ai_registry_api import evaluate_agent_eval_gate_endpoint, get_agent_eval_gate
from aida.db import Base
from aida.exemplar_store import steward_authored_exemplar
from aida.models import AgentRun, AiAsset, AiAssetVersion, GovernanceReview, QueryMemoryEvidence
from aida.schemas import GovernanceDecisionRequest
from aida.semantic_api import decide_governance_review
from tests.context_path_eval.cases import TOOL_MISSING_PARAMETER_REACHES_CLARIFICATION
from tests.context_path_eval.exemplars import replay_steward_exemplar
from tests.context_path_eval.scenario import (
    ORDER_LOOKUP_TOOL_SLUG,
    ContextPathEvalScenario,
    build_scenario,
)
from tests.support.doubles import security_context

# ---------------------------------------------------------------------------
# 1. Pure gate-evaluation-logic tests -- no database
# ---------------------------------------------------------------------------


def _verdict(
    case_id: str, matched: bool, source: ExemplarVerdictSource = "CONFIRMED_RUN"
) -> ExemplarVerdict:
    return ExemplarVerdict(
        case_id=case_id, source=source, matched=matched, drift=() if matched else ("mismatch",)
    )


def test_zero_exemplars_is_insufficient_data_never_a_silent_pass() -> None:
    result = evaluate_agent_eval_gate([], threshold=0.8)

    assert result.verdict == "INSUFFICIENT_DATA"
    assert result.pass_rate is None
    assert result.total_exemplars == 0
    assert "0 exemplar" in result.reason


def test_below_minimum_exemplars_is_insufficient_data_even_with_a_perfect_pass_rate() -> None:
    # A single passing exemplar would clear the *rate* threshold trivially --
    # `minimum_exemplars` exists precisely so a rate computed from too little
    # evidence is never mistaken for a real pass.
    result = evaluate_agent_eval_gate(
        [_verdict("only-one", matched=True)], threshold=0.8, minimum_exemplars=3
    )

    assert result.verdict == "INSUFFICIENT_DATA"
    assert result.pass_rate is None


def test_enough_passing_exemplars_is_pass() -> None:
    verdicts = [_verdict(f"case-{i}", matched=True) for i in range(9)] + [
        _verdict("case-9", matched=False)
    ]

    result = evaluate_agent_eval_gate(verdicts, threshold=0.8)

    assert result.verdict == "PASS"
    assert result.pass_rate == pytest.approx(0.9)
    assert result.passed_exemplars == 9
    assert result.total_exemplars == 10
    # Every contributing verdict stays visible, not collapsed to a boolean.
    assert len(result.verdicts) == 10


def test_too_many_failures_is_fail_naming_the_failing_exemplars() -> None:
    verdicts = [_verdict("good-1", matched=True), _verdict("good-2", matched=True)] + [
        _verdict(f"bad-{i}", matched=False) for i in range(3)
    ]

    result = evaluate_agent_eval_gate(verdicts, threshold=0.8)

    assert result.verdict == "FAIL"
    assert result.pass_rate == pytest.approx(0.4)
    assert set(result.failing_case_ids) == {"bad-0", "bad-1", "bad-2"}
    for case_id in result.failing_case_ids:
        assert case_id in result.reason


def test_pass_rate_exactly_at_threshold_passes() -> None:
    verdicts = [_verdict(f"case-{i}", matched=True) for i in range(4)] + [
        _verdict("case-4", matched=False)
    ]

    result = evaluate_agent_eval_gate(verdicts, threshold=0.8)

    assert result.pass_rate == pytest.approx(0.8)
    assert result.verdict == "PASS"


def test_invalid_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="threshold"):
        evaluate_agent_eval_gate([_verdict("a", matched=True)], threshold=1.5)


def test_invalid_minimum_exemplars_is_rejected() -> None:
    with pytest.raises(ValueError, match="minimum_exemplars"):
        evaluate_agent_eval_gate([_verdict("a", matched=True)], threshold=0.8, minimum_exemplars=0)


def test_result_is_deterministic_for_the_same_input() -> None:
    verdicts = [_verdict("a", matched=True), _verdict("b", matched=False)]
    moment = datetime(2026, 9, 2, tzinfo=UTC)

    first = evaluate_agent_eval_gate(verdicts, threshold=0.8, now=moment)
    second = evaluate_agent_eval_gate(verdicts, threshold=0.8, now=moment)

    assert first == second


# ---------------------------------------------------------------------------
# 2. Real confirmed-run corpus replay -- N17's own machinery, real database
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
    scenario: ContextPathEvalScenario,
    *,
    memory_status: str = "ELIGIBLE",
) -> tuple[AgentRun, QueryMemoryEvidence]:
    """Mirrors `tests/test_exemplar_store.py`'s own `_seed_confirmed_run` --
    a real `AgentRun` + `QueryMemoryEvidence` pair playing the role a live
    orchestrator run plus a real feedback call would otherwise produce.
    """
    tool_version = scenario.tool_version_by_slug[ORDER_LOOKUP_TOOL_SLUG]
    agent_run = AgentRun(
        organization_id=scenario.organization.id,
        datasource_id=scenario.datasource.id,
        principal_id="analyst@bank.com",
        status="COMPLETED",
        question_hash="q" * 64,
        generation_source="TOOL",
        semantic_version=f"technical-metadata:{scenario.datasource.id}",
        plan_evidence={
            "strategy": "TOOL_EXECUTION",
            "selected_tool_version_id": str(tool_version.id),
            "tool_decisions": [{"tool_version_id": str(tool_version.id), "decision": "SELECTED"}],
        },
        retrieval_evidence=[
            {"object_type": "TABLE", "object_id": str(scenario.fact_orders.id)},
        ],
        recommended_tool_version_id=tool_version.id,
        failure_reason=None,
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


async def _create_agent_asset_version(
    db: AsyncSession, organization_id: UUID, *, requested_by: str = "agent-dev"
) -> AiAssetVersion:
    asset = AiAsset(
        organization_id=organization_id,
        asset_key=f"triage-agent-{uuid4().hex[:8]}",
        asset_kind="AGENT",
        created_by=requested_by,
    )
    db.add(asset)
    await db.flush()
    version = AiAssetVersion(
        organization_id=organization_id,
        asset_id=asset.id,
        version=1,
        status="REVIEW_REQUIRED",
        name="Triage Agent",
        description="Summarizes suspicious transactions for human review.",
        intended_use="Assist fraud analysts; never auto-decides.",
        owner_principal=requested_by,
        provider_type="INTERNAL",
        risk_tier="LOW",
        documentation_url=None,
        fingerprint="fp-triage-agent-v1",
        created_by=requested_by,
    )
    db.add(version)
    await db.flush()
    return version


async def _open_review(
    db: AsyncSession, version: AiAssetVersion, *, requested_by: str
) -> GovernanceReview:
    review = GovernanceReview(
        organization_id=version.organization_id,
        object_type="AI_ASSET_VERSION",
        object_id=str(version.id),
        requested_action="APPROVE",
        requested_by=requested_by,
    )
    db.add(review)
    await db.flush()
    return review


async def test_replay_confirmed_run_corpus_reflects_only_currently_confirmed_runs(
    db: AsyncSession,
) -> None:
    scenario = await build_scenario(db)
    confirmed, _ = await _seed_confirmed_run(db, scenario)
    await _seed_confirmed_run(db, scenario, memory_status="OBSERVED")  # not confirmed

    verdicts = await replay_confirmed_run_corpus(db, scenario.organization.id)

    assert len(verdicts) == 1
    assert verdicts[0].case_id == f"confirmed-run-{confirmed.id}"
    assert verdicts[0].source == "CONFIRMED_RUN"
    assert verdicts[0].matched is True


async def test_record_agent_eval_gate_evidence_persists_into_evaluation_evidence(
    db: AsyncSession,
) -> None:
    scenario = await build_scenario(db)
    version = await _create_agent_asset_version(db, scenario.organization.id)
    context = security_context(
        organization_id=scenario.organization.id, roles=frozenset({"AgentDeveloper"})
    )

    result = await compute_agent_eval_gate(db, organization_id=scenario.organization.id)
    record_agent_eval_gate_evidence(db, version, result, context=context, stage="PRE_PUBLISH_CHECK")
    await db.flush()

    assert version.evaluation_evidence["agent_eval_gate"]["verdict"] == "INSUFFICIENT_DATA"
    assert version.evaluation_evidence["pass_rate"] == 0.0


async def test_stored_steward_verdicts_round_trips_through_evidence(db: AsyncSession) -> None:
    scenario = await build_scenario(db)
    version = await _create_agent_asset_version(db, scenario.organization.id)
    context = security_context(
        organization_id=scenario.organization.id, roles=frozenset({"AgentDeveloper"})
    )
    steward_verdicts = [
        ExemplarVerdict(case_id="steward-1", source="STEWARD_AUTHORED", matched=True),
    ]
    result = evaluate_agent_eval_gate(steward_verdicts, threshold=0.8)

    record_agent_eval_gate_evidence(
        db,
        version,
        result,
        context=context,
        stage="PRE_PUBLISH_CHECK",
        steward_authored_verdicts=steward_verdicts,
    )

    round_tripped = stored_steward_verdicts(version)
    assert len(round_tripped) == 1
    assert round_tripped[0].case_id == "steward-1"
    assert round_tripped[0].source == "STEWARD_AUTHORED"
    assert round_tripped[0].matched is True


# ---------------------------------------------------------------------------
# 3. Real integration proof: an actual AiAssetVersion publish is blocked or
#    allowed by the real, shared decide_governance_review dispatcher.
# ---------------------------------------------------------------------------


async def test_publish_is_blocked_with_no_confirmed_exemplars(db: AsyncSession) -> None:
    scenario = await build_scenario(db)
    version = await _create_agent_asset_version(db, scenario.organization.id, requested_by="maker")
    review = await _open_review(db, version, requested_by="maker")
    checker = security_context(
        organization_id=scenario.organization.id,
        principal_id="checker",
        roles=frozenset({"Reviewer"}),
    )

    with pytest.raises(HTTPException) as excinfo:
        await decide_governance_review(
            review.id, GovernanceDecisionRequest(decision="APPROVE"), checker, db
        )

    assert excinfo.value.status_code == 409
    assert "INSUFFICIENT_DATA" in str(excinfo.value.detail)
    # The blocked attempt never reaches the code that mutates
    # `AiAssetVersion.status` -- the gate check runs strictly before it.
    # (`GovernanceReview.status` itself is set unconditionally earlier in
    # `_apply_governance_review_decision`, before the per-object-type branch
    # -- an in-session, not-yet-committed mutation on every raised 409 in
    # that function, this row's included; a real request's `get_session`
    # dependency discards it, uncommitted, when the request ends.)
    refreshed = await db.get(AiAssetVersion, version.id)
    assert refreshed is not None
    assert refreshed.status == "REVIEW_REQUIRED"


async def test_publish_is_allowed_when_confirmed_corpus_passes(db: AsyncSession) -> None:
    scenario = await build_scenario(db)
    await _seed_confirmed_run(db, scenario)
    version = await _create_agent_asset_version(db, scenario.organization.id, requested_by="maker")
    review = await _open_review(db, version, requested_by="maker")
    checker = security_context(
        organization_id=scenario.organization.id,
        principal_id="checker",
        roles=frozenset({"Reviewer"}),
    )

    decided = await decide_governance_review(
        review.id, GovernanceDecisionRequest(decision="APPROVE"), checker, db
    )

    assert decided.status == "APPROVED"
    refreshed = await db.get(AiAssetVersion, version.id)
    assert refreshed is not None
    assert refreshed.status == "APPROVED"
    gate_evidence = refreshed.evaluation_evidence["agent_eval_gate"]
    assert gate_evidence["verdict"] == "PASS"
    assert gate_evidence["total_exemplars"] == 1
    assert refreshed.evaluation_evidence["pass_rate"] == pytest.approx(1.0)


async def test_publish_is_blocked_by_a_named_failing_steward_authored_exemplar(
    db: AsyncSession,
) -> None:
    """A steward previously submitted (via the evaluate endpoint) a corpus
    where most exemplars fail -- the real publish decision must fold that
    stored evidence back in and block, citing the failing case by name, even
    though the organization also has one passing confirmed run.
    """
    scenario = await build_scenario(db)
    await _seed_confirmed_run(db, scenario)
    version = await _create_agent_asset_version(db, scenario.organization.id, requested_by="maker")
    author_context = security_context(
        organization_id=scenario.organization.id,
        principal_id="steward",
        roles=frozenset({"AgentDeveloper"}),
    )

    await evaluate_agent_eval_gate_endpoint(
        version.id,
        AgentEvalGateEvaluateRequest(
            steward_authored_verdicts=[
                AgentEvalGateVerdictInput(case_id="steward-pass", matched=True),
                AgentEvalGateVerdictInput(
                    case_id="steward-fail-1", matched=False, drift=["strategy mismatch"]
                ),
                AgentEvalGateVerdictInput(
                    case_id="steward-fail-2", matched=False, drift=["policy_status mismatch"]
                ),
                AgentEvalGateVerdictInput(case_id="steward-fail-3", matched=False),
            ]
        ),
        author_context,
        db,
    )

    review = await _open_review(db, version, requested_by="maker")
    checker = security_context(
        organization_id=scenario.organization.id,
        principal_id="checker",
        roles=frozenset({"Reviewer"}),
    )

    with pytest.raises(HTTPException) as excinfo:
        await decide_governance_review(
            review.id, GovernanceDecisionRequest(decision="APPROVE"), checker, db
        )

    assert excinfo.value.status_code == 409
    assert "FAIL" in str(excinfo.value.detail)
    assert "steward-fail-1" in str(excinfo.value.detail)
    refreshed = await db.get(AiAssetVersion, version.id)
    assert refreshed is not None
    assert refreshed.status == "REVIEW_REQUIRED"


async def test_preview_endpoint_matches_what_the_real_publish_decision_computes(
    db: AsyncSession,
) -> None:
    scenario = await build_scenario(db)
    await _seed_confirmed_run(db, scenario)
    version = await _create_agent_asset_version(db, scenario.organization.id, requested_by="maker")
    reader = security_context(
        organization_id=scenario.organization.id,
        principal_id="steward",
        roles=frozenset({"Viewer"}),
    )

    preview = await get_agent_eval_gate(version.id, reader, db)

    assert preview.verdict == "PASS"
    assert preview.total_exemplars == 1

    review = await _open_review(db, version, requested_by="maker")
    checker = security_context(
        organization_id=scenario.organization.id,
        principal_id="checker",
        roles=frozenset({"Reviewer"}),
    )
    decided = await decide_governance_review(
        review.id, GovernanceDecisionRequest(decision="APPROVE"), checker, db
    )

    assert decided.status == "APPROVED"


async def test_publish_uses_a_real_steward_authored_orchestrator_replay(db: AsyncSession) -> None:
    """The literal "based on real exemplar replay results" proof: a real
    `GovernedAgentOrchestrator.run()` is driven through N17's own
    `replay_steward_exemplar`, and the resulting verdict genuinely
    contributes to whether a real publish is allowed.
    """
    scenario = await build_scenario(db)
    case = TOOL_MISSING_PARAMETER_REACHES_CLARIFICATION
    exemplar = steward_authored_exemplar(
        case_id=case.case_id,
        description=case.description,
        question=case.question,
        roles=case.roles,
        preferred_tool_slug=case.preferred_tool_slug,
        tool_parameters=case.tool_parameters,
        expected_strategy=case.expected_strategy,
        expected_selected_tool_slug=case.expected_selected_tool_slug,
        expected_resolved_object_types=case.expected_resolved_object_types,
        expected_semantic_version_kind=case.expected_semantic_version_kind,
        expected_policy_status=case.expected_policy_status,
        expected_policy_reason_code=case.expected_policy_reason_code,
        expected_prompt_risk_decision=case.expected_prompt_risk_decision,
    )

    eval_result = await replay_steward_exemplar(db, scenario, exemplar)
    assert eval_result.matched is True  # the case's own expectation holds against a live run

    version = await _create_agent_asset_version(db, scenario.organization.id, requested_by="maker")
    author_context = security_context(
        organization_id=scenario.organization.id,
        principal_id="steward",
        roles=frozenset({"AgentDeveloper"}),
    )
    await evaluate_agent_eval_gate_endpoint(
        version.id,
        AgentEvalGateEvaluateRequest(
            steward_authored_verdicts=[
                AgentEvalGateVerdictInput(
                    case_id=eval_result.case_id,
                    matched=eval_result.matched,
                    drift=list(eval_result.drift),
                )
            ]
        ),
        author_context,
        db,
    )
    review = await _open_review(db, version, requested_by="maker")
    checker = security_context(
        organization_id=scenario.organization.id,
        principal_id="checker",
        roles=frozenset({"Reviewer"}),
    )

    decided = await decide_governance_review(
        review.id, GovernanceDecisionRequest(decision="APPROVE"), checker, db
    )

    assert decided.status == "APPROVED"
    refreshed = await db.get(AiAssetVersion, version.id)
    assert refreshed is not None
    verdicts = refreshed.evaluation_evidence["agent_eval_gate"]["verdicts"]
    assert any(
        v["case_id"] == eval_result.case_id and v["source"] == "STEWARD_AUTHORED" for v in verdicts
    )
