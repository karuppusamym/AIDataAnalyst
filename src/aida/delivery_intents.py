"""Durable outbound delivery: intents, attempts, backoff, and one worker.

**Invariant this module exists to hold:** an outcome may say *delivered* only
when a destination acknowledged the bytes, and anything accepted for sending
survives the process that accepted it.

Two review defects reduce to that one sentence
(`Docs/review-2026-09-05/REVIEW.md`). F04: `route_to_siem` formatted a CEF
message, wrote a local log line and returned ``True`` without opening a
socket. F12: a governance notification whose webhook failed was stamped as
processed anyway, and the sweep that would retry it selects only unstamped
rows. Both are the same error -- "we tried" recorded as "it arrived" -- so
they are fixed here once rather than in two places.

The shape, deliberately mirroring `aida.worm_archive` so the codebase has one
answer to this problem:

* a **transport protocol** with real implementations and an explicitly
  refusing default (`NullTransport`) when nothing is configured, so an
  unconfigured deployment reports *not configured* rather than success;
* a **persisted state machine** on `DeliveryIntent`, advanced only by commits;
* **receipts**: one appended `DeliveryAttempt` row per attempt, carrying the
  destination's answer or the reason there was none.

**The business path never blocks on a transport.** Creating an intent is a
`session.add` inside the caller's transaction -- no socket, no timeout, no
way for a downed SOC collector or a chat outage to fail a governance
decision. Delivery happens later, in `run_delivery_worker_pass`, wired onto
the fleet scheduler.

**Default off.** `delivery_worker_enabled` is False and every transport
resolves to `NullTransport` until an endpoint is named, so enabling this code
in an existing deployment starts no traffic.

Transports are synchronous and blocking, and the worker wraps them in
`asyncio.to_thread`. That keeps a socket's semantics honest and keeps each
transport directly testable against a real local server.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import session_factory
from aida.models import DeliveryAttempt, DeliveryIntent

logger = structlog.get_logger(__name__)

KIND_SIEM: Final = "SIEM_SECURITY_EVENT"
KIND_NOTIFICATION: Final = "GOVERNANCE_NOTIFICATION"

STATE_PENDING: Final = "PENDING"
STATE_DELIVERING: Final = "DELIVERING"
STATE_RETRYING: Final = "RETRYING"
STATE_DELIVERED: Final = "DELIVERED"
STATE_DEAD_LETTER: Final = "DEAD_LETTER"
STATE_DISCARDED: Final = "DISCARDED"
STATE_DUPLICATE: Final = "DUPLICATE"

#: States a worker may claim. `DELIVERING` is not among them: a claim is
#: reclaimed by expiry (`claim_expires_at`), never by another worker deciding
#: the first one looked slow.
CLAIMABLE_STATES: Final = (STATE_PENDING, STATE_RETRYING)
class DeliveryOutcome(StrEnum):
    """What a caller and an operator are entitled to be told.

    Replaces the `bool` `route_to_siem` used to return, in which "the feature
    is switched off", "no destination is configured" and "the SOC has the
    event" were all the same value. Every member below is a distinguishable
    fact, and `DELIVERED` is reachable only from a destination's own answer.
    """

    #: The feature is switched off. Nothing was recorded and nothing was sent.
    DISABLED = "DISABLED"
    #: Switched on, but no usable destination is configured. Nothing was sent.
    NOT_CONFIGURED = "NOT_CONFIGURED"
    #: Durably recorded, not yet attempted. This is what an enqueue returns --
    #: never `DELIVERED`, because at that moment nothing has been.
    QUEUED = "QUEUED"
    #: A destination acknowledged. The only outcome that claims delivery.
    DELIVERED = "DELIVERED"
    #: The attempt failed in a way that may succeed later; backoff is set.
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    #: The attempt failed in a way that will not succeed on repetition, or
    #: the retry budget is exhausted. Dead-lettered.
    FAILED_PERMANENT = "FAILED_PERMANENT"
    #: An equivalent intent for the same destination was already delivered.
    DUPLICATE = "DUPLICATE"


#: Which terminal state each finished outcome writes onto the intent.
_STATE_BY_OUTCOME: Final[dict[DeliveryOutcome, str]] = {
    DeliveryOutcome.DELIVERED: STATE_DELIVERED,
    DeliveryOutcome.FAILED_RETRYABLE: STATE_RETRYING,
    DeliveryOutcome.FAILED_PERMANENT: STATE_DEAD_LETTER,
    DeliveryOutcome.NOT_CONFIGURED: STATE_DISCARDED,
    DeliveryOutcome.DISABLED: STATE_DISCARDED,
    DeliveryOutcome.DUPLICATE: STATE_DUPLICATE,
}


class TransportError(RuntimeError):
    """A transport could not deliver. Carries whether repeating may help."""

    def __init__(self, detail: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retryable = retryable
        self.status_code = status_code


class TransportUnavailable(TransportError):
    """No usable destination is configured for this channel."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail, retryable=False)


