"""N17: convert a promoted `ExemplarCase` (`aida.exemplar_store`) into AT-8's
own case format and replay it through AT-8's own runner -- the "benchmark
suite" half of the tracker row.

Only a `STEWARD_AUTHORED` exemplar (`exemplar.is_replayable`, i.e.
`question is not None`) can go through `exemplar_to_eval_case` and
`run_eval_case`: a `CONFIRMED_RUN` exemplar never carries a question,
requester roles, or tool parameters (see `aida.exemplar_store`'s module
docstring for why that is a real architectural constraint, not an
omission), so there is nothing to re-drive the orchestrator with. A
`CONFIRMED_RUN` exemplar instead "replays" via
`aida.exemplar_store.compare_exemplar_to_current`, which re-derives the
context path from the same immutable `AgentRun` row and diffs it against
the exemplar's frozen snapshot -- see `replay_confirmed_exemplar` below.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from aida.exemplar_store import ExemplarCase, compare_exemplar_to_current
from aida.models import AgentRun
from tests.context_path_eval.cases import ContextPathEvalCase
from tests.context_path_eval.runner import ContextPathEvalResult, run_eval_case
from tests.context_path_eval.scenario import ContextPathEvalScenario


def exemplar_to_eval_case(exemplar: ExemplarCase) -> ContextPathEvalCase:
    """Convert a replayable exemplar into AT-8's own `ContextPathEvalCase`,
    field for field -- the two shapes are deliberately kept identical on
    their `expected_*` side (see `ExemplarCase`'s own docstring) so this
    conversion is lossless and never re-derives anything.
    """
    if not exemplar.is_replayable:
        raise ValueError(
            f"exemplar {exemplar.case_id!r} (source={exemplar.source}) carries no question -- "
            "only a STEWARD_AUTHORED exemplar can be converted into a live-replayable "
            "ContextPathEvalCase; a CONFIRMED_RUN exemplar replays via "
            "replay_confirmed_exemplar instead"
        )
    return ContextPathEvalCase(
        case_id=exemplar.case_id,
        description=exemplar.description,
        question=exemplar.question,  # narrowed non-None by is_replayable above
        roles=exemplar.roles,
        preferred_tool_slug=exemplar.preferred_tool_slug,
        tool_parameters=dict(exemplar.tool_parameters),
        expected_strategy=exemplar.expected_strategy,
        expected_selected_tool_slug=exemplar.expected_selected_tool_slug,
        expected_resolved_object_types=exemplar.expected_resolved_object_types,
        expected_semantic_version_kind=exemplar.expected_semantic_version_kind,
        expected_policy_status=exemplar.expected_policy_status,
        expected_policy_reason_code=exemplar.expected_policy_reason_code,
        expected_prompt_risk_decision=exemplar.expected_prompt_risk_decision,
    )


async def replay_steward_exemplar(
    db: AsyncSession, scenario: ContextPathEvalScenario, exemplar: ExemplarCase
) -> ContextPathEvalResult:
    """The literal "gate change-set submission" proof for a replayable
    exemplar: convert it to AT-8's case format and drive a real
    `GovernedAgentOrchestrator.run()` through AT-8's own `run_eval_case`,
    unchanged. A future CI regression gate would call exactly this over the
    exemplar corpus AT-8's own hand-authored cases already sit next to.
    """
    return await run_eval_case(db, scenario, exemplar_to_eval_case(exemplar))


@dataclass(frozen=True, slots=True)
class ConfirmedExemplarReplayResult:
    """The `CONFIRMED_RUN` counterpart to `ContextPathEvalResult`: no live
    orchestrator run, since there is no question to re-drive it with --
    instead a stored-vs-current consistency check over the same immutable
    `AgentRun` row (see `aida.exemplar_store.compare_exemplar_to_current`).
    """

    case_id: str
    matched: bool
    drift: tuple[str, ...]


def replay_confirmed_exemplar(
    exemplar: ExemplarCase, agent_run: AgentRun
) -> ConfirmedExemplarReplayResult:
    """Replay a `CONFIRMED_RUN` exemplar against the `AgentRun` it was
    promoted from. `agent_run` must be the same row `exemplar.
    promoted_from_agent_run_id` names; the caller (which already has it, to
    have promoted from it) passes it in rather than this function
    re-fetching it, keeping this a pure comparison like
    `runner.compare_to_expected`.
    """
    drift = compare_exemplar_to_current(exemplar, agent_run)
    return ConfirmedExemplarReplayResult(
        case_id=exemplar.case_id, matched=not drift, drift=tuple(drift)
    )
