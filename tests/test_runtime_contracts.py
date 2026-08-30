"""Tests for runtime data contract enforcement."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aida.runtime_contracts import (
    ContractViolation,
    QualityExpectation,
    RuntimeContract,
    SchemaExpectation,
    _check_freshness,
    _check_quality,
    _check_schema_drift,
    enforce_at_query_time,
    evaluate_contract,
)


def _make_contract(**overrides: object) -> RuntimeContract:
    defaults = dict(
        id=uuid4(),
        product_id=uuid4(),
        schema_contract=[
            SchemaExpectation(column_name="id", data_type="INTEGER", required=True),
            SchemaExpectation(column_name="name", data_type="VARCHAR", required=True),
            SchemaExpectation(column_name="email", data_type="VARCHAR", required=False),
        ],
        quality_contract=QualityExpectation(max_null_rate=0.1, min_freshness_minutes=60),
        freshness_sla_minutes=60,
        producer="team-data",
        consumer="team-analytics",
        version=1,
    )
    defaults.update(overrides)
    return RuntimeContract(**defaults)


# ---------------------------------------------------------------------------
# Schema drift detection
# ---------------------------------------------------------------------------


def test_schema_drift_missing_required_column() -> None:
    contract = _make_contract()
    current_columns = [
        {"name": "id", "physical_type": "INTEGER"},
        # "name" column is missing (required)
    ]
    violations = _check_schema_drift(contract, current_columns)
    critical = [v for v in violations if v.severity == "CRITICAL"]
    assert len(critical) >= 1
    assert any(v.evidence.get("missing_column") == "name" for v in critical)


def test_schema_drift_type_mismatch() -> None:
    contract = _make_contract()
    current_columns = [
        {"name": "id", "physical_type": "VARCHAR"},  # was INTEGER
        {"name": "name", "physical_type": "VARCHAR"},
        {"name": "email", "physical_type": "VARCHAR"},
    ]
    violations = _check_schema_drift(contract, current_columns)
    assert len(violations) >= 1
    assert violations[0].violation_type == "SCHEMA_DRIFT"
    assert violations[0].evidence["actual_type"] == "VARCHAR"


def test_schema_drift_missing_optional_is_warning() -> None:
    contract = _make_contract()
    current_columns = [
        {"name": "id", "physical_type": "INTEGER"},
        {"name": "name", "physical_type": "VARCHAR"},
        # "email" missing but not required
    ]
    violations = _check_schema_drift(contract, current_columns)
    email_violations = [v for v in violations if v.evidence.get("missing_column") == "email"]
    assert len(email_violations) == 1
    assert email_violations[0].severity == "WARNING"


def test_no_schema_drift_when_all_match() -> None:
    contract = _make_contract()
    current_columns = [
        {"name": "id", "physical_type": "INTEGER"},
        {"name": "name", "physical_type": "VARCHAR"},
        {"name": "email", "physical_type": "VARCHAR"},
    ]
    violations = _check_schema_drift(contract, current_columns)
    assert len(violations) == 0


# ---------------------------------------------------------------------------
# Quality breach detection
# ---------------------------------------------------------------------------


def test_quality_breach_critical() -> None:
    contract = _make_contract()
    observations = [{"quality_score": 30, "anomaly_types": ["NULL_SPIKE"]}]
    violations = _check_quality(contract, observations)
    assert len(violations) == 1
    assert violations[0].violation_type == "QUALITY_BREACH"
    assert violations[0].severity == "CRITICAL"


def test_quality_breach_warning() -> None:
    contract = _make_contract()
    observations = [{"quality_score": 60, "anomaly_types": ["VOLUME_DROP"]}]
    violations = _check_quality(contract, observations)
    assert len(violations) == 1
    assert violations[0].severity == "WARNING"


def test_quality_no_breach() -> None:
    contract = _make_contract()
    observations = [{"quality_score": 95, "anomaly_types": []}]
    violations = _check_quality(contract, observations)
    assert len(violations) == 0


# ---------------------------------------------------------------------------
# Freshness breach detection
# ---------------------------------------------------------------------------


def test_freshness_breach_no_profile() -> None:
    contract = _make_contract(freshness_sla_minutes=60)
    violations = _check_freshness(contract, last_profile_at=None)
    assert len(violations) == 1
    assert violations[0].violation_type == "FRESHNESS_BREACH"
    assert violations[0].severity == "CRITICAL"


def test_freshness_breach_stale_profile() -> None:
    contract = _make_contract(freshness_sla_minutes=60)
    stale_time = datetime.now(UTC) - timedelta(minutes=90)
    violations = _check_freshness(contract, last_profile_at=stale_time)
    assert len(violations) == 1
    assert violations[0].violation_type == "FRESHNESS_BREACH"


def test_freshness_ok() -> None:
    contract = _make_contract(freshness_sla_minutes=60)
    fresh_time = datetime.now(UTC) - timedelta(minutes=30)
    violations = _check_freshness(contract, last_profile_at=fresh_time)
    assert len(violations) == 0


def test_freshness_no_sla_skips_check() -> None:
    contract = _make_contract(freshness_sla_minutes=None)
    violations = _check_freshness(contract, last_profile_at=None)
    assert len(violations) == 0


# ---------------------------------------------------------------------------
# Full evaluation
# ---------------------------------------------------------------------------


def test_evaluate_contract_composites_all_checks() -> None:
    contract = _make_contract(freshness_sla_minutes=60)
    current_columns = [
        {"name": "id", "physical_type": "VARCHAR"},  # type mismatch
        {"name": "name", "physical_type": "VARCHAR"},
    ]
    observations = [{"quality_score": 40, "anomaly_types": ["NULL_SPIKE"]}]
    stale_time = datetime.now(UTC) - timedelta(minutes=120)

    violations = evaluate_contract(contract, current_columns, observations, stale_time)

    types = {v.violation_type for v in violations}
    assert "SCHEMA_DRIFT" in types
    assert "QUALITY_BREACH" in types
    assert "FRESHNESS_BREACH" in types


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------


def test_enforcement_allows_no_violations() -> None:
    contract = _make_contract()
    result = enforce_at_query_time(contract, [])
    assert result.allowed is True
    assert result.enforcement_action == "ALLOW"


def test_enforcement_blocks_on_critical() -> None:
    contract = _make_contract()
    violations = [
        ContractViolation(
            contract_id=contract.id,
            violation_type="SCHEMA_DRIFT",
            severity="CRITICAL",
            evidence={"test": True},
            detected_at=datetime.now(UTC),
        )
    ]
    result = enforce_at_query_time(contract, violations)
    assert result.allowed is False
    assert result.enforcement_action == "BLOCK"


def test_enforcement_warns_on_non_critical() -> None:
    contract = _make_contract()
    violations = [
        ContractViolation(
            contract_id=contract.id,
            violation_type="QUALITY_BREACH",
            severity="WARNING",
            evidence={"test": True},
            detected_at=datetime.now(UTC),
        )
    ]
    result = enforce_at_query_time(contract, violations)
    assert result.allowed is True
    assert result.enforcement_action == "WARN"


# ---------------------------------------------------------------------------
# SLA tracking
# ---------------------------------------------------------------------------


def test_sla_status_fields() -> None:
    """SlaStatus dataclass includes all expected fields."""
    from aida.runtime_contracts import SlaStatus

    sla = SlaStatus(
        contract_id=uuid4(),
        compliant=True,
        uptime_percent=99.5,
        violations_in_period=2,
        period_start=datetime.now(UTC) - timedelta(days=30),
        period_end=datetime.now(UTC),
    )
    assert sla.compliant is True
    assert sla.uptime_percent == 99.5
    assert sla.violations_in_period == 2
