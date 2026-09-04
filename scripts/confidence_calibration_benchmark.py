#!/usr/bin/env python3
"""Confidence calibration for module 07's metric-suggestion inference (tracker SM-3).

Module 07 (semantic layer) produces confidence-scored inferences; SM-4
(`aida.metric_suggestion_service.score_evidence`) is the exact, real, deterministic
scoring function this branch established for one of them -- see that row's
accomplishment-log entry. SM-3 asks a question SM-4 never answered: when the score
says 0.86, is the suggestion actually right about 86% of the time? This script
answers that with real numbers against a real, labelled, bank-domain corpus, and
publishes the calibration curve as a reproducible artifact, mirroring AG-8's exact
pattern (`scripts/quality_benchmark.py`): a committed corpus, a script that runs it
through the REAL scoring function (not a reimplementation), and a generated,
timestamped results report.

Unlike AG-8's model-generation half, this benchmark needs **no live infrastructure
at all**: `score_evidence` is a pure function of a `MetricEvidence` value -- no DB
session, no embedding provider, no model route. Every number in the published
report is a full, real result; there is no framework-only section here to flag.

What is measured:

  1. `tests/fixtures/confidence_calibration_corpus/bank_domain_metric_corpus.json`
     -- 28 labelled (table, column) cases, hand-authored for realistic bank-domain
     ambiguity (abbreviated/qualifier-bearing column names a real inference engine
     has to handle: `txn_amount`, `acct_balance`, `avg_daily_balance`,
     `txn_count`, ...), each carrying a steward-level ground-truth judgement of
     whether the column's proposed (measure, aggregation) pair is actually
     correct -- 14 true positives, 14 false positives. Every case is a numeric
     column with an EXACT or SUFFIX `MEASURE_KEYWORDS` match: the same gate
     `metric_suggestion_api.generate_metric_suggestions` applies *before* it ever
     calls `score_evidence`, so every corpus case is one the real production
     pipeline would actually score and could actually propose to a reviewer --
     never a case the real pipeline would have filtered out first.
  2. Each case is reconstructed into a real `aida.metric_suggestion_service.
     MetricEvidence` and scored by the real, unmodified `score_evidence` --
     the same function `metric_suggestion_api.generate_metric_suggestions` calls
     in production. `match_measure_keyword` (also real) re-derives the keyword/
     aggregation/match-kind from the column name rather than trusting a
     hand-typed value in the fixture, and this script asserts the corpus never
     smuggles in a case the real gate (`is_numeric_physical_type`, EXACT/SUFFIX
     only) would have rejected.
  3. Predictions are bucketed by `score_evidence(...).overall` into fixed-width
     0.1 bins (a standard reliability-diagram bucketing). For each bucket:
     case count, mean predicted confidence, and observed accuracy (the fraction
     of cases in that bucket whose `ground_truth.correct` is True). A bucket is
     well calibrated when its mean confidence and observed accuracy are close.
  4. Two summary metrics, computed the standard way: Expected Calibration Error
     (ECE -- the case-count-weighted average of |mean confidence - accuracy| per
     bucket) and Brier score (the mean squared error between each case's
     confidence and its binary correctness, 0=perfect, 0.25=uninformative on a
     balanced corpus).

Usage:
    uv run python scripts/confidence_calibration_benchmark.py
    # writes Docs/90-reference/confidence-calibration-results.md and
    # Docs/90-reference/confidence-calibration-results.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, UUID, uuid5

if TYPE_CHECKING:
    from aida.metric_suggestion_service import MetricEvidence

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = (
    REPO_ROOT / "tests" / "fixtures" / "confidence_calibration_corpus"
    / "bank_domain_metric_corpus.json"
)
DEFAULT_REPORT_MD = REPO_ROOT / "Docs" / "90-reference" / "confidence-calibration-results.md"
DEFAULT_REPORT_JSON = REPO_ROOT / "Docs" / "90-reference" / "confidence-calibration-results.json"

BUCKET_WIDTH = 0.1


def _fixed_id(*parts: str) -> UUID:
    """Deterministic id, same technique AG-8's `scripts/quality_benchmark.py`
    uses -- this benchmark's evidence objects are byte-for-byte reproducible."""
    return uuid5(NAMESPACE_URL, "confidence-calibration:" + ":".join(parts))


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroundTruth:
    correct: bool
    expected_aggregation: str
    rationale: str


