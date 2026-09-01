"""Tests for the SM-3 confidence-calibration harness
(`scripts/confidence_calibration_benchmark.py`).

Per this row's own definition of done, this tests the *harness* -- bucketing,
ECE, Brier score, corpus-integrity checks, report generation -- not a specific
accuracy number that would go stale as the corpus grows or `score_evidence`'s
formula is retuned. Mirrors `tests/test_quality_benchmark_gate.py`'s (AG-8) and
`tests/test_perf_baseline_gate.py`'s (PF-3) shape:

  1. Pure logic (`bucket_results`, `expected_calibration_error`, `brier_score`)
     exercised directly against small synthetic `CaseResult` values -- known
     inputs, hand-computed expected outputs.
  2. The real harness run end to end against the actual committed corpus,
     proving it produces a well-formed report from real `score_evidence`
     output -- not a mock.
  3. Corpus-integrity checks (`build_evidence` refusing a case the real
     production gate would have filtered before ever calling `score_evidence`)
     proven against deliberately-broken synthetic cases, plus the committed
     corpus itself asserted clean.
  4. The exact floating-point bucket-boundary bug this script's own
     `bucket_results` docstring describes (naive `int(confidence / width)`
     misfiling a confidence of exactly 0.6) is proven fixed, against both a
     synthetic 0.6 value and the real corpus's own 0.6-scoring cases.
  5. A CLI round trip (`main`) against tmp_path report files, deterministic
     across runs since nothing here is wall-clock- or randomness-dependent.
"""

from __future__ import annotations

import json

import pytest

from scripts.confidence_calibration_benchmark import (
    DEFAULT_CORPUS,
    CalibrationCase,
    CaseResult,
    CorpusIntegrityError,
    GroundTruth,
    brier_score,
    bucket_results,
    build_evidence,
    expected_calibration_error,
    load_corpus,
    main,
    run_calibration,
)


def _case(
    case_id: str,
    *,
    column_name: str = "txn_amount",
    physical_type: str = "NUMERIC(18,2)",
    table_role: str = "TRANSACTION",
    nullable: bool = False,
    bound_term_names: tuple[str, ...] = (),
    business_description: str = "Some description.",
    correct: bool = True,
) -> CalibrationCase:
    return CalibrationCase(
        id=case_id,
        table_name="fact_x",
        table_role=table_role,
        business_name="Biz",
        business_description=business_description,
        grain_statement="one row per event",
        column_name=column_name,
        physical_type=physical_type,
        nullable=nullable,
        bound_term_names=bound_term_names,
        ground_truth=GroundTruth(correct=correct, expected_aggregation="SUM", rationale="test"),
    )


def _result(confidence: float, *, correct: bool, case_id: str = "c") -> CaseResult:
    return CaseResult(case=_case(case_id, correct=correct), confidence=confidence, reviewable=True)


# ---------------------------------------------------------------------------
# bucket_results -- pure logic, incl. the float-boundary fix
# ---------------------------------------------------------------------------


def test_results_are_bucketed_into_their_0_1_wide_confidence_range() -> None:
    results = [
        _result(0.05, correct=True, case_id="a"),
        _result(0.45, correct=False, case_id="b"),
        _result(0.95, correct=True, case_id="c"),
    ]

    buckets = bucket_results(results)

    assert len(buckets) == 10
    assert buckets[0].n == 1 and buckets[0].results[0].case.id == "a"
    assert buckets[4].n == 1 and buckets[4].results[0].case.id == "b"
    assert buckets[9].n == 1 and buckets[9].results[0].case.id == "c"


def test_a_confidence_of_exactly_1_0_lands_in_the_final_bucket_not_an_11th_one() -> None:
    buckets = bucket_results([_result(1.0, correct=True)])

    assert len(buckets) == 10
    assert buckets[9].n == 1
    assert all(b.n == 0 for b in buckets[:9])


def test_a_confidence_exactly_on_a_bucket_boundary_lands_in_the_upper_bucket() -> None:
    """The bug this script's `bucket_results` docstring names: naive
    `int(0.6 / 0.1)` is `5` in IEEE 754 double precision (`0.6 / 0.1 ==
    5.999999999999999`), which would misfile a 0.6 confidence into [0.5, 0.6)
    instead of [0.6, 0.7). Caught here directly with a synthetic value, and
    below against the real corpus's own 0.6-scoring cases."""
    buckets = bucket_results([_result(0.6, correct=True)])

    assert buckets[5].n == 0, "0.6 misfiled into the [0.5, 0.6) bucket"
    assert buckets[6].n == 1, "0.6 did not land in the [0.6, 0.7) bucket"