@dataclass(frozen=True, slots=True)
class TransportReceipt:
    """A destination's acknowledgement. Only a transport may construct one."""

    destination: str
    transport: str
    detail: str = ""
    status_code: int | None = None


class DeliveryTransport(Protocol):
    """What the worker requires of a destination.

    Synchronous on purpose: `send` either returns a receipt it earned or
    raises. There is deliberately no third result that logs and returns
    something success-shaped, because that is the defect being removed.
    """

    name: str
    available: bool
    destination: str

    def send(self, payload: Mapping[str, Any]) -> TransportReceipt:
        """Deliver `payload`, or raise `TransportError`."""


class NullTransport:
    """The configured-nothing default. Refuses, and says why.

    This class is why an unconfigured deployment cannot report deliveries it
    did not make: `send` raises, the worker records DISCARDED with the reason
    on the attempt row, and no outcome ever reads DELIVERED.
    """

    name = "none"
    available = False

    def __init__(self, detail: str, *, destination: str = "unconfigured") -> None:
        self.detail = detail
        self.destination = destination

    def send(self, payload: Mapping[str, Any]) -> TransportReceipt:
        raise TransportUnavailable(self.detail)


#: HTTP statuses worth repeating: the destination is present but cannot take
#: the message right now. Everything else in 4xx is a message this destination
#: will reject every time, so repeating it is noise, not resilience.
_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({408, 423, 425, 429})


