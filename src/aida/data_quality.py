import calendar
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

DEFAULT_POLICY: dict[str, Any] = {
    "volume_change_percent": 30.0,
    "null_rate_change_percent": 10.0,
    "schema_change_enabled": True,
    "metadata_scan_max_age_minutes": 1440,
}


@dataclass(frozen=True, slots=True)
class QualityProfile:
    row_count: int | None
    schema_fingerprint: str | None
    null_rates: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QualityResult:
    status: str
    score: int
    anomaly_types: tuple[str, ...]
    evidence: dict[str, Any]
    severities: dict[str, str]


@dataclass(frozen=True, slots=True)
class SeasonalBaseline:
    """A table's own day-of-week volume baseline, computed from its scan history.

    `weekday` follows `datetime.weekday()` (Monday=0 .. Sunday=6). `stdev` is the
    population standard deviation (`statistics.pstdev`) of same-weekday row counts;
    it is `0.0` when only one same-weekday point exists (variance is undefined, not
    zero, but a single point implies "no observed spread yet" for threshold purposes).
    """

    weekday: int
    mean: float
    stdev: float
    sample_count: int


@dataclass(frozen=True, slots=True)
class DayOfMonthBaseline:
    """A table's own month-end-proximity volume baseline, computed from its scan history.

    `days_before_month_end` counts backward from the last calendar day of a month:
    `0` is the month's last day, `1` is the second-to-last day, and so on. Grouping by
    this offset (rather than the raw calendar day number) lines up a "last business
    day of the month" close spike across months of different lengths -- day 28 of
    February and day 31 of March both have `days_before_month_end == 0` -- which a
    raw day-number match would miss. `stdev` is the population standard deviation
    (`statistics.pstdev`); it is `0.0` when only one same-position point exists.
    """

    days_before_month_end: int
    mean: float
    stdev: float
    sample_count: int


