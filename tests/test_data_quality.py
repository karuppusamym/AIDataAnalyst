from aida.data_quality import QualityProfile, evaluate_quality, normalized_policy
from aida.main import app
from aida.schemas import DataQualityIncidentTransition, DataQualityPolicyUpsert


def profile(
    rows: int | None,
    *,
    schema: str = "schema-a",
    null_rates: dict[str, float] | None = None,
) -> QualityProfile:
    return QualityProfile(rows, schema, null_rates or {"column-a": 0.05})


def test_first_observation_establishes_baseline_without_false_incident() -> None:
    result = evaluate_quality(profile(100), None)

    assert result.status == "NO_BASELINE"
    assert result.score == 100
    assert result.anomaly_types == ()


def test_healthy_profile_is_deterministic() -> None:
    result = evaluate_quality(profile(108, null_rates={"column-a": 0.08}), profile(100))

    assert result.status == "HEALTHY"
    assert result.score == 100
    assert result.evidence["volume_change_percent"] == 8.0
    assert result.evidence["max_null_rate_change_percent"] == 3.0


def test_volume_and_null_anomalies_are_scored_without_values() -> None:
    result = evaluate_quality(
        profile(210, null_rates={"column-a": 0.31}),
        profile(100, null_rates={"column-a": 0.05}),
    )

    assert result.status == "CRITICAL"
    assert result.anomaly_types == ("VOLUME_CHANGE", "NULL_RATE_SHIFT")
    assert result.severities == {"VOLUME_CHANGE": "CRITICAL", "NULL_RATE_SHIFT": "CRITICAL"}
    assert result.score == 30
    assert "affected_column_ids" in result.evidence
    assert "values" not in result.evidence


def test_schema_change_can_be_governed_independently() -> None:
    enabled = evaluate_quality(profile(100, schema="new"), profile(100, schema="old"))
    disabled = evaluate_quality(
        profile(100, schema="new"),
        profile(100, schema="old"),
        {"schema_change_enabled": False},
    )

    assert enabled.status == "WARNING"
    assert enabled.anomaly_types == ("SCHEMA_CHANGE",)
    assert disabled.status == "HEALTHY"


def test_zero_baseline_and_missing_estimate_are_safe() -> None:
    zero_growth = evaluate_quality(profile(1), profile(0))
    samples = evaluate_quality(profile(None), profile(None))

    assert zero_growth.evidence["volume_change_percent"] == 100.0
    assert zero_growth.status == "CRITICAL"
    assert samples.status == "HEALTHY"


def test_quality_contracts_validate_bounds_and_routes() -> None:
    defaults = DataQualityPolicyUpsert()
    transition = DataQualityIncidentTransition(status="ACKNOWLEDGED", reason="Assigned to owner")
    paths = app.openapi()["paths"]

    assert defaults.model_dump(exclude={"table_id", "name", "enabled"}) == normalized_policy()
    assert transition.status == "ACKNOWLEDGED"
    assert "/v1/datasources/{datasource_id}/quality-summary" in paths
    assert "/v1/datasources/{datasource_id}/quality-observations" in paths
    assert "/v1/quality-incidents/{incident_id}/transition" in paths
