"""Notification and escalation routing engine (DQ-1).

Matches quality incidents against notification rules and produces
notification events. Deduplication prevents alert fatigue by hashing
incident fingerprint + channel + time window. ITSM integration formats
incidents as ServiceNow/Jira payloads.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass(frozen=True, slots=True)
class NotificationRule:
    """Declarative routing rule matching incidents to channels."""

    rule_id: str
    organization_id: str
    conditions: dict[str, Any]  # severity, source_id, domain, owner
    channel: str  # EMAIL, WEBHOOK, ITSM
    recipients: list[str]
    escalation_after_minutes: int | None = None
    enabled: bool = True


@dataclass(slots=True)
class NotificationEvent:
    """Outbound notification produced by rule matching."""

    notification_id: str
    incident_id: str
    rule_id: str
    severity: str
    source: str
    domain: str | None
    owner: str | None
    message: str
    channel: str
    recipients: list[str]
    status: str = "PENDING"  # PENDING, SENT, ESCALATED, FAILED
    dedup_key: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sent_at: datetime | None = None
    escalated_at: datetime | None = None
    acknowledged_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Incident:
    """Incoming incident to be routed."""

    incident_id: str
    fingerprint: str
    severity: str
    source_id: str
    domain: str | None = None
    owner: str | None = None
    message: str = ""


def _compute_dedup_key(
    fingerprint: str, channel: str, window_minutes: int = 60
) -> str:
    """Hash incident fingerprint + channel + time window for deduplication."""
    now = datetime.now(UTC)
    window_start = now.replace(
        minute=(now.minute // window_minutes) * window_minutes if window_minutes <= 60 else 0,
        second=0,
        microsecond=0,
    )
    raw = f"{fingerprint}:{channel}:{window_start.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _matches_conditions(incident: Incident, conditions: dict[str, Any]) -> bool:
    """Check whether an incident matches rule conditions."""
    if "severity" in conditions:
        allowed = conditions["severity"]
        if isinstance(allowed, list):
            if incident.severity not in allowed:
                return False
        elif incident.severity != allowed:
            return False
    if "source_id" in conditions and incident.source_id != conditions["source_id"]:
        return False
    if "domain" in conditions and incident.domain != conditions["domain"]:
        return False
    if "owner" in conditions and incident.owner != conditions["owner"]:
        return False
    return True


def route_notification(
    incident: Incident,
    rules: list[NotificationRule],
    *,
    seen_dedup_keys: set[str] | None = None,
) -> list[NotificationEvent]:
    """Match incident to rules, producing deduplicated notification events."""
    if seen_dedup_keys is None:
        seen_dedup_keys = set()

    events: list[NotificationEvent] = []
    for rule in rules:
        if not rule.enabled:
            continue
        if not _matches_conditions(incident, rule.conditions):
            continue

        dedup_key = _compute_dedup_key(incident.fingerprint, rule.channel)
        if dedup_key in seen_dedup_keys:
            continue
        seen_dedup_keys.add(dedup_key)

        event = NotificationEvent(
            notification_id=f"notif-{incident.incident_id}-{rule.rule_id}",
            incident_id=incident.incident_id,
            rule_id=rule.rule_id,
            severity=incident.severity,
            source=incident.source_id,
            domain=incident.domain,
            owner=incident.owner,
            message=incident.message,
            channel=rule.channel,
            recipients=list(rule.recipients),
            dedup_key=dedup_key,
        )
        events.append(event)
    return events


def escalate(event: NotificationEvent) -> NotificationEvent:
    """Escalate an unacknowledged notification event."""
    event.status = "ESCALATED"
    event.escalated_at = datetime.now(UTC)
    return event


def should_escalate(event: NotificationEvent, rule: NotificationRule) -> bool:
    """Determine whether a sent event should be escalated."""
    if rule.escalation_after_minutes is None:
        return False
    if event.status != "SENT":
        return False
    if event.acknowledged_at is not None:
        return False
    if event.sent_at is None:
        return False
    deadline = event.sent_at + timedelta(minutes=rule.escalation_after_minutes)
    return datetime.now(UTC) >= deadline


def format_itsm_payload(incident: Incident, channel: str = "ITSM") -> dict[str, Any]:
    """Format incident as a ServiceNow/Jira-compatible ITSM payload."""
    severity_map = {"CRITICAL": "1", "WARNING": "2", "INFO": "3"}
    return {
        "short_description": incident.message or f"Data quality incident {incident.incident_id}",
        "description": (
            f"Data quality incident detected.\n"
            f"Incident ID: {incident.incident_id}\n"
            f"Severity: {incident.severity}\n"
            f"Source: {incident.source_id}\n"
            f"Domain: {incident.domain or 'N/A'}\n"
            f"Owner: {incident.owner or 'N/A'}"
        ),
        "urgency": severity_map.get(incident.severity, "3"),
        "impact": severity_map.get(incident.severity, "3"),
        "category": "data_quality",
        "subcategory": "incident",
        "caller_id": incident.owner or "system",
        "correlation_id": incident.fingerprint,
    }
