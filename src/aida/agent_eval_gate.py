"""N15: evaluation-gated agent publication.

Makes "production-grade agent" evidenced rather than asserted by adding a
real precondition to the one place an `AiAssetVersion` (EA.10c AI registry)
actually moves to its published/production `APPROVED` state --
`semantic_api._apply_governance_review_decision`'s `AI_ASSET_VERSION`
branch, the single dispatcher both the single-item and bulk governance
review decision endpoints already share (PG-3). No parallel lifecycle is
forked; this module only adds a check *before* that existing transition,
and records its evidence into the exact `AiAssetVersion.evaluation_evidence`
JSON field `ai_registry.compute_ai_trust_score` already reads
(`evaluation_evidence["pass_rate"]`/`["evidence_id"]`) -- so a passing (or
failing) N15 gate run now feeds the EVALUATION_POSTURE trust-score factor
that field was always meant to carry, for platform-native agents, without a
migration or a new column.

**Where "the real publish path" actually lives -- corrected from the
tracker row's own framing.** `ai_registry_api.py` never itself flips a
version to `APPROVED`: `submit_ai_asset_version` only moves DRAFT ->
REVIEW_REQUIRED and opens a `GovernanceReview`; the APPROVE decision that
actually publishes it is applied by `semantic_api._apply_governance_review_decision`,
the *shared* dispatcher every governed object type's maker-checker decision
goes through (checked directly against the code, not assumed from the
tracker item's title). Gating publication for real therefore means editing
that shared dispatcher's `AI_ASSET_VERSION` branch, not `ai_registry_api.py`
-- an even better reuse than the tracker row's own phrasing implied, since
it is the one place every AI asset version approval already flows through.

**Never asserts on answer/value correctness.** Every verdict this module
consumes or produces ultimately traces back to N17's context-path
derivation (`aida.context_path.derive_context_path`) -- object/version
identity, plan strategy, policy outcome -- never a result-set value,
generated SQL, or model output content (INV-6/ADR-0014), the same
discipline AT-8's own eval cases hold to.

**Scope, stated honestly (same finding UX-19 already made, checked again
here rather than assumed).** `AgentRun` carries no foreign key back to
`AiAsset`/`AiAssetVersion`, so there is no persisted way to attribute a
specific `AgentRun` -- confirmed or not -- to one specific registered
`AGENT`-kind asset. Fabricating one (e.g. a name-match heuristic) would be
exactly the kind of invented scoping this row's own instructions forbid.
Nor does `AiAssetVersion` carry any data-domain/table/project scope field
that could stand in for it (checked directly against `models.py`, which is
read-only for this row: it has `context_product_version_ids`,
`model_route_ids`, and `policy_control_ids` -- dependency references, not a
governed-scope filter over runs). So this gate is **organization-wide**,
never claimed to be the one agent's private evaluation history -- the exact
label (`scope="ORGANIZATION_WIDE"`) and honesty pattern UX-19's
`agent_roster.py` already established for the identical gap, reused here
rather than re-invented.

**Two independent, honestly-scoped ways a verdict can be produced, and why
only one of them runs automatically at publish time.**

1. `CONFIRMED_RUN` verdicts (`replay_confirmed_run_corpus` below) -- driven
   entirely from already-persisted rows (`aida.exemplar_store.
   find_confirmed_agent_runs`/`promote_confirmed_agent_run`), no seeded
   scenario or live orchestrator run required. This is genuinely
   production-safe to run synchronously inside a governance decision, so
   it always runs fresh, automatically, at both preview (the read endpoint)
   and at the real publish decision -- no stale corpus can silently gate a
   real publish.

2. `STEWARD_AUTHORED` verdicts, from N17's `tests.context_path_eval.
   exemplars.replay_steward_exemplar`, which re-drives a real
   `GovernedAgentOrchestrator.run()` against a `tests.context_path_eval.
   scenario.ContextPathEvalScenario` -- a from-scratch seeded environment
   that lives under `tests/` on purpose (see that module's own docstring:
   "seeded environment the AT-8 eval cases run against"). Production code
   under `src/aida` must never import from `tests/` (the reverse dependency
   AT-8/N17 already keep one-directional), and there is no equivalent
   production-safe way to re-drive the orchestrator against a synthetic
   scenario without either fabricating one against a real organization's
   real data (unacceptable -- an eval run must never touch production
   state) or building a whole second seeding mechanism (out of scope for
   this row, and exactly the kind of parallel machinery the row's own
   instructions warn against forking). So this module never computes
   `STEWARD_AUTHORED` verdicts itself. Instead, a caller who *does* have a
   live-replay-capable environment (a steward running the suite by hand, or
   a future CI job -- N17's own docstring names "wiring the replay into a
   literal CI gate" as its own documented next step, not yet done) submits
   the resulting verdicts through `record_agent_eval_gate_evidence`'s
   `extra_verdicts`, which persists them into `AiAssetVersion.
   evaluation_evidence["agent_eval_gate"]["steward_authored_verdicts"]` so
   a later publish decision can fold them back in via
   `stored_steward_verdicts` -- without ever running a live replay itself.
   A submitted verdict's `source` is always forced to `"STEWARD_AUTHORED"`
   (see `AgentEvalGateApiVerdict` in `ai_registry_api.py`): a caller can
   never inject a fabricated `CONFIRMED_RUN` verdict this way, since that
   source is only ever produced by `replay_confirmed_run_corpus`'s own live
   database read.

**What a `CONFIRMED_RUN` verdict does and does not prove.** `derive_context_path`
is a pure function of only the fields already persisted on one immutable
`AgentRun` row (see `aida.context_path`'s own docstring) -- no other table is
consulted. So comparing a freshly-promoted exemplar against the very same
row it was just promoted from is a tautology; it can never show drift. A
`CONFIRMED_RUN` verdict's `matched=True` therefore proves something real but
narrow: *this run is still genuinely confirmed and its context path still
derives cleanly, right now* -- not that the organization's governed catalog
has not changed since. `matched=False` only happens if promotion itself
raises (a mismatched or no-longer-confirmed pair), a real, if rare, defensive
signal. A future row wiring N17's `compare_exemplar_to_current` against a
snapshot frozen at an *earlier* gate run (rather than the same run) could add
genuine over-time drift detection; that is not built here.

**Never a silent pass on zero exemplars (S5-style bound reporting).**
`evaluate_agent_eval_gate` returns `INSUFFICIENT_DATA`, not `PASS`, whenever
fewer than `minimum_exemplars` verdicts are available -- including the
empty case. An agent whose organization has confirmed no runs and whose
stewards have authored no exemplars cannot publish by default; a gate with
nothing to evaluate is not evidence of anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.events import record_audit
from aida.exemplar_store import (
    DEFAULT_PROMOTION_SCAN_LIMIT,
    find_confirmed_agent_runs,
    promote_confirmed_agent_run,
)
from aida.models import AiAssetVersion
from aida.schemas import ApiModel
from aida.security import SecurityContext

#: Mirrors `ai_registry.compute_ai_trust_score`'s own existing
#: `HIGH_RISK_EVALUATION_BELOW_THRESHOLD` blocker threshold (0.8) -- reusing
#: the value this codebase already treats as "evaluation posture good
#: enough," rather than inventing an unrelated number for the same concept.
DEFAULT_AGENT_EVAL_GATE_THRESHOLD = 0.8

#: Fewer than this many total verdicts (across both sources) is
#: `INSUFFICIENT_DATA`, never a silent `PASS`. 1 is the floor: an
#: organization with a single confirmed run has *something* to evaluate,
#: zero never does.
DEFAULT_MINIMUM_EXEMPLARS = 1

AgentEvalGateVerdict = Literal["PASS", "FAIL", "INSUFFICIENT_DATA"]
ExemplarVerdictSource = Literal["CONFIRMED_RUN", "STEWARD_AUTHORED"]


@dataclass(frozen=True, slots=True)
class ExemplarVerdict:
    """One exemplar's replay outcome, in a shape deliberately independent of
    N17's own result types (`tests.context_path_eval.runner.
    ContextPathEvalResult`, `tests.context_path_eval.exemplars.
    ConfirmedExemplarReplayResult`) so this module never has to import
    anything from `tests/` -- a test-only caller converts field-for-field
    (see `tests/test_agent_eval_gate.py`), production code builds these
    directly from `replay_confirmed_run_corpus` below.
    """

    case_id: str
    source: ExemplarVerdictSource
    matched: bool
    drift: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class AgentEvalGateResult:
    """Every contributing verdict stays visible on `verdicts` -- this
    session's "every factor inspectable" convention -- rather than
    collapsing straight to a bare pass/fail.
    """

    verdict: AgentEvalGateVerdict
    threshold: float
    minimum_exemplars: int
    total_exemplars: int
    passed_exemplars: int
    pass_rate: float | None
    failing_case_ids: tuple[str, ...]
    verdicts: tuple[ExemplarVerdict, ...]
    reason: str
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def evaluate_agent_eval_gate(
    verdicts: list[ExemplarVerdict],
    *,
    threshold: float = DEFAULT_AGENT_EVAL_GATE_THRESHOLD,
    minimum_exemplars: int = DEFAULT_MINIMUM_EXEMPLARS,
    now: datetime | None = None,
) -> AgentEvalGateResult:
    """Pure, DB-free: given a set of exemplar replay verdicts (from either
    `replay_confirmed_run_corpus` below or a caller-supplied
    `STEWARD_AUTHORED` set -- see module docstring), compute PASS/FAIL/
    INSUFFICIENT_DATA. Context-path facts only; this function never sees,
    and could not assert on, an answer value (INV-6/ADR-0014).
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be within [0.0, 1.0], got {threshold!r}")
    if minimum_exemplars < 1:
        raise ValueError(f"minimum_exemplars must be at least 1, got {minimum_exemplars!r}")

    moment = now or datetime.now(UTC)
    ordered = tuple(verdicts)
    total = len(ordered)
    passed = sum(1 for v in ordered if v.matched)
    failing_case_ids = tuple(v.case_id for v in ordered if not v.matched)

    if total < minimum_exemplars:
        return AgentEvalGateResult(
            verdict="INSUFFICIENT_DATA",
            threshold=threshold,
            minimum_exemplars=minimum_exemplars,
            total_exemplars=total,
            passed_exemplars=passed,
            pass_rate=None,
            failing_case_ids=failing_case_ids,
            verdicts=ordered,
            reason=(
                f"{total} exemplar verdict(s) available, fewer than the "
                f"{minimum_exemplars} required to evaluate a pass rate -- "
                "zero (or too few) exemplars is never treated as a passing gate."
            ),
            evaluated_at=moment,
        )

    pass_rate = passed / total
    if pass_rate >= threshold:
        return AgentEvalGateResult(
            verdict="PASS",
            threshold=threshold,
            minimum_exemplars=minimum_exemplars,
            total_exemplars=total,
            passed_exemplars=passed,
            pass_rate=pass_rate,
            failing_case_ids=failing_case_ids,
            verdicts=ordered,
            reason=(
                f"{passed}/{total} exemplars matched ({pass_rate:.1%}), "
                f"at or above the {threshold:.1%} threshold."
            ),
            evaluated_at=moment,
        )
    named_failures = ", ".join(failing_case_ids) if failing_case_ids else "none named"
    return AgentEvalGateResult(
        verdict="FAIL",
        threshold=threshold,
        minimum_exemplars=minimum_exemplars,
        total_exemplars=total,
        passed_exemplars=passed,
        pass_rate=pass_rate,
        failing_case_ids=failing_case_ids,
        verdicts=ordered,
        reason=(
            f"{passed}/{total} exemplars matched ({pass_rate:.1%}), below the "
            f"{threshold:.1%} threshold. Failing exemplars: {named_failures}."
        ),
        evaluated_at=moment,
    )


async def replay_confirmed_run_corpus(
    session: AsyncSession,
    organization_id: UUID,
    *,
    scan_limit: int = DEFAULT_PROMOTION_SCAN_LIMIT,
) -> list[ExemplarVerdict]:
    """The one exemplar-replay path this module can run automatically and
    synchronously in production: no seeded scenario, no live orchestrator
    run, just the organization's currently human-confirmed `AgentRun` rows
    (see module docstring, "What a CONFIRMED_RUN verdict does and does not
    prove," for exactly what `matched=True` here does and does not show).
    """
    candidates = await find_confirmed_agent_runs(session, organization_id, scan_limit=scan_limit)
    verdicts: list[ExemplarVerdict] = []
    for agent_run, memory_evidence in candidates:
        try:
            exemplar = await promote_confirmed_agent_run(session, agent_run, memory_evidence)
        except ValueError as exc:
            verdicts.append(
                ExemplarVerdict(
                    case_id=f"confirmed-run-{agent_run.id}",
                    source="CONFIRMED_RUN",
                    matched=False,
                    drift=(),
                    detail=str(exc),
                )
            )
            continue
        verdicts.append(
            ExemplarVerdict(
                case_id=exemplar.case_id,
                source="CONFIRMED_RUN",
                matched=True,
                drift=(),
                detail=(
                    "Promoted cleanly from a human-confirmed AgentRun "
                    "(QueryMemoryEvidence.status == ELIGIBLE) with a derivable context path."
                ),
            )
        )
    return verdicts


def stored_steward_verdicts(version: AiAssetVersion) -> list[ExemplarVerdict]:
    """Read back whatever `STEWARD_AUTHORED` verdicts a prior call to
    `record_agent_eval_gate_evidence` persisted for this version (see module
    docstring). Tolerant of a missing/malformed blob -- an old version with
    no gate evidence yet simply contributes nothing, not an error.
    """
    gate = version.evaluation_evidence.get("agent_eval_gate")
    if not isinstance(gate, dict):
        return []
    raw = gate.get("steward_authored_verdicts")
    if not isinstance(raw, list):
        return []
    verdicts: list[ExemplarVerdict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        verdicts.append(
            ExemplarVerdict(
                case_id=str(item.get("case_id", "")),
                source="STEWARD_AUTHORED",
                matched=bool(item.get("matched")),
                drift=tuple(str(d) for d in item.get("drift") or ()),
                detail=str(item.get("detail", "")),
            )
        )
    return verdicts


async def compute_agent_eval_gate(
    session: AsyncSession,
    *,
    organization_id: UUID,
    extra_verdicts: list[ExemplarVerdict] | None = None,
    threshold: float = DEFAULT_AGENT_EVAL_GATE_THRESHOLD,
    minimum_exemplars: int = DEFAULT_MINIMUM_EXEMPLARS,
    confirmed_scan_limit: int = DEFAULT_PROMOTION_SCAN_LIMIT,
    now: datetime | None = None,
) -> AgentEvalGateResult:
    """The one function both the preview read and the real publish
    precondition call, so a steward's preview can never show a different
    answer than the gate that actually decides the publish (deliverable 3's
    "see BEFORE attempting to publish" only means something if the two
    cannot drift).
    """
    confirmed_verdicts = await replay_confirmed_run_corpus(
        session, organization_id, scan_limit=confirmed_scan_limit
    )
    combined = [*confirmed_verdicts, *(extra_verdicts or [])]
    return evaluate_agent_eval_gate(
        combined, threshold=threshold, minimum_exemplars=minimum_exemplars, now=now
    )


def _verdict_payload(verdict: ExemplarVerdict) -> dict[str, Any]:
    return {
        "case_id": verdict.case_id,
        "source": verdict.source,
        "matched": verdict.matched,
        "drift": list(verdict.drift),
        "detail": verdict.detail,
    }


def record_agent_eval_gate_evidence(
    session: AsyncSession,
    version: AiAssetVersion,
    result: AgentEvalGateResult,
    *,
    context: SecurityContext,
    stage: Literal["PRE_PUBLISH_CHECK", "PUBLISH"],
    steward_authored_verdicts: list[ExemplarVerdict] | None = None,
) -> None:
    """Persist the gate result into `AiAssetVersion.evaluation_evidence` --
    reusing the exact field `ai_registry.compute_ai_trust_score` already
    reads `pass_rate`/`evidence_id` from, never a new column -- and record it
    into the existing audit trail via `record_audit` (never a parallel audit
    mechanism). `steward_authored_verdicts`, when given, replaces whatever
    was previously stored under `agent_eval_gate.steward_authored_verdicts`
    (the caller is expected to submit the *current* full set each time, per
    the module docstring -- not an incremental merge); omitted, the
    previously-stored set is carried forward unchanged, which is how the
    real publish-time check (no request body of its own to source new
    verdicts from) reuses whatever a steward most recently submitted via the
    preview/evaluate endpoint.
    """
    existing_gate = version.evaluation_evidence.get("agent_eval_gate")
    carried_forward = (
        existing_gate.get("steward_authored_verdicts", [])
        if isinstance(existing_gate, dict)
        else []
    )
    steward_payload = (
        [_verdict_payload(v) for v in steward_authored_verdicts]
        if steward_authored_verdicts is not None
        else carried_forward
    )
    version.evaluation_evidence = {
        **version.evaluation_evidence,
        "pass_rate": result.pass_rate if result.pass_rate is not None else 0.0,
        "evidence_id": f"agent-eval-gate-{result.evaluated_at.isoformat()}",
        "agent_eval_gate": {
            "verdict": result.verdict,
            "threshold": result.threshold,
            "minimum_exemplars": result.minimum_exemplars,
            "total_exemplars": result.total_exemplars,
            "passed_exemplars": result.passed_exemplars,
            "pass_rate": result.pass_rate,
            "failing_case_ids": list(result.failing_case_ids),
            "verdicts": [_verdict_payload(v) for v in result.verdicts],
            "steward_authored_verdicts": steward_payload,
            "reason": result.reason,
            "stage": stage,
            "evaluated_at": result.evaluated_at.isoformat(),
        },
    }
    record_audit(
        session,
        context,
        action="ai_registry.eval_gate.evaluate",
        resource_type="ai_asset_version",
        resource_id=str(version.id),
        outcome=result.verdict,
        correlation_id=get_correlation_id(),
        details={
            "stage": stage,
            "total_exemplars": result.total_exemplars,
            "passed_exemplars": result.passed_exemplars,
            "pass_rate": result.pass_rate,
            "threshold": result.threshold,
            "failing_case_ids": list(result.failing_case_ids),
        },
    )


# ---------------------------------------------------------------------------
# API shapes -- local `ApiModel`s, matching `agent_roster.py`'s (UX-19) own
# precedent of keeping request/response models next to the composition logic
# rather than in `aida.schemas`/`aida.platform_schemas` (both read-only for
# this row).
# ---------------------------------------------------------------------------


class AgentEvalGateVerdictRead(ApiModel):
    case_id: str
    source: ExemplarVerdictSource
    matched: bool
    drift: list[str]
    detail: str


class AgentEvalGateRead(ApiModel):
    """The gate's current state -- deliverable 3: what a steward reads,
    before ever attempting to publish, to see whether an agent would pass.
    Every contributing verdict is visible on `verdicts`, never collapsed to
    a bare boolean.
    """

    verdict: AgentEvalGateVerdict
    threshold: float
    minimum_exemplars: int
    total_exemplars: int
    passed_exemplars: int
    pass_rate: float | None
    failing_case_ids: list[str]
    verdicts: list[AgentEvalGateVerdictRead]
    reason: str
    evaluated_at: datetime


class AgentEvalGateVerdictInput(ApiModel):
    """One externally-computed `STEWARD_AUTHORED` replay verdict, submitted
    by a caller with a live-replay-capable environment (see module
    docstring) -- there is deliberately no `source` field here: every
    verdict submitted through this shape is forced to `STEWARD_AUTHORED` by
    the endpoint itself, so a caller can never inject a fabricated
    `CONFIRMED_RUN` verdict (that source is only ever produced by this
    module's own live database read).
    """

    case_id: str = Field(min_length=1, max_length=200)
    matched: bool
    drift: list[str] = Field(default_factory=list, max_length=100)
    detail: str = Field(default="", max_length=2000)


class AgentEvalGateEvaluateRequest(ApiModel):
    """Body of `POST .../eval-gate/evaluate`. `steward_authored_verdicts`
    is the caller's assertion of the *current, complete* externally-replayed
    corpus (see `record_agent_eval_gate_evidence`'s docstring: each call
    replaces, never merges, what was previously stored); omit it (or send an
    empty list) to evaluate the CONFIRMED_RUN corpus alone, or to
    deliberately clear a previously-submitted steward-authored set.
    """

    steward_authored_verdicts: list[AgentEvalGateVerdictInput] = Field(
        default_factory=list, max_length=1000
    )


def gate_result_read(result: AgentEvalGateResult) -> AgentEvalGateRead:
    return AgentEvalGateRead(
        verdict=result.verdict,
        threshold=result.threshold,
        minimum_exemplars=result.minimum_exemplars,
        total_exemplars=result.total_exemplars,
        passed_exemplars=result.passed_exemplars,
        pass_rate=result.pass_rate,
        failing_case_ids=list(result.failing_case_ids),
        verdicts=[
            AgentEvalGateVerdictRead(
                case_id=v.case_id,
                source=v.source,
                matched=v.matched,
                drift=list(v.drift),
                detail=v.detail,
            )
            for v in result.verdicts
        ],
        reason=result.reason,
        evaluated_at=result.evaluated_at,
    )


def steward_verdicts_from_input(
    items: list[AgentEvalGateVerdictInput],
) -> list[ExemplarVerdict]:
    """Convert submitted input rows into `ExemplarVerdict`s, forcing
    `source="STEWARD_AUTHORED"` unconditionally -- see
    `AgentEvalGateVerdictInput`'s own docstring for why.
    """
    return [
        ExemplarVerdict(
            case_id=item.case_id,
            source="STEWARD_AUTHORED",
            matched=item.matched,
            drift=tuple(item.drift),
            detail=item.detail,
        )
        for item in items
    ]