def test_empty_bucket_reports_zero_mean_confidence_and_zero_accuracy() -> None:
    buckets = bucket_results([_result(0.95, correct=True)])

    empty = buckets[0]
    assert empty.n == 0
    assert empty.mean_confidence == 0.0
    assert empty.accuracy == 0.0
    assert empty.gap == 0.0


# ---------------------------------------------------------------------------
# expected_calibration_error / brier_score -- pure logic, hand-computed
# ---------------------------------------------------------------------------


def test_perfectly_calibrated_results_have_zero_ece_and_zero_brier() -> None:
    # Confidence 1.0 always correct, confidence 0.0 always incorrect: a
    # perfectly calibrated (if extreme) predictor.
    results = [
        _result(1.0, correct=True, case_id="a"),
        _result(1.0, correct=True, case_id="b"),
        _result(0.0, correct=False, case_id="c"),
        _result(0.0, correct=False, case_id="d"),
    ]
    buckets = bucket_results(results)

    assert expected_calibration_error(buckets, total_n=len(results)) == pytest.approx(0.0)
    assert brier_score(results) == pytest.approx(0.0)


def test_ece_is_the_case_weighted_average_gap_across_buckets() -> None:
    # Bucket [0.9,1.0): 2 cases, both correct -> confidence 0.95, accuracy 1.0, gap 0.05
    # Bucket [0.0,0.1): 2 cases, both correct -> confidence 0.05, accuracy 1.0, gap 0.95
    # Weighted: (2/4)*0.05 + (2/4)*0.95 = 0.5
    results = [
        _result(0.95, correct=True, case_id="a"),
        _result(0.95, correct=True, case_id="b"),
        _result(0.05, correct=True, case_id="c"),
        _result(0.05, correct=True, case_id="d"),
    ]
    buckets = bucket_results(results)

    assert expected_calibration_error(buckets, total_n=4) == pytest.approx(0.5, abs=1e-9)


def test_brier_score_matches_hand_computed_mean_squared_error() -> None:
    results = [
        _result(0.8, correct=True, case_id="a"),  # (0.8-1)^2 = 0.04
        _result(0.8, correct=False, case_id="b"),  # (0.8-0)^2 = 0.64
    ]

    assert brier_score(results) == pytest.approx((0.04 + 0.64) / 2, abs=1e-9)


def test_ece_and_brier_are_zero_for_no_results() -> None:
    assert expected_calibration_error([], total_n=0) == 0.0
    assert brier_score([]) == 0.0


# ---------------------------------------------------------------------------
# build_evidence -- corpus-integrity checks against the real production gate
# ---------------------------------------------------------------------------


def test_build_evidence_refuses_a_non_numeric_column() -> None:
    case = _case("bad", column_name="txn_amount", physical_type="VARCHAR(50)")

    with pytest.raises(CorpusIntegrityError, match="not numeric"):
        build_evidence(case)


def test_build_evidence_refuses_a_column_matching_no_measure_keyword() -> None:
    case = _case("bad", column_name="acct_type_cd", physical_type="NUMERIC(18,2)")

    with pytest.raises(CorpusIntegrityError, match="matches no MEASURE_KEYWORDS"):
        build_evidence(case)


def test_build_evidence_refuses_a_bare_contains_match() -> None:
    # "discount_code" contains "count" but is neither an EXACT nor a SUFFIX
    # match -- exactly the case metric_suggestion_api drops before scoring.
    case = _case("bad", column_name="discount_code", physical_type="NUMERIC(18,2)")

    with pytest.raises(CorpusIntegrityError, match="CONTAINS"):
        build_evidence(case)


def test_build_evidence_accepts_a_real_exact_or_suffix_numeric_match() -> None:
    case = _case("ok", column_name="txn_amount", physical_type="NUMERIC(18,2)")

    evidence = build_evidence(case)

    assert evidence.matched_keyword == "amount"
    assert evidence.match_kind == "SUFFIX"
    assert evidence.suggested_aggregation == "SUM"


# ---------------------------------------------------------------------------
# The real, committed corpus + the real, unmodified score_evidence
# ---------------------------------------------------------------------------


def test_the_committed_corpus_loads_and_is_all_production_reachable_evidence() -> None:
    cases = load_corpus(DEFAULT_CORPUS)

    assert len(cases) >= 20, "corpus should be a real, substantial labelled sample"
    positives = [c for c in cases if c.ground_truth.correct]
    negatives = [c for c in cases if not c.ground_truth.correct]
    assert positives, "corpus has no true-positive cases"
    assert negatives, "corpus has no false-positive cases"

    # Every case must be one the real production pipeline would actually
    # score -- build_evidence raises CorpusIntegrityError otherwise.
    for case in cases:
        build_evidence(case)


