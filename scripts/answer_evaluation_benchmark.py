#!/usr/bin/env python3
"""Answer evaluation over the enriched footprint (tracker R11-FP13, review F06.5).

**The live evaluation this harness exists to run has not been run.** Nothing in
this file's output is an answer-quality result until somebody runs it with
`--live`, which places paid model calls. Everything it reports in its default
mode is either a real, model-free measurement (clearly labelled as such) or an
explicit "not measured". A green test suite over this file is evidence the
harness works, not evidence that answers are correct -- the same standard the
2026-09-16 review applied to FP13 in the first place.

Why a third harness and not a fourth corpus in an existing one:

  - `quality_benchmark.py` (AG-8/R11-B2/R11-FP13's retrieval half) measures
    **rank**: does the enrichment put the right object in front of the model.
    It deliberately makes no network call, and its footprint section says in
    its own report that answer correctness is not measured there.
  - `execution_match_benchmark.py` (R11-B2) measures **result sets** from a
    live Ask against gold SQL on the `Customer Master` sample datasource. It
    has three gaps FP13 names: it runs over a *different* estate from the
    footprint corpus, so nothing it measures is "correctness over enriched
    context"; a refusal is only an outcome bucket (`NOT_ANSWERED`), never an
    expected verdict; and it captures no tokens at all.
  - `model_risk_benchmark.py` (AG-3/MG-5) measures **expected refusals**, but
    of the deterministic pre-retrieval classifier -- not of a model-generated
    answer over retrieved context.

This file is the join. One corpus
(`tests/fixtures/quality_benchmark_corpus/answer_evaluation_corpus.json`), run
over the *same* estate `footprint_enrichment_corpus.json` describes --
`quality_benchmark.seed_catalog` plus `quality_benchmark.enrich_footprint`,
imported rather than re-seeded, so the two corpora cannot drift onto different
catalogs -- carrying four expectations per case as first-class fields:

    expected_evidence / forbidden_evidence   the enriched-footprint objects the
                                             answer must and must not stand on
    expected_tables / forbidden_tables       the tables its SQL must and must
    expected_answer_contains / _excludes     not reference, and what its
                                             explanation must and must not say
    gold_sql                                 result-set equivalence, where the
                                             estate holds real rows
    expected_verdict + reason codes          ANSWERED or REFUSED, with the
                                             refusal's own code

Two modes, one scoring path
---------------------------

`score_case` is pure: a case plus an `AnswerObservation` in, an
`AnswerCaseResult` out. Every mode builds observations and hands them to it, so
the scoring, aggregation, threshold and reporting logic is exercised in full
without a provider:

  - **default (no network, no provider).** Seeds and enriches the catalog
    in-process and observes each case through the *real* code that can answer
    without a model: `GovernedRetriever.retrieve` for the evidence the answer
    would stand on, and `DeterministicPromptRiskClassifier` for the refusals
    that are decided pre-retrieval. Sub-scores that need a generated answer
    (tables, explanation text, result set) report `NOT_EVALUATED`, never a
    pass. This is what CI can run.
  - **`--live` (paid).** Asks each question through `/v1/datasources/{id}/
    agent-analyses` on a running deployment, the same route and the same `Api`
    client `execution_match_benchmark.py` uses, and scores every sub-score
    including tokens. Refuses to start without `--i-understand-this-costs-money`,
    so it cannot fire from CI, a test, or a mistyped flag.

Tokens, and the dollar figure this file will not print
------------------------------------------------------

Per case and in aggregate, from what the run itself reports
(`plan_evidence.model_call_evidence`), and the two kinds are kept apart the way
`agent_budget.py` and `models.AgentBudgetWindow` already keep them apart:
`provider_input_tokens`/`provider_output_tokens` are what the provider said it
billed, `estimated_*` are the gateway's 4-bytes-per-token heuristic, and the
charge is computed by importing `agent_orchestrator.run_token_charge` -- the
same function the runtime charges the agent's budget window with -- so this
harness cannot drift into its own accounting. `basis` says which figure a
number rests on, per case and for the total.

No dollar amount, deliberately. `cost_showback.py:1-28` states it plainly:
nothing in this codebase meters a real dollar cost, there is no billing
integration, and `plan_cost` is a connector-shaped proxy (bytes scanned for a
byte-billed engine, a planner score elsewhere) that is not comparable across
connectors. Multiplying provider tokens by a price list nobody reconciled
would turn a measurement into an invention, so "cost" here means tokens, said
as tokens.

Thresholds
----------

**Unset, and the domain owner's to set** (tracker row R11-FP13: "Needs the
domain owner: acceptance thresholds, which must be set before the answer-
correctness run"). `Docs/90-reference/answer-evaluation-baseline.json` carries
one `minimum` per tracked metric, every one `null`, plus the sign-off fields
that record who set them and when. A null threshold prints `no threshold set`
and cannot fail the run; this file never supplies a default pass mark. The
mechanism is real and is proven to fail a genuine drop
(`tests/test_answer_evaluation_gate.py`), so the day a minimum is filled in the
gate starts working with no code change.

Not wired into the blocking CI gate. With no thresholds signed off there is
nothing for CI to enforce, and adding it with invented numbers is exactly the
failure this row is open for.

Usage:
    # No network, no provider, no spend. Writes the results artifact.
    uv run python scripts/answer_evaluation_benchmark.py

    # Check the corpus resolves against the enriched catalog and stop.
    uv run python scripts/answer_evaluation_benchmark.py --check-corpus

    # The paid run, against a deployment with an approved model route.
    uv run python scripts/answer_evaluation_benchmark.py --live \
        --i-understand-this-costs-money --base-url http://localhost:8000 \
        --org sample-bank
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.execution_match_benchmark import results_match  # noqa: E402
from scripts.quality_benchmark import (  # noqa: E402
    SeededCatalog,
    _make_session,
    enrich_footprint,
    expected_object_id,
    seed_catalog,
)
from scripts.verify_end_to_end import Api, _items  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "quality_benchmark_corpus"
DEFAULT_CORPUS = CORPUS_DIR / "answer_evaluation_corpus.json"
DEFAULT_BASELINE = REPO_ROOT / "Docs" / "90-reference" / "answer-evaluation-baseline.json"
DEFAULT_REPORT = REPO_ROOT / "Docs" / "90-reference" / "answer-evaluation-results.md"

#: The identity and roles the live run asks as. An Analyst, because that is who
#: Ask is for and whose masking and row limits must apply to the answer being
#: scored -- scoring an answer produced under PlatformAdmin would measure a
#: posture no user of Ask ever gets.
ASKER = "answer-evaluation-benchmark"
ROLES = "Analyst"
MAX_ROWS = 500

#: Surfaced on every report rather than left in this docstring, the same way
#: `cost_showback.COST_BASIS` is surfaced on its own.
TOKEN_BASIS = (
    # noqa on the first fragment because that is where ruff reports S105: the
    # constant's name contains "TOKEN" and the rule cannot tell a basis
    # statement about token accounting from a hardcoded credential.
    "Tokens only; no dollar amount. `provider_reported_tokens` is what the "  # noqa: S105
    "provider said it billed for the attempt that answered (ProviderUsage, via the run's "
    "plan_evidence.model_call_evidence). `estimated_tokens` is the gateway's "
    "4-bytes-per-token heuristic across the attempt chain, which is the figure the agent "
    "contract's caps were checked against before the call. `charged_tokens` and `basis` come "
    "from agent_orchestrator.run_token_charge, the same function that reconciles "
    "AgentBudgetWindow, so this report and the budget window cannot disagree. The two kinds "
    "are not interchangeable and are never summed together into one number. No dollar cost is "
    "derived: per cost_showback.py this platform has no billing integration and no reconciled "
    "spend figure, and a price-list multiplication would be an invention, not a measurement."
)

# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """One object a corpus case names by its builder slug, never by raw id."""

    object_type: str
    object_key: str

    def __str__(self) -> str:
        return f"{self.object_type}:{self.object_key}"


@dataclass(frozen=True, slots=True)
class AnswerCase:
    id: str
    question: str
    expected_verdict: str  # ANSWERED | REFUSED
    expected_evidence: tuple[EvidenceRef, ...]
    forbidden_evidence: tuple[EvidenceRef, ...]
    expected_tables: tuple[str, ...]
    forbidden_tables: tuple[str, ...]
    expected_answer_contains: tuple[str, ...]
    expected_answer_excludes: tuple[str, ...]
    gold_sql: str | None
    expected_refusal_reason_code: str | None
    expected_risk_reason_code: str | None
    note: str = ""

    @property
    def expects_refusal(self) -> bool:
        return self.expected_verdict == "REFUSED"


VERDICTS = frozenset({"ANSWERED", "REFUSED"})


def _refs(raw: Any) -> tuple[EvidenceRef, ...]:
    return tuple(
        EvidenceRef(object_type=str(e["object_type"]), object_key=str(e["object_key"]))
        for e in (raw or [])
    )


def load_answer_corpus(path: Path) -> list[AnswerCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases: list[AnswerCase] = []
    for case in data["cases"]:
        verdict = str(case["expected_verdict"])
        if verdict not in VERDICTS:
            raise ValueError(f"case {case['id']}: expected_verdict {verdict!r} is not one of "
                             f"{sorted(VERDICTS)}")
        if verdict == "REFUSED" and not case.get("expected_refusal_reason_code"):
            # The whole point of gap 4: a refusal must be an expected verdict
            # with a code, not a bucket a run lands in.
            raise ValueError(
                f"case {case['id']}: a REFUSED case must name expected_refusal_reason_code"
            )
        cases.append(
            AnswerCase(
                id=str(case["id"]),
                question=str(case["question"]),
                expected_verdict=verdict,
                expected_evidence=_refs(case.get("expected_evidence")),
                forbidden_evidence=_refs(case.get("forbidden_evidence")),
                expected_tables=tuple(case.get("expected_tables") or []),
                forbidden_tables=tuple(case.get("forbidden_tables") or []),
                expected_answer_contains=tuple(case.get("expected_answer_contains") or []),
                expected_answer_excludes=tuple(case.get("expected_answer_excludes") or []),
                gold_sql=case.get("gold_sql"),
                expected_refusal_reason_code=case.get("expected_refusal_reason_code"),
                expected_risk_reason_code=case.get("expected_risk_reason_code"),
                note=str(case.get("note") or ""),
            )
        )
    return cases


def corpus_datasource_prefix(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data["datasource_name_prefix"])


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What one case's run reports it spent, with the two kinds kept apart.

    `provider_reported_tokens` is None when the provider reported nothing (a
    fixture provider, an answer without a usage block, or a run that never
    called a model). That is not zero, and treating it as zero is how an
    unbilled estimate gets quoted as billed spend.
    """

    estimated_input_tokens: int
    estimated_output_tokens: int
    provider_input_tokens: int | None
    provider_output_tokens: int | None
    charged_tokens: int
    estimated_tokens: int
    provider_reported_tokens: int | None
    basis: str
    model_route: str | None = None
    model_id: str | None = None


