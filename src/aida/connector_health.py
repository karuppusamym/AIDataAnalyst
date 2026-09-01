"""Per-connector health scoring (CN-7).

Computes a deterministic, explainable health score for one data source /
connector from its own recorded analysis-run history -- no new signal is
invented and no new column or table is required. Every factor mirrors the
`aida.trust_scoring` / `aida.ai_registry` idiom already established in this
codebase: the score never replaces the underlying evidence, it summarizes it,
and every contributing factor carries its own reason and raw evidence so an
operator (or a caller) can see exactly why a connector is scored the way it
is instead of trusting one opaque number.

Inputs are plain dataclasses so this module has no database dependency and
is fully unit-testable (`tests/test_connector_health.py`) -- the DB-facing
aggregation that turns `AnalysisRun`/`ScanPolicy`/`DataSource` rows into
these dataclasses lives in `aida.fleet` (`datasource_health`,
`fleet_health`), which is the only place that touches a session.

Five factors, each with a fixed point budget summing to 100:

* ``RUN_SUCCESS_RATE`` (35 pts) -- share of the recent terminal runs
  (COMPLETED vs FAILED/CANCELLED/SUBMISSION_FAILED) that succeeded.
* ``STALENESS`` (25 pts) -- how long ago the last successful run finished,
  measured against the connector's own scan-policy interval when one is
  configured (falling back to fixed thresholds otherwise).
* ``FAILURE_STREAK`` (20 pts) -- how many of the most recent terminal runs,
  counting back from the newest, failed consecutively before a success.
* ``PROFILING_COVERAGE`` (10 pts) -- of the tables the latest successful run
  discovered, how many it went on to profile.
* ``DATASOURCE_ENABLEMENT`` (10 pts) -- whether the datasource itself is
  administratively disabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

#: `AnalysisRun.status` values that represent a run that finished on its own
#: terms (as opposed to still being in flight). Mirrors the terminal-status
#: checks already inline in `aida.api` (`cancel_analysis_run`,
#: `resume_analysis_run`).
TERMINAL_SUCCESS_STATUSES = frozenset({"COMPLETED"})
TERMINAL_FAILURE_STATUSES = frozenset({"FAILED", "CANCELLED", "SUBMISSION_FAILED"})
TERMINAL_STATUSES = TERMINAL_SUCCESS_STATUSES | TERMINAL_FAILURE_STATUSES

#: How many of the most recent analysis runs feed the score. Bounded so one
#: very long-lived connector's history can't dominate query cost; recent
#: behavior is what an operator cares about for "is this healthy right now".
RUN_HISTORY_WINDOW = 20

#: Point budget per factor -- see the module docstring. Kept as named
#: constants so the composite-score test can assert they sum to 100 without
#: duplicating the literals.
_MAX_RUN_SUCCESS_RATE = 35.0
_MAX_STALENESS = 25.0
_MAX_FAILURE_STREAK = 20.0
_MAX_PROFILING_COVERAGE = 10.0
_MAX_DATASOURCE_ENABLEMENT = 10.0


@dataclass(frozen=True, slots=True)
class ConnectorRunSample:
    """The health-relevant fields of one `AnalysisRun` row.

    Callers (see `aida.fleet`) are responsible for ordering samples
    most-recent-first by `finished_at` before passing them to
    `compute_connector_health` -- the failure-streak factor depends on that
    ordering to find the *most recent* run first.
    """

    status: str
    finished_at: datetime
    error_class: str | None
    discovered_tables: int
    profiled_tables: int


@dataclass(frozen=True, slots=True)
class HealthFactor:
    """One scored, explainable dimension contributing to the health score."""

    name: str
    score: float
    maximum: float
    reason: str
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConnectorHealthScore:
    """Composite per-connector health score."""

    datasource_id: UUID
    score: int  # 0-100
    status: str  # HEALTHY | DEGRADED | CRITICAL | UNKNOWN
    factors: list[HealthFactor] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    computed_at: datetime = field(default_factory=lambda: datetime.now())


def _score_run_success_rate(runs: list[ConnectorRunSample]) -> HealthFactor:
    terminal = [run for run in runs if run.status in TERMINAL_STATUSES]
    if not terminal:
        return HealthFactor(
            name="RUN_SUCCESS_RATE",
            score=_MAX_RUN_SUCCESS_RATE / 2,
            maximum=_MAX_RUN_SUCCESS_RATE,
            reason="No completed or failed runs are recorded yet; score is neutral.",
            evidence={"successful_runs": 0, "terminal_runs": 0, "success_rate": None},
        )
    successes = sum(1 for run in terminal if run.status in TERMINAL_SUCCESS_STATUSES)
    rate = successes / len(terminal)
    score = round(_MAX_RUN_SUCCESS_RATE * rate, 2)
    return HealthFactor(
        name="RUN_SUCCESS_RATE",
        score=score,
        maximum=_MAX_RUN_SUCCESS_RATE,
        reason=(
            f"{successes} of {len(terminal)} recent runs completed successfully "
            f"({rate:.0%})."
        ),
        evidence={
            "successful_runs": successes,
            "terminal_runs": len(terminal),
            "success_rate": round(rate, 4),
        },
    )


def _last_success(runs: list[ConnectorRunSample]) -> ConnectorRunSample | None:
    successes = [run for run in runs if run.status in TERMINAL_SUCCESS_STATUSES]
    if not successes:
        return None
    return max(successes, key=lambda run: run.finished_at)


def _score_staleness(
    runs: list[ConnectorRunSample],
    *,
    now: datetime,
    scan_interval_minutes: int | None,
) -> HealthFactor:
    last_success = _last_success(runs)
    if last_success is None:
        return HealthFactor(
            name="STALENESS",
            score=0.0,
            maximum=_MAX_STALENESS,
            reason="No analysis run has ever completed successfully.",
            evidence={
                "last_success_at": None,
                "age_minutes": None,
                "scan_interval_minutes": scan_interval_minutes,
            },
        )
    age_minutes = max(0.0, (now - last_success.finished_at).total_seconds() / 60)
    if scan_interval_minutes and scan_interval_minutes > 0:
        ratio = age_minutes / scan_interval_minutes
        score = (
            _MAX_STALENESS
            if ratio <= 1
            else max(0.0, _MAX_STALENESS - (ratio - 1) * _MAX_STALENESS)
        )
        reason = (
            f"Last success was {age_minutes:.0f} minutes ago against a "
            f"{scan_interval_minutes}-minute scan schedule ({ratio:.1f}x the interval)."
        )
    else:
        if age_minutes <= 60:
            score = _MAX_STALENESS
        elif age_minutes <= 24 * 60:
            score = 18.0
        elif age_minutes <= 72 * 60:
            score = 10.0
        else:
            score = 0.0
        reason = (
            f"Last success was {age_minutes:.0f} minutes ago; no scan schedule is "
            "configured to compare against, so a fixed threshold was used."
        )
    return HealthFactor(
        name="STALENESS",
        score=round(score, 2),
        maximum=_MAX_STALENESS,
        reason=reason,
        evidence={
            "last_success_at": last_success.finished_at.isoformat(),
            "age_minutes": round(age_minutes, 1),
            "scan_interval_minutes": scan_interval_minutes,
        },
    )


def _score_failure_streak(runs: list[ConnectorRunSample]) -> tuple[HealthFactor, int]:
    terminal_ordered = [run for run in runs if run.status in TERMINAL_STATUSES]
    streak = 0
    most_recent_error_class: str | None = None
    for run in terminal_ordered:
        if run.status in TERMINAL_FAILURE_STATUSES:
            streak += 1
            if most_recent_error_class is None:
                most_recent_error_class = run.error_class
        else:
            break
    scores = {0: _MAX_FAILURE_STREAK, 1: 12.0, 2: 6.0}
    score = scores.get(streak, 0.0)
    if streak == 0:
        reason = (
            "Most recent completed run succeeded."
            if terminal_ordered
            else "No terminal runs recorded yet."
        )
    else:
        reason = f"{streak} consecutive run(s) failed most recently."
    factor = HealthFactor(
        name="FAILURE_STREAK",
        score=score,
        maximum=_MAX_FAILURE_STREAK,
        reason=reason,
        evidence={
            "consecutive_failures": streak,
            "most_recent_error_class": most_recent_error_class,
        },
    )
    return factor, streak


def _score_profiling_coverage(runs: list[ConnectorRunSample]) -> HealthFactor:
    last_success = _last_success(runs)
    if last_success is None:
        return HealthFactor(
            name="PROFILING_COVERAGE",
            score=_MAX_PROFILING_COVERAGE / 2,
            maximum=_MAX_PROFILING_COVERAGE,
            reason="No successful run to measure profiling coverage from; score is neutral.",
            evidence={"discovered_tables": None, "profiled_tables": None},
        )
    discovered = last_success.discovered_tables
    profiled = last_success.profiled_tables
    if discovered <= 0:
        return HealthFactor(
            name="PROFILING_COVERAGE",
            score=_MAX_PROFILING_COVERAGE / 2,
            maximum=_MAX_PROFILING_COVERAGE,
            reason="The latest successful run discovered no tables to profile.",
            evidence={"discovered_tables": discovered, "profiled_tables": profiled},
        )
    ratio = min(profiled / discovered, 1.0)
    score = round(_MAX_PROFILING_COVERAGE * ratio, 2)
    return HealthFactor(
        name="PROFILING_COVERAGE",
        score=score,
        maximum=_MAX_PROFILING_COVERAGE,
        reason=f"{profiled} of {discovered} discovered tables were profiled ({ratio:.0%}).",
        evidence={"discovered_tables": discovered, "profiled_tables": profiled},
    )


def _score_datasource_enablement(datasource_status: str) -> HealthFactor:
    if datasource_status == "DISABLED":
        return HealthFactor(
            name="DATASOURCE_ENABLEMENT",
            score=0.0,
            maximum=_MAX_DATASOURCE_ENABLEMENT,
            reason="The datasource is administratively disabled.",
            evidence={"datasource_status": datasource_status},
        )
    return HealthFactor(
        name="DATASOURCE_ENABLEMENT",
        score=_MAX_DATASOURCE_ENABLEMENT,
        maximum=_MAX_DATASOURCE_ENABLEMENT,
        reason=f"The datasource status is {datasource_status}.",
        evidence={"datasource_status": datasource_status},
    )


def compute_connector_health(
    *,
    datasource_id: UUID,
    datasource_status: str,
    runs: list[ConnectorRunSample],
    scan_interval_minutes: int | None,
    now: datetime,
) -> ConnectorHealthScore:
    """Compute a composite, explainable health score for one connector.

    `runs` should be the connector's most recent analysis runs, newest
    first (see `RUN_HISTORY_WINDOW`); an empty list means the connector has
    never been run, which is reported as ``UNKNOWN`` rather than a low
    score -- there is no evidence of poor health, only an absence of
    evidence.
    """
    failure_streak_factor, streak = _score_failure_streak(runs)
    factors = [
        _score_run_success_rate(runs),
        _score_staleness(runs, now=now, scan_interval_minutes=scan_interval_minutes),
        failure_streak_factor,
        _score_profiling_coverage(runs),
        _score_datasource_enablement(datasource_status),
    ]

    blockers: list[str] = []
    if not runs:
        blockers.append("NO_RUN_HISTORY")
    elif _last_success(runs) is None:
        blockers.append("NO_SUCCESSFUL_RUN")
    if datasource_status == "DISABLED":
        blockers.append("DATASOURCE_DISABLED")
    if streak >= 3:
        blockers.append("REPEATED_FAILURES")

    raw_score = sum(factor.score for factor in factors)
    score = int(round(max(0.0, min(100.0, raw_score))))

    if not runs:
        status = "UNKNOWN"
    elif blockers:
        status = "DEGRADED" if score >= 60 else "CRITICAL"
    elif score >= 85:
        status = "HEALTHY"
    elif score >= 60:
        status = "DEGRADED"
    else:
        status = "CRITICAL"

    return ConnectorHealthScore(
        datasource_id=datasource_id,
        score=score,
        status=status,
        factors=factors,
        blockers=blockers,
        computed_at=now,
    )
