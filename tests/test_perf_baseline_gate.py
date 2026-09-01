"""Tests for the CI performance regression gate (tracker PF-3).

Three things are covered:

  1. The pure comparison logic (`find_regressions`) is exercised directly
     against small synthetic baseline/current dicts, so the threshold and
     "informational only if a benchmark is missing on either side" rules are
     verified without needing to run any real benchmark.
  2. `measure()` is proven to actually catch a slowdown in a *real*
     benchmarked function -- `aida.policy_engine.evaluate`, the engine wired
     into the query-execution path, wrapped with an artificial `time.sleep`
     -- mirroring how `tests/test_openapi_diff_gate.py` proves
     `openapi_diff.py`'s gate catches a real breaking API change rather than
     only testing the classifier against synthetic fixtures.
  3. The committed baseline (`Docs/90-reference/perf-baseline.json`) is
     asserted to have an entry for every benchmark `all_benchmarks()`
     currently defines, so the gate starts green and a newly added benchmark
     doesn't silently go unenforced.
"""

from __future__ import annotations

import json
import time

from scripts.perf_baseline import (
    DEFAULT_BASELINE,
    Benchmark,
    Regression,
    _load_baseline,
    all_benchmarks,
    find_regressions,
    main,
    measure,
)

# ---------------------------------------------------------------------------
# find_regressions -- pure comparison logic
# ---------------------------------------------------------------------------


def test_slower_benchmark_beyond_threshold_is_a_regression() -> None:
    baseline = {"a": 100.0}
    current = {"a": 130.0}  # +30%

    regressions = find_regressions(baseline, current, threshold_pct=20.0)

    assert len(regressions) == 1
    assert regressions[0].name == "a"
    assert regressions[0].pct_change == 30.0


def test_slower_benchmark_within_threshold_is_not_a_regression() -> None:
    baseline = {"a": 100.0}
    current = {"a": 115.0}  # +15%, under a 20% threshold

    assert find_regressions(baseline, current, threshold_pct=20.0) == []


def test_exactly_at_threshold_is_not_a_regression() -> None:
    """`> threshold_pct`, not `>=` -- exactly the threshold passes."""
    baseline = {"a": 100.0}
    current = {"a": 120.0}  # exactly +20%

    assert find_regressions(baseline, current, threshold_pct=20.0) == []


def test_just_over_threshold_is_a_regression() -> None:
    baseline = {"a": 100.0}
    current = {"a": 120.01}

    regressions = find_regressions(baseline, current, threshold_pct=20.0)

    assert len(regressions) == 1


def test_faster_benchmark_is_not_a_regression() -> None:
    baseline = {"a": 100.0}
    current = {"a": 50.0}

    assert find_regressions(baseline, current, threshold_pct=20.0) == []


def test_benchmark_missing_from_current_is_ignored() -> None:
    """A removed benchmark is not itself a regression -- there's nothing to compare."""
    baseline = {"a": 100.0, "b": 50.0}
    current = {"a": 100.0}

    assert find_regressions(baseline, current, threshold_pct=20.0) == []


def test_benchmark_missing_from_baseline_is_ignored() -> None:
    """A newly added benchmark has nothing to regress against yet."""
    baseline = {"a": 100.0}
    current = {"a": 100.0, "b": 999.0}

    assert find_regressions(baseline, current, threshold_pct=20.0) == []


def test_zero_baseline_is_ignored_rather_than_dividing_by_zero() -> None:
    baseline = {"a": 0.0}
    current = {"a": 5.0}

    assert find_regressions(baseline, current, threshold_pct=20.0) == []


def test_regression_str_reports_direction_and_magnitude() -> None:
    r = Regression(name="a", baseline_ms=100.0, current_ms=150.0, pct_change=50.0)

    assert "REGRESSION" in str(r)
    assert "a" in str(r)
    assert "+50.0%" in str(r)


# ---------------------------------------------------------------------------
# measure() -- the timing harness itself
# ---------------------------------------------------------------------------


def test_measure_reports_a_plausible_median_for_a_known_duration() -> None:
    bench = Benchmark(
        name="sleep-1ms",
        description="synthetic: sleeps ~1ms per iteration",
        run_iteration=lambda: time.sleep(0.001),
        iterations=5,
        warmup=1,
    )

    median_ms = measure(bench)

    # Real wall-clock sleep is never exact, but it should be well within an
    # order of magnitude of the requested 1ms, not (say) near-zero from a
    # broken timer or absurdly large from a broken loop.
    assert 0.5 <= median_ms <= 20.0


# ---------------------------------------------------------------------------
# The gate actually catches a real regression (not just the synthetic
# classifier above) -- matches how TS-4's own suite proves its diff gate
# catches a real breaking change, per PF-3's definition of done.
# ---------------------------------------------------------------------------


