"""Security event routing to SOC/SIEM (OB-2).

Formats security events in CEF (Common Event Format) and routes them
via syslog or webhook transport to the organization's security operations
center.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    """Security event to be routed to SIEM."""

    event_type: str  # AUTH_FAILURE, POLICY_VIOLATION, INJECTION_DETECTED,
    # CROSS_TENANT_ATTEMPT, PRIVILEGE_ESCALATION
    severity: str  # LOW, MEDIUM, HIGH, CRITICAL
    source: str
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    organization_id: str | None = None
    principal_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class SiemConfig:
    """SIEM routing configuration."""

    transport: str = "webhook"  # syslog, webhook
    endpoint: str = ""
    enabled: bool = False
    include_details: bool = True


SEVERITY_MAP: dict[str, int] = {
    "LOW": 3,
    "MEDIUM": 5,
    "HIGH": 8,
    "CRITICAL": 10,
}

EVENT_TYPE_IDS: dict[str, int] = {
    "AUTH_FAILURE": 100,
    "POLICY_VIOLATION": 200,
    "INJECTION_DETECTED": 300,
    "CROSS_TENANT_ATTEMPT": 400,
    "PRIVILEGE_ESCALATION": 500,
}


def format_cef(event: SecurityEvent) -> str:
    """Format a security event in CEF (Common Event Format).

    CEF format: CEF:Version|Device Vendor|Device Product|Device Version|
                Signature ID|Name|Severity|Extension
    """
    sig_id = EVENT_TYPE_IDS.get(event.event_type, 999)
    severity = SEVERITY_MAP.get(event.severity, 5)
    name = event.event_type.replace("_", " ").title()

    extensions: list[str] = [
        f"src={event.source}",
        f"rt={event.timestamp.strftime('%b %d %Y %H:%M:%S')}",
    ]
    if event.organization_id:
        extensions.append(f"cs1={event.organization_id}")
        extensions.append("cs1Label=OrganizationId")
    if event.principal_id:
        extensions.append(f"suser={event.principal_id}")
    if event.correlation_id:
        extensions.append(f"cn1={event.correlation_id}")
        extensions.append("cn1Label=CorrelationId")

    extension_str = " ".join(extensions)
    return (
        f"CEF:0|Atlas|DataIntelligence|1.0|{sig_id}|{name}|{severity}|"
        f"{extension_str}"
    )


def format_webhook_payload(event: SecurityEvent) -> dict[str, Any]:
    """Format a security event as a JSON webhook payload."""
    payload: dict[str, Any] = {
        "event_type": event.event_type,
        "severity": event.severity,
        "source": event.source,
        "timestamp": event.timestamp.isoformat(),
        "cef_severity": SEVERITY_MAP.get(event.severity, 5),
    }
    if event.organization_id:
        payload["organization_id"] = event.organization_id
    if event.principal_id:
        payload["principal_id"] = event.principal_id
    if event.correlation_id:
        payload["correlation_id"] = event.correlation_id
    if event.details:
        payload["details"] = event.details
    return payload


def route_to_siem(event: SecurityEvent, config: SiemConfig) -> bool:
    """Route a security event to SOC/SIEM.

    Returns True if successfully routed, False otherwise. This is a
    synchronous routing stub; production deployments should use async
    transports.
    """
    if not config.enabled:
        logger.debug("siem_routing_disabled", event_type=event.event_type)
        return False

    if not config.endpoint:
        logger.warning("siem_endpoint_not_configured", event_type=event.event_type)
        return False

    if config.transport == "syslog":
        cef_message = format_cef(event)
        logger.info(
            "siem_event_routed",
            transport="syslog",
            event_type=event.event_type,
            severity=event.severity,
            cef_length=len(cef_message),
        )
        return True
    elif config.transport == "webhook":
        payload = format_webhook_payload(event)
        logger.info(
            "siem_event_routed",
            transport="webhook",
            event_type=event.event_type,
            severity=event.severity,
            endpoint=config.endpoint,
            payload_field_count=len(payload),
        )
        return True
    else:
        logger.error("siem_unsupported_transport", transport=config.transport)
        return False