class WebhookTransport:
    """HTTP POST of a JSON body, over `httpx`'s synchronous client.

    Sync rather than async so every transport in this module has one shape and
    can be exercised against a real local HTTP server rather than a patched
    client. The worker calls it through `asyncio.to_thread`.

    Redirects are not followed: a webhook endpoint that answers 3xx is
    misconfigured, and quietly re-posting a security event or a governance
    notification to wherever it points is not a behaviour worth having.
    """

    name = "webhook"
    available = True

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = 5.0,
        verify_tls: bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.url = url
        self.destination = destination_label(url)
        self._timeout = timeout_seconds
        self._verify = verify_tls
        self._headers = dict(headers or {})

    def send(self, payload: Mapping[str, Any]) -> TransportReceipt:
        try:
            with httpx.Client(
                timeout=self._timeout, follow_redirects=False, verify=self._verify
            ) as client:
                response = client.post(self.url, json=dict(payload), headers=self._headers)
        except httpx.TimeoutException as exc:
            raise TransportError(f"timeout: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"transport error: {exc}", retryable=True) from exc

        code = response.status_code
        if 200 <= code < 300:
            return TransportReceipt(
                destination=self.destination,
                transport=self.name,
                detail=f"HTTP {code}",
                status_code=code,
            )
        retryable = code >= 500 or code in _RETRYABLE_STATUSES
        raise TransportError(f"HTTP {code}", retryable=retryable, status_code=code)


def destination_label(url: str) -> str:
    """A destination an operator can recognise and a log can safely hold.

    A Slack or Teams incoming-webhook URL is a bearer credential in its path,
    so the path, query and any userinfo are dropped and replaced by a short
    digest. The label identifies *which* destination without being usable as
    one. The live endpoint is re-read from settings at delivery time, never
    from the stored row -- which also means a rotated secret takes effect for
    intents queued before the rotation.
    """
    split = urlsplit(url)
    if not split.scheme:
        return url[:200]
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    host = split.hostname or ""
    port = f":{split.port}" if split.port else ""
    return f"{split.scheme}://{host}{port}/#{digest}"


def default_worker_identity() -> str:
    """Identify this replica for claim ownership. Host + pid is enough."""
    return f"{socket.gethostname()}:{os.getpid()}"


def dedup_key_for(*parts: object) -> str:
    """Stable identity of "this message, to this destination".

    Hashed rather than concatenated so the column has a fixed width and no
    part of a payload leaks into an index an operator can read.
    """
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def backoff_seconds(attempt_count: int, *, base: float, cap: float) -> float:
    """Exponential backoff, clamped. `attempt_count` is attempts already made."""
    if attempt_count <= 0:
        return 0.0
    exponent = min(attempt_count - 1, 16)
    return min(base * (2.0**exponent), cap)


# --- enqueue ---------------------------------------------------------------


def enqueue_intent(
    session: AsyncSession,
    *,
    organization_id: UUID | None,
    kind: str,
    channel: str,
    destination: str,
    dedup_key: str,
    payload: dict[str, Any],
    correlation_id: str | None = None,
    now: datetime | None = None,
) -> DeliveryIntent | None:
    """Stage one intent in the caller's transaction. No I/O, no commit.

    Returns the new row, or `None` when an equivalent intent is already
    staged in this same session -- the cheap half of deduplication, which
    stops one request that audits the same denial twice from queueing two
    messages. The durable half is the worker's, for the reason given in
    `DeliveryIntent`'s docstring: a unique constraint here could abort the
    business transaction, and a notification must never be able to do that.
    """
    moment = now or datetime.now(UTC)
    for staged in session.new:
        if (
            isinstance(staged, DeliveryIntent)
            and staged.dedup_key == dedup_key
            and staged.channel == channel
            and staged.kind == kind
        ):
            return None

    intent = DeliveryIntent(
        organization_id=organization_id,
        kind=kind,
        channel=channel,
        destination=destination,
        dedup_key=dedup_key,
        payload=payload,
        correlation_id=correlation_id,
        state=STATE_PENDING,
        requested_at=moment,
        next_attempt_at=moment,
        attempt_count=0,
    )
    session.add(intent)
    return intent


# --- claim -----------------------------------------------------------------


async def claim_due_intents(
    session: AsyncSession,
    *,
    kinds: tuple[str, ...],
    owner: str,
    now: datetime,
    batch_size: int,
    claim_seconds: int,
) -> list[DeliveryIntent]:
    """Take ownership of the next due intents, and commit the claim.

    Compare-and-set, not select-then-update: the `UPDATE ... WHERE state IN
    (claimable)` is what arbitrates between replicas, so two workers get
    disjoint batches without a lock statement in either dialect. The claim is
    committed before anything is sent, which is also what makes the worker's
    duplicate check see the other worker's in-flight row.

    An expired claim is reclaimable -- that is how a worker killed mid-attempt
    releases its work. The cost is that a message may be sent twice if the
    process dies between a destination's acknowledgement and our commit; at-
    least-once is the honest guarantee for a durable queue over one database,
    and it is the right side to err on for a security event.
    """
    expires_at = now + timedelta(seconds=claim_seconds)
    candidate_ids = (
        await session.scalars(
            select(DeliveryIntent.id)
            .where(
                DeliveryIntent.kind.in_(kinds),
                or_(
                    DeliveryIntent.state.in_(CLAIMABLE_STATES),
                    (DeliveryIntent.state == STATE_DELIVERING)
                    & (DeliveryIntent.claim_expires_at < now),
                ),
                DeliveryIntent.next_attempt_at <= now,
            )
            .order_by(DeliveryIntent.next_attempt_at, DeliveryIntent.requested_at)
            .limit(batch_size)
        )
    ).all()
    if not candidate_ids:
        return []

    claimed = 0
    for candidate in candidate_ids:
        result = await session.execute(
            update(DeliveryIntent)
            .where(
                DeliveryIntent.id == candidate,
                or_(
                    DeliveryIntent.state.in_(CLAIMABLE_STATES),
                    (DeliveryIntent.state == STATE_DELIVERING)
                    & (DeliveryIntent.claim_expires_at < now),
                ),
            )
            .values(state=STATE_DELIVERING, claimed_by=owner, claim_expires_at=expires_at)
        )
        claimed += int(getattr(result, "rowcount", 0) or 0)
    await session.commit()
    if not claimed:
        return []

    rows = (
        await session.scalars(
            select(DeliveryIntent)
            .where(
                DeliveryIntent.id.in_(candidate_ids),
                DeliveryIntent.state == STATE_DELIVERING,
                DeliveryIntent.claimed_by == owner,
            )
            .order_by(DeliveryIntent.requested_at)
        )
    ).all()
    return list(rows)


async def _already_delivered_elsewhere(
    session: AsyncSession, intent: DeliveryIntent
) -> DeliveryIntent | None:
    """The equivalent intent that wins, if this one is a duplicate.

    An equivalent intent wins if it is already DELIVERED, or if it is in
    flight and was requested earlier. Ordering by `requested_at` makes the
    tie-break deterministic, so a race between two workers suppresses the
    later row rather than sending both.
    """
    winner: DeliveryIntent | None = await session.scalar(
        select(DeliveryIntent)
        .where(
            DeliveryIntent.id != intent.id,
            DeliveryIntent.kind == intent.kind,
            DeliveryIntent.channel == intent.channel,
            DeliveryIntent.dedup_key == intent.dedup_key,
            or_(
                DeliveryIntent.state == STATE_DELIVERED,
                (DeliveryIntent.state == STATE_DELIVERING)
                & (DeliveryIntent.requested_at < intent.requested_at),
            ),
        )
        .order_by(DeliveryIntent.requested_at)
        .limit(1)
    )
    return winner


# --- one attempt -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """What one attempt did, for logs and for the tests."""

    intent_id: UUID
    outcome: DeliveryOutcome
    state: str
    attempt_number: int
    detail: str = ""


async def deliver_claimed_intent(
    session: AsyncSession,
    intent: DeliveryIntent,
    transport: DeliveryTransport,
    *,
    now: datetime,
    max_attempts: int,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
) -> AttemptResult:
    """Attempt one claimed intent and commit whatever actually happened.

    The commit is not optional and not the caller's: an attempt whose record
    is lost is indistinguishable from an attempt that never happened, which is
    precisely how F12's failures became invisible.
    """
    duplicate = await _already_delivered_elsewhere(session, intent)
    if duplicate is not None:
        return await _finish(
            session,
            intent,
            outcome=DeliveryOutcome.DUPLICATE,
            now=now,
            detail=f"suppressed: equivalent intent {duplicate.id} for the same destination",
            transport_name=transport.name,
            destination=transport.destination,
            max_attempts=max_attempts,
            backoff_base_seconds=backoff_base_seconds,
            backoff_max_seconds=backoff_max_seconds,
        )

    attempt_number = int(intent.attempt_count or 0) + 1
    try:
        receipt = await asyncio.to_thread(transport.send, intent.payload)
    except TransportUnavailable as error:
        return await _finish(
            session,
            intent,
            outcome=DeliveryOutcome.NOT_CONFIGURED,
            now=now,
            detail=str(error),
            transport_name=transport.name,
            destination=transport.destination,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            backoff_base_seconds=backoff_base_seconds,
            backoff_max_seconds=backoff_max_seconds,
        )
    except TransportError as error:
        return await _finish(
            session,
            intent,
            outcome=(
                DeliveryOutcome.FAILED_RETRYABLE
                if error.retryable
                else DeliveryOutcome.FAILED_PERMANENT
            ),
            now=now,
            detail=error.detail,
            status_code=error.status_code,
            transport_name=transport.name,
            destination=transport.destination,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            backoff_base_seconds=backoff_base_seconds,
            backoff_max_seconds=backoff_max_seconds,
        )
    except OSError as error:  # pragma: no cover - destination-specific
        return await _finish(
            session,
            intent,
            outcome=DeliveryOutcome.FAILED_RETRYABLE,
            now=now,
            detail=f"socket error: {error}",
            transport_name=transport.name,
            destination=transport.destination,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            backoff_base_seconds=backoff_base_seconds,
            backoff_max_seconds=backoff_max_seconds,
        )

    return await _finish(
        session,
        intent,
        outcome=DeliveryOutcome.DELIVERED,
        now=now,
        detail=receipt.detail,
        status_code=receipt.status_code,
        transport_name=receipt.transport,
        destination=receipt.destination,
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        backoff_base_seconds=backoff_base_seconds,
        backoff_max_seconds=backoff_max_seconds,
    )


async def _finish(
    session: AsyncSession,
    intent: DeliveryIntent,
    *,
    outcome: DeliveryOutcome,
    now: datetime,
    detail: str,
    transport_name: str,
    destination: str,
    max_attempts: int,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
    status_code: int | None = None,
    attempt_number: int | None = None,
) -> AttemptResult:
    """Write the attempt row, advance the state machine, commit."""
    state = _STATE_BY_OUTCOME[outcome]
    resolved_attempt = attempt_number if attempt_number is not None else int(intent.attempt_count)

    if attempt_number is not None:
        intent.attempt_count = attempt_number
        intent.attempted_at = now
        session.add(
            DeliveryAttempt(
                intent_id=intent.id,
                organization_id=intent.organization_id,
                attempt_number=attempt_number,
                outcome=str(outcome),
                transport=transport_name,
                destination=destination,
                status_code=status_code,
                detail=detail[:1000] or None,
                started_at=now,
                completed_at=now,
            )
        )

    if outcome is DeliveryOutcome.DELIVERED:
        intent.delivered_at = now
    elif outcome is DeliveryOutcome.FAILED_RETRYABLE:
        if resolved_attempt >= max_attempts:
            state = STATE_DEAD_LETTER
            outcome = DeliveryOutcome.FAILED_PERMANENT
            detail = f"{detail} (retry budget of {max_attempts} attempts exhausted)"
        else:
            intent.next_attempt_at = now + timedelta(
                seconds=backoff_seconds(
                    resolved_attempt, base=backoff_base_seconds, cap=backoff_max_seconds
                )
            )

    intent.state = state
    intent.last_outcome = str(outcome)
    intent.last_error = None if outcome is DeliveryOutcome.DELIVERED else detail[:1000] or None
    intent.claimed_by = None
    intent.claim_expires_at = None
    await session.commit()

    log = logger.info if outcome is DeliveryOutcome.DELIVERED else logger.warning
    log(
        "delivery_intent_attempt",
        intent_id=str(intent.id),
        kind=intent.kind,
        channel=intent.channel,
        destination=destination,
        outcome=str(outcome),
        state=state,
        attempt=resolved_attempt,
        detail=detail[:200],
    )
    return AttemptResult(
        intent_id=intent.id,
        outcome=outcome,
        state=state,
        attempt_number=resolved_attempt,
        detail=detail,
    )


# --- transport resolution --------------------------------------------------

#: `(intent, settings) -> transport`. A resolver never raises: an unusable
#: configuration returns `NullTransport`, which refuses at send time and is
#: recorded as NOT_CONFIGURED rather than as an exception nobody sees.
TransportResolver = Callable[[DeliveryIntent, Settings], DeliveryTransport]


def notification_transport_for(intent: DeliveryIntent, settings: Settings) -> DeliveryTransport:
    """Resolve a governance-notification channel to its live webhook.

    Read from settings, never from the stored row, so the credential in a
    Slack webhook URL is never persisted and a rotated URL applies to intents
    queued before the rotation.
    """
    if not settings.governance_notifications_enabled:
        return NullTransport("governance notifications are disabled")
    urls = {"SLACK": settings.slack_webhook_url, "TEAMS": settings.teams_webhook_url}
    url = urls.get(intent.channel)
    if not url:
        return NullTransport(f"no webhook URL is configured for channel {intent.channel!r}")
    return WebhookTransport(
        url,
        timeout_seconds=settings.governance_notification_timeout_seconds,
        verify_tls=settings.delivery_webhook_verify_tls,
    )


def default_resolvers() -> dict[str, TransportResolver]:
    """The two kinds this worker drains.

    `aida.siem_delivery` is imported inside the function on purpose: it
    imports this module for the transport protocol, so a module-level import
    here would be a cycle. One local import is a smaller price than splitting
    the protocol into a third file nobody would find.
    """
    from aida.siem_delivery import siem_transport_for

    return {KIND_SIEM: siem_transport_for, KIND_NOTIFICATION: notification_transport_for}


# --- worker ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeliveryPassResult:
    """What one worker pass did. Counts are per outcome, never a bare total."""

    claimed: int = 0
    delivered: int = 0
    retrying: int = 0
    dead_lettered: int = 0
    discarded: int = 0
    duplicates: int = 0
    results: tuple[AttemptResult, ...] = field(default=())


async def run_delivery_worker_pass(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    resolvers: Mapping[str, TransportResolver] | None = None,
    owner: str | None = None,
    session: AsyncSession | None = None,
) -> DeliveryPassResult:
    """Drain due delivery intents once. The scheduler calls this every tick.

    Off by default (`delivery_worker_enabled`), so an existing deployment that
    picks up this code sends nothing until an operator opts in -- and the
    intents queued in the meantime are still there when they do.

    `session` is injectable so a test can drive the worker against its own
    in-memory database; production passes none and the pass opens its own.
    """
    active = settings or get_settings()
    if not active.delivery_worker_enabled:
        return DeliveryPassResult()
    if session is not None:
        return await _drain(
            session, active, now=now or datetime.now(UTC), resolvers=resolvers, owner=owner
        )
    async with session_factory() as owned:
        return await _drain(
            owned, active, now=now or datetime.now(UTC), resolvers=resolvers, owner=owner
        )


async def _drain(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime,
    resolvers: Mapping[str, TransportResolver] | None,
    owner: str | None,
) -> DeliveryPassResult:
    table = dict(resolvers) if resolvers is not None else default_resolvers()
    identity = owner or default_worker_identity()
    claimed = await claim_due_intents(
        session,
        kinds=tuple(table),
        owner=identity,
        now=now,
        batch_size=settings.delivery_worker_batch_size,
        claim_seconds=settings.delivery_claim_seconds,
    )

    results: list[AttemptResult] = []
    for intent in claimed:
        resolver = table[intent.kind]
        result = await deliver_claimed_intent(
            session,
            intent,
            resolver(intent, settings),
            now=now,
            max_attempts=settings.delivery_max_attempts,
            backoff_base_seconds=settings.delivery_backoff_base_seconds,
            backoff_max_seconds=settings.delivery_backoff_max_seconds,
        )
        results.append(result)

    tally = {outcome: 0 for outcome in DeliveryOutcome}
    for result in results:
        tally[result.outcome] += 1
    return DeliveryPassResult(
        claimed=len(claimed),
        delivered=tally[DeliveryOutcome.DELIVERED],
        retrying=tally[DeliveryOutcome.FAILED_RETRYABLE],
        dead_lettered=tally[DeliveryOutcome.FAILED_PERMANENT],
        discarded=tally[DeliveryOutcome.NOT_CONFIGURED] + tally[DeliveryOutcome.DISABLED],
        duplicates=tally[DeliveryOutcome.DUPLICATE],
        results=tuple(results),
    )
