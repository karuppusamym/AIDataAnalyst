"""CN-7 -- per-connector health scoring (`aida.connector_health`).

Pure-logic tests, no database: every factor and the composite score are
deterministic functions of plain `ConnectorRunSample` dataclasses, mirroring
`tests/test_trust_scoring.py` and `tests/test_knowledge_graph.py`'s own
"pure logic tested without a database" convention. The DB-facing aggregation
(`aida.fleet.datasource_health` / `fleet_health`) has its own integration
tests in `tests/test_operational_behaviors.py`.
"""

from datetime import UTC, datetime
from uuid import uuid4

from aida.connector_health import (
    ConnectorRunSample,
    HealthFactor,
    _last_success,
    _score_datasource_enablement,
    _score_failure_streak,
    _score_profiling_coverage,
    _score_run_success_rate,
    _score_staleness,
    compute_connector_health,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _run(
    status: str,
    *,
    minutes_ago: float = 0,
    error_class: str | None = None,
    discovered_tables: int = 10,
    profiled_tables: int = 10,
) -> ConnectorRunSample:
    return ConnectorRunSample(
        status=status,
        finished_at=NOW - _delta(minutes_ago),
        error_class=error_class,
        discovered_tables=discovered_tables,
        profiled_tables=profiled_tables,
    )


def _delta(minutes: float):
    from datetime import timedelta

    return timedelta(minutes=minutes)


# --- _score_run_success_rate ---


def test_success_rate_no_terminal_runs_is_neutral() -> None:
    factor = _score_run_success_rate([_run("QUEUED"), _run("RUNNING")])
    assert factor.score == 17.5
    assert factor.maximum == 35.0
    assert factor.evidence["terminal_runs"] == 0


def test_success_rate_all_successful() -> None:
    runs = [_run("COMPLETED", minutes_ago=m) for m in (10, 20, 30)]
    factor = _score_run_success_rate(runs)
    assert factor.score == 35.0
    assert factor.evidence["success_rate"] == 1.0


def test_success_rate_all_failed() -> None:
    runs = [_run("FAILED", minutes_ago=m) for m in (10, 20, 30)]
    factor = _score_run_success_rate(runs)
    assert factor.score == 0.0


def test_success_rate_mixed() -> None:
    runs = [
        _run("COMPLETED", minutes_ago=10),
        _run("FAILED", minutes_ago=20),
        _run("COMPLETED", minutes_ago=30),
        _run("CANCELLED", minutes_ago=40),
    ]
    factor = _score_run_success_rate(runs)
    assert factor.evidence["successful_runs"] == 2
    assert factor.evidence["terminal_runs"] == 4
    assert factor.score == 17.5  # 35 * 0.5


def test_success_rate_ignores_active_runs() -> None:
    runs = [
        _run("COMPLETED", minutes_ago=10),
        _run("QUEUED", minutes_ago=1),
        _run("RUNNING", minutes_ago=2),
    ]
    factor = _score_run_success_rate(runs)
    assert factor.evidence["terminal_runs"] == 1
    assert factor.score == 35.0


# --- _score_staleness ---


def test_staleness_no_successful_run_scores_zero() -> None:
    factor = _score_staleness([_run("FAILED", minutes_ago=5)], now=NOW, scan_interval_minutes=60)
    assert factor.score == 0.0
    assert factor.evidence["last_success_at"] is None


def test_staleness_within_interval_is_full_score() -> None:
    runs = [_run("COMPLETED", minutes_ago=10)]
    factor = _score_staleness(runs, now=NOW, scan_interval_minutes=60)
    assert factor.score == 25.0


def test_staleness_exactly_double_interval_scores_zero() -> None:
    runs = [_run("COMPLETED", minutes_ago=120)]
    factor = _score_staleness(runs, now=NOW, scan_interval_minutes=60)
    assert factor.score == 0.0


def test_staleness_degrades_linearly_past_interval() -> None:
    runs = [_run("COMPLETED", minutes_ago=90)]
    factor = _score_staleness(runs, now=NOW, scan_interval_minutes=60)
    # ratio = 1.5 -> score = 25 - 0.5*25 = 12.5
    assert factor.score == 12.5


def test_staleness_no_schedule_uses_fixed_thresholds() -> None:
    fresh = _score_staleness(
        [_run("COMPLETED", minutes_ago=30)], now=NOW, scan_interval_minutes=None
    )
    stale_day = _score_staleness(
        [_run("COMPLETED", minutes_ago=200)], now=NOW, scan_interval_minutes=None
    )
    very_stale = _score_staleness(
        [_run("COMPLETED", minutes_ago=10000)], now=NOW, scan_interval_minutes=None
    )
    assert fresh.score == 25.0
    assert stale_day.score == 18.0
    assert very_stale.score == 0.0


def test_staleness_picks_the_most_recent_success_even_if_not_first_in_list() -> None:
    runs = [
        _run("FAILED", minutes_ago=1),
        _run("COMPLETED", minutes_ago=5),
        _run("COMPLETED", minutes_ago=500),
    ]
    factor = _score_staleness(runs, now=NOW, scan_interval_minutes=60)
    assert factor.evidence["age_minutes"] == 5.0


# --- _score_failure_streak ---


def test_failure_streak_zero_when_latest_run_succeeded() -> None:
    runs = [_run("COMPLETED", minutes_ago=1), _run("FAILED", minutes_ago=2)]
    factor, streak = _score_failure_streak(runs)
    assert streak == 0
    assert factor.score == 20.0


def test_failure_streak_counts_consecutive_failures_from_the_top() -> None:
    runs = [
        _run("FAILED", minutes_ago=1),
        _run("FAILED", minutes_ago=2),
        _run("COMPLETED", minutes_ago=3),
    ]
    factor, streak = _score_failure_streak(runs)
    assert streak == 2
    assert factor.score == 6.0


def test_failure_streak_of_three_or_more_scores_zero_and_is_capped() -> None:
    runs = [_run("FAILED", minutes_ago=m) for m in (1, 2, 3, 4, 5)]
    factor, streak = _score_failure_streak(runs)
    assert streak == 5
    assert factor.score == 0.0


def test_failure_streak_ignores_non_terminal_runs_interleaved() -> None:
    runs = [
        _run("QUEUED", minutes_ago=0),
        _run("FAILED", minutes_ago=1),
        _run("COMPLETED", minutes_ago=2),
    ]
    factor, streak = _score_failure_streak(runs)
    assert streak == 1
    assert factor.evidence["most_recent_error_class"] is None


def test_failure_streak_captures_error_class() -> None:
    runs = [_run("FAILED", minutes_ago=1, error_class="ConnectionTimeout")]
    factor, streak = _score_failure_streak(runs)
    assert factor.evidence["most_recent_error_class"] == "ConnectionTimeout"


# --- _score_profiling_coverage ---


def test_profiling_coverage_full() -> None:
    runs = [_run("COMPLETED", minutes_ago=1, discovered_tables=20, profiled_tables=20)]
    factor = _score_profiling_coverage(runs)
    assert factor.score == 10.0


def test_profiling_coverage_partial() -> None:
    runs = [_run("COMPLETED", minutes_ago=1, discovered_tables=20, profiled_tables=5)]
    factor = _score_profiling_coverage(runs)
    assert factor.score == 2.5


def test_profiling_coverage_no_success_is_neutral() -> None:
    factor = _score_profiling_coverage([_run("FAILED", minutes_ago=1)])
    assert factor.score == 5.0


def test_profiling_coverage_zero_discovered_is_neutral() -> None:
    runs = [_run("COMPLETED", minutes_ago=1, discovered_tables=0, profiled_tables=0)]
    factor = _score_profiling_coverage(runs)
    assert factor.score == 5.0


# --- _score_datasource_enablement ---


def test_enablement_disabled_scores_zero() -> None:
    factor = _score_datasource_enablement("DISABLED")
    assert factor.score == 0.0


def test_enablement_active_scores_full() -> None:
    factor = _score_datasource_enablement("ACTIVE")
    assert factor.score == 10.0


# --- _last_success ---


def test_last_success_none_when_no_successes() -> None:
    assert _last_success([_run("FAILED", minutes_ago=1)]) is None


def test_last_success_picks_most_recent() -> None:
    newest = _run("COMPLETED", minutes_ago=1)
    result = _last_success([_run("COMPLETED", minutes_ago=50), newest])
    assert result is newest


# --- compute_connector_health (composite) ---


def test_weights_sum_to_one_hundred() -> None:
    datasource_id = uuid4()
    result = compute_connector_health(
        datasource_id=datasource_id,
        datasource_status="ACTIVE",
        runs=[],
        scan_interval_minutes=None,
        now=NOW,
    )
    total = sum(factor.maximum for factor in result.factors)
    assert total == 100.0


def test_no_run_history_is_unknown_not_critical() -> None:
    result = compute_connector_health(
        datasource_id=uuid4(),
        datasource_status="ACTIVE",
        runs=[],
        scan_interval_minutes=60,
        now=NOW,
    )
    assert result.status == "UNKNOWN"
    assert "NO_RUN_HISTORY" in result.blockers


def test_perfect_history_scores_healthy() -> None:
    runs = [
        _run("COMPLETED", minutes_ago=m, discovered_tables=10, profiled_tables=10)
        for m in (10, 70, 130)
    ]
    result = compute_connector_health(
        datasource_id=uuid4(),
        datasource_status="ACTIVE",
        runs=runs,
        scan_interval_minutes=60,
        now=NOW,
    )
    assert result.score == 100
    assert result.status == "HEALTHY"
    assert result.blockers == []


def test_disabled_datasource_is_blocked_even_with_good_runs() -> None:
    runs = [_run("COMPLETED", minutes_ago=5)]
    result = compute_connector_health(
        datasource_id=uuid4(),
        datasource_status="DISABLED",
        runs=runs,
        scan_interval_minutes=60,
        now=NOW,
    )
    assert "DATASOURCE_DISABLED" in result.blockers
    assert result.status != "HEALTHY"


def test_repeated_failures_blocker_and_critical_status() -> None:
    runs = [_run("FAILED", minutes_ago=m, error_class="AuthError") for m in (1, 2, 3, 4)]
    result = compute_connector_health(
        datasource_id=uuid4(),
        datasource_status="ACTIVE",
        runs=runs,
        scan_interval_minutes=60,
        now=NOW,
    )
    assert "REPEATED_FAILURES" in result.blockers
    assert "NO_SUCCESSFUL_RUN" in result.blockers
    assert result.status == "CRITICAL"


def test_score_is_deterministic() -> None:
    runs = [
        _run("COMPLETED", minutes_ago=10),
        _run("FAILED", minutes_ago=20, error_class="Timeout"),
        _run("COMPLETED", minutes_ago=30),
    ]
    kwargs = dict(
        datasource_id=uuid4(),
        datasource_status="ACTIVE",
        runs=runs,
        scan_interval_minutes=60,
        now=NOW,
    )
    r1 = compute_connector_health(**kwargs)
    r2 = compute_connector_health(**kwargs)
    assert r1.score == r2.score
    assert r1.status == r2.status


def test_score_clamped_to_0_100() -> None:
    runs = [_run("COMPLETED", minutes_ago=1)]
    result = compute_connector_health(
        datasource_id=uuid4(),
        datasource_status="ACTIVE",
        runs=runs,
        scan_interval_minutes=60,
        now=NOW,
    )
    assert 0 <= result.score <= 100


def test_all_factors_are_explainable() -> None:
    runs = [_run("COMPLETED", minutes_ago=1)]
    result = compute_connector_health(
        datasource_id=uuid4(),
        datasource_status="ACTIVE",
        runs=runs,
        scan_interval_minutes=60,
        now=NOW,
    )
    assert len(result.factors) == 5
    for factor in result.factors:
        assert isinstance(factor, HealthFactor)
        assert factor.name != ""
        assert factor.reason != ""
        assert 0.0 <= factor.score <= factor.maximum
        assert isinstance(factor.evidence, dict)


def test_run_history_window_ordering_matters_for_streak() -> None:
    # Caller contract: runs must be newest-first. A caller that accidentally
    # reverses the order would (correctly, given the contract) get a
    # different failure-streak reading -- this test documents that the
    # function trusts its ordering input rather than re-sorting.
    newest_first = [_run("FAILED", minutes_ago=1), _run("COMPLETED", minutes_ago=2)]
    oldest_first = list(reversed(newest_first))
    _, streak_newest_first = _score_failure_streak(newest_first)
    _, streak_oldest_first = _score_failure_streak(oldest_first)
    assert streak_newest_first == 1
    assert streak_oldest_first == 0
