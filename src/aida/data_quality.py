from dataclasses import dataclass, field
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


def normalized_policy(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    policy.update(overrides or {})
    return policy


def _severity(value: float, threshold: float) -> str:
    return "CRITICAL" if value >= threshold * 2 else "WARNING"


def evaluate_quality(
    current: QualityProfile,
    baseline: QualityProfile | None,
    policy_overrides: dict[str, Any] | None = None,
) -> QualityResult:
    """Compare value-free profile statistics using deterministic, explainable controls."""
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
