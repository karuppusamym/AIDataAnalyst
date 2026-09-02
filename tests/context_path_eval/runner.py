"""The replay mechanism: derive a context path from a persisted `AgentRun`
and compare it against a stored eval case's expectation.

`derive_context_path` (and the `ContextPath` it returns) now live in
`aida.context_path` -- promoted out of this module for N17, so the
production exemplar-promotion code (`aida.exemplar_store`) can share the
exact same derivation this runner already proved out, rather than
re-implementing an equivalent reader of `AgentRun` state a second time. Both
still re-export at this name for every existing import site; nothing about
AT-8's own behavior changed. See `aida.context_path`'s own docstring for the
"read back what was recorded, never recompute a live equivalent" rationale.

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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_orchestrator import (
    AgentClarificationRequired,
    AgentPolicyRejected,
    GovernedAgentOrchestrator,
    ModelRouteUnavailable,
)
from aida.config import Settings
from aida.context_path import ContextPath, derive_context_path
from aida.models import AgentRun
from tests.context_path_eval.cases import ContextPathEvalCase
from tests.context_path_eval.scenario import ContextPathEvalScenario

__all__ = [
    "ContextPath",
    "ContextPathEvalResult",
    "compare_to_expected",
    "derive_context_path",
    "run_eval_case",
]

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
