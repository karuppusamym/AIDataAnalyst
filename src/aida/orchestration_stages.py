"""Typed stage inputs and outputs for the governed agent orchestrator.

`agent_orchestrator.GovernedAgentOrchestrator.run` was one ~830-line function
in which screening, retrieval, planning, statement production, execution and
explanation shared a single namespace of ~20 mutable locals. R02 in the
2026-09-05 review asks for it to be split "into screen, retrieve, plan,
validate, execute, explain; typed stage inputs/outputs" -- and warns against
extracting helpers merely to shorten a function.

So this module holds the *contract* between those stages, and nothing else: no
policy, no SQL, no I/O. Each type below names exactly what one stage is allowed
to hand the next, which is what makes each stage independently readable -- a
stage cannot quietly depend on a local somebody set 300 lines earlier, because
the only thing it receives is the value here.

`RunLedger` is the one piece of shared mutable state, and it exists to hold one
invariant that used to be restated at every transition: **a runtime stage
change always leaves a trace entry.** Before, `state = state.transition(...)`
and `trace.append(_trace(state, ...))` were two independent lines repeated
fourteen times, and nothing stopped the second from being forgotten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import UUID

from aida.agent_runtime import RuntimeStage, RuntimeState

if TYPE_CHECKING:
    from aida.agent_intelligence import AgentPlan, RetrievalHit
    from aida.models import AgentContract, AgentRun, DataSource, ToolExecution
    from aida.prompt_risk import PromptRiskAssessment
    from aida.query_gateway import GatewayResult
    from aida.security import SecurityContext


def trace_entry(
    state: RuntimeState, control_type: str, details: dict[str, object] | None = None
) -> dict[str, object]:
    """One step of the run's auditable trace, in the shape `AgentRun.step_trace`
    stores. The sequence number comes from the state itself so it cannot drift
    from the number of transitions actually made."""
    entry: dict[str, object] = {
        "sequence": state.step_count,
        "stage": state.stage.value,
        "control_type": control_type,
    }
    if details:
        entry["details"] = details
    return entry


@dataclass(frozen=True, slots=True)
class OrchestrationRequest:
    """Everything the caller supplied, frozen for the life of the run.

    Passed whole to each stage rather than unpacked into positional arguments:
    a stage that starts needing a new input becomes a visible change to this
    type instead of a quietly-widened signature.
    """

    datasource: DataSource
    context: SecurityContext
    correlation_id: str
    question: str
    candidate_sql: str | None
    preferred_tool_version_id: UUID | None
    tool_parameters: dict[str, Any]
    requested_limit: int | None
    agent_asset_version_id: UUID | None = None

    @property
    def organization_id(self) -> UUID:
        return self.datasource.organization_id


@dataclass(slots=True)
class RunLedger:
    """The run's advancing state, trace and plan evidence.

    Every stage appends to this and nothing else. `advance` is the only way the
    runtime stage changes, which is what guarantees the trace and the state can
    never disagree about how far the run got.
    """

    agent_run: AgentRun
    state: RuntimeState
    trace: list[dict[str, object]] = field(default_factory=list)
    plan_evidence: dict[str, Any] = field(default_factory=dict)

    def record(self, control_type: str, details: dict[str, object] | None = None) -> None:
        """Trace the current state without changing it -- used for the initial
        entry and for checkpoints that confirm rather than transition."""
        self.trace.append(trace_entry(self.state, control_type, details))

    def advance(
        self,
        stage: RuntimeStage,
        *,
        control_type: str,
        details: dict[str, object] | None = None,
        **transition: Any,
    ) -> None:
        self.state = self.state.transition(stage, **transition)
        self.record(control_type, details)

    def publish_plan_evidence(self) -> None:
        """Push accumulated evidence onto the run row.

        Called after every mutation of `plan_evidence` rather than at the end:
        a run that refuses partway through must persist the evidence that
        explains the refusal, not an empty dict.
        """
        self.agent_run.plan_evidence = self.plan_evidence


@dataclass(frozen=True, slots=True)
class ScreenOutcome:
    """What screening established. A run that reaches here was admitted:
    its agent contract (if any) exists and is not killed, and its prompt did
    not trip the deterministic safety classifier."""

    prompt_risk: PromptRiskAssessment
    agent_contract: AgentContract | None


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    """The grounding this answer is allowed to stand on.

    `hits` is what the planner sees; `rejected` is what the retrieval bound
    discarded and is recorded as evidence rather than dropped silently.
    """

    semantic_version: str
    hits: list[RetrievalHit]
    rejected: list[RetrievalHit]
    evidence: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    plan: AgentPlan


@dataclass(frozen=True, slots=True)
class ValidatedStatement:
    """A statement that policy has already agreed may run.

    Reaching this type means every pre-execution refusal has been considered:
    the planned tool is published, the agent's capability envelope allows it,
    its dependencies carry no blocking quality incident, its parameters
    rendered, or -- on the generation paths -- the override is enabled or an
    approved model route answered. Nothing downstream re-checks any of that,
    which is why producing this value is the whole of the validate stage.
    """

    sql: str
    generation_source: str
    tool_execution: ToolExecution | None = None


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """The gateway's result, after the orchestrator's own independent
    re-verification of the validated/costed/executed checkpoints."""

    gateway_result: GatewayResult


@dataclass(frozen=True, slots=True)
class ExplanationOutcome:
    explanation: str
    trust_evidence: dict[str, Any] | None


__all__ = [
    "ExecutionOutcome",
    "ExplanationOutcome",
    "OrchestrationRequest",
    "PlanOutcome",
    "RetrievalOutcome",
    "RunLedger",
    "ScreenOutcome",
    "ValidatedStatement",
    "trace_entry",
]
