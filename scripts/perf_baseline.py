#!/usr/bin/env python3
"""CI performance regression gate (tracker PF-3).

This is the ratchet-pattern regression gate for a *small, fast, deterministic*
set of in-process hot paths -- it is deliberately not, and does not attempt to
be, the bank-scale load/soak/spike testing tracked separately as PF-1, PF-2,
PF-4, PF-7 and TS-7, all of which need real infrastructure (a live warehouse,
sustained concurrent load, a soak rig) this CI runner does not have. What this
gate *does* provide: if an otherwise-innocuous change makes one of these hot
paths meaningfully slower, CI turns red before the change merges, the same way
`scripts/openapi_diff.py` (TS-4) turns CI red on an unacknowledged breaking
API change and import-linter (ST-02) turns CI red on a new layering violation.

    1. Run each benchmark in ``all_benchmarks()`` -- a warmup, then several
       timed iterations of a real, already-exercised-by-tests hot path,
       taking the median (p50) wall-clock time per iteration.
    2. Compare each benchmark's current median against a committed baseline
       (``Docs/90-reference/perf-baseline.json``).
    3. Exit non-zero if any benchmark got more than ``--threshold-pct``
       (default 20%) slower. A benchmark with no baseline entry yet (newly
       added) or a baseline entry with no current benchmark (removed) is
       informational only, mirroring ``openapi_diff.py``'s treatment of
       added/removed paths -- never a hard failure by itself.

Why median (p50), not p95/p99, and why a 20% threshold:

    A shared CI runner's tail latency is dominated by scheduler noise (a
    neighboring job on the same host stealing a timeslice) that has nothing
    to do with the code under test; the median stays representative of
    steady-state cost with far fewer measured iterations than it would take
    to get a stable p95 on a noisy runner. Repeated local measurement of
    these four benchmarks showed run-to-run median variance in the
    single-digit percent for the fast ones and comfortably under 15% even for
    the slowest (OpenAPI schema generation); 20% leaves headroom above that
    noise floor while still catching a real regression (an accidentally
    quadratic loop, a stray synchronous I/O call, a debug sleep) rather than
    ratcheting on every recompile-to-recompile jitter. On top of that, any
    benchmark that crosses the threshold is re-measured once before the gate
    actually fails (see ``main``), so a single transient blip on a loaded
    runner does not fail the build by itself -- the regression has to
    reproduce.

Caveat, stated plainly rather than glossed over: this baseline reflects the
absolute wall-clock speed of the machine it was captured on, not a
CI-runner-independent unit. If GitHub Actions runners turn out to be
meaningfully faster or slower than the sandbox this baseline was generated
in, the *first* real Actions run of this job may need one deliberate
``--accept-baseline`` re-capture. That is expected and is exactly the
auditable, explicit "update the baseline on purpose" path this script
provides -- not a silent adjustment.

What is benchmarked (four real, already-tested hot paths -- nothing invented
just to have something to time):

  - ``sql_guard_validate_adversarial_corpus`` -- ``SqlGuard.validate()``, the
    query gateway's deterministic guard pipeline (QG-1's own adversarial SQL
    corpus, ``tests/fixtures/adversarial_sql_corpus/*.json``, is the input).
  - ``abac_evaluate_500_policies`` -- ``abac.evaluate()`` over 500 policies,
    the exact scenario PG-1's own ``test_evaluation_under_50ms_with_500_
    policies`` p95 test already exercises.
  - ``fusion_ranking_rrf_500_candidates`` -- ``fuse_results()``, hybrid
    retrieval's reciprocal-rank-fusion combiner, over a synthetic
    500-candidate catalog.
  - ``openapi_schema_generation`` -- ``app.openapi()``, the same call TS-4's
    diff gate already exercises, timed with FastAPI's schema cache cleared
    each iteration so it is genuinely regenerated every time.

Usage:
    # CI gate: compare current benchmark medians to the committed baseline;
    # exit 1 on an unacknowledged regression.
    uv run python scripts/perf_baseline.py

    # After a deliberate, reviewed change to one of the benchmarked paths
    # (or the first capture on a new machine/runner class): regenerate the
    # baseline from current measurements and commit it.
    uv run python scripts/perf_baseline.py --accept-baseline
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "Docs" / "90-reference" / "perf-baseline.json"
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "adversarial_sql_corpus"

DEFAULT_THRESHOLD_PCT = 20.0


@dataclass(frozen=True)
class Benchmark:
    """One timed hot path.

    ``run_iteration`` is called once per warmup pass and once per measured
    iteration; it should do a fixed, deterministic amount of real work and
    discard the result (the work itself, not its output, is what is timed).
    """

    name: str
    description: str
    run_iteration: Callable[[], None]
    iterations: int
    warmup: int


# ---------------------------------------------------------------------------
# Benchmark definitions
# ---------------------------------------------------------------------------


def _load_sql_guard_corpus() -> list[tuple[str, str]]:
    """(sql, dialect) pairs from every case in QG-1's adversarial SQL corpus."""
    cases: list[tuple[str, str]] = []
    for path in sorted(CORPUS_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        dialect = data["dialect"]
        for case in data["cases"]:
            cases.append((case["sql"], dialect))
    if not cases:
        raise RuntimeError(f"no adversarial SQL corpus cases found under {CORPUS_DIR}")
    return cases


def _make_sql_guard_benchmark() -> Benchmark:
    import aida.sql_guard as sql_guard_module

    # sqlglot logs a WARNING for every corpus case it falls back to parsing
    # as a bare Command (that is the point of those cases -- they are
    # deliberately unusual SQL); it is real, already-known-about parser
    # behaviour, not benchmark noise worth reprinting on every iteration.
    logging.getLogger("sqlglot").setLevel(logging.ERROR)

    corpus = _load_sql_guard_corpus()
    guard = sql_guard_module.SqlGuard(default_row_limit=1000, hard_row_limit=100_000)

    def run_iteration() -> None:
        for sql, dialect in corpus:
            guard.validate(sql, dialect=dialect)

    return Benchmark(
        name="sql_guard_validate_adversarial_corpus",
        description=(
            f"SqlGuard.validate() over all {len(corpus)} QG-1 adversarial-SQL-corpus cases "
            "across 5 certified dialects -- the exact deterministic guard pipeline every "
            "agent SQL statement passes through before execution."
        ),
        run_iteration=run_iteration,
        iterations=15,
        warmup=3,
    )


def _make_abac_benchmark() -> Benchmark:
    import aida.abac as abac_module

    policies = [
        abac_module.AbacPolicy(
            id=f"p{i}",
            policy_key="perf-baseline",
            version=1,
            name=f"perf baseline policy {i}",
            effect="PERMIT" if i % 2 == 0 else "DENY",
            subject_conditions={"role": f"role_{i}"},
            resource_conditions={"classification": f"class_{i}"},
            environment_conditions={},
            priority=i,
        )
        for i in range(500)
    ]

    def run_iteration() -> None:
        for _ in range(50):
            abac_module.evaluate(
                {"role": "role_250"}, {"classification": "class_250"}, {}, policies
            )

    return Benchmark(
        name="abac_evaluate_500_policies",
        description=(
            "abac.evaluate() over 500 policies -- PG-1's own p95<50ms target scenario "
            "(tests/test_abac.py::TestPerformance) -- 50 calls per measured iteration."
        ),
        run_iteration=run_iteration,
        iterations=20,
        warmup=3,
    )


def _make_fusion_ranking_benchmark() -> Benchmark:
    import aida.fusion_ranking as fusion_module

    # Fixed seed: a deterministic synthetic catalog, not a security-sensitive
    # use of `random` -- reproducibility across runs is the point.
    rng = random.Random(20260830)  # noqa: S311
    candidates: list[object] = []
    for i in range(500):
        signals = []
        if rng.random() < 0.9:
            signals.append(
                fusion_module.SignalScore("lexical", rng.random(), rng.randint(1, 200))
            )
        if rng.random() < 0.9:
            signals.append(fusion_module.SignalScore("vector", rng.random(), rng.randint(1, 200)))
        if rng.random() < 0.6:
            signals.append(fusion_module.SignalScore("graph", rng.random(), rng.randint(1, 200)))
        candidates.append(
            fusion_module.RankedCandidate(
                object_type="table",
                object_id=f"obj-{i}",
                display_name=f"object {i}",
                signals=signals,
            )
        )
    config = fusion_module.FusionConfig()

    def run_iteration() -> None:
        # 100 calls/iteration, not 20: a single `fuse_results()` call is
        # sub-millisecond, which on a noisy/shared runner makes even one
        # scheduler stall dominate the whole measured iteration. A longer
        # per-iteration workload dilutes that noise proportionally.
        for _ in range(100):
            fusion_module.fuse_results(list(candidates), config=config, top_k=25)

    return Benchmark(
        name="fusion_ranking_rrf_500_candidates",
        description=(
            "fuse_results() (hybrid retrieval's reciprocal-rank-fusion combiner) over a "
            "synthetic 500-candidate catalog with lexical/vector/graph signals -- 100 calls "
            "per measured iteration."
        ),
        run_iteration=run_iteration,
        iterations=20,
        warmup=3,
    )


def _make_openapi_generation_benchmark() -> Benchmark:
    from aida.main import app

    def run_iteration() -> None:
        # FastAPI caches `app.openapi_schema` after the first call; clearing
        # it forces a genuine regeneration from the route table every
        # iteration instead of timing a dict lookup.
        app.openapi_schema = None
        app.openapi()

    return Benchmark(
        name="openapi_schema_generation",
        description=(
            "app.openapi() -- the same call TS-4's diff gate exercises -- with FastAPI's "
            "schema cache cleared each iteration so it is regenerated from the route table "
            "every time."
        ),
        run_iteration=run_iteration,
        iterations=8,
        warmup=2,
    )


def all_benchmarks() -> list[Benchmark]:
    """Construct all gated benchmarks. Imports are lazy per-benchmark (see
    each ``_make_*`` function) so `--help` and any test that only exercises
    `find_regressions` don't pay for importing the full application."""
    return [
        _make_sql_guard_benchmark(),
        _make_abac_benchmark(),
        _make_fusion_ranking_benchmark(),
        _make_openapi_generation_benchmark(),
    ]


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def measure(benchmark: Benchmark) -> float:
    """Run `benchmark`, returning the median wall-clock ms per iteration."""
    for _ in range(benchmark.warmup):
        benchmark.run_iteration()

    timings_ms: list[float] = []
    for _ in range(benchmark.iterations):
        start = time.perf_counter()
        benchmark.run_iteration()
        timings_ms.append((time.perf_counter() - start) * 1000)

    return statistics.median(timings_ms)


# ---------------------------------------------------------------------------
# Comparison against baseline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Regression:
    """One benchmark whose current median is more than the threshold slower
    than its committed baseline median."""

    name: str
    baseline_ms: float
    current_ms: float
    pct_change: float

    def __str__(self) -> str:
        return (
            f"[REGRESSION] {self.name}: {self.baseline_ms:.3f}ms -> {self.current_ms:.3f}ms "
            f"({self.pct_change:+.1f}%)"
        )


def find_regressions(
    baseline: dict[str, float], current: dict[str, float], *, threshold_pct: float
) -> list[Regression]:
    """Pure comparison: which benchmarks present in both dicts got more than
    `threshold_pct` slower.

    A benchmark present only in `current` (newly added) or only in
    `baseline` (removed) is informational only, mirroring
    `scripts/openapi_diff.py`'s treatment of added/removed paths -- it is
    never by itself a reason to fail the gate.
    """
    regressions: list[Regression] = []
    for name, baseline_ms in baseline.items():
        if name not in current or baseline_ms <= 0:
            continue
        current_ms = current[name]
        pct_change = (current_ms - baseline_ms) / baseline_ms * 100
        if pct_change > threshold_pct:
            regressions.append(Regression(name, baseline_ms, current_ms, pct_change))
    return regressions


def _load_baseline(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text())
    benchmarks = data.get("benchmarks")
    if not isinstance(benchmarks, dict):
        raise ValueError(f"baseline at {path} has no 'benchmarks' object")
    return {str(name): float(entry["median_ms"]) for name, entry in benchmarks.items()}


def _write_baseline(path: Path, benchmarks: list[Benchmark], medians: dict[str, float]) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "threshold_pct": DEFAULT_THRESHOLD_PCT,
        "benchmarks": {
            b.name: {
                "median_ms": round(medians[b.name], 4),
                "iterations": b.iterations,
                "warmup": b.warmup,
                "description": b.description,
            }
            for b in benchmarks
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=f"Path to the committed baseline (default: {DEFAULT_BASELINE}).",
    )
    parser.add_argument(
        "--accept-baseline",
        action="store_true",
        help=(
            "Regenerate the baseline from current measurements and write it. Run this "
            "deliberately -- after reviewing the reported change -- then commit the updated "
            "baseline file. This is the explicit, auditable path for updating expected "
            "performance, matching scripts/openapi_diff.py's --accept-baseline."
        ),
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=DEFAULT_THRESHOLD_PCT,
        help=(
            f"Fail a benchmark that is more than this percent slower "
            f"(default: {DEFAULT_THRESHOLD_PCT})."
        ),
    )
    parser.add_argument(
        "--no-retry",
        action="store_true",
        help=(
            "Do not re-measure a flagged benchmark before failing. By default a benchmark "
            "that crosses the threshold is re-measured once (a fresh set of iterations) so a "
            "single transient blip on a loaded CI runner does not fail the build by itself; "
            "the regression must reproduce on the retry."
        ),
    )
    args = parser.parse_args(argv)

    # Checked before running any (deliberately slow) benchmark so a missing
    # baseline fails fast instead of burning CI time first.
    if not args.accept_baseline and not args.baseline.exists():
        print(f"::error::No perf baseline found at {args.baseline}.")
        print("Run `uv run python scripts/perf_baseline.py --accept-baseline` to create one.")
        return 1

    benchmarks = all_benchmarks()
    current = {b.name: measure(b) for b in benchmarks}

    if args.accept_baseline:
        _write_baseline(args.baseline, benchmarks, current)
        print(f"Baseline regenerated at {args.baseline}.")
        for b in benchmarks:
            print(f"  {b.name}: {current[b.name]:.3f}ms median over {b.iterations} iterations")
        print("Review `git diff` for that file before committing it.")
        return 0

    baseline = _load_baseline(args.baseline)

    for name, ms in current.items():
        base = baseline.get(name)
        if base is None:
            print(f"[new] {name}: {ms:.3f}ms (no baseline entry yet)")
        else:
            pct = (ms - base) / base * 100 if base else 0.0
            print(f"[measured] {name}: {base:.3f}ms -> {ms:.3f}ms ({pct:+.1f}%)")

    regressions = find_regressions(baseline, current, threshold_pct=args.threshold_pct)

    if regressions and not args.no_retry:
        flagged = {r.name for r in regressions}
        print(
            f"\n{len(regressions)} possible regression(s) crossed {args.threshold_pct}% -- "
            "re-measuring the flagged benchmark(s) once to rule out transient CI-runner noise."
        )
        by_name = {b.name: b for b in benchmarks}
        for name in flagged:
            current[name] = measure(by_name[name])
            base = baseline[name]
            pct = (current[name] - base) / base * 100
            print(f"  [re-measured] {name}: {base:.3f}ms -> {current[name]:.3f}ms ({pct:+.1f}%)")
        regressions = find_regressions(baseline, current, threshold_pct=args.threshold_pct)

    if not regressions:
        print(f"\nNo perf regressions beyond {args.threshold_pct}% against {args.baseline}.")
        return 0

    print(f"\n{len(regressions)} perf regression(s) confirmed against {args.baseline}:")
    for r in regressions:
        print(r)

    print(
        "\n::error::Perf regression(s) reproduced on re-measurement -- not CI-runner noise.\n"
        "If this is an expected consequence of a deliberate change: review why the benchmarked "
        "path got slower, then run `uv run python scripts/perf_baseline.py --accept-baseline` "
        "and commit the refreshed baseline. If it wasn't expected, fix the regression instead."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
