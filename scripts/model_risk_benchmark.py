#!/usr/bin/env python3
"""Bank model-risk evaluation corpus + harness (tracker AG-3 / MG-5).

AG-3 ("Bank model-risk evaluation corpus") and MG-5 ("Model-risk evaluation
corpus", the tracker's own "Same as AG-3" cross-reference) call for published
accuracy and refusal results against a bank scenario. This sandbox has no real
bank data and no live, approved model route to publish an actual
generation-quality benchmark against -- the same honest limit AG-8's
`scripts/quality_benchmark.py` documents for model-*generation* quality. What
this script measures instead, with real numbers and zero fabrication:

    1. **Refusal** -- `aida.prompt_risk.DeterministicPromptRiskClassifier`
       (the real pre-retrieval policy screen wired into
       `GovernedAgentOrchestrator.run()`) against
       `tests/fixtures/model_risk_corpus/refusal_corpus.json`: bank-governance
       attacks (maker-checker/dual-control bypass, AML/KYC/sanctions-hold
       override, audit-trail and regulatory-reporting suppression, plus the
       pre-existing generic attack categories phrased in a bank-analyst
       context) that must all be BLOCKed (zero bypasses), and ordinary bank
       questions -- several deliberately reusing the same governance
       vocabulary without the accompanying bypass verb -- that must all be
       ALLOWed (zero false positives).
    2. **Accuracy** -- `aida.sql_guard.SqlGuard.validate` (the real query
       gateway guard) against
       `tests/fixtures/model_risk_corpus/sql_safety_corpus.json` (bank-domain
       safe reads and attack SQL), and `aida.agent_intelligence
       .GovernedPlanner.plan` (the real PLANNED-state tool-selection decision)
       against
       `tests/fixtures/model_risk_corpus/tool_selection_corpus.json`
       (synthetic bank governed-tool retrieval hits covering role binding,
       the match-score threshold, missing required parameters, an explicit
       preferred-tool override, and multi-tool ranking order).

All three run through `aida.agent_evals`'s I/O-free harness functions --
production code, not a reimplementation -- so this script's own job is only
to load the corpus JSON, run it, ratchet the result against a committed
baseline (same pattern as PF-3/AG-8), and publish a results report.

Zero bypasses / zero false positives are asserted as a hard gate independent
of the point-threshold ratchet below: a single malicious case slipping through
BLOCK, or a single benign case wrongly BLOCKed, fails the run outright, the
same "no skip/xfail path" discipline QG-1's own adversarial corpus uses.

Usage:
    # CI / local gate: compare current metrics to the committed baseline; exit 1
    # on an unacknowledged regression, or on any bypass/false-positive at all.
    uv run python scripts/model_risk_benchmark.py

    # After a deliberate, reviewed change to the classifier, guard, or
    # planner: regenerate the baseline from current measurements.
    uv run python scripts/model_risk_benchmark.py --accept-baseline
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aida.agent_evals import BankModelRiskEvaluation, run_bank_model_risk_evaluation
from aida.config import Settings

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "model_risk_corpus"
DEFAULT_BASELINE = REPO_ROOT / "Docs" / "90-reference" / "model-risk-benchmark-baseline.json"
DEFAULT_REPORT = REPO_ROOT / "Docs" / "90-reference" / "model-risk-benchmark-results.md"

DEFAULT_THRESHOLD_POINTS = 5.0

TRACKED_METRICS = (
    "malicious_block_recall",
    "benign_false_positive_rate",
    "sql_safety_pass_rate",
    "tool_selection_pass_rate",
    "accuracy_pass_rate",
)


# ---------------------------------------------------------------------------
# Baseline comparison (same ratchet pattern as scripts/quality_benchmark.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricRegression:
    name: str
    baseline: float
    current: float
    point_change: float

    def __str__(self) -> str:
        return (
            f"[REGRESSION] {self.name}: {self.baseline:.4f} -> {self.current:.4f} "
            f"({self.point_change:+.2f} points)"
        )


def find_regressions(
    baseline: dict[str, float], current: dict[str, float], *, threshold_points: float
) -> list[MetricRegression]:
    """`benign_false_positive_rate` is a "lower is better" metric -- an
    *increase* is the regression there, so its sign is flipped before
    comparing against every other "higher is better" metric's convention.
    """
    regressions: list[MetricRegression] = []
    for name, base in baseline.items():
        if name not in current:
            continue
        raw_change = (current[name] - base) * 100
        point_change = -raw_change if name == "benign_false_positive_rate" else raw_change
        if point_change < -threshold_points:
            regressions.append(
                MetricRegression(
                    name=name, baseline=base, current=current[name], point_change=point_change
                )
            )
    return regressions


def _load_baseline(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text())
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError(f"baseline at {path} has no 'metrics' object")
    return {str(name): float(entry["value"]) for name, entry in metrics.items()}


def _write_baseline(path: Path, current: dict[str, float], *, threshold_points: float) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "threshold_points": threshold_points,
        "metrics": {name: {"value": round(value, 4)} for name, value in current.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# Results report
# ---------------------------------------------------------------------------


def _metric_row(
    name: str, current_metrics: dict[str, float], baseline_metrics: dict[str, float] | None
) -> str:
    current = current_metrics.get(name)
    current_display = f"{current:.4f}" if current is not None else "n/a"
    base = baseline_metrics.get(name) if baseline_metrics else None
    if base is None:
        return f"| `{name}` | {current_display} | n/a (new) | — |"
    raw_change = (current - base) * 100 if current is not None else 0.0
    change = -raw_change if name == "benign_false_positive_rate" else raw_change
    return f"| `{name}` | {current_display} | {base:.4f} | {change:+.2f} pts |"


def _write_report(
    path: Path,
    *,
    evaluation: BankModelRiskEvaluation,
    current_metrics: dict[str, float],
    baseline_metrics: dict[str, float] | None,
    regressions: list[MetricRegression],
) -> None:
    lines: list[str] = []
    lines.append("# Bank model-risk benchmark results (AG-3 / MG-5)")
    lines.append("")
    lines.append(
        f"Generated {datetime.now(UTC).isoformat()} by `scripts/model_risk_benchmark.py`. "
        "Reproduce with `uv run python scripts/model_risk_benchmark.py` (requires "
        "`AIDA_ENVIRONMENT` set, e.g. `development`). Every number below comes from a real "
        "run of `aida.agent_evals.run_bank_model_risk_evaluation` -- the live, deterministic "
        "`DeterministicPromptRiskClassifier`/`SqlGuard`/`GovernedPlanner` code paths -- against "
        "the committed corpus in `tests/fixtures/model_risk_corpus/`, not hand-typed."
    )
    lines.append("")
    lines.append(
        "Scope: this sandbox has no real bank data and no live, approved model route, so this "
        "is not a generation-quality benchmark (the same honest limit AG-8's "
        "`quality-benchmark-results.md` documents for model generation). It measures the "
        "deterministic controls that already sit on the live agent path: prompt-risk refusal, "
        "SQL-safety validation, and tool-selection accuracy."
    )
    lines.append("")

    lines.append("## Refusal (bank-governance attack corpus)")
    lines.append("")
    lines.append(
        f"`DeterministicPromptRiskClassifier` over "
        f"`tests/fixtures/model_risk_corpus/refusal_corpus.json` "
        f"({len(evaluation.malicious_cases)} malicious cases, "
        f"{len(evaluation.benign_cases)} benign cases)."
    )
    lines.append("")
    lines.append("| Metric | Value | Baseline | Change |")
    lines.append("|---|---|---|---|")
    for name in ("malicious_block_recall", "benign_false_positive_rate"):
        lines.append(_metric_row(name, current_metrics, baseline_metrics))
    lines.append("")
    lines.append("| Category | Malicious cases | Blocked | Benign cases | Falsely blocked |")
    lines.append("|---|---|---|---|---|")
    categories = sorted({r.case.category for r in evaluation.refusal_results})
    for category in categories:
        cat_results = [r for r in evaluation.refusal_results if r.case.category == category]
        malicious = [r for r in cat_results if r.case.kind == "malicious"]
        benign = [r for r in cat_results if r.case.kind == "benign"]
        blocked = sum(1 for r in malicious if r.passed)
        falsely_blocked = sum(1 for r in benign if not r.passed)
        lines.append(
            f"| {category} | {len(malicious)} | {blocked}/{len(malicious)} | "
            f"{len(benign)} | {falsely_blocked}/{len(benign)} |"
        )
    lines.append("")
    if not evaluation.zero_bypasses:
        lines.append("**BYPASS DETECTED** — see failing case ids below.")
        lines.append("")

    lines.append("## Accuracy: SQL safety (bank-domain corpus)")
    lines.append("")
    lines.append(
        f"`SqlGuard.validate` over `tests/fixtures/model_risk_corpus/sql_safety_corpus.json` "
        f"({len(evaluation.sql_results)} cases)."
    )
    lines.append("")
    lines.append("| Metric | Value | Baseline | Change |")
    lines.append("|---|---|---|---|")
    lines.append(_metric_row("sql_safety_pass_rate", current_metrics, baseline_metrics))
    lines.append("")
    lines.append("| Case | Category | Dialect | Kind | Passed |")
    lines.append("|---|---|---|---|---|")
    for sql_r in evaluation.sql_results:
        lines.append(
            f"| {sql_r.case.case_id} | {sql_r.case.category} | {sql_r.case.dialect} | "
            f"{sql_r.case.kind} | {'yes' if sql_r.passed else 'no'} |"
        )
    lines.append("")

    lines.append("## Accuracy: tool selection (bank governed-tool corpus)")
    lines.append("")
    lines.append(
        f"`GovernedPlanner.plan` over "
        f"`tests/fixtures/model_risk_corpus/tool_selection_corpus.json` "
        f"({len(evaluation.tool_results)} cases)."
    )
    lines.append("")
    lines.append("| Metric | Value | Baseline | Change |")
    lines.append("|---|---|---|---|")
    lines.append(_metric_row("tool_selection_pass_rate", current_metrics, baseline_metrics))
    lines.append("")
    lines.append("| Case | Category | Expected strategy | Actual strategy | Passed |")
    lines.append("|---|---|---|---|---|")
    for tool_r in evaluation.tool_results:
        lines.append(
            f"| {tool_r.case.case_id} | {tool_r.case.category} | {tool_r.case.expected_strategy} "
            f"| {tool_r.plan.strategy} | {'yes' if tool_r.passed else 'no'} |"
        )
    lines.append("")

    lines.append("## Combined accuracy")
    lines.append("")
    lines.append("| Metric | Value | Baseline | Change |")
    lines.append("|---|---|---|---|")
    lines.append(_metric_row("accuracy_pass_rate", current_metrics, baseline_metrics))
    lines.append("")

    failing = evaluation.failing_case_ids()
    if failing:
        lines.append("## Failing case ids")
        lines.append("")
        for case_id in failing:
            lines.append(f"- {case_id}")
        lines.append("")

    if regressions:
        lines.append("## Regressions against the committed baseline")
        lines.append("")
        for regression in regressions:
            lines.append(f"- {regression}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run(
    *, refusal_path: Path, sql_path: Path, tool_path: Path, settings: Settings
) -> BankModelRiskEvaluation:
    refusal_corpus = json.loads(refusal_path.read_text())
    sql_corpus = json.loads(sql_path.read_text())
    tool_corpus = json.loads(tool_path.read_text())
    return run_bank_model_risk_evaluation(
        refusal_corpus=refusal_corpus,
        sql_safety_corpus=sql_corpus,
        tool_selection_corpus=tool_corpus,
        settings=settings,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--no-report", action="store_true", help="Do not write the results report.")
    parser.add_argument("--refusal-corpus", type=Path, default=CORPUS_DIR / "refusal_corpus.json")
    parser.add_argument("--sql-corpus", type=Path, default=CORPUS_DIR / "sql_safety_corpus.json")
    parser.add_argument(
        "--tool-corpus", type=Path, default=CORPUS_DIR / "tool_selection_corpus.json"
    )
    parser.add_argument(
        "--accept-baseline",
        action="store_true",
        help=(
            "Regenerate the baseline from current measurements and write it. Run this "
            "deliberately after reviewing the reported change, then commit the updated "
            "baseline file -- the same explicit, auditable path PF-3/AG-8 provide."
        ),
    )
    parser.add_argument("--threshold-points", type=float, default=DEFAULT_THRESHOLD_POINTS)
    args = parser.parse_args(argv)

    if not args.accept_baseline and not args.baseline.exists():
        print(f"::error::No model-risk baseline found at {args.baseline}.")
        print(
            "Run `uv run python scripts/model_risk_benchmark.py --accept-baseline` to create one."
        )
        return 1

    evaluation = _run(
        refusal_path=args.refusal_corpus,
        sql_path=args.sql_corpus,
        tool_path=args.tool_corpus,
        settings=Settings(),
    )

    current_metrics = {
        "malicious_block_recall": evaluation.malicious_block_recall,
        "benign_false_positive_rate": evaluation.benign_false_positive_rate,
        "sql_safety_pass_rate": evaluation.sql_safety_pass_rate,
        "tool_selection_pass_rate": evaluation.tool_selection_pass_rate,
        "accuracy_pass_rate": evaluation.accuracy_pass_rate,
    }

    print("Refusal:")
    print(f"  malicious block recall:    {evaluation.malicious_block_recall:.4f}")
    print(f"  benign false positive rate:{evaluation.benign_false_positive_rate:.4f}")
    print("Accuracy:")
    print(f"  SQL safety pass rate:      {evaluation.sql_safety_pass_rate:.4f}")
    print(f"  tool selection pass rate:  {evaluation.tool_selection_pass_rate:.4f}")
    print(f"  combined accuracy:         {evaluation.accuracy_pass_rate:.4f}")

    failing = evaluation.failing_case_ids()
    if not evaluation.zero_bypasses or evaluation.benign_false_positive_rate > 0.0 or failing:
        if not args.no_report:
            baseline_for_report = (
                current_metrics
                if args.accept_baseline
                else (_load_baseline(args.baseline) if args.baseline.exists() else None)
            )
            _write_report(
                args.report,
                evaluation=evaluation,
                current_metrics=current_metrics,
                baseline_metrics=baseline_for_report,
                regressions=[],
            )
        print(f"\n::error::{len(failing)} failing case(s), zero bypasses required: {failing}")
        return 1

    if args.accept_baseline:
        _write_baseline(args.baseline, current_metrics, threshold_points=args.threshold_points)
        print(f"\nBaseline regenerated at {args.baseline}.")
        if not args.no_report:
            _write_report(
                args.report,
                evaluation=evaluation,
                current_metrics=current_metrics,
                baseline_metrics=current_metrics,
                regressions=[],
            )
            print(f"Report written to {args.report}.")
        return 0

    baseline_metrics = _load_baseline(args.baseline)
    regressions = find_regressions(
        baseline_metrics, current_metrics, threshold_points=args.threshold_points
    )

    if not args.no_report:
        _write_report(
            args.report,
            evaluation=evaluation,
            current_metrics=current_metrics,
            baseline_metrics=baseline_metrics,
            regressions=regressions,
        )
        print(f"\nReport written to {args.report}.")

    if not regressions:
        print(
            f"\nNo model-risk regressions beyond {args.threshold_points} points "
            f"against {args.baseline}."
        )
        return 0

    print(f"\n{len(regressions)} model-risk regression(s) against {args.baseline}:")
    for r in regressions:
        print(r)
    print(
        "\n::error::Model-risk regression(s) detected. If this is an expected consequence of a "
        "deliberate change to the classifier, guard, or planner: review why the metric moved, "
        "then run `uv run python scripts/model_risk_benchmark.py --accept-baseline` and commit "
        "the refreshed baseline. If it wasn't expected, fix the regression instead."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
