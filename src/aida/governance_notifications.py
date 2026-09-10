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

**Nothing is sent from here.** A hook point stages a `DeliveryIntent`
(`aida.delivery_intents`) inside its own transaction and returns; the fleet
scheduler's delivery worker attempts it, retries with backoff, and records
each attempt. This module used to POST inline with a two-shot retry and no
durable record of failure, which is how a failed notification became
permanently indistinguishable from a delivered one
(`Docs/review-2026-09-05/REVIEW.md` F12).

**Value-free (INV-6).** A message carries object type, id, principal, risk
tier and a link. Never a row, never SQL, never a description's text -- a
governance notification that leaked a column value into a Slack channel would
be the most public possible breach of the control plane's core property.

**Fail closed and silent.** Disabled or unconfigured means nothing is queued
and the reason is persisted as its own status, so an operator can tell "not
configured" from "queued" from "delivered". Delivery never raises into the
caller's transaction: a downed Slack must not roll back a governance
decision -- and now cannot even be reached from one.

**The commit lifecycle, which was the other half of F12.** Two hook points
(`semantic_api.decide_governance_review`, `agent_contract_api`'s kill switch)
commit their business transaction and *then* call in here; the FastAPI
session dependency yields and closes rather than committing, so rows staged
after that commit were discarded. Two others (`quality_service`,
`certification_expiry_warning`) call mid-transaction and must not have their
business writes committed out from under them. `session.in_transaction()`,
sampled before anything is staged, distinguishes the two cases exactly: a
caller that owns an open transaction keeps the commit, a caller that has
none gets its notification evidence committed here. Either way the intent
and the decision it describes commit together or not at all.

That discrimination relies on `atlas.platform.db` building sessions with
`expire_on_commit=False`. Otherwise reading an attribute off the row a caller
just committed would autobegin a new transaction, and a post-commit caller
would be indistinguishable from a mid-transaction one. It does; this
paragraph exists so that changing it is a decision somebody makes on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.context import get_correlation_id
from aida.delivery_intents import (
    KIND_NOTIFICATION,
    STATE_DEAD_LETTER,
    STATE_DELIVERED,
    STATE_DISCARDED,
    STATE_DUPLICATE,
    dedup_key_for,
    destination_label,
    enqueue_intent,
)
from aida.events import record_audit
from aida.models import DeliveryIntent, NotificationEventRecord
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
#: Durably recorded and owed to a destination, but not yet attempted. This is
#: the status a hook point now produces: it is the truthful one at the moment
#: the business transaction commits, and the worker replaces it with SENT or
#: FAILED once a destination has actually answered.
STATUS_QUEUED = "QUEUED"

#: How a finished delivery intent reads in the notification ledger.
_LEDGER_STATUS_BY_STATE: Final[dict[str, str]] = {
    STATE_DELIVERED: STATUS_SENT,
    STATE_DEAD_LETTER: STATUS_FAILED,
    STATE_DISCARDED: STATUS_SKIPPED_NO_URL,
    STATE_DUPLICATE: STATUS_SENT,
}


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
    same hook point does not produce two deliveries for one event.

    Shared by the notification ledger row and its delivery intent, which is
    what lets the worker's outcome be reflected back onto the ledger without a
    foreign key between a governance concern and a transport one.
    """
    return dedup_key_for(kind, payload.get("object_id"), payload.get("occurred_at"), channel)


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




async def notify_governance_event(
    session: AsyncSession,
    organization_id: UUID,
    event_kind: str,
    payload: dict[str, Any],
    *,
    settings: Settings,
) -> list[NotificationOutcome]:
    """Record one governance event as owed to every configured channel.

    Stages a `DeliveryIntent` per channel plus the `NotificationEventRecord`
    an operator reads, so the ledger and the obligation to deliver commit
    together. Nothing is sent here: no socket is opened on the caller's
    thread, so a chat outage cannot delay, fail or roll back the governance
    write that triggered this.

    `STATUS_QUEUED` is deliberately not `STATUS_SENT`. The previous
    implementation POSTed inline and wrote SENT or FAILED, then discarded the
    FAILED row's meaning by never retrying it; a status that says "queued"
    until a destination answers is the only one true at this point.
    """
    if event_kind not in EVENT_KINDS:
        logger.warning("governance_notification_unknown_kind", event_kind=event_kind)
        return [NotificationOutcome("NONE", STATUS_SKIPPED_EVENT_KIND, event_kind)]

    enabled = settings.governance_notifications_enabled
    selected = settings.governance_notification_events

    if enabled and event_kind not in selected:
        return [NotificationOutcome("NONE", STATUS_SKIPPED_EVENT_KIND, "not selected")]

    # Sampled before anything is staged: afterwards SQLAlchemy has autobegun
    # and every caller would look identical. See the module docstring.
    caller_owns_transaction = session.in_transaction()

    payload = {**payload, "occurred_at": payload.get("occurred_at") or ""}
    outcomes: list[NotificationOutcome] = []

    for channel, url in _configured_channels(settings):
        dedup_key = _dedup_key(event_kind, payload, channel)
        if not enabled:
            status, error = STATUS_SKIPPED_DISABLED, None
        elif not url:
            status, error = STATUS_SKIPPED_NO_URL, None
        else:
            status, error = STATUS_QUEUED, None
            enqueue_intent(
                session,
                organization_id=organization_id,
                kind=KIND_NOTIFICATION,
                channel=channel,
                destination=destination_label(url),
                dedup_key=dedup_key,
                payload=render_message(settings, event_kind, payload, channel=channel),
                correlation_id=get_correlation_id(),
            )
        session.add(
            NotificationEventRecord(
                organization_id=organization_id,
                incident_id=None,
                rule_id=None,
                channel=channel,
                recipients=[],
                status=status,
                dedup_key=dedup_key,
                sent_at=None,
            )
        )
        outcomes.append(NotificationOutcome(channel, status, error))

    record_audit(
        session,
        _system_context(organization_id),
        action="governance.notification.dispatch",
        resource_type="notification_event",
        resource_id=str(payload.get("object_id") or ""),
        # A queued intent is a successful dispatch: the platform has durably
        # accepted the obligation. Whether a destination accepted it is the
        # delivery worker's audit trail, not this one's.
        outcome="SUCCESS" if any(o.status == STATUS_QUEUED for o in outcomes) else "DENIED",
        correlation_id=get_correlation_id(),
        details={
            "event_kind": event_kind,
            "object_type": payload.get("object_type"),
            "channels": {o.channel: o.status for o in outcomes},
        },
    )
    if not caller_owns_transaction:
        await session.commit()
    return outcomes


async def sync_notification_ledger(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 500
) -> int:
    """Reflect finished delivery intents back onto the notification ledger.

    `NotificationEventRecord` is what the notifications API and the portal
    read, and it is written by the business transaction, which cannot know
    whether Slack later answered. This closes that loop from the other side: a
    QUEUED ledger row whose intent has reached a terminal state becomes SENT
    or FAILED, matched on the `dedup_key` both rows were given.

    Called from the scheduler's notification pass, so it converges every tick
    without pushing a governance concern into the generic delivery worker.
    """
    moment = now or datetime.now(UTC)
    finished = (
        await session.execute(
            select(
                DeliveryIntent.dedup_key,
                DeliveryIntent.channel,
                DeliveryIntent.organization_id,
                DeliveryIntent.state,
                DeliveryIntent.delivered_at,
            )
            .where(
                DeliveryIntent.kind == KIND_NOTIFICATION,
                DeliveryIntent.state.in_(tuple(_LEDGER_STATUS_BY_STATE)),
            )
            .order_by(DeliveryIntent.requested_at.desc())
            .limit(limit)
        )
    ).all()

    updated = 0
    for dedup_key, channel, organization_id, state, delivered_at in finished:
        status = _LEDGER_STATUS_BY_STATE[state]
        result = await session.execute(
            update(NotificationEventRecord)
            .where(
                NotificationEventRecord.dedup_key == dedup_key,
                NotificationEventRecord.channel == channel,
                NotificationEventRecord.organization_id == organization_id,
                NotificationEventRecord.status == STATUS_QUEUED,
            )
            .values(
                status=status,
                sent_at=(delivered_at or moment) if status == STATUS_SENT else None,
            )
        )
        updated += int(getattr(result, "rowcount", 0) or 0)
    return updated


async def notify_safely(
    session: AsyncSession,
    organization_id: UUID,
    event_kind: str,
    payload: dict[str, Any],
    *,
    settings: Settings,
) -> None:
    """The form every hook point calls: one line, and it cannot break the
    caller.

    What it swallows has narrowed, and that narrowing is the point of F12. It
    used to swallow *delivery* failures, which is how a message nobody
    received became indistinguishable from one that arrived. There is no
    delivery here any more -- only staging -- so the only thing left to
    swallow is a programming error while composing an intent, and that
    genuinely must not roll back a governance decision that already
    happened."""
    if not settings.governance_notifications_enabled:
        return
    try:
        await notify_governance_event(
            session, organization_id, event_kind, payload, settings=settings
        )
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning(
            "governance_notification_enqueue_failed",
            event_kind=event_kind,
            error=str(exc)[:500],
        )
