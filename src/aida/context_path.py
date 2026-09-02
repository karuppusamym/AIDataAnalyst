"""Pure derivation of the *context path* a completed `AgentRun` took.

Moved out of `tests/context_path_eval/runner.py` (AT-8) so production code
(`exemplar_store.py`, N17) can share the exact same derivation the eval
runner already proved out, rather than re-implementing an equivalent reader
of `AgentRun` state a second time. `tests/context_path_eval/runner.py`
imports `ContextPath`/`derive_context_path` from here unchanged -- nothing
about AT-8's own behavior changes, this is a pure relocation.

`derive_context_path` is a pure function over `AgentRun` fields that already
exist on the model (`plan_evidence`, `retrieval_evidence`, `semantic_version`,
`status`, `failure_reason`) -- "read back what was recorded, never recompute
a live equivalent," the same idiom AT-16's `answer_provenance.py` and AT-6's
`agent_run_replay.py` both use. It only ever touches object identifiers, a
version string, a plan strategy, and a policy outcome -- the context path,
never a result-set value or generated SQL content (INV-6/ADR-0014).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aida.models import AgentRun


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