def token_usage_from_plan_evidence(plan_evidence: dict[str, Any] | None) -> TokenUsage | None:
    """Read the run's own token evidence, and charge it the way the runtime does.

    `run_token_charge` is imported from `agent_orchestrator` rather than
    reimplemented: it is the function that reconciles `AgentBudgetWindow`, and
    a benchmark that computed its own charge would eventually disagree with the
    budget window about the same run. Returns None when the run reports no
    model call at all -- a governed-tool answer, a refusal, or a cached one.
    """
    if not isinstance(plan_evidence, dict):
        return None
    evidence = plan_evidence.get("model_call_evidence")
    if not isinstance(evidence, dict):
        return None

    from aida.agent_orchestrator import run_token_charge
    from aida.model_gateway import ModelCallEvidence

    attempts = plan_evidence.get("model_call_attempts")
    attempt_count = len(attempts) if isinstance(attempts, list) and attempts else 1
    call = ModelCallEvidence(
        route=str(evidence.get("route") or ""),
        provider_type=str(evidence.get("provider_type") or ""),
        model_id=str(evidence.get("model_id") or ""),
        endpoint_alias=str(evidence.get("endpoint_alias") or ""),
        input_fingerprint=str(evidence.get("input_fingerprint") or ""),
        output_fingerprint=str(evidence.get("output_fingerprint") or ""),
        input_size_bytes=0,
        output_size_bytes=0,
        schema_name=str(evidence.get("schema_name") or ""),
        estimated_input_tokens=int(evidence.get("estimated_input_tokens") or 0),
        estimated_output_tokens=int(evidence.get("estimated_output_tokens") or 0),
        provider_input_tokens=evidence.get("provider_input_tokens"),
        provider_output_tokens=evidence.get("provider_output_tokens"),
    )
    charge = run_token_charge(call, attempt_count)
    return TokenUsage(
        estimated_input_tokens=call.estimated_input_tokens,
        estimated_output_tokens=call.estimated_output_tokens,
        provider_input_tokens=call.provider_input_tokens,
        provider_output_tokens=call.provider_output_tokens,
        charged_tokens=charge.charged,
        estimated_tokens=charge.estimated,
        provider_reported_tokens=charge.billed,
        basis=charge.basis,
        model_route=call.route or None,
        model_id=call.model_id or None,
    )


@dataclass(frozen=True, slots=True)
class TokenTotals:
    """The aggregate, with the same separation held at the total.

    `provider_reported_tokens` is None unless *every* case that called a model
    reported usage, because a partial sum presented as the total would
    understate spend by exactly the cases that reported nothing.
    """

    cases_with_model_call: int
    cases_with_provider_reported_usage: int
    estimated_tokens: int
    provider_reported_tokens: int | None
    charged_tokens: int
    basis: str

    @property
    def provider_reported_display(self) -> str:
        """Three different absences, said three different ways.

        No model call, a partial set of reports, and a real total are not the
        same thing, and printing "0" or a partial sum for either absence is how
        an unbilled run gets quoted as a cheap one.
        """
        if self.cases_with_model_call == 0:
            return "no model call placed"
        if self.provider_reported_tokens is None:
            return (
                f"not reported for every case ({self.cases_with_provider_reported_usage} of "
                f"{self.cases_with_model_call} reported); no total is given"
            )
        return str(self.provider_reported_tokens)


