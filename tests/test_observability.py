from datetime import UTC, datetime

from aida.main import app
from aida.observability import (
    MetricsConfig,
    TracingConfig,
    configure_metrics,
    configure_tracing,
    traced,
)
from aida.siem_routing import (
    SecurityEvent,
    SiemConfig,
    format_cef,
    format_webhook_payload,
    route_to_siem,
)
from aida.worm_archive import (
    ArchiveConfig,
    AuditEventEnvelope,
    archive_audit_events,
    apply_legal_hold,
    release_legal_hold,
    retention_policy_for_classification,
    validate_archive_integrity,
)
from aida.schemas import ArchiveStatusRead, SloBudgetRead, SloDefinitionCreate


# --- OB-1: OpenTelemetry tracing/metrics ---


def test_tracing_disabled_returns_false() -> None:
    config = TracingConfig(enabled=False)
    assert configure_tracing(config) is False


def test_metrics_disabled_returns_false() -> None:
    config = MetricsConfig(enabled=False)
    assert configure_metrics(config) is False


def test_tracing_graceful_without_sdk() -> None:
    config = TracingConfig(enabled=True, endpoint="http://localhost:4317")
    # May return True if opentelemetry is installed, False if not;
    # the point is it doesn't raise.
    result = configure_tracing(config)
    assert isinstance(result, bool)


def test_metrics_graceful_without_sdk() -> None:
    config = MetricsConfig(enabled=True, endpoint="http://localhost:4317")
    result = configure_metrics(config)
    assert isinstance(result, bool)


def test_traced_decorator_sync() -> None:
    @traced
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5


def test_traced_decorator_async() -> None:
    import asyncio

    @traced
    async def add_async(a: int, b: int) -> int:
        return a + b

    result = asyncio.run(add_async(2, 3))
    assert result == 5


# --- OB-2: SIEM routing ---


def _event(
    event_type: str = "AUTH_FAILURE",
    severity: str = "HIGH",
) -> SecurityEvent:
    return SecurityEvent(
        event_type=event_type,
        severity=severity,
        source="10.0.0.1",
        organization_id="org-1",
        principal_id="user-1",
        correlation_id="corr-1",
        timestamp=datetime(2024, 6, 15, 12, 0, tzinfo=UTC),
    )


def test_format_cef_structure() -> None:
    cef = format_cef(_event())
    assert cef.startswith("CEF:0|Atlas|DataIntelligence|1.0|")
    assert "|100|" in cef  # AUTH_FAILURE signature ID
    assert "|8|" in cef  # HIGH severity
    assert "src=10.0.0.1" in cef
    assert "cs1=org-1" in cef
    assert "suser=user-1" in cef


def test_format_cef_policy_violation() -> None:
    cef = format_cef(_event(event_type="POLICY_VIOLATION", severity="CRITICAL"))
    assert "|200|" in cef
    assert "|10|" in cef


def test_format_webhook_payload() -> None:
    payload = format_webhook_payload(_event())
    assert payload["event_type"] == "AUTH_FAILURE"
    assert payload["severity"] == "HIGH"
    assert payload["cef_severity"] == 8
    assert payload["organization_id"] == "org-1"


def test_route_to_siem_disabled() -> None:
    config = SiemConfig(enabled=False)
    assert route_to_siem(_event(), config) is False


def test_route_to_siem_no_endpoint() -> None:
    config = SiemConfig(enabled=True, endpoint="")
    assert route_to_siem(_event(), config) is False


def test_route_to_siem_syslog() -> None:
    config = SiemConfig(enabled=True, endpoint="syslog://host:514", transport="syslog")
    assert route_to_siem(_event(), config) is True


def test_route_to_siem_webhook() -> None:
    config = SiemConfig(
        enabled=True, endpoint="https://siem.example.com/events", transport="webhook"
    )
    assert route_to_siem(_event(), config) is True


def test_route_to_siem_unsupported_transport() -> None:
    config = SiemConfig(enabled=True, endpoint="https://example.com", transport="kafka")
    assert route_to_siem(_event(), config) is False


