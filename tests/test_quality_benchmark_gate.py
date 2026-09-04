"""Tests for the quality/accuracy regression gate (tracker AG-8).

Mirrors `tests/test_perf_baseline_gate.py`'s (PF-3) shape:

  1. The pure comparison logic (`find_regressions`) is exercised directly
     against small synthetic baseline/current dicts.
  2. The real benchmark harness is run end to end -- `seed_catalog` +
     `run_retrieval_benchmark` + `run_tool_selection_benchmark` -- against the
     actual committed corpus files, proving specific, named cases resolve the
     way the corpus calibration (recorded in `scripts/quality_benchmark.py`'s
     module docstring) says they should, not just an aggregate rate.
  3. The gate is proven to catch a *real* regression -- retrieval genuinely
     returning nothing -- rather than only a synthetic classifier test,
     matching PF-3's and `test_openapi_diff_gate.py`'s own definition of done.
  4. The CLI round-trips `--accept-baseline` then a normal compare run cleanly
     against a tmp_path baseline/report, with no wall-clock stubbing needed:
     unlike PF-3's timing, these metrics are deterministic across runs.
  5. The committed baseline is asserted to have an entry for every metric this
     file's `TRACKED_METRICS` tracks, so the gate starts green.
"""

from __future__ import annotations

import json

import pytest

from scripts.quality_benchmark import (
    CORPUS_DIR,
    DEFAULT_BASELINE,
    DEFAULT_REPORT,
    TRACKED_METRICS,
    MetricRegression,
    _load_baseline,
    _make_session,
    check_model_generation_posture,
    find_regressions,
    load_retrieval_corpus,
    load_tool_selection_corpus,
    main,
    run_retrieval_benchmark,
    run_tool_selection_benchmark,
    seed_catalog,
)

# ---------------------------------------------------------------------------
# find_regressions -- pure comparison logic
# ---------------------------------------------------------------------------


def test_metric_drop_beyond_threshold_is_a_regression() -> None:
    baseline = {"retrieval_hit_at_1_rate": 0.90}
    current = {"retrieval_hit_at_1_rate": 0.80}  # -10 points

    regressions = find_regressions(baseline, current, threshold_points=5.0)

    assert len(regressions) == 1
    assert regressions[0].name == "retrieval_hit_at_1_rate"
    assert regressions[0].point_change == pytest.approx(-10.0)


def test_metric_drop_within_threshold_is_not_a_regression() -> None:
    baseline = {"retrieval_hit_at_1_rate": 0.90}
    current = {"retrieval_hit_at_1_rate": 0.87}  # -3 points, under a 5-point threshold

    assert find_regressions(baseline, current, threshold_points=5.0) == []


def test_metric_improvement_is_not_a_regression() -> None:
    baseline = {"retrieval_hit_at_1_rate": 0.80}
    current = {"retrieval_hit_at_1_rate": 1.0}

    assert find_regressions(baseline, current, threshold_points=5.0) == []


def test_metric_missing_from_current_is_ignored() -> None:
    baseline = {"a": 0.9, "b": 0.5}
    current = {"a": 0.9}

    assert find_regressions(baseline, current, threshold_points=5.0) == []


def test_metric_missing_from_baseline_is_ignored() -> None:
    baseline = {"a": 0.9}
    current = {"a": 0.9, "b": 0.1}

    assert find_regressions(baseline, current, threshold_points=5.0) == []


def test_regression_str_reports_direction_and_magnitude() -> None:
    r = MetricRegression(name="retrieval_mrr", baseline=0.9, current=0.7, point_change=-20.0)

    assert "REGRESSION" in str(r)
    assert "retrieval_mrr" in str(r)
    assert "-20.00 points" in str(r)


# ---------------------------------------------------------------------------
# The real harness, against the actual committed corpora
# ---------------------------------------------------------------------------


async def test_retrieval_benchmark_resolves_named_cases_as_calibrated() -> None:
    """Spot-checks specific, named corpus cases -- not just an aggregate rate --
    against the real live `GovernedRetriever.retrieve` path, matching the ranks
    `scripts/quality_benchmark.py`'s module docstring records as measured."""
    session, engine = await _make_session()
    try:
        catalog = await seed_catalog(session)
        cases = load_retrieval_corpus(CORPUS_DIR / "retrieval_quality_corpus.json")
        report = await run_retrieval_benchmark(session, catalog, cases)
    finally:
        await session.close()
        await engine.dispose()

    by_id = {r.case.id: r for r in report.results}
    assert by_id["orders-lexical-top1"].rank == 1
    assert by_id["governed-tool-top1"].rank == 1
    assert by_id["customer-lookup-tool-outranks"].rank == 2
    assert by_id["orders-related-customer-recall"].within_bound

    # Aggregate metrics stay within a generous band of the committed baseline's
    # measured values (0.8333 / 1.0 / 0.9028) -- generous because this test's job
    # is proving the harness resolves real cases correctly, not re-asserting the
    # exact baseline (the CLI regression gate below already does that job).
    assert report.hit_at_1_rate >= 0.75
    assert report.recall_within_bound_rate == 1.0
    assert report.mrr >= 0.85


