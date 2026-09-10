"""Security event routing to SOC/SIEM (OB-2): formatting and enqueue.

**Invariant this module exists to hold:** a caller is told what actually
happened to a security event -- switched off, no destination, or durably
queued for a transport that will report a destination's own answer -- and
never that it was delivered when it was not.

`route_to_siem` used to format a CEF message, write a local structlog line
and `return True` for both configured transports without opening a socket
(`Docs/review-2026-09-05/REVIEW.md` F04). An operator watching
`siem_event_routed` had every reason to believe their SOC was receiving
events. That is now three separable facts, expressed as
`aida.delivery_intents.DeliveryOutcome`:

* `DISABLED` -- the feature is off;
* `NOT_CONFIGURED` -- on, but `siem_endpoint` names no reachable destination
  (including the historical placeholder `internal://security-log-pipeline`,
  which is a label, not an address);
* `QUEUED` -- a `DeliveryIntent` is committed with the caller's transaction
  and `aida.siem_delivery` will attempt it. `DELIVERED` is not reachable from
  here, because at the moment of the call nothing has been delivered.

**Minimisation is applied once, at enqueue.** When `include_details` is
false the details are omitted from the *stored payload*, so every transport
rendered from that payload -- the webhook body and the CEF extension alike --
suppresses them. The old code dropped details from neither in practice; the
fix is not to remember twice but to have one place where they can be present.

This module deliberately does not import `aida.siem_delivery`: transports
import these formatters, so the dependency runs one way only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from urllib.parse import urlsplit
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from aida.delivery_intents import (
    KIND_SIEM,
    DeliveryOutcome,
    dedup_key_for,
    destination_label,
    enqueue_intent,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    """Security event to be routed to SIEM."""

    event_type: str  # AUTH_FAILURE, POLICY_VIOLATION, INJECTION_DETECTED,
    # CROSS_TENANT_ATTEMPT, PRIVILEGE_ESCALATION, SECURITY_CONTROL_CHANGE
    severity: str  # LOW, MEDIUM, HIGH, CRITICAL
    source: str
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    organization_id: str | None = None
    principal_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class SiemConfig:
    """SIEM routing configuration.

    `enabled` defaults False and `endpoint` to empty: this dataclass is
    constructed at two call sites from settings, and a default-constructed one
    must never be a live destination.
    """

    transport: str = "webhook"  # syslog, webhook
    endpoint: str = ""
    enabled: bool = False
    include_details: bool = True
    timeout_seconds: float = 5.0
    verify_tls: bool = True


SEVERITY_MAP: dict[str, int] = {
    "LOW": 3,
    "MEDIUM": 5,
    "HIGH": 8,
    "CRITICAL": 10,
}

#: RFC 5424 severity for each of our four levels. Facility is local0 (16).
SYSLOG_SEVERITY_MAP: dict[str, int] = {
    "LOW": 6,  # informational
    "MEDIUM": 4,  # warning
    "HIGH": 3,  # error
    "CRITICAL": 2,  # critical
}

EVENT_TYPE_IDS: dict[str, int] = {
    "AUTH_FAILURE": 100,
    "POLICY_VIOLATION": 200,
    "INJECTION_DETECTED": 300,
    "CROSS_TENANT_ATTEMPT": 400,
    "PRIVILEGE_ESCALATION": 500,
    # Admin/security-control changes that are not attacks but still SOC-
    # notable (kill-switch engagement, token revocation): OB-2.
    "SECURITY_CONTROL_CHANGE": 600,
}

#: Endpoint schemes that name no destination. `internal://` was this
#: platform's default `siem_endpoint` when routing was a structlog line, and
#: a deployment carrying it forward has not chosen a SOC collector -- so it
#: resolves to NOT_CONFIGURED rather than starting traffic on upgrade.
_SENTINEL_SCHEMES: Final[frozenset[str]] = frozenset({"internal", "none", "stub"})

_CEF_ESCAPES: Final[tuple[tuple[str, str], ...]] = (
    ("\\", "\\\\"),
    ("|", "\\|"),
    ("=", "\\="),
    ("\n", "\\n"),
    ("\r", "\\n"),
)


@dataclass(frozen=True, slots=True)
class SiemDestination:
    """A parsed `siem_endpoint`. `usable` is False when it names nothing."""

    kind: str  # webhook, syslog, none
    usable: bool
    url: str = ""
    host: str = ""
    port: int = 0
    protocol: str = "udp"  # syslog only: udp or tcp
    reason: str = ""


def parse_siem_endpoint(config: SiemConfig) -> SiemDestination:
    """Resolve configuration to a destination, or to a stated reason there is none.

    Accepted:

    * webhook -- `http://` or `https://`;
    * syslog -- `syslog://host:port` (UDP, RFC 5424 over a datagram),
      `syslog+udp://host:port`, or `syslog+tcp://host:port` (RFC 6587
      octet-counted framing). The port defaults to 514 for UDP and 601 for
      TCP, the IANA assignments for the two.

    Everything else -- empty, a sentinel scheme, a scheme that does not match
    the selected transport -- is unusable, and says so.
    """
    endpoint = (config.endpoint or "").strip()
    if not endpoint:
        return SiemDestination("none", False, reason="siem_endpoint is empty")
    split = urlsplit(endpoint)
    scheme = split.scheme.lower()
    if scheme in _SENTINEL_SCHEMES:
        return SiemDestination(
            "none",
            False,
            reason=f"siem_endpoint {endpoint!r} names no destination (scheme {scheme!r})",
        )

    if config.transport == "webhook":
        if scheme not in ("http", "https") or not split.hostname:
            return SiemDestination(
                "none",
                False,
                reason=f"webhook transport needs an http(s) siem_endpoint, got {endpoint!r}",
            )
        return SiemDestination("webhook", True, url=endpoint)

    if config.transport == "syslog":
        if scheme not in ("syslog", "syslog+udp", "syslog+tcp") or not split.hostname:
            return SiemDestination(
                "none",
                False,
                reason=(
                    "syslog transport needs syslog://, syslog+udp:// or syslog+tcp://, "
                    f"got {endpoint!r}"
                ),
            )
        protocol = "tcp" if scheme == "syslog+tcp" else "udp"
        port = split.port or (601 if protocol == "tcp" else 514)
        return SiemDestination(
            "syslog", True, host=split.hostname, port=port, protocol=protocol, url=endpoint
        )

    return SiemDestination("none", False, reason=f"unsupported siem_transport {config.transport!r}")


def _escape_cef(value: str) -> str:
    for raw, escaped in _CEF_ESCAPES:
        value = value.replace(raw, escaped)
    return value


def siem_payload(event: SecurityEvent, config: SiemConfig) -> dict[str, Any]:
    """The minimised, transport-neutral record persisted on the intent.

    Details survive here only when `include_details` is true. Both transports
    render from this dict, which is what makes "honour include_details" a
    property of the data rather than a rule two formatters must remember.
    """
    payload: dict[str, Any] = {
        "event_type": event.event_type,
        "severity": event.severity,
        "source": event.source,
        "timestamp": event.timestamp.isoformat(),
        "cef_severity": SEVERITY_MAP.get(event.severity, 5),
        "organization_id": event.organization_id,
        "principal_id": event.principal_id,
        "correlation_id": event.correlation_id,
    }
    if config.include_details and event.details:
        payload["details"] = dict(event.details)
    return payload


def format_webhook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The JSON body. Null-valued optional fields are dropped, details are not
    re-derived -- if they are absent from `payload` they are absent here."""
    body = {key: value for key, value in payload.items() if value is not None}
    return body