# --- OB-3: WORM audit archive ---


def _envelope(event_id: str = "ev-1") -> AuditEventEnvelope:
    return AuditEventEnvelope(
        event_id=event_id,
        organization_id="org-1",
        action="data_access",
        resource_type="table",
        resource_id="tbl-1",
        principal_id="user-1",
        occurred_at=datetime(2024, 6, 15, 12, 0, tzinfo=UTC),
    )


def test_archive_empty_events() -> None:
    config = ArchiveConfig()
    result = archive_audit_events([], config)
    assert result.archived_count == 0
    assert result.archive_id == ""


def test_archive_events_produces_result() -> None:
    config = ArchiveConfig(retention_days=365, storage_backend="s3")
    events = [_envelope("ev-1"), _envelope("ev-2")]
    result = archive_audit_events(events, config)
    assert result.archived_count == 2
    assert result.archive_id.startswith("archive-")
    assert len(result.checksum) == 64  # SHA-256 hex
    assert result.storage_backend == "s3"
    assert result.legal_hold is False


def test_archive_checksum_deterministic() -> None:
    events = [_envelope("ev-1"), _envelope("ev-2")]
    config = ArchiveConfig()
    r1 = archive_audit_events(events, config)
    r2 = archive_audit_events(events, config)
    assert r1.checksum == r2.checksum


def test_validate_archive_integrity_pass() -> None:
    events = [_envelope("ev-1")]
    config = ArchiveConfig()
    result = archive_audit_events(events, config)
    assert validate_archive_integrity(events, result.checksum) is True


def test_validate_archive_integrity_fail() -> None:
    events = [_envelope("ev-1")]
    assert validate_archive_integrity(events, "bad-checksum") is False


def test_archive_legal_hold_enabled() -> None:
    config = ArchiveConfig(legal_hold_enabled=True)
    result = archive_audit_events([_envelope()], config)
    assert result.legal_hold is True


def test_apply_legal_hold() -> None:
    result = apply_legal_hold("archive-1", "litigation")
    assert result["legal_hold"] is True
    assert result["reason"] == "litigation"


def test_release_legal_hold() -> None:
    result = release_legal_hold("archive-1", "case closed")
    assert result["legal_hold"] is False
    assert result["reason"] == "case closed"


def test_retention_policy_by_classification() -> None:
    assert retention_policy_for_classification("PUBLIC") == 365
    assert retention_policy_for_classification("INTERNAL") == 1825
    assert retention_policy_for_classification("CONFIDENTIAL") == 2555
    assert retention_policy_for_classification("RESTRICTED") == 3650
    assert retention_policy_for_classification("UNKNOWN") == 2555  # default


# --- OB-4: Observability API routes ---


def test_slo_definition_create_validates() -> None:
    slo = SloDefinitionCreate(
        slo_key="quality-slo",
        name="Quality SLO",
        target=99.5,
        window_days=30,
        threshold=99.0,
    )
    assert slo.slo_key == "quality-slo"


def test_slo_budget_read_schema() -> None:
    from uuid import uuid4

    budget = SloBudgetRead(
        slo_id=uuid4(),
        slo_key="quality-slo",
        name="Quality SLO",
        target=99.5,
        current_value=99.2,
        budget_remaining=0.3,
        window_days=30,
        status="AT_RISK",
    )
    assert budget.status == "AT_RISK"


def test_archive_status_read_schema() -> None:
    status = ArchiveStatusRead(
        total_archives=10,
        total_events_archived=5000,
        latest_archive_id="archive-20240615-abc123",
        latest_checksum="abcdef" * 10 + "abcd",
        legal_hold_count=1,
        status="LEGAL_HOLD_ACTIVE",
    )
    assert status.total_archives == 10


def test_observability_api_routes_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/observability/slo" in paths
    assert "/v1/observability/slo/{slo_id}/budget" in paths
    assert "/v1/observability/archive/status" in paths