def test_gate_catches_an_artificial_slowdown_in_a_real_benchmarked_function(monkeypatch) -> None:
    import aida.policy_engine as policy_engine_module
    from scripts.perf_baseline import _make_policy_engine_benchmark

    # Baseline: the real, unmodified policy_engine.evaluate().
    baseline_ms = measure(_make_policy_engine_benchmark())

    # Wrap the real benchmarked function with an artificial delay -- the
    # same technique the task's own definition of done calls for, and the
    # same idea as TS-4's test suite proving its gate catches a real
    # breaking change rather than only a synthetic one.
    original_evaluate = policy_engine_module.evaluate

    def slow_evaluate(*args: object, **kwargs: object) -> object:
        time.sleep(0.002)  # +2ms/call * 50 calls/iteration dwarfs the ~0.7ms baseline
        return original_evaluate(*args, **kwargs)

    monkeypatch.setattr(policy_engine_module, "evaluate", slow_evaluate)

    # `_make_policy_engine_benchmark()` re-imports `policy_engine.evaluate` fresh
    # each call, so calling it now (after the patch) picks up the slow version.
    current_ms = measure(_make_policy_engine_benchmark())

    regressions = find_regressions(
        {"policy_engine_evaluate_500_policies": baseline_ms},
        {"policy_engine_evaluate_500_policies": current_ms},
        threshold_pct=20.0,
    )

    assert len(regressions) == 1
    assert regressions[0].name == "policy_engine_evaluate_500_policies"
    assert current_ms > baseline_ms


def test_gate_passes_when_nothing_regressed() -> None:
    from scripts.perf_baseline import _make_policy_engine_benchmark

    baseline_ms = measure(_make_policy_engine_benchmark())
    current_ms = measure(_make_policy_engine_benchmark())  # unmodified, same code path

    # A generous threshold, not the production 20.0 default: this proves the
    # gate's *direction* -- unmodified real code isn't flagged -- using real
    # wall-clock timing, which on a heavily loaded shared machine (as opposed
    # to a dedicated CI runner) can otherwise land right at a tight boundary
    # for a fast benchmark. The exact-threshold boundary behavior itself is
    # already covered precisely by the synthetic `find_regressions` tests
    # above, which carry no timing risk.
    regressions = find_regressions(
        {"policy_engine_evaluate_500_policies": baseline_ms},
        {"policy_engine_evaluate_500_policies": current_ms},
        threshold_pct=200.0,
    )

    assert regressions == []


# ---------------------------------------------------------------------------
# CLI wiring, end to end
# ---------------------------------------------------------------------------


def test_accept_baseline_then_compare_round_trips_clean(tmp_path, monkeypatch) -> None:
    """CLI wiring, not timing precision: `measure()` is stubbed to a fixed value so
    this only exercises `main()`'s argument handling, file I/O, and JSON schema --
    real wall-clock measurement is proven separately (and is legitimately noisy on
    a heavily loaded shared machine, which this test must not flake on)."""
    import scripts.perf_baseline as perf_baseline_module

    monkeypatch.setattr(perf_baseline_module, "measure", lambda benchmark: 10.0)

    baseline_path = tmp_path / "perf-baseline.json"

    accept_rc = main(["--baseline", str(baseline_path), "--accept-baseline"])
    assert accept_rc == 0
    assert baseline_path.exists()

    data = json.loads(baseline_path.read_text())
    assert "benchmarks" in data
    names = set(data["benchmarks"])
    assert names == {b.name for b in all_benchmarks()}
    for entry in data["benchmarks"].values():
        assert entry["median_ms"] == 10.0
        assert entry["iterations"] > 0

    # Same stubbed 10.0ms on both sides -- an exact match, not noise-dependent.
    compare_rc = main(["--baseline", str(baseline_path)])
    assert compare_rc == 0


def test_missing_baseline_file_fails_with_guidance(tmp_path, capsys) -> None:
    missing = tmp_path / "does-not-exist.json"

    rc = main(["--baseline", str(missing)])

    assert rc == 1
    out = capsys.readouterr().out
    assert "--accept-baseline" in out


# ---------------------------------------------------------------------------
# Baseline freshness -- the gate must start green and stay wired to reality.
# ---------------------------------------------------------------------------


def test_committed_baseline_has_an_entry_for_every_current_benchmark() -> None:
    assert DEFAULT_BASELINE.exists(), (
        f"perf baseline missing at {DEFAULT_BASELINE}; regenerate with "
        "`uv run python scripts/perf_baseline.py --accept-baseline`."
    )
    baseline = _load_baseline(DEFAULT_BASELINE)
    current_names = {b.name for b in all_benchmarks()}

    assert current_names <= set(baseline), (
        "Committed perf baseline is missing an entry for a current benchmark. "
        "Regenerate it with `uv run python scripts/perf_baseline.py --accept-baseline`."
    )


def test_default_baseline_path_lives_under_docs_reference() -> None:
    assert DEFAULT_BASELINE.parts[-3:] == ("Docs", "90-reference", "perf-baseline.json")
