"""The replay mechanism: derive a context path from a persisted `AgentRun`
and compare it against a stored eval case's expectation.

`derive_context_path` is a pure function over `AgentRun` state that already
exists on the model today (`plan_evidence`, `retrieval_evidence`,
`semantic_version`, `status`, `failure_reason`) -- the same "read back what
was recorded, never recompute a live equivalent" idiom AT-16's
`answer_provenance.py` and AT-6's `agent_run_replay.py` both use. It never
touches `AgentRun.grounding_fragment_digests`' resolved content or any
result-set value: only object identifiers, a version string, a plan
strategy, and a policy outcome -- the context path, per INV-6/ADR-0014.

`run_eval_case` is "replayable" in the concrete sense the tracker row asks
for: it drives a real `GovernedAgentOrchestrator.run()` against the
currently-seeded state and re-derives the context path from scratch every
time it is called. Calling it twice against the same scenario (see
`test_at8_context_path_eval.py::test_replaying_the_same_case_reaches_the_same_context_path`)
reaches the identical context path both times; calling it after the
governed state changes (`test_replay_reports_structural_drift_...`) reaches
a *different* one, reported as a named field-level drift rather than a
silent pass or a hard crash -- informational, not necessarily a failure,
since context paths legitimately evolve as governed content changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_orchestrator import (
    AgentClarificationRequired,
    AgentPolicyRejected,
    GovernedAgentOrchestrator,
    ModelRouteUnavailable,
)
from aida.config import Settings
from aida.models import AgentRun
from tests.context_path_eval.cases import ContextPathEvalCase
from tests.context_path_eval.scenario import ContextPathEvalScenario

#: Every terminal exception `GovernedAgentOrchestrator.run` raises once it has
#: already persisted the `AgentRun` this module reads back -- see
#: `agent_orchestrator.py`'s module docstring / `_persist_rejection`. Every
#: eval case in `cases.py` is deliberately shaped to land on one of these
#: (never a live SQL warehouse or model call), so any other exception here is
#: a real bug in the case or the scenario, not an expected outcome.
_EXPECTED_TERMINAL_EXCEPTIONS: tuple[type[Exception], ...] = (
    AgentClarificationRequired,
    AgentPolicyRejected,
    ModelRouteUnavailable,
)


@dataclass(frozen=True, slots=True)
class ContextPath:
    """The structural facts this eval suite asserts on -- object/version
    identity, plan selection, policy outcome. Deliberately excludes anything
    that looks like a business value or a final answer (INV-6/ADR-0014):
    no result rows, no generated SQL text, no model output content.
    """

    strategy: str | None
    selected_tool_version_id: str | None
    tool_decisions: tuple[tuple[str, str], ...]  # (tool_version_id, decision)
    resolved_object_types: frozenset[str]
    resolved_objects: frozenset[tuple[str, str]]  # (object_type, object_id)
    semantic_version: str | None
    semantic_version_kind: str
    policy_status: str
    policy_reason_code: str | None
    prompt_risk_decision: str | None


def derive_context_path(agent_run: AgentRun) -> ContextPath:
    plan_evidence: dict[str, Any] = agent_run.plan_evidence or {}
    resolved_objects = frozenset(
        (str(entry.get("object_type", "")), str(entry.get("object_id", "")))
        for entry in agent_run.retrieval_evidence
    )
    semantic_version = agent_run.semantic_version
    prompt_risk = plan_evidence.get("prompt_risk") or {}
    return ContextPath(
        strategy=plan_evidence.get("strategy"),
        selected_tool_version_id=plan_evidence.get("selected_tool_version_id"),
        tool_decisions=tuple(
            sorted(
                (str(d.get("tool_version_id", "")), str(d.get("decision", "")))
                for d in plan_evidence.get("tool_decisions") or []
            )
        ),
        resolved_object_types=frozenset(object_type for object_type, _ in resolved_objects),
        resolved_objects=resolved_objects,
        semantic_version=semantic_version,
        semantic_version_kind=semantic_version.split(":", 1)[0] if semantic_version else "",
        policy_status=agent_run.status,
        policy_reason_code=agent_run.failure_reason,
        prompt_risk_decision=prompt_risk.get("decision"),
    )


def compare_to_expected(
    actual: ContextPath, case: ContextPathEvalCase, scenario: ContextPathEvalScenario
) -> list[str]:
    """Field-by-field structural comparison. Returns one human-readable drift
    description per mismatched field, empty when the context path matches
    the case's expectation exactly.
    """
    expected_selected_tool_version_id = (
        str(scenario.tool_version_id(case.expected_selected_tool_slug))
        if case.expected_selected_tool_slug
        else None
    )
    drift: list[str] = []
    if actual.strategy != case.expected_strategy:
        drift.append(f"strategy: expected {case.expected_strategy!r}, got {actual.strategy!r}")
    if actual.selected_tool_version_id != expected_selected_tool_version_id:
        drift.append(
            "selected_tool_version_id: expected "
            f"{expected_selected_tool_version_id!r} (slug {case.expected_selected_tool_slug!r}), "
            f"got {actual.selected_tool_version_id!r}"
        )
    if actual.resolved_object_types != case.expected_resolved_object_types:
        drift.append(
            "resolved_object_types: expected "
            f"{sorted(case.expected_resolved_object_types)}, "
            f"got {sorted(actual.resolved_object_types)}"
        )
    if actual.semantic_version_kind != case.expected_semantic_version_kind:
        drift.append(
            "semantic_version_kind: expected "
            f"{case.expected_semantic_version_kind!r}, got {actual.semantic_version_kind!r}"
        )
    if actual.policy_status != case.expected_policy_status:
        drift.append(
            f"policy_status: expected {case.expected_policy_status!r}, got "
            f"{actual.policy_status!r}"
        )
    if actual.policy_reason_code != case.expected_policy_reason_code:
        drift.append(
            "policy_reason_code: expected "
            f"{case.expected_policy_reason_code!r}, got {actual.policy_reason_code!r}"
        )
    if actual.prompt_risk_decision != case.expected_prompt_risk_decision:
        drift.append(
            "prompt_risk_decision: expected "
            f"{case.expected_prompt_risk_decision!r}, got {actual.prompt_risk_decision!r}"
        )
    return drift


@dataclass(frozen=True, slots=True)
class ContextPathEvalResult:
    case_id: str
    matched: bool
    drift: tuple[str, ...]
    actual: ContextPath


async def run_eval_case(
    db: AsyncSession, scenario: ContextPathEvalScenario, case: ContextPathEvalCase
) -> ContextPathEvalResult:
    """Drive one real orchestrator run for `case` against `scenario`'s
    current (live, possibly-since-mutated) state, derive its context path,
    and compare it against the case's stored expectation.
    """
    settings = Settings(_env_file=None)
    orchestrator = GovernedAgentOrchestrator(settings)
    context = scenario.context(case.roles)
    preferred_tool_version_id = (
        scenario.tool_version_id(case.preferred_tool_slug) if case.preferred_tool_slug else None
    )

    existing_run_ids = frozenset(
        (
            await db.execute(
                select(AgentRun.id).where(AgentRun.datasource_id == scenario.datasource.id)
            )
        )
        .scalars()
        .all()
    )
    try:
        await orchestrator.run(
            db,
            datasource=scenario.datasource,
            context=context,
            correlation_id=f"corr-{case.case_id}",
            question=case.question,
            candidate_sql=None,
            preferred_tool_version_id=preferred_tool_version_id,
            tool_parameters=dict(case.tool_parameters),
            requested_limit=None,
        )
    except _EXPECTED_TERMINAL_EXCEPTIONS:
        pass

    all_runs_query = select(AgentRun).where(AgentRun.datasource_id == scenario.datasource.id)
    if existing_run_ids:
        # `NOT IN (<empty>)` is unsafe in standard SQL when the comparison set
        # is empty and built from a NULL sentinel, so only filter when there
        # is something to exclude -- otherwise every run for this datasource
        # is new by definition.
        all_runs_query = all_runs_query.where(AgentRun.id.not_in(existing_run_ids))
    new_runs = (await db.execute(all_runs_query)).scalars().all()
    if len(new_runs) != 1:
        raise AssertionError(
            f"eval case {case.case_id!r} expected exactly one new AgentRun, found "
            f"{len(new_runs)} -- the scenario or case is not isolated from prior runs"
        )
    actual = derive_context_path(new_runs[0])
    drift = compare_to_expected(actual, case, scenario)
    return ContextPathEvalResult(
        case_id=case.case_id, matched=not drift, drift=tuple(drift), actual=actual
    )
