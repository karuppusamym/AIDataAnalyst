from datetime import UTC, datetime, timedelta

from aida.freshness import FreshnessResult, WatermarkConfig, evaluate_freshness
from aida.main import app
from aida.schemas import FreshnessConfigUpsert, FreshnessStatusRead


def _config(
    *,
    threshold_minutes: int = 60,
    status: str = "ACTIVE",
    classification: str = "INTERNAL",
) -> WatermarkConfig:
    return WatermarkConfig(
        table_id="tbl-1",
        watermark_column="updated_at",
        classification=classification,
        threshold_minutes=threshold_minutes,
        retention_days=365,
        status=status,
    )


# --- evaluate_freshness ---


def test_not_configured_when_config_is_none() -> None:
    result = evaluate_freshness(None, None)
    assert result.status == "NOT_CONFIGURED"
    assert result.age_minutes is None
    assert result.threshold_minutes is None
    assert "no freshness configuration" in result.evidence["reason"]


def test_awaiting_approval_when_pending() -> None:
    config = _config(status="PENDING_APPROVAL")
    result = evaluate_freshness(config, None)
    assert result.status == "AWAITING_APPROVAL"
    assert result.threshold_minutes == config.threshold_minutes


def test_disabled_returns_not_configured() -> None:
    config = _config(status="DISABLED")
    result = evaluate_freshness(config, None)
    assert result.status == "NOT_CONFIGURED"
    assert "disabled" in result.evidence["reason"]


def test_stale_when_no_watermark_observed() -> None:
    config = _config()
    result = evaluate_freshness(config, None)
    assert result.status == "STALE"
    assert result.age_minutes is None
    assert "no watermark observation" in result.evidence["reason"]


def test_fresh_when_within_threshold() -> None:
    config = _config(threshold_minutes=60)
    now = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
    watermark = now - timedelta(minutes=30)
    result = evaluate_freshness(config, watermark, evaluation_time=now)
    assert result.status == "FRESH"
    assert result.age_minutes == 30.0
    assert result.threshold_minutes == 60
    assert result.evidence["evaluation_source"] == "data_watermark"


def test_stale_when_beyond_threshold() -> None:
    config = _config(threshold_minutes=60)
    now = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
    watermark = now - timedelta(minutes=90)
    result = evaluate_freshness(config, watermark, evaluation_time=now)
    assert result.status == "STALE"
    assert result.age_minutes == 90.0


def test_freshness_is_deterministic_with_fixed_time() -> None:
    config = _config(threshold_minutes=120)
    now = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
    watermark = datetime(2024, 6, 15, 11, 0, tzinfo=UTC)
    result1 = evaluate_freshness(config, watermark, evaluation_time=now)
    result2 = evaluate_freshness(config, watermark, evaluation_time=now)
    assert result1.status == result2.status
    assert result1.age_minutes == result2.age_minutes == 60.0


def test_exactly_at_threshold_is_fresh() -> None:
    config = _config(threshold_minutes=60)
    now = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
    watermark = now - timedelta(minutes=60)
    result = evaluate_freshness(config, watermark, evaluation_time=now)
    assert result.status == "FRESH"
    assert result.age_minutes == 60.0


def test_evidence_always_contains_evaluation_source() -> None:
    """ADR-0016: scan age is NEVER presented as freshness."""
    config = _config()
    now = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
    watermark = now - timedelta(minutes=10)
    result = evaluate_freshness(config, watermark, evaluation_time=now)
    assert result.evidence["evaluation_source"] == "data_watermark"
    assert result.evidence["watermark_column"] == "updated_at"


# --- schema contracts ---


def test_freshness_config_upsert_validates() -> None:
    config = FreshnessConfigUpsert(
        watermark_column="updated_at",
        threshold_minutes=120,
    )
    assert config.classification == "INTERNAL"
    assert config.retention_days == 365


def test_freshness_status_read_schema() -> None:
    from uuid import uuid4

    status = FreshnessStatusRead(
        table_id=uuid4(),
        status="FRESH",
        last_watermark=datetime.now(UTC),
        age_minutes=10.0,
        threshold_minutes=60,
        evidence={"evaluation_source": "data_watermark"},
    )
    assert status.status == "FRESH"


# --- API routes ---


def test_freshness_api_routes_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/datasources/{datasource_id}/freshness-config/{table_id}" in paths
    assert "/v1/datasources/{datasource_id}/freshness" in paths
    assert "/v1/datasources/{datasource_id}/freshness/{table_id}" in paths