def total_tokens(usages: Iterable[TokenUsage | None]) -> TokenTotals:
    present = [u for u in usages if u is not None]
    reported = [u for u in present if u.provider_reported_tokens is not None]
    all_reported = bool(present) and len(reported) == len(present)
    return TokenTotals(
        cases_with_model_call=len(present),
        cases_with_provider_reported_usage=len(reported),
        estimated_tokens=sum(u.estimated_tokens for u in present),
        provider_reported_tokens=(
            sum(u.provider_reported_tokens or 0 for u in reported) if all_reported else None
        ),
        charged_tokens=sum(u.charged_tokens for u in present),
        basis=(
            "NO_MODEL_CALL"
            if not present
            else "PROVIDER_REPORTED"
            if all_reported
            else f"MIXED_{len(reported)}_OF_{len(present)}_CASES_PROVIDER_REPORTED"
        ),
    )


# ---------------------------------------------------------------------------
# Observation: what one attempt at a case actually produced
# ---------------------------------------------------------------------------

NOT_EVALUATED = "NOT_EVALUATED"


@dataclass(frozen=True, slots=True)
class AnswerObservation:
    """One case's outcome, from whichever source produced it.

    The single seam between transport and scoring. A live HTTP call, the
    offline no-provider path and a test stub all build one of these, so
    `score_case` and everything above it is exercised identically by all three
    and none of them needs a provider to prove the scoring is right.

    `answer_available` is the honest flag: False means no model produced an
    answer for this case, so every sub-score that reads a generated artifact is
    `None` rather than a pass. `rows` is held in memory for the comparison and
    never written to the report (ADR-0014).
    """

    source: str
    refused: bool
    refusal_reason_code: str | None = None
    risk_reason_code: str | None = None
    retrieved_objects: tuple[tuple[str, str], ...] = ()
    retrieved_display_names: tuple[tuple[str, str], ...] = ()
    answer_available: bool = False
    referenced_tables: tuple[str, ...] = ()
    answer_text: str = ""
    rows: list[dict[str, Any]] | None = None
    gold_rows: list[dict[str, Any]] | None = None
    tokens: TokenUsage | None = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Scoring -- pure, and the only place a case's verdict is decided
# ---------------------------------------------------------------------------


def _normalise_table(name: str) -> str:
    """Compare tables by their own name, not by how a dialect spelled the path.

    `referenced_tables` arrives as `public.fact_orders` from one connector and
    `warehouse.public.fact_orders` from another; a corpus should not have to
    encode that. The last dotted segment, case-folded and unquoted, is the
    table, and that is what is compared.
    """
    # Split first, then unquote: a quoted identifier keeps its quotes *around*
    # each path segment (`warehouse."public"."FACT_ORDERS"`), so stripping
    # before splitting leaves the inner quotes in the middle of the string and
    # the comparison silently never matches.
    return name.strip().rsplit(".", 1)[-1].strip().strip('"').strip("`").strip("[]").casefold()


def _normalise_key(name: str) -> str:
    """A display name reduced to the slug shape a corpus names an object by.

    `GovernedRetriever` returns a routine as `public.nightly_settlement_rollup`
    and an ontology concept as `End of day position`, while the corpus names
    both by the builder's slug. Dropping a schema prefix and folding spaces to
    underscores lets one comparison serve every object type and both estates.
    """
    return _normalise_table(name).replace(" ", "_").replace("-", "_")


