"""NT-1: governance events, delivered where people already work.

The competitive research behind `00-product/08` §6.3 makes one adoption point
repeatedly: a governance platform nobody opens governs nothing. Atlan's own
differentiation claim is collaboration-hub UX rather than catalog depth, and
Genie shipped to Slack and Teams before it shipped anywhere else.

This is the cheapest credible version of that: the seven governance events a
human actually needs to react to, pushed to Slack or Teams with a deep link
back into the portal. It is not a chat integration and deliberately not a
second control surface -- every message is a notification plus a link, never
an action, so nothing here can approve, publish, or grant.

**Reuses the existing delivery mechanism.** `quality_service.emit_itsm_webhook`
already POSTs to a configured webhook and persists a `NotificationEventRecord`
per attempt; this follows that shape rather than building a second one.

**Value-free (INV-6).** A message carries object type, id, principal, risk
tier and a link. Never a row, never SQL, never a description's text -- a
governance notification that leaked a column value into a Slack channel would
be the most public possible breach of the control plane's core property.

**Fail closed and silent.** Disabled or unconfigured means nothing is sent and
the reason is persisted as its own status, so an operator can tell "not
configured" from "delivered". Delivery never raises into the caller's
transaction: a downed Slack must not roll back a governance decision.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.context import get_correlation_id
from aida.events import record_audit
from aida.models import NotificationEventRecord
from aida.security import SecurityContext

logger = structlog.get_logger(__name__)

GovernanceEventKind = Literal[
    "REVIEW_REQUESTED",
    "REVIEW_DECIDED",
    "QUALITY_INCIDENT_OPENED",
    "QUALITY_INCIDENT_RESOLVED",
    "KILL_SWITCH_ENGAGED",
    "KILL_SWITCH_RELEASED",
    "CERTIFICATION_EXPIRING",
]

#: Every event kind this module knows how to render. A kind not listed here is
#: refused rather than sent as a bare dict, so a caller cannot invent an
#: unreviewed notification shape.
EVENT_KINDS: Final[tuple[str, ...]] = (
    "REVIEW_REQUESTED",
    "REVIEW_DECIDED",
    "QUALITY_INCIDENT_OPENED",
    "QUALITY_INCIDENT_RESOLVED",
    "KILL_SWITCH_ENGAGED",
    "KILL_SWITCH_RELEASED",
    "CERTIFICATION_EXPIRING",
)

#: Which portal screen each kind deep-links to. `ui-next` routes on
#: `#/<screen-id>`, so a link is the portal base plus that fragment.
_SCREEN_BY_KIND: Final[dict[str, str]] = {
    "REVIEW_REQUESTED": "governance",
    "REVIEW_DECIDED": "governance",
    "QUALITY_INCIDENT_OPENED": "quality",
    "QUALITY_INCIDENT_RESOLVED": "quality",
    "KILL_SWITCH_ENGAGED": "agents",
    "KILL_SWITCH_RELEASED": "agents",
    "CERTIFICATION_EXPIRING": "stewardship",
}

_HEADLINE_BY_KIND: Final[dict[str, str]] = {
    "REVIEW_REQUESTED": "Approval requested",
    "REVIEW_DECIDED": "Approval decided",
    "QUALITY_INCIDENT_OPENED": "Data quality incident opened",
    "QUALITY_INCIDENT_RESOLVED": "Data quality incident resolved",
    "KILL_SWITCH_ENGAGED": "AI kill switch ENGAGED",
    "KILL_SWITCH_RELEASED": "AI kill switch released",
    "CERTIFICATION_EXPIRING": "Certification expiring",
}

STATUS_SENT = "SENT"
STATUS_SKIPPED_DISABLED = "SKIPPED_DISABLED"
STATUS_SKIPPED_NO_URL = "SKIPPED_NO_URL"
STATUS_SKIPPED_EVENT_KIND = "SKIPPED_EVENT_KIND"
STATUS_FAILED = "FAILED"

_MAX_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class NotificationOutcome:
    channel: str
    status: str
    error: str | None = None


def deep_link(settings: Settings, kind: str, *, object_id: str | None) -> str | None:
    """A link back into the portal, or `None` when no base URL is configured.

    A notification without a link is still worth sending -- it tells someone
    something happened -- so an unconfigured base URL degrades the message
    rather than suppressing it.
    """
    base = (settings.portal_base_url or "").rstrip("/")
    if not base:
        return None
    screen = _SCREEN_BY_KIND.get(kind, "home")
    link = f"{base}/#/{screen}"
    if object_id:
        link = f"{link}?focus={object_id}"
    return link


def render_message(
    settings: Settings,
    kind: str,
    payload: dict[str, Any],
    *,
    channel: str,
) -> dict[str, Any]:
    """The wire body for one channel.

    Composed only from fields the caller passed and this module's own
    headline table -- never from free text a model produced, and never from
    anything that could carry a source value.
    """
    headline = _HEADLINE_BY_KIND.get(kind, kind.replace("_", " ").title())
    parts = [f"*{headline}*"]
    for label, key in (
        ("Object", "object_type"),
        ("Name", "object_name"),
        ("Risk tier", "risk_tier"),
        ("By", "principal_id"),
        ("Severity", "severity"),
        ("Expires", "expires_at"),
    ):
        value = payload.get(key)
        if value:
            parts.append(f"{label}: {value}")
    link = deep_link(settings, kind, object_id=payload.get("object_id"))
    if link:
        parts.append(link)
    text = "\n".join(parts)

    if channel == "TEAMS":
        # Teams' simple message-card shape; deliberately not an Adaptive Card
        # with actions, because a notification here must never be an action.
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": headline,
            "title": headline,
            "text": text.replace("*", ""),
        }
    return {"text": text}


def _dedup_key(kind: str, payload: dict[str, Any], channel: str) -> str:
    """Stable per (kind, object, channel) so a retry or a double-emit at the
    same hook point does not produce two rows for one event."""
    raw = f"{kind}|{payload.get('object_id')}|{payload.get('occurred_at')}|{channel}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def _system_context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id="system:governance-notifications",
        principal_type="SERVICE",
        organization_id=organization_id,
        roles=frozenset({"Operations"}),
    )


def _configured_channels(settings: Settings) -> list[tuple[str, str | None]]:
    return [
        ("SLACK", settings.slack_webhook_url),
        ("TEAMS", settings.teams_webhook_url),
    ]


async def _post(
    url: str, body: dict[str, Any], *, timeout_seconds: float
) -> tuple[str, str | None]:
    """POST with a bounded retry. Never raises: see the module docstring."""
    last_error: str | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds, follow_redirects=False
            ) as client:
                response = await client.post(url, json=body)
                response.raise_for_status()
            return STATUS_SENT, None
        except httpx.HTTPError as exc:
            last_error = str(exc)[:1000]
    return STATUS_FAILED, last_error


async def notify_governance_event(
    session: AsyncSession,
    organization_id: UUID,
    event_kind: str,
    payload: dict[str, Any],
    *,
    settings: Settings,
) -> list[NotificationOutcome]:
    """Deliver one governance event to every configured channel.

    Persists one `NotificationEventRecord` per channel per attempt so an
    operator can tell, from the ledger alone, whether a message was sent,
    skipped, or failed -- and why. Returns the outcomes rather than raising,
    because the caller is in the middle of a governance transaction that must
    not be rolled back by a chat outage.
    """
    if event_kind not in EVENT_KINDS:
        logger.warning("governance_notification_unknown_kind", event_kind=event_kind)
        return [NotificationOutcome("NONE", STATUS_SKIPPED_EVENT_KIND, event_kind)]

    outcomes: list[NotificationOutcome] = []
    enabled = settings.governance_notifications_enabled
    selected = settings.governance_notification_events

    if enabled and event_kind not in selected:
        return [NotificationOutcome("NONE", STATUS_SKIPPED_EVENT_KIND, "not selected")]

    payload = {**payload, "occurred_at": payload.get("occurred_at") or ""}

    for channel, url in _configured_channels(settings):
        if not enabled:
            status, error = STATUS_SKIPPED_DISABLED, None
        elif not url:
            status, error = STATUS_SKIPPED_NO_URL, None
        else:
            status, error = await _post(
                url,
                render_message(settings, event_kind, payload, channel=channel),
                timeout_seconds=settings.governance_notification_timeout_seconds,
            )
        session.add(
            NotificationEventRecord(
                organization_id=organization_id,
                incident_id=None,
                rule_id=None,
                channel=channel,
                recipients=[],
                status=status,
                dedup_key=_dedup_key(event_kind, payload, channel),
                sent_at=datetime.now(UTC) if status == STATUS_SENT else None,
            )
        )
        outcomes.append(NotificationOutcome(channel, status, error))

    record_audit(
        session,
        _system_context(organization_id),
        action="governance.notification.dispatch",
        resource_type="notification_event",
        resource_id=str(payload.get("object_id") or ""),
        outcome="SUCCESS" if any(o.status == STATUS_SENT for o in outcomes) else "DENIED",
        correlation_id=get_correlation_id(),
        details={
            "event_kind": event_kind,
            "object_type": payload.get("object_type"),
            "channels": {o.channel: o.status for o in outcomes},
        },
    )
    return outcomes


async def notify_safely(
    session: AsyncSession,
    organization_id: UUID,
    event_kind: str,
    payload: dict[str, Any],
    *,
    settings: Settings,
) -> None:
    """The form every hook point calls: one line, and it cannot break the
    caller. A notification failure is logged and dropped, never raised --
    the governance write that triggered it has already happened and is the
    thing that matters."""
    if not settings.governance_notifications_enabled:
        return
    try:
        await notify_governance_event(
            session, organization_id, event_kind, payload, settings=settings
        )
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning(
            "governance_notification_failed", event_kind=event_kind, error=str(exc)[:500]
        )