def format_cef(payload: dict[str, Any]) -> str:
    """Format a minimised payload in CEF (Common Event Format).

    ``CEF:Version|Device Vendor|Device Product|Device Version|Signature ID|
    Name|Severity|Extension``

    Details, when present, become numbered `cs`/`csLabel` extension pairs.
    When `include_details` suppressed them at enqueue they are simply not in
    `payload`, so there is no branch here that could forget to honour it.
    """
    event_type = str(payload.get("event_type", "UNKNOWN"))
    sig_id = EVENT_TYPE_IDS.get(event_type, 999)
    severity = int(payload.get("cef_severity", 5))
    name = event_type.replace("_", " ").title()
    timestamp = payload.get("timestamp")
    moment = datetime.fromisoformat(str(timestamp)) if timestamp else datetime.now(UTC)

    extensions: list[str] = [
        f"src={_escape_cef(str(payload.get('source', '')))}",
        f"rt={moment.strftime('%b %d %Y %H:%M:%S')}",
    ]
    if payload.get("organization_id"):
        extensions.append(f"cs1={_escape_cef(str(payload['organization_id']))}")
        extensions.append("cs1Label=OrganizationId")
    if payload.get("principal_id"):
        extensions.append(f"suser={_escape_cef(str(payload['principal_id']))}")
    if payload.get("correlation_id"):
        extensions.append(f"cn1={_escape_cef(str(payload['correlation_id']))}")
        extensions.append("cn1Label=CorrelationId")

    details = payload.get("details")
    if isinstance(details, dict):
        # cs1 is taken by the organization id above, so detail slots start at
        # 2. CEF defines cs1..cs6; anything past that is dropped rather than
        # emitted as an invalid key a collector would reject.
        slot = 2
        for key in sorted(details):
            if slot > 6:
                break
            value = details[key]
            if value is None:
                continue
            extensions.append(f"cs{slot}={_escape_cef(str(value))}")
            extensions.append(f"cs{slot}Label={_escape_cef(str(key))}")
            slot += 1

    extension_str = " ".join(extensions)
    return f"CEF:0|Atlas|DataIntelligence|1.0|{sig_id}|{name}|{severity}|{extension_str}"


