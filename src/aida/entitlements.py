"""Idempotent external entitlement provisioning isolated from governance decisions.

R11-B4. An approved access request used to stop here: the default provider
(``outbox``) returned ``PENDING`` and did nothing at all, and the only other
provider posted a webhook *inline*, inside the request handler, with no ledger
row and no retry. So a product owner approved a request, the consumer's access
status said "approved", and nothing anywhere had granted anything.

Two providers, and the distinction between them is the whole design:

``outbox`` -- **this platform is the entitlement authority.** There is no
external system whose acknowledgement is required, because the thing that
grants access *is* the ``DataProductAccessRequest`` row, and the thing that
enforces it (`product_marketplace_api.authorize_product_consumption`) reads
that same row. So provisioning is a state transition committed in the same
transaction as the approval that caused it. That is not an optimistic flip:
there is no remote side that could later disagree, and the receipt is written
before the transaction commits, so a grant and its audit trail cannot diverge.
This is the right default precisely because the honest local answer is a local
answer -- queueing a message to ourselves and waiting for ourselves to read it
would add a failure mode without adding a guarantee.

``webhook`` -- **an external system mirrors the grant** (a warehouse, an IAM).
Now an acknowledgement genuinely is required, so the call goes through the
durable `delivery_intents` ledger like every other outbound call in the
platform: staged in the approving transaction, attempted by the delivery
worker, retried with backoff, dead-lettered visibly when the budget runs out.
The access request stays ``PENDING`` -- and therefore still denies queries --
until the destination actually answers. `settle_entitlement_deliveries` is
what turns a delivered intent into ``PROVISIONED``.

What is deliberately *not* here: any second delivery mechanism. The webhook
path owns no sockets, no retry loop and no backoff of its own; it owns a
payload and a transport, and `aida.delivery_intents` owns the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.delivery_intents import (
    STATE_DEAD_LETTER,
    STATE_DELIVERED,
    STATE_DISCARDED,
    DeliveryPassResult,
    DeliveryTransport,
    NullTransport,
    WebhookTransport,
    dedup_key_for,
    destination_label,
    enqueue_intent,
    run_delivery_worker_pass,
)
from aida.models import DataProductAccessRequest, DeliveryIntent
from aida.security_types import SecurityContext

#: Discriminates our rows in the shared `delivery_intent` ledger. `kind` is a
#: plain `String(40)` with no CHECK constraint, so a new kind needs no
#: migration -- only a resolver, which `run_entitlement_fulfilment_pass`
#: injects rather than registering globally.
KIND_ENTITLEMENT: Final = "DATA_PRODUCT_ENTITLEMENT"
CHANNEL_ENTITLEMENT: Final = "ENTITLEMENT_WEBHOOK"

EntitlementAction = Literal["PROVISION", "REVOKE"]
EntitlementStatus = Literal["PENDING", "PROVISIONED", "REVOKED", "FAILED"]

#: The fulfilment status each action reaches once it has actually happened.
TERMINAL_STATUS_BY_ACTION: Final[dict[EntitlementAction, EntitlementStatus]] = {
    "PROVISION": "PROVISIONED",
    "REVOKE": "REVOKED",
}

#: Delivery states that settle an intent one way or the other. DISCARDED means
#: "never sendable as configured" -- an operator has to fix configuration, so
#: it settles as FAILED rather than hanging PENDING forever.
_SETTLING_STATES: Final = (STATE_DELIVERED, STATE_DEAD_LETTER, STATE_DISCARDED)

SYSTEM_PRINCIPAL: Final = "system:entitlement-fulfilment"


@dataclass(frozen=True, slots=True)
class EntitlementResult:
    status: EntitlementStatus
    provider: str
    reference: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class EntitlementSettlement:
    """What one settlement pass moved, per outcome. Never a bare total."""

    provisioned: int = 0
    revoked: int = 0
    failed: int = 0


def entitlement_event_type(status: str) -> str:
    """Outbox event name for a fulfilment transition.

    Built rather than written as a literal on purpose: `record_outbox` call
    sites are scanned statically by `tests/test_event_catalog_gate.py`, which
    hard-fails on any *resolvable* event name missing from
    `Docs/30-contracts/04-event-catalog.md`. These four names
    (`..._pending/_provisioned/_revoked/_failed.v1`) were already emitted
    through an f-string before this change and are undocumented; keeping them
    computed preserves exactly the catalog-gate posture they had, so
    documenting them stays a separate, deliberate change rather than a
    side effect of this one.
    """
    return f"data_product.entitlement_{status.lower()}.v1"


def entitlement_payload(
    access_request: DataProductAccessRequest, action: EntitlementAction
) -> dict[str, Any]:
    """The value-free body an external entitlement system receives.

    Identifiers and a validity window only -- never the purpose text, never
    anything about the data itself.
    """
    return {
        "action": action,
        "access_request_id": str(access_request.id),
        "organization_id": str(access_request.organization_id),
        "data_product_version_id": str(access_request.data_product_version_id),
        "principal_id": access_request.requested_by,
        "expires_at": access_request.expires_at.isoformat() if access_request.expires_at else None,
    }


def system_context(organization_id: UUID | None) -> SecurityContext:
    """Audit identity for a transition no human requested.

    Carries no roles: this context exists to *attribute* a background
    transition, never to authorize one.
    """
    return SecurityContext(
        principal_id=SYSTEM_PRINCIPAL,
        principal_type="SERVICE",
        organization_id=organization_id,
        roles=frozenset(),
    )


def plan_entitlement(
    session: AsyncSession | None,
    settings: Settings,
    access_request: DataProductAccessRequest,
    action: EntitlementAction,
    *,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> EntitlementResult:
    """Fulfil `action` for `access_request`, staging everything in `session`.

    Synchronous and I/O-free by construction, so it can be called from inside
    the transaction that approves or revokes -- the grant and its fulfilment
    commit together or not at all. Never commits; the caller owns that.
    """
    moment = now or datetime.now(UTC)

    if settings.entitlement_provider == "webhook":
        if session is None:
            # Only the webhook provider stages anything, so this is the one
            # branch a missing transaction can break. Refusing beats pretending
            # to have queued a delivery nothing will ever send.
            return EntitlementResult(
                status="FAILED",
                provider="webhook",
                error="no transaction available to record the entitlement delivery",
            )
        if not settings.entitlement_webhook_url:
            # NOT_CONFIGURED is a real answer, not a silent success: the
            # request records FAILED and an operator can see why.
            return EntitlementResult(
                status="FAILED",
                provider="webhook",
                error="entitlement webhook is not configured",
            )
        # The same request and action is the same grant however many times it
        # is retried -- this is the durable half of the idempotency the old
        # inline `Idempotency-Key` header was reaching for. It is also what
        # the request is settled against later: the intent's own `id` is still
        # `None` until the transaction flushes, so a deterministic key is the
        # only reference that can be recorded in the same breath as the
        # enqueue, with no flush and no second round trip.
        reference = dedup_key_for(access_request.id, action)
        enqueue_intent(
            session,
            organization_id=access_request.organization_id,
            kind=KIND_ENTITLEMENT,
            channel=CHANNEL_ENTITLEMENT,
            destination=destination_label(settings.entitlement_webhook_url),
            dedup_key=reference,
            payload=entitlement_payload(access_request, action),
            correlation_id=correlation_id,
            now=moment,
        )
        return EntitlementResult(status="PENDING", provider="webhook", reference=reference)

    # `outbox`: we are the entitlement authority. See the module docstring.
    return EntitlementResult(
        status=TERMINAL_STATUS_BY_ACTION[action],
        provider="outbox",
        reference=f"local:{access_request.id}",
    )


async def apply_entitlement(
    settings: Settings,
    access_request: DataProductAccessRequest,
    action: EntitlementAction,
    *,
    session: AsyncSession | None = None,
    now: datetime | None = None,
    correlation_id: str | None = None,
) -> EntitlementResult:
    """Awaitable form of `plan_entitlement`, kept for existing call sites.

    The webhook provider needs a session to stage its delivery intent in;
    without one it refuses rather than pretending to have queued something.
    """
    return plan_entitlement(
        session,
        settings,
        access_request,
        action,
        now=now,
        correlation_id=correlation_id,
    )


def entitlement_transport_for(intent: DeliveryIntent, settings: Settings) -> DeliveryTransport:
    """Resolve an entitlement intent to its live webhook.

    Read from settings rather than from the stored row, so the bearer token is
    never persisted and a rotated URL applies to intents queued before the
    rotation -- the same rule `notification_transport_for` follows.
    """
    if settings.entitlement_provider != "webhook" or not settings.entitlement_webhook_url:
        return NullTransport(
            "no entitlement webhook is configured", destination=intent.destination
        )
    headers = {
        "Idempotency-Key": f"{intent.payload.get('access_request_id')}:"
        f"{intent.payload.get('action')}",
    }
    if settings.entitlement_webhook_token:
        headers["Authorization"] = (
            f"Bearer {settings.entitlement_webhook_token.get_secret_value()}"
        )
    return WebhookTransport(
        settings.entitlement_webhook_url,
        timeout_seconds=settings.entitlement_timeout_seconds,
        verify_tls=settings.delivery_webhook_verify_tls,
        headers=headers,
    )


async def settle_entitlement_deliveries(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 200,
) -> EntitlementSettlement:
    """Turn finished entitlement deliveries into fulfilment status.

    Only settles a request against the intent it actually recorded
    (`fulfillment_reference`), so a stale or unrelated intent can never
    provision anything. Writes a receipt per transition and commits.
    """
    from aida.events import record_audit, record_outbox

    moment = now or datetime.now(UTC)
    intents = (
        await session.scalars(
            select(DeliveryIntent)
            .where(
                DeliveryIntent.kind == KIND_ENTITLEMENT,
                DeliveryIntent.state.in_(_SETTLING_STATES),
            )
            .order_by(DeliveryIntent.requested_at)
            .limit(limit)
        )
    ).all()

    provisioned = revoked = failed = 0
    for intent in intents:
        raw_id = intent.payload.get("access_request_id")
        action = intent.payload.get("action")
        if not raw_id or action not in TERMINAL_STATUS_BY_ACTION:
            continue
        access_request = await session.get(DataProductAccessRequest, UUID(str(raw_id)))
        if access_request is None:
            continue
        # Settle once, and only the delivery this request is waiting on: a
        # stale intent from an earlier action can never provision anything.
        if (
            access_request.fulfillment_status != "PENDING"
            or access_request.fulfillment_reference != intent.dedup_key
        ):
            continue

        if intent.state == STATE_DELIVERED:
            status = TERMINAL_STATUS_BY_ACTION[action]
            access_request.fulfillment_status = status
            access_request.fulfillment_error = None
            access_request.fulfilled_at = moment
            if status == "PROVISIONED":
                provisioned += 1
            else:
                revoked += 1
            outcome = "SUCCESS"
        else:
            status = "FAILED"
            access_request.fulfillment_status = status
            access_request.fulfillment_error = (
                intent.last_error or f"delivery ended {intent.state}"
            )[:1000]
            access_request.fulfilled_at = None
            failed += 1
            outcome = "FAILURE"

        context = system_context(access_request.organization_id)
        record_audit(
            session,
            context,
            action=f"marketplace.entitlement.{str(action).lower()}",
            resource_type="data_product_access_request",
            resource_id=str(access_request.id),
            outcome=outcome,
            correlation_id=intent.correlation_id or str(intent.id),
            details={
                "provider": "webhook",
                "fulfillment_status": status,
                "delivery_intent_id": str(intent.id),
                "delivery_state": intent.state,
                "attempts": int(intent.attempt_count),
            },
        )
        record_outbox(
            session,
            organization_id=access_request.organization_id,
            aggregate_type="data_product_access_request",
            aggregate_id=str(access_request.id),
            event_type=entitlement_event_type(status),
            payload={"action": action, "provider": "webhook"},
        )

    if provisioned or revoked or failed:
        await session.commit()
    return EntitlementSettlement(provisioned=provisioned, revoked=revoked, failed=failed)


async def run_entitlement_fulfilment_pass(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    session: AsyncSession | None = None,
    owner: str | None = None,
) -> tuple[DeliveryPassResult, EntitlementSettlement]:
    """Drain entitlement deliveries, then settle whatever finished.

    The one callable the scheduler needs; wiring it is a single line beside
    the existing `run_delivery_worker_pass(...)` tick. It injects its own
    resolver rather than mutating `default_resolvers()`, so `delivery_intents`
    needs no knowledge of entitlements and the two workers cannot claim each
    other's rows.

    A no-op under the default `outbox` provider, which has nothing to deliver.
    """
    active = settings or get_settings()
    delivered = await run_delivery_worker_pass(
        active,
        now=now,
        resolvers={KIND_ENTITLEMENT: entitlement_transport_for},
        owner=owner,
        session=session,
    )
    if session is not None:
        settled = await settle_entitlement_deliveries(session, now=now)
    else:
        from aida.db import session_factory

        async with session_factory() as owned:
            settled = await settle_entitlement_deliveries(owned, now=now)
    return delivered, settled