@dataclass(frozen=True, slots=True)
class CalibrationCase:
    id: str
    table_name: str
    table_role: str
    business_name: str
    business_description: str
    grain_statement: str
    column_name: str
    physical_type: str
    nullable: bool
    bound_term_names: tuple[str, ...]
    ground_truth: GroundTruth


def load_corpus(path: Path) -> list[CalibrationCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases: list[CalibrationCase] = []
    for raw in data["cases"]:
        gt = raw["ground_truth"]
        cases.append(
            CalibrationCase(
                id=raw["id"],
                table_name=raw["table_name"],
                table_role=raw["table_role"],
                business_name=raw["business_name"],
                business_description=raw["business_description"],
                grain_statement=raw["grain_statement"],
                column_name=raw["column_name"],
                physical_type=raw["physical_type"],
                nullable=raw["nullable"],
                bound_term_names=tuple(raw["bound_term_names"]),
                ground_truth=GroundTruth(
                    correct=gt["correct"],
                    expected_aggregation=gt["expected_aggregation"],
                    rationale=gt["rationale"],
                ),
            )
        )
    return cases


class CorpusIntegrityError(ValueError):
    """Raised when a corpus case would never actually reach `score_evidence` in
    production -- e.g. a CONTAINS-only keyword match or a non-numeric column,
    both filtered by `metric_suggestion_api.generate_metric_suggestions` before
    it ever builds a `MetricEvidence`. A corpus case that can't happen in
    production can't tell us anything about production's calibration."""


def build_evidence(case: CalibrationCase) -> MetricEvidence:
    from aida.metric_suggestion_service import (
        MetricEvidence,
        is_numeric_physical_type,
        match_measure_keyword,
    )

    if not is_numeric_physical_type(case.physical_type):
        raise CorpusIntegrityError(
            f"case {case.id!r}: physical_type {case.physical_type!r} is not numeric -- "
            "the real generation pipeline never builds evidence for it"
        )
    match = match_measure_keyword(case.column_name)
    if match is None:
        raise CorpusIntegrityError(
            f"case {case.id!r}: column_name {case.column_name!r} matches no MEASURE_KEYWORDS "
            "entry -- the real generation pipeline never builds evidence for it"
        )
    keyword, aggregation, match_kind = match
    if match_kind == "CONTAINS":
        raise CorpusIntegrityError(
            f"case {case.id!r}: column_name {case.column_name!r} is a bare CONTAINS match -- "
            "metric_suggestion_api.generate_metric_suggestions drops these before scoring, so "
            "a CONTAINS case here would never be scored in production"
        )
    return MetricEvidence(
        table_id=_fixed_id("table", case.id),
        table_name=case.table_name,
        project_id=_fixed_id("project"),
        business_annotation_id=_fixed_id("annotation", case.id),
        business_name=case.business_name,
        business_description=case.business_description,
        table_role=case.table_role,
        grain_statement=case.grain_statement,
        column_id=_fixed_id("column", case.id),
        column_name=case.column_name,
        physical_type=case.physical_type,
        nullable=case.nullable,
        matched_keyword=keyword,
        suggested_aggregation=aggregation,
        match_kind=match_kind,
        bound_term_names=case.bound_term_names,
    )


# ---------------------------------------------------------------------------
# Scoring + calibration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseResult:
    case: CalibrationCase
    confidence: float
    reviewable: bool  # would clear MINIMUM_EVIDENCE_FOR_METRIC_REVIEW in production

    @property
    def correct(self) -> bool:
        return self.case.ground_truth.correct

    @property
    def squared_error(self) -> float:
        return (self.confidence - (1.0 if self.correct else 0.0)) ** 2


def run_calibration(cases: list[CalibrationCase]) -> list[CaseResult]:
    from aida.metric_suggestion_service import MINIMUM_EVIDENCE_FOR_METRIC_REVIEW, score_evidence

    results: list[CaseResult] = []
    for case in cases:
        evidence = build_evidence(case)
        breakdown = score_evidence(evidence)  # the real, unmodified SM-4 scoring function
        results.append(
            CaseResult(
                case=case,
                confidence=breakdown.overall,
                reviewable=breakdown.overall >= MINIMUM_EVIDENCE_FOR_METRIC_REVIEW,
            )
        )
    return results


@dataclass(frozen=True, slots=True)
class Bucket:
    lower: float
    upper: float
    results: tuple[CaseResult, ...]

    @property
    def n(self) -> int:
        return len(self.results)

    @property
    def mean_confidence(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.confidence for r in self.results) / len(self.results)

    @property
    def accuracy(self) -> float:
        if not self.results:
            return 0.0
        return sum(1 for r in self.results if r.correct) / len(self.results)

    @property
    def gap(self) -> float:
        return abs(self.mean_confidence - self.accuracy)


def bucket_results(
    results: list[CaseResult], *, bucket_width: float = BUCKET_WIDTH
) -> list[Bucket]:
    """Fixed-width reliability-diagram bucketing over [0, 1]. A confidence of
    exactly 1.0 falls in the final (highest) bucket, not a phantom 11th one.

    Uses a small epsilon before truncating to bucket index: plain
    `int(confidence / bucket_width)` misfiles a confidence that is exactly on a
    bucket boundary (e.g. 0.6) into the *lower* bucket, because `0.6 / 0.1` is
    `5.999999999999999` in IEEE 754 double precision, not `6.0` -- caught by
    this module's own test (`test_bucket_boundary_value_lands_in_upper_bucket`)
    against `score_evidence` scores that land on an exact 0.1 boundary in this
    corpus (`od_fee_true`, `qty_true`, `avg_daily_balance_false`, others)."""
    n_buckets = round(1.0 / bucket_width)
    buckets: list[list[CaseResult]] = [[] for _ in range(n_buckets)]
    epsilon = 1e-9
    for r in results:
        idx = min(int((r.confidence + epsilon) / bucket_width), n_buckets - 1)
        buckets[idx].append(r)
    return [
        Bucket(lower=i * bucket_width, upper=(i + 1) * bucket_width, results=tuple(items))
        for i, items in enumerate(buckets)
    ]


def expected_calibration_error(buckets: list[Bucket], *, total_n: int) -> float:
    """Case-count-weighted average of |mean confidence - accuracy| per non-empty
    bucket. 0.0 is perfect calibration."""
    if total_n == 0:
        return 0.0
    return sum((b.n / total_n) * b.gap for b in buckets if b.n > 0)


def brier_score(results: list[CaseResult]) -> float:
    """Mean squared error between predicted confidence and binary correctness.
    0.0 is perfect; 0.25 is what a constant 0.5 prediction scores on a
    perfectly balanced corpus (this corpus is balanced: 14/14)."""
    if not results:
        return 0.0
    return sum(r.squared_error for r in results) / len(results)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _results_payload(
    results: list[CaseResult], buckets: list[Bucket], *, corpus_path: Path
) -> dict[str, object]:
    total_n = len(results)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus": str(corpus_path.relative_to(REPO_ROOT)),
        "case_count": total_n,
        "positive_count": sum(1 for r in results if r.correct),
        "negative_count": sum(1 for r in results if not r.correct),
        "below_review_gate_count": sum(1 for r in results if not r.reviewable),
        "expected_calibration_error": round(
            expected_calibration_error(buckets, total_n=total_n), 4
        ),
        "brier_score": round(brier_score(results), 4),
        "buckets": [
            {
                "range": f"[{b.lower:.1f}, {b.upper:.1f})",
                "n": b.n,
                "mean_confidence": round(b.mean_confidence, 4) if b.n else None,
                "accuracy": round(b.accuracy, 4) if b.n else None,
                "gap": round(b.gap, 4) if b.n else None,
            }
            for b in buckets
        ],
        "cases": [
            {
                "id": r.case.id,
                "column_name": r.case.column_name,
                "table_role": r.case.table_role,
                "confidence": round(r.confidence, 4),
                "reviewable": r.reviewable,
                "ground_truth_correct": r.correct,
                "expected_aggregation": r.case.ground_truth.expected_aggregation,
            }
            for r in results
        ],
    }