def _evidence_present(
    ref: EvidenceRef,
    observation: AnswerObservation,
    resolved_ids: dict[str, str],
) -> bool:
    """By id where the estate's ids are known, by name where they are not.

    Offline, `expected_object_id` gives the deterministic id `seed_catalog`
    assigned and the match is exact. Against a live deployment the ids are that
    deployment's own, so the display name carries the identity instead. Both
    paths are here rather than in two harnesses.
    """
    resolved = resolved_ids.get(str(ref))
    for object_type, object_id in observation.retrieved_objects:
        if object_type == ref.object_type and resolved is not None and object_id == resolved:
            return True
    for object_type, display_name in observation.retrieved_display_names:
        if object_type == ref.object_type and _normalise_key(display_name) == _normalise_key(
            ref.object_key
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class AnswerCaseResult:
    """Every sub-score is a tri-state: passed, failed, or was not evaluated.

    `None` is load-bearing. It is what a sub-score reads when this mode could
    not measure it -- no model answered, or the case carries no gold SQL -- and
    it keeps that case out of the metric's denominator instead of counting an
    unmeasured expectation as met.
    """

    case: AnswerCase
    source: str
    verdict_correct: bool | None
    refusal_code_correct: bool | None
    risk_code_correct: bool | None
    evidence_supported: bool | None
    forbidden_evidence_absent: bool | None
    tables_correct: bool | None
    text_correct: bool | None
    result_set_correct: bool | None
    tokens: TokenUsage | None
    detail: str

    @property
    def scored(self) -> list[bool]:
        candidates = (
            self.verdict_correct,
            self.refusal_code_correct,
            self.risk_code_correct,
            self.evidence_supported,
            self.forbidden_evidence_absent,
            self.tables_correct,
            self.text_correct,
            self.result_set_correct,
        )
        return [c for c in candidates if c is not None]

    @property
    def outcome(self) -> str:
        """PASS only when something was measured and everything measured passed."""
        scored = self.scored
        if not scored:
            return NOT_EVALUATED
        return "PASS" if all(scored) else "FAIL"


def score_case(
    case: AnswerCase,
    observation: AnswerObservation,
    resolved_ids: dict[str, str] | None = None,
) -> AnswerCaseResult:
    """Score one observation against one case. No I/O, no provider, no state."""
    ids = resolved_ids or {}
    details: list[str] = []
    if observation.detail:
        details.append(observation.detail)

    # The verdict is settled by a refusal that happened or an answer that was
    # produced. A mode that produced neither can settle only one direction: a
    # case that *must* be refused and was not has failed, whatever a model
    # would have gone on to do. A case that must be answered and simply was not
    # attempted is unmeasured -- calling that a pass because nothing refused it
    # would let a run with no model route report a perfect verdict rate, which
    # is precisely the kind of claim FP13 was reopened over.
    verdict_correct: bool | None
    if observation.refused:
        verdict_correct = case.expects_refusal
    elif observation.answer_available:
        verdict_correct = not case.expects_refusal
    elif case.expects_refusal:
        verdict_correct = False
    else:
        verdict_correct = None
    if verdict_correct is False:
        details.append(
            f"expected {case.expected_verdict}, got "
            + ("REFUSED" if observation.refused else "no refusal")
        )

    refusal_code_correct: bool | None = None
    risk_code_correct: bool | None = None
    if case.expects_refusal and observation.refused:
        if case.expected_refusal_reason_code is not None:
            refusal_code_correct = (
                observation.refusal_reason_code == case.expected_refusal_reason_code
            )
            if not refusal_code_correct:
                details.append(
                    f"refusal code {observation.refusal_reason_code!r} != "
                    f"{case.expected_refusal_reason_code!r}"
                )
        if case.expected_risk_reason_code is not None and observation.risk_reason_code is not None:
            risk_code_correct = observation.risk_reason_code == case.expected_risk_reason_code
            if not risk_code_correct:
                details.append(
                    f"risk code {observation.risk_reason_code!r} != "
                    f"{case.expected_risk_reason_code!r}"
                )

    # Evidence is scored whenever context was retrieved at all, including on a
    # refusal -- but a refusal decided before retrieval has no context to score,
    # which is why this reads the observation rather than the verdict.
    have_context = bool(observation.retrieved_objects or observation.retrieved_display_names)
    evidence_supported: bool | None = None
    forbidden_evidence_absent: bool | None = None
    if have_context and case.expected_evidence:
        missing = [
            str(ref)
            for ref in case.expected_evidence
            if not _evidence_present(ref, observation, ids)
        ]
        evidence_supported = not missing
        if missing:
            details.append("expected evidence absent: " + ", ".join(missing))
    if have_context and case.forbidden_evidence:
        reached = [
            str(ref) for ref in case.forbidden_evidence if _evidence_present(ref, observation, ids)
        ]
        forbidden_evidence_absent = not reached
        if reached:
            details.append("unapproved evidence reached: " + ", ".join(reached))

    tables_correct: bool | None = None
    text_correct: bool | None = None
    result_set_correct: bool | None = None
    if observation.answer_available:
        referenced = {_normalise_table(t) for t in observation.referenced_tables}
        missing_tables = [t for t in case.expected_tables if _normalise_table(t) not in referenced]
        present_forbidden = [
            t for t in case.forbidden_tables if _normalise_table(t) in referenced
        ]
        if case.expected_tables or case.forbidden_tables:
            tables_correct = not missing_tables and not present_forbidden
            if missing_tables:
                details.append("answer did not reference: " + ", ".join(missing_tables))
            if present_forbidden:
                details.append("answer referenced forbidden: " + ", ".join(present_forbidden))
        text = observation.answer_text.casefold()
        missing_text = [s for s in case.expected_answer_contains if s.casefold() not in text]
        present_text = [s for s in case.expected_answer_excludes if s.casefold() in text]
        if case.expected_answer_contains or case.expected_answer_excludes:
            text_correct = not missing_text and not present_text
            if missing_text:
                details.append("answer text missing: " + ", ".join(missing_text))
            if present_text:
                details.append("answer text contains excluded: " + ", ".join(present_text))
        if case.gold_sql and observation.rows is not None and observation.gold_rows is not None:
            result_set_correct = results_match(observation.rows, observation.gold_rows)
            if not result_set_correct:
                # Shape only. Row values are compared in memory and never
                # reported (ADR-0014), the same discipline
                # `execution_match_benchmark.py` holds.
                details.append(
                    f"result set differs: {len(observation.rows)} rows vs gold "
                    f"{len(observation.gold_rows)}"
                )

    return AnswerCaseResult(
        case=case,
        source=observation.source,
        verdict_correct=verdict_correct,
        refusal_code_correct=refusal_code_correct,
        risk_code_correct=risk_code_correct,
        evidence_supported=evidence_supported,
        forbidden_evidence_absent=forbidden_evidence_absent,
        tables_correct=tables_correct,
        text_correct=text_correct,
        result_set_correct=result_set_correct,
        tokens=observation.tokens,
        detail="; ".join(details),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

TRACKED_METRICS = (
    "answer_verdict_pass_rate",
    "evidence_support_rate",
    "unapproved_evidence_avoidance_rate",
    "refusal_reason_code_pass_rate",
    "answer_table_pass_rate",
    "answer_text_pass_rate",
    "result_set_match_rate",
    "overall_case_pass_rate",
)

#: Metrics that read a *generated answer* and therefore cannot be measured
#: without a paid model call. Named rather than inferred, so the report can say
#: which of its own numbers are answer-correctness numbers -- the review's
#: complaint about FP13 was precisely that retrieval figures were standing in
#: for answer figures, and a metrics table that does not distinguish them
#: invites the same reading again.
LIVE_ONLY_METRICS = frozenset(
    {"answer_table_pass_rate", "answer_text_pass_rate", "result_set_match_rate"}
)


def _rate(flags: Iterable[bool | None]) -> float | None:
    """None when nothing was measured, so an unmeasured metric cannot read 0.0
    (a failure) or 1.0 (a pass). `quality_benchmark._rate` returns 0.0 for an
    empty sequence because every one of its cases is always measurable; here a
    whole metric can be honestly absent, and the difference matters."""
    values = [f for f in flags if f is not None]
    if not values:
        return None
    return sum(1 for v in values if v) / len(values)


@dataclass(frozen=True, slots=True)
class AnswerEvaluationReport:
    results: list[AnswerCaseResult]
    mode: str
    estate: str
    #: Why a whole section could not be measured in this mode, in the run's own
    #: words -- surfaced on the report instead of leaving a reader to infer it
    #: from a row of dashes.
    unmeasured_reason: str = ""

    @property
    def case_count(self) -> int:
        return len(self.results)

    @property
    def metrics(self) -> dict[str, float | None]:
        return {
            "answer_verdict_pass_rate": _rate(r.verdict_correct for r in self.results),
            "evidence_support_rate": _rate(r.evidence_supported for r in self.results),
            "unapproved_evidence_avoidance_rate": _rate(
                r.forbidden_evidence_absent for r in self.results
            ),
            "refusal_reason_code_pass_rate": _rate(r.refusal_code_correct for r in self.results),
            "answer_table_pass_rate": _rate(r.tables_correct for r in self.results),
            "answer_text_pass_rate": _rate(r.text_correct for r in self.results),
            "result_set_match_rate": _rate(r.result_set_correct for r in self.results),
            "overall_case_pass_rate": _rate(
                None if r.outcome == NOT_EVALUATED else r.outcome == "PASS" for r in self.results
            ),
        }

    @property
    def case_counts(self) -> dict[str, int]:
        return {
            "answer_verdict_pass_rate": sum(
                1 for r in self.results if r.verdict_correct is not None
            ),
            "evidence_support_rate": sum(
                1 for r in self.results if r.evidence_supported is not None
            ),
            "unapproved_evidence_avoidance_rate": sum(
                1 for r in self.results if r.forbidden_evidence_absent is not None
            ),
            "refusal_reason_code_pass_rate": sum(
                1 for r in self.results if r.refusal_code_correct is not None
            ),
            "answer_table_pass_rate": sum(1 for r in self.results if r.tables_correct is not None),
            "answer_text_pass_rate": sum(1 for r in self.results if r.text_correct is not None),
            "result_set_match_rate": sum(
                1 for r in self.results if r.result_set_correct is not None
            ),
            "overall_case_pass_rate": sum(
                1 for r in self.results if r.outcome != NOT_EVALUATED
            ),
        }

    @property
    def tokens(self) -> TokenTotals:
        return total_tokens(r.tokens for r in self.results)

    @property
    def not_evaluated(self) -> list[AnswerCaseResult]:
        return [r for r in self.results if r.outcome == NOT_EVALUATED]

    @property
    def failed(self) -> list[AnswerCaseResult]:
        return [r for r in self.results if r.outcome == "FAIL"]


def run_evaluation(
    cases: Sequence[AnswerCase],
    observe: Callable[[AnswerCase], AnswerObservation],
    *,
    mode: str,
    estate: str,
    resolved_ids: dict[str, str] | None = None,
    unmeasured_reason: str = "",
) -> AnswerEvaluationReport:
    """The one loop. Every mode -- offline, live, and a test's stub -- passes its
    own `observe` and gets the same scoring, aggregation and reporting."""
    return AnswerEvaluationReport(
        results=[score_case(case, observe(case), resolved_ids) for case in cases],
        mode=mode,
        estate=estate,
        unmeasured_reason=unmeasured_reason,
    )


# ---------------------------------------------------------------------------
# Thresholds -- the mechanism, with the values left to the domain owner
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThresholdFailure:
    name: str
    minimum: float
    measured: float

    def __str__(self) -> str:
        return (
            f"[BELOW THRESHOLD] {self.name}: {self.measured:.4f} < "
            f"minimum {self.minimum:.4f}"
        )


@dataclass(frozen=True, slots=True)
class Thresholds:
    minimums: dict[str, float | None]
    signed_off_by: str | None
    signed_off_at: str | None

    @property
    def any_set(self) -> bool:
        return any(v is not None for v in self.minimums.values())


def load_thresholds(path: Path) -> Thresholds:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("thresholds") or {}
    minimums: dict[str, float | None] = {}
    for name in TRACKED_METRICS:
        entry = raw.get(name) or {}
        minimum = entry.get("minimum")
        minimums[name] = None if minimum is None else float(minimum)
    sign_off = data.get("sign_off") or {}
    return Thresholds(
        minimums=minimums,
        signed_off_by=sign_off.get("signed_off_by"),
        signed_off_at=sign_off.get("signed_off_at"),
    )


def check_thresholds(
    metrics: dict[str, float | None], thresholds: Thresholds
) -> list[ThresholdFailure]:
    """Only a metric that was measured against a minimum somebody set can fail.

    An unset minimum cannot be breached, and an unmeasured metric cannot breach
    one -- neither is silently treated as a pass either: the caller reports both
    states by name.
    """
    failures: list[ThresholdFailure] = []
    for name, minimum in thresholds.minimums.items():
        measured = metrics.get(name)
        if minimum is None or measured is None:
            continue
        if measured < minimum:
            failures.append(ThresholdFailure(name=name, minimum=minimum, measured=measured))
    return failures


def _write_baseline(path: Path, report: AnswerEvaluationReport, thresholds: Thresholds) -> None:
    """Record this run's measurements beside the thresholds, never as them.

    `metrics` is what was measured; `thresholds` is what somebody decided is
    acceptable. Writing a measurement into a threshold is how a benchmark
    starts grading itself, so this keeps them in separate objects and leaves
    every `minimum` exactly as the file already had it.
    """
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    metrics = report.metrics
    counts = report.case_counts
    payload = {
        **existing,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": report.mode,
        "estate": report.estate,
        "corpus_cases": report.case_count,
        "token_basis": TOKEN_BASIS,
        "thresholds": {
            name: {
                "minimum": thresholds.minimums.get(name),
                "note": ((existing.get("thresholds") or {}).get(name) or {}).get("note", ""),
            }
            for name in TRACKED_METRICS
        },
        "sign_off": existing.get("sign_off")
        or {"signed_off_by": None, "signed_off_at": None, "note": ""},
        "metrics": {
            name: {
                "value": None if metrics.get(name) is None else round(metrics[name] or 0.0, 4),
                "case_count": counts.get(name, 0),
            }
            for name in TRACKED_METRICS
        },
        "tokens": {
            "cases_with_model_call": report.tokens.cases_with_model_call,
            "cases_with_provider_reported_usage": (
                report.tokens.cases_with_provider_reported_usage
            ),
            "estimated_tokens": report.tokens.estimated_tokens,
            "provider_reported_tokens": report.tokens.provider_reported_tokens,
            "charged_tokens": report.tokens.charged_tokens,
            "basis": report.tokens.basis,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Offline observation: real code, no provider, no network
# ---------------------------------------------------------------------------

OFFLINE_SOURCE = "OFFLINE_NO_PROVIDER"
OFFLINE_UNMEASURED = (
    "This run placed no model call, so no generated answer existed to score. The tables an "
    "answer references, the text it produces and its result set are reported NOT_EVALUATED, "
    "not as passes. Evidence support is measured for real -- the retrieval that would feed the "
    "model ran -- and the refusals decided before retrieval are measured for real too."
)


@dataclass
class _OfflineObserver:
    """Observes each case through the real code that can answer without a model.

    Two real components, no stand-ins: `GovernedRetriever.retrieve` produces
    the context an answer would stand on, and
    `DeterministicPromptRiskClassifier` produces the refusals the platform
    decides before retrieval. What needs a model is reported absent.
    """

    hits_by_question: dict[str, tuple[tuple[str, str, str], ...]]
    risk_by_question: dict[str, tuple[str, list[str]]]

    def __call__(self, case: AnswerCase) -> AnswerObservation:
        decision, reason_codes = self.risk_by_question[case.question]
        if decision == "BLOCK":
            return AnswerObservation(
                source=OFFLINE_SOURCE,
                refused=True,
                refusal_reason_code="PROMPT_POLICY_DENIED",
                risk_reason_code=reason_codes[0] if reason_codes else None,
                detail="refused by the pre-retrieval prompt-risk screen",
            )
        hits = self.hits_by_question[case.question]
        return AnswerObservation(
            source=OFFLINE_SOURCE,
            refused=False,
            retrieved_objects=tuple((t, i) for t, i, _ in hits),
            retrieved_display_names=tuple((t, n) for t, _, n in hits),
            answer_available=False,
            detail="no model call placed",
        )


async def observe_offline(
    cases: Sequence[AnswerCase],
) -> tuple[_OfflineObserver, dict[str, str], str]:
    """Seed, enrich, retrieve and screen -- then hand back an observer.

    The estate is `quality_benchmark`'s, imported: `seed_catalog` then
    `enrich_footprint`, the same two calls `run_footprint_benchmark` makes for
    its `after` run. That is what makes this "over the enriched footprint"
    rather than over a catalog that merely resembles it.
    """
    from aida.agent_intelligence import GovernedRetriever
    from aida.config import get_settings
    from aida.models import DataSource
    from aida.prompt_risk import DeterministicPromptRiskClassifier

    session, engine = await _make_session()
    try:
        catalog = await seed_catalog(session)
        await enrich_footprint(session, catalog)
        datasource = await session.get(DataSource, catalog.datasource_id)
        if datasource is None:  # pragma: no cover - seed_catalog guarantees it
            raise RuntimeError("seeded datasource missing -- seed_catalog did not commit")
        retriever = GovernedRetriever(get_settings())
        classifier = DeterministicPromptRiskClassifier()
        hits_by_question: dict[str, tuple[tuple[str, str, str], ...]] = {}
        risk_by_question: dict[str, tuple[str, list[str]]] = {}
        for case in cases:
            assessment = classifier.assess(case.question)
            risk_by_question[case.question] = (assessment.decision, list(assessment.reason_codes))
            if assessment.decision == "BLOCK":
                # The live platform blocks before retrieval, so retrieving here
                # would measure a path no run takes.
                hits_by_question[case.question] = ()
                continue
            hits = await retriever.retrieve(
                session, datasource=datasource, question=case.question
            )
            hits_by_question[case.question] = tuple(
                (hit.object_type, hit.object_id, hit.display_name) for hit in hits
            )
        resolved = resolve_expected_ids(cases, catalog)
        estate = str(datasource.name)
    finally:
        await session.close()
        await engine.dispose()
    return (
        _OfflineObserver(hits_by_question=hits_by_question, risk_by_question=risk_by_question),
        resolved,
        estate,
    )


def resolve_expected_ids(
    cases: Sequence[AnswerCase], catalog: SeededCatalog
) -> dict[str, str]:
    """`EvidenceRef` -> the id the enriched catalog gave that object.

    Raises on an unresolvable slug rather than skipping it: a case naming an
    object the enriched estate does not build is a corpus defect, and silently
    dropping it would turn that defect into a pass.
    """
    resolved: dict[str, str] = {}
    for case in cases:
        for ref in (*case.expected_evidence, *case.forbidden_evidence):
            resolved[str(ref)] = expected_object_id(catalog, ref.object_type, ref.object_key)
    return resolved


# ---------------------------------------------------------------------------
# Live observation: the paid path
# ---------------------------------------------------------------------------

LIVE_SOURCE = "LIVE_ASK"

#: HTTP status -> the stable refusal code the platform persists for it. The Ask
#: route returns prose for a prompt-policy block (`api.py`'s 422 carries
#: `str(AgentPolicyRejected)`), while the audit row carries the stable
#: `PROMPT_POLICY_DENIED`; a structured 409 carries its own `detail.code`. This
#: mapping is the one place that difference is reconciled, so a corpus can name
#: a stable code and a scored run can find it.
REFUSAL_CODE_BY_STATUS = {
    503: "MODEL_ROUTE_UNAVAILABLE",
    429: "MODEL_PROVIDER_THROTTLED",
}
#: Stable codes the 422 path returns verbatim rather than as prose.
STRUCTURED_422_CODES = frozenset(
    {
        "CONTEXT_PRODUCT_NOT_AVAILABLE",
        "CONTEXT_PRODUCT_CONSUMER_ROLE_REQUIRED",
        "CONTEXT_PRODUCT_TABLE_OUT_OF_SCOPE",
        "agent_daily_token_cap_exhausted",
        "agent_per_run_token_cap_exceeded",
        "agent_wall_clock_cap_exceeded",
    }
)


def refusal_code_from_response(status: int, payload: Any) -> str:
    """The stable refusal code for a non-200 Ask response.

    Pure, and tested without a deployment: the mapping from what the route
    returns to the code a corpus names is exactly the kind of logic that is
    wrong until something exercises it, and it must not need a paid call to be
    exercised.
    """
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict) and detail.get("code"):
        return str(detail["code"])
    if status in REFUSAL_CODE_BY_STATUS:
        return REFUSAL_CODE_BY_STATUS[status]
    if isinstance(detail, str):
        stripped = detail.strip()
        if stripped in STRUCTURED_422_CODES:
            return stripped
        if "prompt safety" in stripped.casefold():
            return "PROMPT_POLICY_DENIED"
        if stripped.startswith("QUALITY_INCIDENT_BLOCK"):
            return "QUALITY_INCIDENT_BLOCK"
    return f"UNMAPPED_HTTP_{status}"


@dataclass
class _LiveObserver:
    """Asks each question through Ask on a running deployment.

    Every call here is a paid model call. Nothing constructs this except the
    explicit `--live --i-understand-this-costs-money` path.
    """

    api: Api
    org_id: str
    ds_id: str

    def _gold_rows(self, case: AnswerCase) -> list[dict[str, Any]] | None:
        if not case.gold_sql:
            return None
        status, gold = self.api.call(
            "POST",
            f"/v1/datasources/{self.ds_id}/query-executions",
            body={"sql": case.gold_sql, "max_rows": MAX_ROWS},
            principal=ASKER,
            roles=ROLES,
            org_id=self.org_id,
        )
        if status != 200 or not isinstance(gold, dict):
            return None
        return list(gold.get("rows") or [])

    def __call__(self, case: AnswerCase) -> AnswerObservation:
        gold_rows = self._gold_rows(case)
        status, answer = self.api.call(
            "POST",
            f"/v1/datasources/{self.ds_id}/agent-analyses",
            body={"question": case.question, "max_rows": MAX_ROWS},
            principal=ASKER,
            roles=ROLES,
            org_id=self.org_id,
            timeout=180,
        )
        if status != 200 or not isinstance(answer, dict):
            return AnswerObservation(
                source=LIVE_SOURCE,
                refused=True,
                refusal_reason_code=refusal_code_from_response(status, answer),
                gold_rows=gold_rows,
                detail=f"Ask HTTP {status}",
            )
        return live_observation(answer, gold_rows=gold_rows)


def live_observation(
    answer: dict[str, Any], *, gold_rows: list[dict[str, Any]] | None = None
) -> AnswerObservation:
    """Project a 200 Ask response into an observation.

    Split out from `_LiveObserver` so the projection -- which is where a field
    rename in the response would silently stop scoring something -- is testable
    against a recorded payload without a deployment or a provider.
    """
    execution = answer.get("execution") or {}
    plan_evidence = answer.get("plan_evidence")
    retrieval = answer.get("retrieval_evidence") or []
    objects: list[tuple[str, str]] = []
    names: list[tuple[str, str]] = []
    if isinstance(retrieval, list):
        for hit in retrieval:
            if not isinstance(hit, dict):
                continue
            object_type = str(hit.get("object_type") or "")
            objects.append((object_type, str(hit.get("object_id") or "")))
            names.append((object_type, str(hit.get("display_name") or "")))
    rows = list(execution.get("rows") or []) if isinstance(execution, dict) else []
    answer_text = " ".join(
        str(part)
        for part in (
            answer.get("explanation"),
            execution.get("normalized_sql") if isinstance(execution, dict) else None,
        )
        if part
    )
    return AnswerObservation(
        source=LIVE_SOURCE,
        refused=False,
        retrieved_objects=tuple(objects),
        retrieved_display_names=tuple(names),
        answer_available=True,
        referenced_tables=tuple(
            str(t) for t in (execution.get("referenced_tables") or [])
        )
        if isinstance(execution, dict)
        else (),
        answer_text=answer_text,
        rows=rows,
        gold_rows=gold_rows,
        tokens=token_usage_from_plan_evidence(
            plan_evidence if isinstance(plan_evidence, dict) else None
        ),
        detail=f"generation_source={answer.get('generation_source')}",
    )


def resolve_live_estate(api: Api, org_slug: str, prefix: str) -> tuple[str, str, str]:
    """(org_id, datasource_id, datasource name) for the estate the corpus names."""
    status, orgs = api.call("GET", "/v1/organizations?limit=200", roles="PlatformAdmin")
    org = next((o for o in _items(orgs) if o.get("slug") == org_slug), None)
    if status != 200 or org is None:
        raise SystemExit(
            f"organization {org_slug!r} not found (HTTP {status}); seed the estate first"
        )
    org_id = str(org["id"])
    status, sources = api.call(
        "GET",
        f"/v1/organizations/{org_id}/datasources?limit=50",
        org_id=org_id,
        roles="PlatformAdmin",
    )
    source = next(
        (d for d in _items(sources) if str(d.get("name", "")).startswith(prefix)), None
    )
    if source is None:
        raise SystemExit(
            f"no datasource named {prefix!r}... in {org_slug!r}. This corpus scores answers "
            "over the *enriched* footprint (routines with reviewed lineage, a published "
            "ontology concept mapped to a table); an estate without those objects cannot "
            "answer its questions, and scoring it would measure something else."
        )
    return org_id, str(source["id"]), str(source["name"])


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

NOT_RUN_BANNER = (
    "**The live, paid answer evaluation has not been run.** The numbers in this file come "
    "from the no-provider mode described below. Until the command in "
    "[How to run the live evaluation](#how-to-run-the-live-evaluation) has been run and this "
    "file regenerated from it, nothing here is a claim about the correctness of "
    "model-generated answers, and a green `tests/test_answer_evaluation_gate.py` is evidence "
    "that the harness works rather than evidence that answers are right."
)


def _metric_row(name: str, report: AnswerEvaluationReport, thresholds: Thresholds) -> str:
    value = report.metrics.get(name)
    count = report.case_counts.get(name, 0)
    minimum = thresholds.minimums.get(name)
    measured = "not measured" if value is None else f"{value:.4f}"
    threshold = "no threshold set" if minimum is None else f"{minimum:.4f}"
    if value is None or minimum is None:
        verdict = "—"
    else:
        verdict = "pass" if value >= minimum else "**BELOW THRESHOLD**"
    reads = "generated answer" if name in LIVE_ONLY_METRICS else "retrieved context / verdict"
    return f"| `{name}` | {reads} | {measured} | {count} | {threshold} | {verdict} |"


def _write_report(
    path: Path,
    report: AnswerEvaluationReport,
    thresholds: Thresholds,
    failures: list[ThresholdFailure],
    *,
    live: bool,
) -> None:
    tokens = report.tokens
    lines: list[str] = []
    lines.append("# Answer evaluation over the enriched footprint (R11-FP13)")
    lines.append("")
    lines.append(
        f"Generated {datetime.now(UTC).isoformat()} by `scripts/answer_evaluation_benchmark.py` "
        f"in **{report.mode}** mode against **{report.estate}**, over "
        f"`tests/fixtures/quality_benchmark_corpus/answer_evaluation_corpus.json` "
        f"({report.case_count} cases)."
    )
    lines.append("")
    if not live:
        lines.append(NOT_RUN_BANNER)
        lines.append("")
    lines.append("## What this measures, and what FP13 asks for")
    lines.append("")
    lines.append(
        "R11-FP13's retrieval half (`scripts/quality_benchmark.py`, "
        "`footprint_enrichment_corpus.json`) measures whether enrichment puts the right "
        "object in front of the model -- rank, not answers. This file measures the answer, "
        "over the same estate: `quality_benchmark.seed_catalog` plus "
        "`quality_benchmark.enrich_footprint`, imported rather than re-seeded, so the two "
        "corpora cannot drift onto different catalogs. `execution_match_benchmark.py` scores "
        "result sets on a different datasource, treats a refusal as an outcome bucket rather "
        "than an expected verdict, and captures no tokens; this corpus carries expected "
        "evidence, an expected answer, an expected refusal with its reason code, and token "
        "capture per case."
    )
    lines.append("")
    if report.unmeasured_reason:
        lines.append(f"**Not measured in this mode.** {report.unmeasured_reason}")
        lines.append("")

    lines.append("## Metrics against the signed-off thresholds")
    lines.append("")
    lines.append(
        "The `Reads` column is there so no number here can be mistaken for another. Only the "
        "three metrics reading a **generated answer** are answer-correctness metrics; the "
        "rest score the retrieved context and the answered/refused verdict, which is a "
        "different claim and the one FP13 already had."
    )
    lines.append("")
    lines.append("| Metric | Reads | Measured | Cases measured | Threshold | Verdict |")
    lines.append("|---|---|---|---|---|---|")
    for name in TRACKED_METRICS:
        lines.append(_metric_row(name, report, thresholds))
    lines.append("")
    if thresholds.any_set:
        lines.append(
            f"Thresholds signed off by {thresholds.signed_off_by or 'nobody recorded'} "
            f"at {thresholds.signed_off_at or 'no recorded date'}."
        )
    else:
        lines.append(
            "**No acceptance thresholds are set.** Every `minimum` in "
            "`Docs/90-reference/answer-evaluation-baseline.json` is `null`, and this harness "
            "does not supply a default: the tracker records acceptance thresholds as the "
            "domain owner's decision, to be made before the answer-correctness run. A metric "
            "with no threshold cannot pass or fail, and this run reports `no threshold set` "
            "rather than inventing a pass mark. Filling in a `minimum` and the `sign_off` "
            "block is all that is needed to make the gate enforce it."
        )
    lines.append("")

    lines.append("## Per case")
    lines.append("")
    lines.append(
        "| Case | Expected | Outcome | Verdict | Refusal code | Evidence | Gap kept | "
        "Tables | Text | Result set |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in report.results:
        lines.append(
            f"| {r.case.id} | {r.case.expected_verdict} | {r.outcome} | "
            f"{_tri(r.verdict_correct)} | {_tri(r.refusal_code_correct)} | "
            f"{_tri(r.evidence_supported)} | {_tri(r.forbidden_evidence_absent)} | "
            f"{_tri(r.tables_correct)} | {_tri(r.text_correct)} | "
            f"{_tri(r.result_set_correct)} |"
        )
    lines.append("")
    detailed = [r for r in report.results if r.detail]
    if detailed:
        lines.append("| Case | Detail |")
        lines.append("|---|---|")
        for r in detailed:
            lines.append(f"| {r.case.id} | {r.detail} |")
        lines.append("")

    lines.append("## Tokens")
    lines.append("")
    lines.append("| Measure | Value |")
    lines.append("|---|---|")
    lines.append(f"| Cases that called a model | {tokens.cases_with_model_call} |")
    lines.append(
        f"| Cases whose provider reported usage | {tokens.cases_with_provider_reported_usage} |"
    )
    lines.append(f"| Estimated tokens (gateway heuristic) | {tokens.estimated_tokens} |")
    lines.append(f"| Provider-reported tokens | {tokens.provider_reported_display} |")
    lines.append(f"| Charged against the agent budget window | {tokens.charged_tokens} |")
    lines.append(f"| Basis | `{tokens.basis}` |")
    lines.append("")
    lines.append(TOKEN_BASIS)
    lines.append("")
    if tokens.cases_with_model_call:
        lines.append("| Case | Estimated | Provider-reported | Charged | Basis | Model |")
        lines.append("|---|---|---|---|---|---|")
        for r in report.results:
            if r.tokens is None:
                continue
            reported = (
                "not reported"
                if r.tokens.provider_reported_tokens is None
                else str(r.tokens.provider_reported_tokens)
            )
            lines.append(
                f"| {r.case.id} | {r.tokens.estimated_tokens} | {reported} | "
                f"{r.tokens.charged_tokens} | `{r.tokens.basis}` | "
                f"{r.tokens.model_id or 'n/a'} |"
            )
        lines.append("")

    lines.append("## How to run the live evaluation")
    lines.append("")
    lines.append(
        "Every case is a paid model call. The run needs a deployment with an approved, "
        "selected, credentialed model route (module 15's five conditions) and a datasource "
        "carrying the enriched footprint the corpus names -- the routines with reviewed "
        "lineage and the published ontology concept mapped to a table. Then:"
    )
    lines.append("")
    lines.append("```")
    lines.append("uv run python scripts/answer_evaluation_benchmark.py \\")
    lines.append("    --live --i-understand-this-costs-money \\")
    lines.append("    --base-url http://localhost:8000 --org sample-bank")
    lines.append("```")
    lines.append("")
    lines.append(
        f"That is {report.case_count} Ask calls, one per corpus case, of which the "
        f"{sum(1 for r in report.results if not r.case.expects_refusal)} non-refusal cases "
        "reach a provider; the "
        f"{sum(1 for r in report.results if r.case.expects_refusal)} refusal cases are "
        "decided by the deterministic pre-retrieval screen and cost nothing. Cases carrying "
        "`gold_sql` add one governed query execution each, which is a warehouse query rather "
        "than a model call. The run prints per-case and total tokens on the basis each rests "
        "on, and regenerates this file."
    )
    lines.append("")
    if failures:
        lines.append("## Metrics below a signed-off threshold")
        lines.append("")
        for f in failures:
            lines.append(f"- {f}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tri(value: bool | None) -> str:
    if value is None:
        return "—"
    return "yes" if value else "**no**"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

COST_REFUSAL = (
    "--live places one paid model call per non-refusal corpus case. Re-run with "
    "--i-understand-this-costs-money to confirm you mean to spend money, or drop --live to "
    "run the no-provider mode."
)


def force_offline_provider() -> None:
    """Make the offline mode's "no provider, no network" promise true.

    It was an assumption, and on a configured machine it was false.
    `observe_offline` builds a `GovernedRetriever` from `get_settings()`, which
    reads `.env`; with an embedding provider set there, retrieval embeds each
    candidate live. A run on 2026-09-17 made about five `gemini-embedding-001`
    requests while printing `no provider, no network` and reporting 0 tokens --
    a harness misreporting its own network use, on the one measurement whose
    whole value is being believed. The gate tests never hit it because they
    force the provider off (`tests/test_answer_evaluation_gate.py`); the CLI did
    not, so the two disagreed about what "offline" meant.

    An environment variable outranks `.env` in pydantic-settings' source order,
    and `get_settings` is cached, so this sets the one and clears the other.
    Only the offline branch calls it: `--live` is the path that is *meant* to
    reach a provider, and it is gated behind its own explicit flag.
    """
    os.environ["AIDA_EMBEDDING_PROVIDER"] = "unset"
    from aida.config import get_settings

    get_settings.cache_clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-report", action="store_true", help="Do not write the report.")
    parser.add_argument(
        "--record",
        action="store_true",
        help=(
            "Write this run's measurements into the baseline file's `metrics` object. Never "
            "touches `thresholds` or `sign_off`: a measurement is not an acceptance criterion."
        ),
    )
    parser.add_argument(
        "--check-corpus",
        action="store_true",
        help=(
            "Resolve every corpus case against the enriched catalog and exit. Proves each "
            "expected-evidence slug names an object the enriched estate actually builds."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Ask each question through a running deployment. PLACES PAID MODEL CALLS.",
    )
    parser.add_argument(
        "--i-understand-this-costs-money",
        dest="confirm_cost",
        action="store_true",
        help="Required with --live. Without it, --live refuses to start.",
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--org", default="sample-bank")
    args = parser.parse_args(argv)

    cases = load_answer_corpus(args.corpus)
    thresholds = (
        load_thresholds(args.baseline)
        if args.baseline.exists()
        else Thresholds(
            minimums=dict.fromkeys(TRACKED_METRICS), signed_off_by=None, signed_off_at=None
        )
    )

    if args.check_corpus:
        observer, resolved, estate = asyncio.run(observe_offline(cases))
        print(f"{len(cases)} cases resolve against the enriched catalog {estate!r}:")
        for ref, object_id in sorted(resolved.items()):
            print(f"  {ref} -> {object_id}")
        return 0

    if args.live and not args.confirm_cost:
        print(f"::error::{COST_REFUSAL}")
        return 2

    if args.live:
        api = Api(args.base_url)
        prefix = corpus_datasource_prefix(args.corpus)
        org_id, ds_id, estate = resolve_live_estate(api, args.org, prefix)
        print(f"Answer evaluation (LIVE, paid) against {estate}")
        report = run_evaluation(
            cases,
            _LiveObserver(api=api, org_id=org_id, ds_id=ds_id),
            mode="LIVE_ASK",
            estate=estate,
        )
    else:
        force_offline_provider()
        observer, resolved, estate = asyncio.run(observe_offline(cases))
        print(f"Answer evaluation (no provider, no network) against {estate}")
        report = run_evaluation(
            cases,
            observer,
            mode=OFFLINE_SOURCE,
            estate=estate,
            resolved_ids=resolved,
            unmeasured_reason=OFFLINE_UNMEASURED,
        )

    for r in report.results:
        suffix = f" -- {r.detail}" if r.detail else ""
        print(f"  [{r.outcome:<13}] {r.case.id}{suffix}")

    print("\nMetrics:")
    metrics = report.metrics
    counts = report.case_counts
    for name in TRACKED_METRICS:
        value = metrics.get(name)
        minimum = thresholds.minimums.get(name)
        measured = "not measured" if value is None else f"{value:.4f}"
        bound = "no threshold set" if minimum is None else f"minimum {minimum:.4f}"
        print(f"  {name:<38} {measured:<13} ({counts.get(name, 0)} cases; {bound})")

    tokens = report.tokens
    print("\nTokens:")
    print(f"  cases that called a model:        {tokens.cases_with_model_call}")
    print(f"  provider reported usage for:      {tokens.cases_with_provider_reported_usage}")
    print(f"  estimated (gateway heuristic):    {tokens.estimated_tokens}")
    print(f"  provider-reported:                {tokens.provider_reported_display}")
    print(f"  charged to the budget window:     {tokens.charged_tokens} ({tokens.basis})")
    print("  no dollar figure is derived; see TOKEN_BASIS and src/aida/cost_showback.py")

    failures = check_thresholds(metrics, thresholds)

    if not args.no_report:
        _write_report(args.report, report, thresholds, failures, live=args.live)
        print(f"\nReport written to {args.report}.")
    if args.record:
        _write_baseline(args.baseline, report, thresholds)
        print(f"Measurements recorded in {args.baseline} (thresholds untouched).")

    if not args.live:
        print(
            "\n::notice::The live, paid answer evaluation has NOT been run. This was the "
            "no-provider mode: evidence support and the pre-retrieval refusals are real "
            "measurements; answer tables, text and result sets were not evaluated."
        )
    if not thresholds.any_set:
        print(
            "::notice::No acceptance thresholds are set, so this run cannot pass or fail on "
            f"answer quality. Set a `minimum` and the `sign_off` block in {args.baseline}; "
            "the tracker records this as the domain owner's decision."
        )
    if failures:
        print(f"\n{len(failures)} metric(s) below a signed-off threshold:")
        for f in failures:
            print(f)
        print("\n::error::Answer evaluation below the signed-off acceptance thresholds.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