def normalized_policy(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    policy.update(overrides or {})
    return policy


def _severity(value: float, threshold: float) -> str:
    return "CRITICAL" if value >= threshold * 2 else "WARNING"


def day_of_week_baseline(
    history: Sequence[tuple[datetime, int]],
    observed_at: datetime,
    *,
    min_samples: int = 3,
) -> SeasonalBaseline | None:
    """Pure, DB-free day-of-week baseline over a table's already-persisted scan history.

    `history` is a sequence of (timestamp, row_count) points -- e.g. one per past
    profiling scan of the same table, in any order, with no assumptions about
    granularity beyond having a real timestamp. This groups them by ISO weekday and
    returns the mean/stdev/sample_count of the points that share `observed_at`'s
    weekday, so (for example) a Saturday reading is judged against a table's own
    other Saturdays rather than against Friday's value or an undifferentiated
    rolling window that mixes weekdays and weekends together.

    Returns `None` when fewer than `min_samples` same-weekday points are available,
    so a caller can fall back to a non-seasonal comparison instead of trusting a
    baseline built from too thin a sample (e.g. a table with only a few weeks of
    scan history so far).
    """
    weekday = observed_at.weekday()
    same_weekday_values = [
        float(value) for observed, value in history if observed.weekday() == weekday
    ]
    if len(same_weekday_values) < min_samples:
        return None
    mean = statistics.fmean(same_weekday_values)
    stdev = statistics.pstdev(same_weekday_values) if len(same_weekday_values) > 1 else 0.0
    return SeasonalBaseline(
        weekday=weekday, mean=mean, stdev=stdev, sample_count=len(same_weekday_values)
    )


def _days_before_month_end(observed_at: datetime) -> int:
    last_day = calendar.monthrange(observed_at.year, observed_at.month)[1]
    return last_day - observed_at.day


def day_of_month_baseline(
    history: Sequence[tuple[datetime, int]],
    observed_at: datetime,
    *,
    min_samples: int = 3,
) -> DayOfMonthBaseline | None:
    """Pure, DB-free month-end-proximity baseline over a table's persisted scan history.

    Companion to `day_of_week_baseline`, for the seasonality a weekday grouping
    cannot see: a recurring month-end close batch lands on a different weekday
    every month, so it is spread across several weekday buckets instead of forming
    its own pattern there. This groups `history` by `_days_before_month_end` instead
    (`0` = the month's last day, `1` = the second-to-last, ...) and returns the
    mean/stdev/sample_count of the points sharing `observed_at`'s position, so a
    month-end reading is judged against a table's own other month-ends rather than
    the single ordinary day right before it.

    Returns `None` when fewer than `min_samples` same-position points are available,
    matching `day_of_week_baseline`'s fallback contract.
    """
    anchor = _days_before_month_end(observed_at)
    same_position_values = [
        float(value) for observed, value in history if _days_before_month_end(observed) == anchor
    ]
    if len(same_position_values) < min_samples:
        return None
    mean = statistics.fmean(same_position_values)
    stdev = statistics.pstdev(same_position_values) if len(same_position_values) > 1 else 0.0
    return DayOfMonthBaseline(
        days_before_month_end=anchor, mean=mean, stdev=stdev, sample_count=len(same_position_values)
    )


def _seasonal_verdict(
    current_row_count: int,
    mean: float,
    stdev: float,
    *,
    volume_threshold: float,
    zscore_threshold: float,
) -> tuple[bool, str, dict[str, Any]]:
    """Shared z-score-or-percent verdict logic for a seasonal (mean, stdev) baseline.

    Used identically by both the day-of-week and month-end grouping strategies:
    z-score against the baseline's own spread when it has one, falling back to a
    plain percent-change against the baseline mean when `stdev` is `0.0` (a single
    same-position sample has no spread to score against).
    """
    evidence: dict[str, Any] = {}
    if stdev > 0:
        zscore = abs(current_row_count - mean) / stdev
        evidence["seasonal_zscore"] = round(zscore, 4)
        is_anomaly = zscore > zscore_threshold
        severity = _severity(zscore, zscore_threshold)
    else:
        change_percent = 0.0 if mean == 0 else abs(current_row_count - mean) / mean * 100
        evidence["seasonal_change_percent"] = round(change_percent, 4)
        is_anomaly = change_percent > volume_threshold
        severity = _severity(change_percent, volume_threshold)
    return is_anomaly, severity, evidence


def evaluate_quality(
    current: QualityProfile,
    baseline: QualityProfile | None,
    policy_overrides: dict[str, Any] | None = None,
    *,
    row_count_history: Sequence[tuple[datetime, int]] | None = None,
    current_observed_at: datetime | None = None,
    seasonality_enabled: bool = False,
    seasonality_min_samples: int = 3,
    seasonality_zscore_threshold: float = 3.0,
    month_end_seasonality_enabled: bool = False,
    month_end_window_days: int = 3,
) -> QualityResult:
    """Compare value-free profile statistics using deterministic, explainable controls.

    The volume-change control ("VOLUME_CHANGE") always records `volume_change_percent`
    against the single most recent prior profile (`baseline`), unchanged from before.
    Two independent, additive seasonal strategies can each supersede that raw
    comparison for the anomaly *verdict itself* -- whether VOLUME_CHANGE fires, and
    its severity -- when `row_count_history`/`current_observed_at` are supplied with
    enough matching history (DQ-6):

    * `seasonality_enabled` judges against the table's own day-of-week baseline
      (see `day_of_week_baseline`) -- a plain rolling comparison to whatever day ran
      last cannot distinguish "this table always drops on Saturdays" from a genuine
      volume incident.
    * `month_end_seasonality_enabled` judges against the table's own month-end
      baseline (see `day_of_month_baseline`) when `current_observed_at` falls within
      the last `month_end_window_days` calendar days of its month -- a recurring
      month-end close spike lands on a different weekday every month, so the
      day-of-week strategy alone cannot group it.

    Both are opt-in and can be enabled together: when a reading falls inside the
    month-end window and has enough same-position history, the month-end baseline
    takes priority (it is the more specific signal for that day); otherwise the
    day-of-week baseline is used when available. With too little matching history
    for whichever strategy applies, or when both flags are off, this falls back to
    the original rolling-previous-profile comparison automatically.
    """
    policy = normalized_policy(policy_overrides)
    evidence: dict[str, Any] = {
        "current_row_count": current.row_count,
        "compared_column_count": 0,
        "control_version": "quality-v1",
    }
    if baseline is None:
        return QualityResult("NO_BASELINE", 100, (), evidence, {})

    anomalies: list[str] = []
    severities: dict[str, str] = {}
    penalties = 0
    volume_threshold = float(policy["volume_change_percent"])
    if current.row_count is not None and baseline.row_count is not None:
        if baseline.row_count == 0:
            volume_change = 0.0 if current.row_count == 0 else 100.0
        else:
            volume_change = abs(current.row_count - baseline.row_count) / baseline.row_count * 100
        evidence["baseline_row_count"] = baseline.row_count
        evidence["volume_change_percent"] = round(volume_change, 4)

        month_end_seasonal = None
        if (
            month_end_seasonality_enabled
            and current_observed_at is not None
            and row_count_history
            and _days_before_month_end(current_observed_at) < month_end_window_days
        ):
            month_end_seasonal = day_of_month_baseline(
                row_count_history, current_observed_at, min_samples=seasonality_min_samples
            )

        weekday_seasonal = None
        if seasonality_enabled and current_observed_at is not None and row_count_history:
            weekday_seasonal = day_of_week_baseline(
                row_count_history, current_observed_at, min_samples=seasonality_min_samples
            )

        if month_end_seasonal is not None:
            evidence["threshold_strategy"] = "SEASONAL_MONTH_END"
            evidence["seasonal_days_before_month_end"] = month_end_seasonal.days_before_month_end
            evidence["seasonal_sample_count"] = month_end_seasonal.sample_count
            evidence["seasonal_mean_row_count"] = round(month_end_seasonal.mean, 4)
            evidence["seasonal_stdev_row_count"] = round(month_end_seasonal.stdev, 4)
            is_volume_anomaly, volume_severity, verdict_evidence = _seasonal_verdict(
                current.row_count,
                month_end_seasonal.mean,
                month_end_seasonal.stdev,
                volume_threshold=volume_threshold,
                zscore_threshold=seasonality_zscore_threshold,
            )
            evidence.update(verdict_evidence)
            if is_volume_anomaly:
                anomalies.append("VOLUME_CHANGE")
                severities["VOLUME_CHANGE"] = volume_severity
        elif weekday_seasonal is not None:
            evidence["threshold_strategy"] = "SEASONAL_DAY_OF_WEEK"
            evidence["seasonal_weekday"] = weekday_seasonal.weekday
            evidence["seasonal_sample_count"] = weekday_seasonal.sample_count
            evidence["seasonal_mean_row_count"] = round(weekday_seasonal.mean, 4)
            evidence["seasonal_stdev_row_count"] = round(weekday_seasonal.stdev, 4)
            is_volume_anomaly, volume_severity, verdict_evidence = _seasonal_verdict(
                current.row_count,
                weekday_seasonal.mean,
                weekday_seasonal.stdev,
                volume_threshold=volume_threshold,
                zscore_threshold=seasonality_zscore_threshold,
            )
            evidence.update(verdict_evidence)
            if is_volume_anomaly:
                anomalies.append("VOLUME_CHANGE")
                severities["VOLUME_CHANGE"] = volume_severity
        else:
            evidence["threshold_strategy"] = "ROLLING_PREVIOUS"
            if volume_change > volume_threshold:
                anomalies.append("VOLUME_CHANGE")
                severities["VOLUME_CHANGE"] = _severity(volume_change, volume_threshold)

    shared_columns = sorted(current.null_rates.keys() & baseline.null_rates.keys())
    evidence["compared_column_count"] = len(shared_columns)
    null_changes = {
        column_id: abs(current.null_rates[column_id] - baseline.null_rates[column_id]) * 100
        for column_id in shared_columns
    }
    max_null_change = max(null_changes.values(), default=0.0)
    evidence["max_null_rate_change_percent"] = round(max_null_change, 4)
    null_threshold = float(policy["null_rate_change_percent"])
    if max_null_change > null_threshold:
        anomalies.append("NULL_RATE_SHIFT")
        severities["NULL_RATE_SHIFT"] = _severity(max_null_change, null_threshold)
        # Identifiers are retained for diagnosis; no sampled values are persisted.
        evidence["affected_column_ids"] = [
            column_id for column_id, change in null_changes.items() if change > null_threshold
        ][:100]

    if (
        bool(policy["schema_change_enabled"])
        and current.schema_fingerprint
        and baseline.schema_fingerprint
        and current.schema_fingerprint != baseline.schema_fingerprint
    ):
        anomalies.append("SCHEMA_CHANGE")
        severities["SCHEMA_CHANGE"] = "WARNING"
        evidence["schema_fingerprint_changed"] = True

    for anomaly in anomalies:
        penalties += 35 if severities[anomaly] == "CRITICAL" else 15
    status = (
        "CRITICAL" if "CRITICAL" in severities.values() else "WARNING" if anomalies else "HEALTHY"
    )
    return QualityResult(status, max(0, 100 - penalties), tuple(anomalies), evidence, severities)