def _dedup_key(payload: dict[str, Any], destination: str) -> str:
    return dedup_key_for(
        payload.get("event_type"),
        payload.get("severity"),
        payload.get("organization_id"),
        payload.get("principal_id"),
        payload.get("correlation_id"),
        payload.get("timestamp"),
        destination,
    )


def _organization_uuid(raw: str | None) -> UUID | None:
    """`SecurityEvent` carries the organization as a string; the intent row
    carries a foreign key. An unparseable value becomes NULL rather than a
    failed insert -- an AUTH_FAILURE has no organization at all, and no
    security event is worth losing to a formatting mismatch."""
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def routing_state(config: SiemConfig) -> tuple[DeliveryOutcome, SiemDestination]:
    """Classify configuration without touching the database or a socket."""
    if not config.enabled:
        return DeliveryOutcome.DISABLED, SiemDestination("none", False, reason="siem_enabled=false")
    destination = parse_siem_endpoint(config)
    if not destination.usable:
        return DeliveryOutcome.NOT_CONFIGURED, destination
    return DeliveryOutcome.QUEUED, destination


def route_to_siem(
    session: AsyncSession, event: SecurityEvent, config: SiemConfig
) -> DeliveryOutcome:
    """Record a security event for delivery, inside the caller's transaction.

    Synchronous and I/O-free: a `session.add`, nothing more. A SOC collector
    that is slow, down or misconfigured therefore cannot delay or fail the
    audited operation that produced the event -- which is the property the
    old implementation obtained by not sending anything at all.

    Returns `DISABLED`, `NOT_CONFIGURED` or `QUEUED`. It never returns
    `DELIVERED`; only `aida.siem_delivery`, holding a destination's answer,
    can produce that.
    """
    outcome, destination = routing_state(config)
    if outcome is not DeliveryOutcome.QUEUED:
        logger.debug(
            "siem_event_not_routed",
            event_type=event.event_type,
            outcome=str(outcome),
            reason=destination.reason,
        )
        return outcome

    payload = siem_payload(event, config)
    label = (
        destination_label(destination.url)
        if destination.kind == "webhook"
        else f"syslog+{destination.protocol}://{destination.host}:{destination.port}"
    )
    intent = enqueue_intent(
        session,
        organization_id=_organization_uuid(event.organization_id),
        kind=KIND_SIEM,
        channel=destination.kind.upper(),
        destination=label,
        dedup_key=_dedup_key(payload, label),
        payload=payload,
        correlation_id=event.correlation_id,
    )
    logger.info(
        "siem_event_queued",
        transport=destination.kind,
        event_type=event.event_type,
        severity=event.severity,
        destination=label,
        deduplicated=intent is None,
    )
    return DeliveryOutcome.QUEUED


async def route_to_siem_durably(event: SecurityEvent, config: SiemConfig) -> DeliveryOutcome:
    """Record a security event in this module's own committed transaction.

    For the one caller whose session is about to be discarded: authentication
    refusal happens before a `SecurityContext` exists and ends in an
    `HTTPException`, so the request's session is rolled back and closed. A
    rejected bearer token is exactly the event a SOC most needs, and staging
    it into a transaction that will never commit would reintroduce F12's
    defect on the security path.

    Costs one INSERT per refused authentication -- and only when a destination
    is actually configured, since an unusable configuration returns before any
    session is opened.
    """
    outcome, destination = routing_state(config)
    if outcome is not DeliveryOutcome.QUEUED:
        logger.debug(
            "siem_event_not_routed",
            event_type=event.event_type,
            outcome=str(outcome),
            reason=destination.reason,
        )
        return outcome

    from aida.db import session_factory

    async with session_factory() as session, session.begin():
        return route_to_siem(session, event, config)