async def test_tool_selection_benchmark_resolves_named_cases_as_calibrated() -> None:
    session, engine = await _make_session()
    try:
        catalog = await seed_catalog(session)
        cases = load_tool_selection_corpus(CORPUS_DIR / "tool_selection_corpus.json")
        report = await run_tool_selection_benchmark(session, catalog, cases)
    finally:
        await session.close()
        await engine.dispose()

    by_id = {r.case.id: r for r in report.results}
    assert by_id["approved-tool-analyst-selected"].actual_strategy == "GOVERNED_TOOL"
    assert by_id["approved-tool-analyst-selected"].actual_tool_key == "customer-account-summary"
    assert by_id["approved-tool-role-denied-falls-to-development-sql"].actual_strategy == (
        "DEVELOPMENT_SQL"
    )
    assert by_id["approved-tool-role-denied-requires-generation"].actual_strategy == (
        "MODEL_GENERATION"
    )
    assert report.pass_rate == 1.0


def test_model_generation_posture_is_honest_about_this_sandbox() -> None:
    """This repo's dev/test settings carry no model credentials -- proving the
    posture check reports that truthfully rather than defaulting to an
    unverifiable 'maybe'."""
    posture = check_model_generation_posture()

    assert posture.model_generation_enabled is False
    assert posture.activatable is False


# ---------------------------------------------------------------------------
# The gate actually catches a real regression, not just a synthetic one
# ---------------------------------------------------------------------------


async def test_gate_catches_retrieval_genuinely_returning_nothing(monkeypatch) -> None:
    import aida.agent_intelligence as agent_intelligence_module

    session, engine = await _make_session()
    try:
        catalog = await seed_catalog(session)
        cases = load_retrieval_corpus(CORPUS_DIR / "retrieval_quality_corpus.json")
        baseline_report = await run_retrieval_benchmark(session, catalog, cases)

        async def _empty_retrieve(self, session, *, datasource, question, **kwargs):
            return []

        monkeypatch.setattr(
            agent_intelligence_module.GovernedRetriever, "retrieve", _empty_retrieve
        )
        broken_report = await run_retrieval_benchmark(session, catalog, cases)
    finally:
        await session.close()
        await engine.dispose()

    regressions = find_regressions(
        {"retrieval_hit_at_1_rate": baseline_report.hit_at_1_rate},
        {"retrieval_hit_at_1_rate": broken_report.hit_at_1_rate},
        threshold_points=5.0,
    )

    assert broken_report.hit_at_1_rate == 0.0
    assert len(regressions) == 1
    assert regressions[0].name == "retrieval_hit_at_1_rate"


# ---------------------------------------------------------------------------
# CLI wiring, end to end -- deterministic, no wall-clock stubbing needed
# ---------------------------------------------------------------------------


def test_accept_baseline_then_compare_round_trips_clean(tmp_path) -> None:
    baseline_path = tmp_path / "quality-benchmark-baseline.json"
    report_path = tmp_path / "quality-benchmark-results.md"

    accept_rc = main(
        ["--baseline", str(baseline_path), "--report", str(report_path), "--accept-baseline"]
    )
    assert accept_rc == 0
    assert baseline_path.exists()
    assert report_path.exists()

    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert set(data["metrics"]) == set(TRACKED_METRICS)

    # Same corpus, same seeded catalog, same code -- an exact match, not
    # noise-dependent the way PF-3's wall-clock measurement is.
    compare_rc = main(["--baseline", str(baseline_path), "--report", str(report_path)])
    assert compare_rc == 0


def test_missing_baseline_file_fails_with_guidance(tmp_path, capsys) -> None:
    missing = tmp_path / "does-not-exist.json"

    rc = main(["--baseline", str(missing), "--no-report"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "--accept-baseline" in out


# ---------------------------------------------------------------------------
# Baseline freshness -- the gate must start green and stay wired to reality.
# ---------------------------------------------------------------------------


def test_committed_baseline_has_an_entry_for_every_tracked_metric() -> None:
    assert DEFAULT_BASELINE.exists(), (
        f"quality baseline missing at {DEFAULT_BASELINE}; regenerate with "
        "`uv run python scripts/quality_benchmark.py --accept-baseline`."
    )
    baseline = _load_baseline(DEFAULT_BASELINE)

    assert set(TRACKED_METRICS) <= set(baseline), (
        "Committed quality baseline is missing an entry for a tracked metric. "
        "Regenerate it with `uv run python scripts/quality_benchmark.py --accept-baseline`."
    )


def test_default_paths_live_under_docs_reference() -> None:
    assert DEFAULT_BASELINE.parts[-3:] == (
        "Docs",
        "90-reference",
        "quality-benchmark-baseline.json",
    )
    assert DEFAULT_REPORT.parts[-3:] == ("Docs", "90-reference", "quality-benchmark-results.md")


def test_committed_corpus_files_exist_and_parse() -> None:
    retrieval_cases = load_retrieval_corpus(CORPUS_DIR / "retrieval_quality_corpus.json")
    tool_cases = load_tool_selection_corpus(CORPUS_DIR / "tool_selection_corpus.json")

    assert len(retrieval_cases) > 0
    assert len(tool_cases) > 0
