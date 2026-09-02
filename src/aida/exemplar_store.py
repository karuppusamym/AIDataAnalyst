"""N17 (scoped half): promote confirmed context paths into exemplars.

**Scope** (`Docs/review-2026-08/atlan-context/01-context-studio.md`, "Split
N17"): the *context-path regression* half only -- mine the corpus from
confirmed-correct runs and BI/query history, assert on resolved objects +
versions + selected tool + policy decision, gate change-set submission. The
*answer-delta* half (checking whether returned VALUES are still correct)
stays out of scope: INV-6/ADR-0014 forbid asserting business-value
expectations, the same reasoning `AT-8` already applied to its own
hand-authored cases (`tests/context_path_eval/cases.py`) and this module
does not regress.

**Where exemplars come from, and what each source can actually carry.**
Three sources are named in the design doc: promoted analyses, review-confirmed
agent runs, and steward-authored pairs. Investigating what "review-confirmed"
means in this codebase's *actual* data (rather than inventing a new flag)
turned up a real signal already wired end to end:

* `QueryFeedback` (`intelligence_api.py::upsert_query_feedback`) is a human
  rating a specific `AgentRun` "HELPFUL"/otherwise. `QueryMemoryEvidence.status`
  reaches `"ELIGIBLE"` only once positive feedback exists and no negative
  feedback is outstanding (`query_memory.py`'s own docstring: "Negative
  feedback already suppresses reuse before this module ever runs"). That is
  the codebase's one existing, human-confirmed "this run was correct"
  signal -- `promote_confirmed_agent_run` below uses it directly, not a new
  flag.
* `aida.consumption_lineage.get_consumption_by_resource_counts` is the
  "BI/query history" signal the scoping doc also names: a heavily-reused
  governed-tool pairing is evidence of trustworthiness even absent an
  explicit human confirmation. `rank_candidates_by_consumption` below folds
  it in as a *prioritization* signal over the confirmed-run candidate pool
  (which tool pairing to promote first when there are more confirmed runs
  than the corpus needs), not a second, independent promotion path -- there
  is no `AgentRun` behind a bare consumption count to derive a context path
  from, so promoting from frequency alone would have nothing to promote.

**Why a promoted exemplar can never carry a live-replayable question.**
`AgentRun`'s own docstring: "raw user questions are intentionally not
persisted" -- only `question_hash`. Digging further: `AgentRun` does not
persist the requester's roles, the tool the requester asked for, or the
tool-call parameters supplied either (all of that is either redacted or
never written at all -- see `agent_orchestrator.py`; `sql_redaction.py`).
So a `CONFIRMED_RUN` exemplar can only ever carry the **output** side of a
case -- the resolved context path (`ExemplarCase.expected_*`) -- never the
**input** side (`question`/`roles`/`preferred_tool_slug`/`tool_parameters`)
`tests/context_path_eval/runner.run_eval_case` needs to literally re-drive
the orchestrator. This is not a gap this module works around; it is the
same value-freedom `AgentRun` already enforces on itself, one layer further
than just the final answer.

`STEWARD_AUTHORED` exemplars are the one source that *can* carry a
replayable question, exactly the way AT-8's own hand-authored cases do --
a human steward types the question directly, the same act of authorship
`cases.py` already performs, just tagged with this module's provenance
fields instead of being a bare `ContextPathEvalCase`.
`tests/context_path_eval/exemplars.py` converts a replayable
(`question is not None`) `ExemplarCase` into a `ContextPathEvalCase` and
feeds it through AT-8's own `run_eval_case` unchanged -- the literal
"benchmark suite" proof. A `CONFIRMED_RUN` exemplar instead "replays" as a
stored-vs-current consistency check (`compare_exemplar_to_current` below):
re-derive the context path from the same immutable `AgentRun` row and diff
against the frozen snapshot -- the same "verify the digest still matches"
idiom `agent_run_replay.py` (AT-6) already uses, adapted from a single
grounding fragment to a whole context path.

**Storage.** No new table, no new column, no migration (forbidden by this
row's own hard constraint). `AgentRun` is only ever written once, by
`GovernedAgentOrchestrator.run` (grep confirms nothing else assigns to its
`plan_evidence`/`retrieval_evidence`/`semantic_version`/`status`/
`failure_reason` fields), so the pair (a `COMPLETED` `AgentRun`, its
`ELIGIBLE` `QueryMemoryEvidence` row) is *already* immutable, already
persisted evidence of "a question was asked, answered, and a human
confirmed it" -- exactly N16's own "no new persisted state was needed"
precedent (`NegativeAssertionRecord` reused as-is for negative knowledge),
applied here to a different existing pair of tables instead of one. A
`CONFIRMED_RUN` `ExemplarCase` is therefore a deterministic, content-addressed
*projection* of that pair, computed once at promotion time
(`promote_confirmed_agent_run`) and stable forever after because its inputs
never change -- not a live re-derivation recomputed differently on every
read. `STEWARD_AUTHORED` exemplars, lacking any DB row to project from, are
plain frozen dataclasses the caller constructs once
(`steward_authored_exemplar`) and is expected to keep wherever it keeps
other reviewed content (this repository's git history, same as AT-8's own
`cases.py`, for the ones promoted into the stored benchmark suite -- see
`tests/context_path_eval/exemplars.py`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.consumption_lineage import get_consumption_by_resource_counts
from aida.context_path import ContextPath, derive_context_path
from aida.models import AgentRun, GovernedTool, GovernedToolVersion, QueryMemoryEvidence

#: `QueryMemoryEvidence.status` value that marks a run as human-confirmed --
#: reused verbatim from `query_memory.ELIGIBLE_STATUS` rather than redefined,
#: so the two modules can never drift on what "confirmed" means.
CONFIRMED_STATUS = "ELIGIBLE"

#: Bound every promotion scan (coding standard S5: a multi-row operation
#: takes a bound and reports truncation, never a silent partial result).
DEFAULT_PROMOTION_SCAN_LIMIT = 200

ExemplarSource = Literal["CONFIRMED_RUN", "STEWARD_AUTHORED"]


@dataclass(frozen=True, slots=True)
class ExemplarCase:
    """An exemplar in AT-8's own case shape (question/roles/parameters as
    *inputs*, `expected_*` as the asserted context path), plus provenance.

    `question`/`roles`/`preferred_tool_slug`/`tool_parameters` are `None`/
    empty for a `CONFIRMED_RUN` exemplar -- see the module docstring for why
    that is a real architectural constraint, not an omission. They are
    populated for a `STEWARD_AUTHORED` exemplar. `is_replayable` names the
    condition `tests/context_path_eval/exemplars.py` checks before handing
    one to AT-8's `run_eval_case`.
    """

    case_id: str
    source: ExemplarSource
    description: str
    question: str | None
    question_hash: str | None
    roles: frozenset[str]
    preferred_tool_slug: str | None
    tool_parameters: dict[str, str] = field(default_factory=dict)

    # --- expected context path -- structural facts only, never a value/answer,
    # matching `tests.context_path_eval.cases.ContextPathEvalCase` field for
    # field so a replayable exemplar converts to one with no lossy mapping.
    expected_strategy: str = ""
    expected_selected_tool_slug: str | None = None
    expected_resolved_object_types: frozenset[str] = frozenset()
    expected_semantic_version_kind: str = ""
    expected_policy_status: str = ""
    expected_policy_reason_code: str | None = None
    expected_prompt_risk_decision: str = "ALLOW"

    # --- provenance
    promoted_from_agent_run_id: str | None = None
    artifact_hash: str = ""

    @property
    def is_replayable(self) -> bool:
        """True only for a `STEWARD_AUTHORED` exemplar that carries a real
        question -- the one case `tests/context_path_eval/exemplars.py` can
        convert into a `ContextPathEvalCase` and re-drive the orchestrator
        with. A `CONFIRMED_RUN` exemplar is never replayable this way (see
        module docstring); it replays instead via
        `compare_exemplar_to_current`.
        """
        return self.question is not None


def _canonical_case_payload(
    *,
    case_id: str,
    source: str,
    question: str | None,
    question_hash: str | None,
    roles: frozenset[str],
    preferred_tool_slug: str | None,
    tool_parameters: dict[str, str],
    expected_strategy: str,
    expected_selected_tool_slug: str | None,
    expected_resolved_object_types: frozenset[str],
    expected_semantic_version_kind: str,
    expected_policy_status: str,
    expected_policy_reason_code: str | None,
    expected_prompt_risk_decision: str,
    promoted_from_agent_run_id: str | None,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "source": source,
        "question": question,
        "question_hash": question_hash,
        "roles": sorted(roles),
        "preferred_tool_slug": preferred_tool_slug,
        "tool_parameters": dict(sorted(tool_parameters.items())),
        "expected_strategy": expected_strategy,
        "expected_selected_tool_slug": expected_selected_tool_slug,
        "expected_resolved_object_types": sorted(expected_resolved_object_types),
        "expected_semantic_version_kind": expected_semantic_version_kind,
        "expected_policy_status": expected_policy_status,
        "expected_policy_reason_code": expected_policy_reason_code,
        "expected_prompt_risk_decision": expected_prompt_risk_decision,
        "promoted_from_agent_run_id": promoted_from_agent_run_id,
    }


def _artifact_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_exemplar_case(
    *,
    case_id: str,
    source: ExemplarSource,
    description: str,
    question: str | None,
    question_hash: str | None,
    roles: frozenset[str],
    preferred_tool_slug: str | None,
    tool_parameters: dict[str, str],
    context_path: ContextPath,
    selected_tool_slug: str | None,
    promoted_from_agent_run_id: str | None,
) -> ExemplarCase:
    expected_policy_status = context_path.policy_status
    expected_policy_reason_code = context_path.policy_reason_code
    expected_prompt_risk_decision = context_path.prompt_risk_decision or "ALLOW"
    payload = _canonical_case_payload(
        case_id=case_id,
        source=source,
        question=question,
        question_hash=question_hash,
        roles=roles,
        preferred_tool_slug=preferred_tool_slug,
        tool_parameters=tool_parameters,
        expected_strategy=context_path.strategy or "",
        expected_selected_tool_slug=selected_tool_slug,
        expected_resolved_object_types=context_path.resolved_object_types,
        expected_semantic_version_kind=context_path.semantic_version_kind,
        expected_policy_status=expected_policy_status,
        expected_policy_reason_code=expected_policy_reason_code,
        expected_prompt_risk_decision=expected_prompt_risk_decision,
        promoted_from_agent_run_id=promoted_from_agent_run_id,
    )
    return ExemplarCase(
        case_id=case_id,
        source=source,
        description=description,
        question=question,
        question_hash=question_hash,
        roles=roles,
        preferred_tool_slug=preferred_tool_slug,
        tool_parameters=dict(tool_parameters),
        expected_strategy=context_path.strategy or "",
        expected_selected_tool_slug=selected_tool_slug,
        expected_resolved_object_types=context_path.resolved_object_types,
        expected_semantic_version_kind=context_path.semantic_version_kind,
        expected_policy_status=expected_policy_status,
        expected_policy_reason_code=expected_policy_reason_code,
        expected_prompt_risk_decision=expected_prompt_risk_decision,
        promoted_from_agent_run_id=promoted_from_agent_run_id,
        artifact_hash=_artifact_hash(payload),
    )


async def _resolve_tool_slug(
    session: AsyncSession, organization_id: UUID, tool_version_id: str | None
) -> str | None:
    """`ContextPath.selected_tool_version_id` is a `GovernedToolVersion.id`;
    exemplar cases (matching `cases.py`'s own `preferred_tool_slug`/
    `expected_selected_tool_slug` convention) name a tool by its stable
    `GovernedTool.slug` instead, so the run stays reproducible even if a new
    tool version is later published under the same slug.
    """
    if not tool_version_id:
        return None
    try:
        version_uuid = UUID(tool_version_id)
    except ValueError:
        return None
    row = (
        await session.execute(
            select(GovernedTool.slug)
            .join(GovernedToolVersion, GovernedToolVersion.tool_id == GovernedTool.id)
            .where(
                GovernedToolVersion.id == version_uuid,
                GovernedToolVersion.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    return row


async def find_confirmed_agent_runs(
    session: AsyncSession,
    organization_id: UUID,
    *,
    scan_limit: int = DEFAULT_PROMOTION_SCAN_LIMIT,
) -> list[tuple[AgentRun, QueryMemoryEvidence]]:
    """Real, already-persisted "review-confirmed" candidates for promotion:
    a `COMPLETED` `AgentRun` whose `QueryMemoryEvidence` has reached
    `CONFIRMED_STATUS` (positive human feedback, no outstanding negative
    feedback -- see module docstring). Ordered by evidence recency, bounded
    by `scan_limit` and never claiming completeness beyond it (S5).
    """
    rows = (
        await session.execute(
            select(AgentRun, QueryMemoryEvidence)
            .join(QueryMemoryEvidence, QueryMemoryEvidence.agent_run_id == AgentRun.id)
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.status == "COMPLETED",
                QueryMemoryEvidence.status == CONFIRMED_STATUS,
            )
            .order_by(QueryMemoryEvidence.updated_at.desc(), AgentRun.id)
            .limit(scan_limit)
        )
    ).all()
    return [(agent_run, memory) for agent_run, memory in rows]


async def promote_confirmed_agent_run(
    session: AsyncSession,
    agent_run: AgentRun,
    memory_evidence: QueryMemoryEvidence,
) -> ExemplarCase:
    """Promote one confirmed `AgentRun` into a `CONFIRMED_RUN` exemplar.

    Refuses anything not actually confirmed (`memory_evidence.status !=
    CONFIRMED_STATUS`) or mismatched (`memory_evidence` naming a different
    run) rather than silently promoting weaker evidence. Deterministic: two
    calls against the same (immutable) `agent_run`/`memory_evidence` pair
    produce byte-identical `ExemplarCase.artifact_hash` values -- see module
    docstring for why that holds without a separate persisted snapshot.
    """
    if memory_evidence.agent_run_id != agent_run.id:
        raise ValueError(
            "memory_evidence does not belong to agent_run "
            f"({memory_evidence.agent_run_id} != {agent_run.id})"
        )
    if memory_evidence.status != CONFIRMED_STATUS:
        raise ValueError(
            f"agent_run {agent_run.id} is not confirmed "
            f"(query_memory_evidence.status={memory_evidence.status!r}, "
            f"expected {CONFIRMED_STATUS!r})"
        )
    context_path = derive_context_path(agent_run)
    selected_tool_slug = await _resolve_tool_slug(
        session, agent_run.organization_id, context_path.selected_tool_version_id
    )
    return _build_exemplar_case(
        case_id=f"confirmed-run-{agent_run.id}",
        source="CONFIRMED_RUN",
        description=(
            f"Promoted from AgentRun {agent_run.id}: human-confirmed correct "
            "(QueryMemoryEvidence reached ELIGIBLE via positive QueryFeedback, "
            "no outstanding negative feedback). Question text is not carried -- "
            "AgentRun never persists it (raw questions, roles, and tool "
            "parameters are all intentionally value-free); only the resolved "
            "context path is."
        ),
        question=None,
        question_hash=agent_run.question_hash,
        roles=frozenset(),
        preferred_tool_slug=None,
        tool_parameters={},
        context_path=context_path,
        selected_tool_slug=selected_tool_slug,
        promoted_from_agent_run_id=str(agent_run.id),
    )


def steward_authored_exemplar(
    *,
    case_id: str,
    description: str,
    question: str,
    roles: frozenset[str],
    preferred_tool_slug: str | None,
    tool_parameters: dict[str, str] | None = None,
    expected_strategy: str,
    expected_selected_tool_slug: str | None,
    expected_resolved_object_types: frozenset[str],
    expected_semantic_version_kind: str,
    expected_policy_status: str,
    expected_policy_reason_code: str | None,
    expected_prompt_risk_decision: str = "ALLOW",
) -> ExemplarCase:
    """Build a `STEWARD_AUTHORED` exemplar: the one source that carries a
    real, replayable question, authored the same way AT-8's own
    `cases.py` cases are -- a human writes the question and the expected
    context path down directly. No `AgentRun` required; deterministic by
    construction (same arguments -> same `artifact_hash`).
    """
    context_path = ContextPath(
        strategy=expected_strategy,
        selected_tool_version_id=None,
        tool_decisions=(),
        resolved_object_types=expected_resolved_object_types,
        resolved_objects=frozenset(),
        semantic_version=None,
        semantic_version_kind=expected_semantic_version_kind,
        policy_status=expected_policy_status,
        policy_reason_code=expected_policy_reason_code,
        prompt_risk_decision=expected_prompt_risk_decision,
    )
    return _build_exemplar_case(
        case_id=case_id,
        source="STEWARD_AUTHORED",
        description=description,
        question=question,
        question_hash=None,
        roles=roles,
        preferred_tool_slug=preferred_tool_slug,
        tool_parameters=dict(tool_parameters or {}),
        context_path=context_path,
        selected_tool_slug=expected_selected_tool_slug,
        promoted_from_agent_run_id=None,
    )


def compare_exemplar_to_current(
    exemplar: ExemplarCase, agent_run: AgentRun
) -> list[str]:
    """Replay a `CONFIRMED_RUN` exemplar the way its source allows: re-derive
    the context path from the *same* `AgentRun` row right now and diff
    against the frozen snapshot, rather than re-driving the orchestrator
    (which needs a question this exemplar never carries -- see module
    docstring). Mirrors `agent_run_replay.py`'s "verify the digest still
    matches" idiom and `tests.context_path_eval.runner.compare_to_expected`'s
    field-by-field shape. Empty list means no drift.
    """
    current = derive_context_path(agent_run)
    drift: list[str] = []
    if current.strategy != exemplar.expected_strategy:
        drift.append(
            f"strategy: expected {exemplar.expected_strategy!r}, got {current.strategy!r}"
        )
    if current.resolved_object_types != exemplar.expected_resolved_object_types:
        drift.append(
            "resolved_object_types: expected "
            f"{sorted(exemplar.expected_resolved_object_types)}, "
            f"got {sorted(current.resolved_object_types)}"
        )
    if current.semantic_version_kind != exemplar.expected_semantic_version_kind:
        drift.append(
            "semantic_version_kind: expected "
            f"{exemplar.expected_semantic_version_kind!r}, got "
            f"{current.semantic_version_kind!r}"
        )
    if current.policy_status != exemplar.expected_policy_status:
        drift.append(
            f"policy_status: expected {exemplar.expected_policy_status!r}, got "
            f"{current.policy_status!r}"
        )
    if current.policy_reason_code != exemplar.expected_policy_reason_code:
        drift.append(
            "policy_reason_code: expected "
            f"{exemplar.expected_policy_reason_code!r}, got "
            f"{current.policy_reason_code!r}"
        )
    return drift


async def rank_candidates_by_consumption(
    session: AsyncSession,
    organization_id: UUID,
    candidates: list[tuple[AgentRun, QueryMemoryEvidence]],
    *,
    scan_limit: int = DEFAULT_PROMOTION_SCAN_LIMIT,
) -> list[tuple[AgentRun, QueryMemoryEvidence, int]]:
    """Fold in the "BI/query history" signal the scoping doc names alongside
    confirmed runs: rank already-confirmed candidates by how often their
    recommended governed tool has actually been consumed
    (`consumption_lineage.get_consumption_by_resource_counts`), highest
    reuse first. A confirmed run whose tool nobody else exercises is still
    promotable (human confirmation is sufficient on its own); this only
    decides *promotion order* when a corpus size cap means not every
    confirmed candidate gets promoted -- heavily-reused pairings are
    evidence of trustworthiness worth keeping first, per the design doc.
    Candidates with no `recommended_tool_version_id` (e.g. a MODEL_GENERATION
    strategy) sort last, at count 0, rather than being dropped.
    """
    counts = await get_consumption_by_resource_counts(
        session,
        organization_id=organization_id,
        resource_type="governed_tool_version",
        limit=scan_limit,
    )
    count_by_tool_version_id = {resource_id: count for resource_id, count, _ in counts}
    ranked = [
        (
            agent_run,
            memory,
            count_by_tool_version_id.get(
                str(agent_run.recommended_tool_version_id), 0
            )
            if agent_run.recommended_tool_version_id
            else 0,
        )
        for agent_run, memory in candidates
    ]
    ranked.sort(key=lambda entry: (-entry[2], str(entry[0].id)))
    return ranked
