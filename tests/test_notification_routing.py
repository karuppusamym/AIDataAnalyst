from aida.main import app
from aida.notification_routing import (
    Incident,
    NotificationEvent,
    NotificationRule,
    _compute_dedup_key,
    _matches_conditions,
    escalate,
    format_itsm_payload,
    route_notification,
    should_escalate,
)
from aida.schemas import NotificationRuleCreate, NotificationRuleUpdate


def _rule(
    rule_id: str = "r1",
    *,
    severity: str | list[str] | None = None,
    channel: str = "EMAIL",
    escalation_after_minutes: int | None = None,
    enabled: bool = True,
) -> NotificationRule:
    conditions: dict = {}
    if severity is not None:
        conditions["severity"] = severity
    return NotificationRule(
        rule_id=rule_id,
        organization_id="org-1",
        conditions=conditions,
        channel=channel,
        recipients=["ops@example.com"],
        escalation_after_minutes=escalation_after_minutes,
        enabled=enabled,
    )


def _incident(
    incident_id: str = "inc-1",
    *,
    severity: str = "CRITICAL",
    fingerprint: str = "fp-1",
    source_id: str = "src-1",
    domain: str | None = None,
    owner: str | None = None,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        fingerprint=fingerprint,
        severity=severity,
        source_id=source_id,
        domain=domain,
        owner=owner,
        message="Test incident",
    )


# --- route_notification ---


def test_route_notification_matches_enabled_rule() -> None:
    events = route_notification(_incident(), [_rule()])
    assert len(events) == 1
    assert events[0].incident_id == "inc-1"
    assert events[0].channel == "EMAIL"
    assert events[0].status == "PENDING"


def test_route_notification_skips_disabled_rule() -> None:
    events = route_notification(_incident(), [_rule(enabled=False)])
    assert events == []


def test_route_notification_deduplicates() -> None:
    incident = _incident()
    seen: set[str] = set()
    first = route_notification(incident, [_rule()], seen_dedup_keys=seen)
    second = route_notification(incident, [_rule()], seen_dedup_keys=seen)
    assert len(first) == 1
    assert len(second) == 0


def test_route_notification_severity_filter_list() -> None:
    rule = _rule(severity=["CRITICAL", "WARNING"])
    events_match = route_notification(_incident(severity="CRITICAL"), [rule])
    events_no_match = route_notification(
        _incident(severity="INFO", fingerprint="fp-2"), [rule]
    )
    assert len(events_match) == 1
    assert len(events_no_match) == 0


def test_route_notification_severity_filter_string() -> None:
    rule = _rule(severity="CRITICAL")
    events_match = route_notification(_incident(severity="CRITICAL"), [rule])
    events_no_match = route_notification(
        _incident(severity="WARNING", fingerprint="fp-3"), [rule]
    )
    assert len(events_match) == 1
    assert len(events_no_match) == 0


# --- _matches_conditions ---


def test_matches_conditions_source_filter() -> None:
    incident = _incident(source_id="src-1")
    assert _matches_conditions(incident, {"source_id": "src-1"}) is True
    assert _matches_conditions(incident, {"source_id": "src-2"}) is False


def test_matches_conditions_domain_and_owner() -> None:
    incident = _incident(domain="finance", owner="alice")
    assert _matches_conditions(incident, {"domain": "finance"}) is True
    assert _matches_conditions(incident, {"owner": "alice"}) is True
    assert _matches_conditions(incident, {"domain": "hr"}) is False


# --- deduplication ---


def test_dedup_key_deterministic() -> None:
    key1 = _compute_dedup_key("fp-1", "EMAIL")
    key2 = _compute_dedup_key("fp-1", "EMAIL")
    assert key1 == key2
    assert len(key1) == 32


def test_dedup_key_varies_by_channel() -> None:
    email_key = _compute_dedup_key("fp-1", "EMAIL")
    webhook_key = _compute_dedup_key("fp-1", "WEBHOOK")
    assert email_key != webhook_key


# --- escalation ---


def test_escalate_sets_status() -> None:
    event = NotificationEvent(
        notification_id="n-1",
        incident_id="inc-1",
        rule_id="r-1",
        severity="CRITICAL",
        source="src-1",
        domain=None,
        owner=None,
        message="test",
        channel="EMAIL",
        recipients=["ops@example.com"],
        status="SENT",
    )
    result = escalate(event)
    assert result.status == "ESCALATED"
    assert result.escalated_at is not None


def test_should_escalate_no_deadline() -> None:
    event = NotificationEvent(
        notification_id="n-1",
        incident_id="inc-1",
        rule_id="r-1",
        severity="CRITICAL",
        source="src-1",
        domain=None,
        owner=None,
        message="test",
        channel="EMAIL",
        recipients=["ops@example.com"],
        status="SENT",
    )
    rule = _rule(escalation_after_minutes=None)
    assert should_escalate(event, rule) is False


def test_should_escalate_already_acknowledged() -> None:
    from datetime import UTC, datetime

    event = NotificationEvent(
        notification_id="n-1",
        incident_id="inc-1",
        rule_id="r-1",
        severity="CRITICAL",
        source="src-1",
        domain=None,
        owner=None,
        message="test",
        channel="EMAIL",
        recipients=["ops@example.com"],
        status="SENT",
        sent_at=datetime(2024, 1, 1, tzinfo=UTC),
        acknowledged_at=datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
    )
    rule = _rule(escalation_after_minutes=30)
    assert should_escalate(event, rule) is False


# --- ITSM payload ---


def test_format_itsm_payload_structure() -> None:
    payload = format_itsm_payload(_incident())
    assert payload["category"] == "data_quality"
    assert payload["urgency"] == "1"  # CRITICAL maps to "1"
    assert "short_description" in payload
    assert "description" in payload
    assert "correlation_id" in payload


def test_format_itsm_payload_warning_severity() -> None:
    payload = format_itsm_payload(_incident(severity="WARNING"))
    assert payload["urgency"] == "2"


# --- schema contracts ---


def test_notification_rule_create_validates() -> None:
    rule = NotificationRuleCreate(
        name="test-rule",
        channel="EMAIL",
        recipients=["ops@example.com"],
    )
    assert rule.enabled is True
    assert rule.escalation_after_minutes is None


def test_notification_rule_update_requires_change() -> None:
    import pytest

    with pytest.raises(Exception):
        NotificationRuleUpdate()


def test_notification_api_routes_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/notification-rules" in paths
    assert "/v1/notifications" in paths
    assert "/v1/notifications/{notification_id}/acknowledge" in paths