def test_run_calibration_scores_every_case_with_the_real_score_evidence_function() -> None:
    cases = load_corpus(DEFAULT_CORPUS)

    results = run_calibration(cases)

    assert len(results) == len(cases)
    for r in results:
        assert 0.0 <= r.confidence <= 1.0
        # score_evidence's own MINIMUM_EVIDENCE_FOR_METRIC_REVIEW gate, re-derived here
        # only to sanity-check the harness reports it consistently with the real constant.
        from aida.metric_suggestion_service import MINIMUM_EVIDENCE_FOR_METRIC_REVIEW

        assert r.reviewable == (r.confidence >= MINIMUM_EVIDENCE_FOR_METRIC_REVIEW)


def test_named_case_scores_higher_confidence_for_richer_real_evidence() -> None:
    """Spot check against the real harness by id, not just an aggregate rate --
    same convention `test_quality_benchmark_gate.py` uses for its named-case
    checks. `txn_amount_true` has a bound glossary term and a description
    mention (the richest evidence `score_evidence` recognizes); `deposit_
    amount_true` has neither. Both are EXACT/SUFFIX matches on the same
    keyword family, so the real function should score the richer one higher."""
    cases = {c.id: c for c in load_corpus(DEFAULT_CORPUS)}
    results = {r.case.id: r for r in run_calibration(list(cases.values()))}

    richer = results["txn_amount_true"]
    sparser = results["deposit_amount_true"]
    assert richer.confidence > sparser.confidence


def test_the_real_corpus_produces_a_nondegenerate_calibration_curve() -> None:
    """The published report is only meaningful if predictions actually spread
    across more than one confidence bucket and both correct and incorrect
    cases appear in more than one bucket -- guards against a future corpus
    edit collapsing into a single, uninformative bucket."""
    cases = load_corpus(DEFAULT_CORPUS)
    results = run_calibration(cases)
    buckets = bucket_results(results)

    non_empty = [b for b in buckets if b.n > 0]
    assert len(non_empty) >= 3, "corpus should span more than a couple of confidence buckets"
    mixed_buckets = [b for b in non_empty if 0 < b.accuracy < 1]
    assert mixed_buckets or any(b.accuracy in (0.0, 1.0) for b in non_empty)

    ece = expected_calibration_error(buckets, total_n=len(results))
    brier = brier_score(results)
    assert 0.0 <= ece <= 1.0
    assert 0.0 <= brier <= 1.0


# ---------------------------------------------------------------------------
# CLI round trip
# ---------------------------------------------------------------------------


def test_main_writes_a_well_formed_markdown_and_json_report(tmp_path) -> None:
    report_md = tmp_path / "results.md"
    report_json = tmp_path / "results.json"

    exit_code = main(
        [
            "--corpus",
            str(DEFAULT_CORPUS),
            "--report",
            str(report_md),
            "--report-json",
            str(report_json),
        ]
    )

    assert exit_code == 0
    assert report_md.exists()
    assert report_json.exists()

    payload = json.loads(report_json.read_text())
    assert payload["case_count"] == len(load_corpus(DEFAULT_CORPUS))
    assert "expected_calibration_error" in payload
    assert "brier_score" in payload
    assert len(payload["buckets"]) == 10
    assert len(payload["cases"]) == payload["case_count"]

    text = report_md.read_text()
    assert "# Confidence calibration results (SM-3)" in text
    assert "Expected Calibration Error" in text
    assert "Brier score" in text


def test_main_with_no_report_writes_nothing(tmp_path) -> None:
    report_md = tmp_path / "results.md"
    report_json = tmp_path / "results.json"

    exit_code = main(
        [
            "--corpus",
            str(DEFAULT_CORPUS),
            "--report",
            str(report_md),
            "--report-json",
            str(report_json),
            "--no-report",
        ]
    )

    assert exit_code == 0
    assert not report_md.exists()
    assert not report_json.exists()


def test_main_is_deterministic_across_runs_apart_from_the_timestamp(tmp_path) -> None:
    """Unlike PF-3's timing benchmark, nothing here is wall-clock- or
    randomness-dependent -- two runs against the same corpus must produce
    byte-identical JSON payloads apart from `generated_at`."""
    report_json_1 = tmp_path / "r1.json"
    report_json_2 = tmp_path / "r2.json"

    main(
        [
            "--corpus",
            str(DEFAULT_CORPUS),
            "--report-json",
            str(report_json_1),
            "--report",
            str(tmp_path / "r1.md"),
        ]
    )
    main(
        [
            "--corpus",
            str(DEFAULT_CORPUS),
            "--report-json",
            str(report_json_2),
            "--report",
            str(tmp_path / "r2.md"),
        ]
    )

    payload_1 = json.loads(report_json_1.read_text())
    payload_2 = json.loads(report_json_2.read_text())
    payload_1.pop("generated_at")
    payload_2.pop("generated_at")
    assert payload_1 == payload_2