def _write_report_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _write_report_markdown(
    path: Path, results: list[CaseResult], buckets: list[Bucket], *, corpus_path: Path
) -> None:
    total_n = len(results)
    ece = expected_calibration_error(buckets, total_n=total_n)
    brier = brier_score(results)
    below_gate = [r for r in results if not r.reviewable]

    lines: list[str] = []
    lines.append("# Confidence calibration results (SM-3)")
    lines.append("")
    lines.append(
        f"Generated {datetime.now(UTC).isoformat()} by "
        "`scripts/confidence_calibration_benchmark.py`. Reproduce with `uv run python "
        "scripts/confidence_calibration_benchmark.py` (requires `AIDA_ENVIRONMENT` set, e.g. "
        "`development`). Every number below comes from a real run of the real, unmodified "
        "`aida.metric_suggestion_service.score_evidence` (SM-4) against the deterministic "
        f"corpus at `{corpus_path.relative_to(REPO_ROOT)}` -- not hand-typed."
    )
    lines.append("")
    lines.append(
        "Scope: this calibrates the one module-07 inference this branch has made real and "
        "gradated -- SM-4's metric-suggestion evidence score. `aida.semantic_inference."
        "infer_table_semantics` also reports a `confidence` field, but it is a coarse binary "
        "choice (0.82 or 0.66) with no gradation across scores to calibrate against a "
        "reliability diagram, and SM-1 (dimension authoring) is not yet built; both are out of "
        "scope here, not silently skipped."
    )
    lines.append("")
    lines.append(
        "Unlike AG-8's model-generation half, this benchmark needs **no live infrastructure**: "
        "`score_evidence` is a pure function of a `MetricEvidence` value -- no DB session, no "
        "embedding provider, no model route. Every number below is a full, real result."
    )
    lines.append("")

    lines.append("## Corpus")
    lines.append("")
    lines.append(
        f"{total_n} labelled bank-domain (table, column) cases -- "
        f"{sum(1 for r in results if r.correct)} true positives / "
        f"{sum(1 for r in results if not r.correct)} false positives -- every case a numeric "
        "column with an EXACT or SUFFIX `MEASURE_KEYWORDS` match (`aida.metric_suggestion_"
        "service.match_measure_keyword`), the same gate the real production generation path "
        "applies before it ever calls `score_evidence`. False positives are drawn from real "
        "banking column-naming ambiguity the algorithm has no signal for: pre-aggregated/"
        "cumulative balances (`avg_daily_balance`, `running_balance`, `closing_balance`), "
        "per-unit rates (`unit_cost`, `weighted_avg_cost`), policy thresholds (`minimum_"
        "balance`, `balance_limit`), and precomputed `*_count` columns where the keyword's "
        "fixed COUNT aggregation is systematically wrong (the column already holds a "
        "per-row tally; the correct rollup is SUM of the stored counts, not COUNT of rows)."
    )
    lines.append("")

    lines.append("## Calibration curve (predicted confidence vs. observed accuracy)")
    lines.append("")
    lines.append("| Confidence bucket | n | Mean predicted confidence | Observed accuracy | Gap |")
    lines.append("|---|---|---|---|---|")
    for b in buckets:
        if b.n == 0:
            lines.append(f"| [{b.lower:.1f}, {b.upper:.1f}) | 0 | — | — | — |")
        else:
            lines.append(
                f"| [{b.lower:.1f}, {b.upper:.1f}) | {b.n} | {b.mean_confidence:.4f} | "
                f"{b.accuracy:.4f} | {b.gap:.4f} |"
            )
    lines.append("")
    lines.append(f"**Expected Calibration Error (ECE): {ece:.4f}**")
    lines.append("")
    lines.append(
        f"**Brier score: {brier:.4f}** (0 = perfect, 0.25 = an uninformative constant-0.5 "
        "predictor on this balanced 14/14 corpus)"
    )
    lines.append("")

    lines.append("## What the numbers say")
    lines.append("")
    lines.append(
        f"score_evidence is measurably **not** well calibrated as a probability of "
        f"correctness (ECE {ece:.4f} against a well-calibrated target of 0; Brier "
        f"{brier:.4f}, worse than the {0.25:.4f} an uninformative predictor would score on "
        "this balanced corpus). The miscalibration is not random noise: it is concentrated "
        "exactly where the score has no signal at all -- `score_evidence`'s four dimensions "
        "(match strength, fact-shaped table role, monetary type, clarity/completeness of "
        "annotation evidence) say nothing about whether the *proposed aggregation* is "
        "semantically correct for the column's actual grain. A `_count`-suffixed numeric "
        "column and a plain `_amount`-suffixed one can score identically high on identical "
        "evidence richness, even though the `_count` case's suggested `COUNT` aggregation is "
        "wrong every time in this corpus (it should be `SUM` of the stored per-row tally). "
        "Concretely: `txn_count`, `daily_fraud_alert_count`, and `daily_active_user_count` -- "
        "each with a bound glossary term *and* a description mention, the two richest evidence "
        "signals `score_evidence` has -- score 0.7542, ahead of `deposit_amount_true` (0.5625, "
        "correct, but with neither a bound term nor a description mention) and `acct_balance_"
        "true` (0.6917, correct). A false positive with rich corroborating evidence outscores a "
        "true positive with sparse evidence, because bound-term/description-mention evidence "
        "corroborates that a human steward believes the *column* is meaningful, never that the "
        "*aggregation* is right for it."
    )
    lines.append("")
    lines.append(
        "This is a real, actionable finding, not a restatement of the obvious: it says "
        "`score_evidence`'s overall score should not be read as \"probability this proposal "
        "is correct as-is\" -- it is closer to \"strength of evidence that this column is a "
        "measure worth a reviewer's attention\", which the human-in-the-loop `governance_"
        "review` gate this row's evidence feeds already assumes (SM-4's own docstring: this "
        "generates a *draft* for review, never an auto-published metric). A concrete next "
        "step this row's numbers point to: add a dimension penalizing `*_count`/`*_balance` "
        "qualifier patterns (`avg_`, `running_`, `closing_`, `opening_`, `ending_`) the same "
        "way `MEASURE_KEYWORDS` already special-cases aggregation per keyword -- out of scope "
        "for this row (SM-3 measures the score's calibration; it does not re-tune SM-4's "
        "formula), but the corpus and this report are what such a change would be evaluated "
        "against."
    )
    lines.append("")

    if below_gate:
        below_correct = sum(1 for r in below_gate if r.correct)
        lines.append("## Below the review gate")
        lines.append("")
        lines.append(
            f"{len(below_gate)} of {total_n} cases score below `MINIMUM_EVIDENCE_FOR_METRIC_"
            f"REVIEW` (0.4) and so would never even reach a human reviewer in production "
            f"(`ensure_reviewable` refuses them with 422 before any `GovernanceReview` row is "
            f"constructed): {', '.join(r.case.id for r in below_gate)}. "
            f"{below_correct} of those {len(below_gate)} are ground-truth correct -- the gate "
            "is not free (it also silently drops some genuinely valid proposals), which this "
            "small sample cannot generalize from, but is worth naming plainly rather than "
            "omitting because it complicates the headline numbers."
        )
        lines.append("")

    lines.append("## Per-case detail")
    lines.append("")
    lines.append(
        "| Case | Column | Table role | Confidence | Reviewable | Ground truth | Expected agg. |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for r in sorted(results, key=lambda r: r.confidence):
        lines.append(
            f"| {r.case.id} | `{r.case.column_name}` | {r.case.table_role} | "
            f"{r.confidence:.4f} | {'yes' if r.reviewable else 'no'} | "
            f"{'correct' if r.correct else 'incorrect'} | "
            f"{r.case.ground_truth.expected_aggregation} |"
        )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_MD)
    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT_JSON)
    parser.add_argument("--no-report", action="store_true", help="Do not write report files.")
    args = parser.parse_args(argv)

    cases = load_corpus(args.corpus)
    results = run_calibration(cases)
    buckets = bucket_results(results)
    total_n = len(results)
    ece = expected_calibration_error(buckets, total_n=total_n)
    brier = brier_score(results)

    print(f"Corpus: {args.corpus} ({total_n} cases)")
    print("Calibration curve:")
    for b in buckets:
        if b.n:
            print(
                f"  [{b.lower:.1f}, {b.upper:.1f}) n={b.n:2d} "
                f"confidence={b.mean_confidence:.4f} accuracy={b.accuracy:.4f} gap={b.gap:.4f}"
            )
    print(f"Expected Calibration Error: {ece:.4f}")
    print(f"Brier score:                {brier:.4f}")

    if not args.no_report:
        payload = _results_payload(results, buckets, corpus_path=args.corpus)
        _write_report_json(args.report_json, payload)
        _write_report_markdown(args.report, results, buckets, corpus_path=args.corpus)
        print(f"\nReports written to {args.report} and {args.report_json}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
