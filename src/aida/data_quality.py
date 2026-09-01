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
) -> QualityResult:
    """Compare value-free profile statistics using deterministic, explainable controls.

    The volume-change control ("VOLUME_CHANGE") always records `volume_change_percent`
    against the single most recent prior profile (`baseline`), unchanged from before.
    When `seasonality_enabled` is true and `row_count_history`/`current_observed_at`
    are supplied with enough same-weekday history (see `day_of_week_baseline`), the
    anomaly *verdict itself* -- whether VOLUME_CHANGE fires, and its severity -- is
    decided against the table's own day-of-week baseline instead: a plain rolling
    comparison to whatever day ran last cannot distinguish "this table always drops
    on Saturdays" from a genuine volume incident. With too little same-weekday
    history, or when the flag is off, this falls back to the original
    rolling-previous-profile comparison automatically -- DQ-6.
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

        seasonal = None
        if seasonality_enabled and current_observed_at is not None and row_count_history:
            seasonal = day_of_week_baseline(
                row_count_history, current_observed_at, min_samples=seasonality_min_samples
            )

        if seasonal is not None:
            evidence["threshold_strategy"] = "SEASONAL_DAY_OF_WEEK"
            evidence["seasonal_weekday"] = seasonal.weekday
            evidence["seasonal_sample_count"] = seasonal.sample_count
            evidence["seasonal_mean_row_count"] = round(seasonal.mean, 4)
            evidence["seasonal_stdev_row_count"] = round(seasonal.stdev, 4)
            if seasonal.stdev > 0:
                seasonal_zscore = abs(current.row_count - seasonal.mean) / seasonal.stdev
                evidence["seasonal_zscore"] = round(seasonal_zscore, 4)
                is_volume_anomaly = seasonal_zscore > seasonality_zscore_threshold
                volume_severity = _severity(seasonal_zscore, seasonality_zscore_threshold)
            else:
                seasonal_change = (
                    0.0
                    if seasonal.mean == 0
                    else abs(current.row_count - seasonal.mean) / seasonal.mean * 100
                )
                evidence["seasonal_change_percent"] = round(seasonal_change, 4)
                is_volume_anomaly = seasonal_change > volume_threshold
                volume_severity = _severity(seasonal_change, volume_threshold)
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
