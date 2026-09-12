"""R11-B4 -- an approved access request becomes usable access, and a revoked one stops being it.

Tracker exit condition (`Docs/60-delivery/03-tracker.md`, R11-B4): "Request
through PROVISIONED and successful query; revoke denies query; durable retries
and audit receipts."

The defect these tests pin down is not a wrong status, it is a status nothing
backed. Before this change the default provider returned ``PENDING`` forever
and emitted an event no consumer subscribed to, so approval granted nothing;
and no code path anywhere read a `DataProductAccessRequest` when deciding
whether a principal could use a product, so revocation took nothing away.
Both halves are asserted here against a real in-memory database rather than
against mocks of our own functions -- the transport is the only double, and it
implements `DeliveryTransport` exactly as a real one does.

PostgreSQL is not reachable in this sandbox, so the endpoint-level tests run
the real handler bodies against SQLite via aiosqlite, in the style of
`tests/test_marketplace_personalization.py`.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.config import Settings
from aida.db import Base
from aida.delivery_intents import (
    STATE_DEAD_LETTER,
    STATE_DELIVERED,
    STATE_RETRYING,
    TransportError,
    TransportReceipt,
    run_delivery_worker_pass,
)
from aida.entitlements import (
    KIND_ENTITLEMENT,
    plan_entitlement,
    run_entitlement_fulfilment_pass,
    settle_entitlement_deliveries,
)
from aida.models import (
    AuditEvent,
    DataProduct,
    DataProductAccessRequest,
    DataProductPort,
    DataProductVersion,
    DeliveryAttempt,
    DeliveryIntent,
    GovernanceReview,
    Organization,
)
from aida.platform_schemas import EntitlementOperation, MarketplaceAccessRequestCreate
from aida.product_marketplace_api import (
    authorize_product_consumption,
    consume_marketplace_product,
    fulfill_marketplace_entitlement,
    request_marketplace_access,
    revoke_marketplace_access,
)
from aida.security_types import SecurityContext
from aida.semantic_api import _decide_data_product_access_request

# A fixed clock: `claim_due_intents` selects on `next_attempt_at <= now`, so a
# test that enqueues at wall-clock time and claims at a hard-coded instant is a
# time bomb (the lesson `tests/test_delivery_intents.py` records).
QUEUED_AT = datetime(2026, 9, 12, 11, 0, tzinfo=UTC)
CLAIM_AT = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

CONSUMER = "analyst@tenant.example"
OWNER = "owner@tenant.example"
REVIEWER = "reviewer@tenant.example"


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


class RecordingTransport:
    """A `DeliveryTransport` that answers however the test tells it to.

    Implements the protocol rather than patching our own code: the boundary
    being faked is the destination, not the delivery machinery under test.
    """

    name = "recording"
    available = True
    destination = "recording://entitlements"

    def __init__(self, *, error: TransportError | None = None) -> None:
        self.error = error
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: Mapping[str, Any]) -> TransportReceipt:
        if self.error is not None:
            raise self.error
        self.sent.append(dict(payload))
        return TransportReceipt(
            destination=self.destination, transport=self.name, detail="accepted"
        )


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"environment": "test"}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def _webhook_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "entitlement_provider": "webhook",
        "entitlement_webhook_url": "https://entitlements.example/grants",
        "delivery_worker_enabled": True,
        "delivery_max_attempts": 3,
        "delivery_backoff_base_seconds": 2.0,
        "delivery_backoff_max_seconds": 60.0,
    }
    values.update(overrides)
    return _settings(**values)


def _resolve_to(transport: RecordingTransport):
    """`resolvers` maps a kind to a resolver, not to a transport."""
    return lambda intent, settings: transport


def _context(principal: str, *roles: str, organization_id=None) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset(roles),
    )


async def _seed(session: AsyncSession, *, consumer_roles: list[str] | None = None):
    """One organization, one published product the consumer's role does NOT allow.

    `consumer_roles` deliberately excludes `Analyst` by default: the whole
    point is a product that can only be reached through an entitlement, so a
    role-based allow cannot mask a broken one.
    """
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    product = DataProduct(
        id=uuid4(),
        organization_id=org.id,
        project_id=uuid4(),
        product_key=f"revenue-{uuid4().hex[:6]}",
        lifecycle_status="ACTIVE",
        created_by=OWNER,
    )
    version = DataProductVersion(
        id=uuid4(),
        organization_id=org.id,
        product_id=product.id,
        version=1,
        status="PUBLISHED",
        name="Finance revenue model",
        description="A published marketplace product used for R11-B4 tests.",
        domain_name="fin",
        owner_principal=OWNER,
        usage_terms="Approved analytical use only.",
        classification="INTERNAL",
        discoverable_roles=["*"],
        consumer_roles=consumer_roles if consumer_roles is not None else ["DataProductOwner"],
        fingerprint=uuid4().hex,
        created_by=OWNER,
        published_at=QUEUED_AT,
    )
    port = DataProductPort(
        id=uuid4(),
        organization_id=org.id,
        data_product_version_id=version.id,
        port_key="revenue_model",
        direction="OUTPUT",
        name="revenue_model",
        description="Semantic model for revenue.",
        asset_type="SEMANTIC_MODEL",
        asset_id=str(uuid4()),
    )
    session.add_all([org, product, version, port])
    await session.commit()
    return org, version


async def _request_access(session: AsyncSession, org, version) -> DataProductAccessRequest:
    consumer = _context(CONSUMER, "Analyst", organization_id=org.id)
    return await request_marketplace_access(
        version.id,
        MarketplaceAccessRequestCreate(purpose="quarterly close reporting", duration_days=30),
        context=consumer,
        session=session,
    )


async def _approve_through_the_review_queue(
    session: AsyncSession, org, access_request: DataProductAccessRequest, *, now: datetime
) -> None:
    """Approve exactly the way the product owner does -- the unified review queue.

    Driving `_decide_data_product_access_request` rather than calling the
    shared transition directly is deliberate: the wiring that makes approval
    fulfil lives at that call site, so a test that bypassed it would keep
    passing if the wiring were removed.
    """
    review = await session.get(GovernanceReview, access_request.governance_review_id)
    assert review is not None
    await _decide_data_product_access_request(
        session,
        review,
        decision="APPROVE",
        reason="Approved for quarterly close.",
        context=_context(REVIEWER, "DataSteward", organization_id=org.id),
        now=now,
    )
    await session.commit()


async def _audit_actions(session: AsyncSession, resource_id: str) -> list[tuple[str, str]]:
    rows = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.resource_id == resource_id).order_by(AuditEvent.id)
        )
    ).all()
    return [(row.action, row.outcome) for row in rows]


# ---------------------------------------------------------------------------
# The headline cycle: request -> approve -> PROVISIONED -> query -> revoke -> denied.
# ---------------------------------------------------------------------------


async def test_approved_request_provisions_then_consumption_succeeds_then_revoke_denies(
    session: AsyncSession,
) -> None:
    org, version = await _seed(session)
    access_request = await _request_access(session, org, version)
    assert access_request.status == "PENDING"
    assert access_request.fulfillment_status == "NOT_REQUESTED"

    consumer = _context(CONSUMER, "Analyst", organization_id=org.id)

    # Before approval the consumer is refused, and told it is still pending
    # rather than being handed a product they have no grant for.
    with pytest.raises(HTTPException) as pending_refusal:
        await consume_marketplace_product(version.id, context=consumer, session=session)
    assert pending_refusal.value.status_code == 403
    assert "awaiting a decision" in pending_refusal.value.detail

    await _approve_through_the_review_queue(session, org, access_request, now=CLAIM_AT)
    await session.refresh(access_request)

    # This is the transition that never used to happen.
    assert access_request.status == "APPROVED"
    assert access_request.fulfillment_status == "PROVISIONED"
    assert access_request.fulfillment_provider == "outbox"
    assert access_request.fulfilled_at is not None
    assert access_request.fulfillment_error is None

    consumption = await consume_marketplace_product(
        version.id, context=consumer, session=session
    )
    assert consumption.basis == "ENTITLEMENT"
    assert consumption.access_request_id == access_request.id
    assert [port.port_key for port in consumption.ports] == ["revenue_model"]

    # Revoke, as the product owner.
    owner = _context(OWNER, "DataProductOwner", organization_id=org.id)
    revoked = await revoke_marketplace_access(
        access_request.id, context=owner, session=session
    )
    assert revoked.status == "REVOKED"
    assert revoked.revoked_by == OWNER
    # De-provisioned, not quietly reset to "provisioning in progress".
    assert revoked.fulfillment_status == "REVOKED"

    with pytest.raises(HTTPException) as refusal:
        await consume_marketplace_product(version.id, context=consumer, session=session)
    assert refusal.value.status_code == 403
    # The refusal is attributable: it names who revoked it.
    assert OWNER in refusal.value.detail
    assert "revoked" in refusal.value.detail.lower()


async def test_every_transition_leaves_an_audit_receipt(session: AsyncSession) -> None:
    org, version = await _seed(session)
    access_request = await _request_access(session, org, version)
    await _approve_through_the_review_queue(session, org, access_request, now=CLAIM_AT)

    consumer = _context(CONSUMER, "Analyst", organization_id=org.id)
    await consume_marketplace_product(version.id, context=consumer, session=session)
    await revoke_marketplace_access(
        access_request.id,
        context=_context(OWNER, "DataProductOwner", organization_id=org.id),
        session=session,
    )
    with pytest.raises(HTTPException):
        await consume_marketplace_product(version.id, context=consumer, session=session)

    on_request = await _audit_actions(session, str(access_request.id))
    assert ("marketplace.access.request", "SUCCESS") in on_request
    assert ("marketplace.entitlement.provision", "SUCCESS") in on_request
    assert ("marketplace.access.revoke", "SUCCESS") in on_request
    assert ("marketplace.entitlement.revoke", "SUCCESS") in on_request

    on_version = await _audit_actions(session, str(version.id))
    # The allowed query and the refused one are both receipts, and the refusal
    # is DENIED -- which is also what routes it to the SIEM funnel.
    assert ("marketplace.product.consume", "SUCCESS") in on_version
    assert ("marketplace.product.consume", "DENIED") in on_version


async def test_role_granted_access_needs_no_entitlement_and_is_unchanged(
    session: AsyncSession,
) -> None:
    """This work adds a second way to be allowed; it must not disturb the first."""
    org, version = await _seed(session, consumer_roles=["Analyst"])
    consumer = _context(CONSUMER, "Analyst", organization_id=org.id)

    decision = await authorize_product_consumption(session, consumer, version, now=CLAIM_AT)
    assert decision.allowed is True
    assert decision.basis == "ROLE"
    assert decision.access_request_id is None

    consumption = await consume_marketplace_product(version.id, context=consumer, session=session)
    assert consumption.basis == "ROLE"


async def test_an_unrelated_principal_is_refused_another_principals_grant(
    session: AsyncSession,
) -> None:
    org, version = await _seed(session)
    access_request = await _request_access(session, org, version)
    await _approve_through_the_review_queue(session, org, access_request, now=CLAIM_AT)

    stranger = _context("someone-else@tenant.example", "Analyst", organization_id=org.id)
    with pytest.raises(HTTPException) as refusal:
        await consume_marketplace_product(version.id, context=stranger, session=session)
    assert refusal.value.status_code == 403
    assert "No access request" in refusal.value.detail


async def test_expired_entitlement_denies_even_while_the_row_says_approved(
    session: AsyncSession,
) -> None:
    org, version = await _seed(session)
    access_request = await _request_access(session, org, version)
    await _approve_through_the_review_queue(session, org, access_request, now=CLAIM_AT)
    await session.refresh(access_request)
    assert access_request.fulfillment_status == "PROVISIONED"

    consumer = _context(CONSUMER, "Analyst", organization_id=org.id)
    expires_at = access_request.expires_at.replace(tzinfo=UTC)
    later = expires_at + timedelta(seconds=1)
    decision = await authorize_product_consumption(session, consumer, version, now=later)
    assert decision.allowed is False
    assert decision.reason_code == "entitlement_expired"


# ---------------------------------------------------------------------------
# The webhook provider: durable, retryable, and never silently "processed".
# ---------------------------------------------------------------------------


async def test_webhook_provisioning_queues_a_durable_intent_and_denies_until_delivered(
    session: AsyncSession,
) -> None:
    org, version = await _seed(session)
    access_request = await _request_access(session, org, version)
    settings = _webhook_settings()

    await _approve_through_the_review_queue(session, org, access_request, now=QUEUED_AT)
    # The review path resolves settings itself, so re-run the fulfilment under
    # the webhook provider to exercise that branch on the same row.
    access_request.fulfillment_status = "PENDING"
    result = plan_entitlement(
        session, settings, access_request, "PROVISION", now=QUEUED_AT
    )
    access_request.fulfillment_status = result.status
    access_request.fulfillment_provider = result.provider
    access_request.fulfillment_reference = result.reference
    await session.commit()

    assert result.status == "PENDING"
    intent = await session.scalar(
        select(DeliveryIntent).where(DeliveryIntent.kind == KIND_ENTITLEMENT)
    )
    assert intent is not None
    assert intent.payload["action"] == "PROVISION"
    assert intent.payload["access_request_id"] == str(access_request.id)
    # Value-free: the purpose text never leaves the platform.
    assert "purpose" not in intent.payload
    # The credentialed URL is not what was stored.
    assert "entitlements.example" in intent.destination
    assert "/grants" not in intent.destination

    # Approved, queued -- and still refused, because nothing has granted it yet.
    consumer = _context(CONSUMER, "Analyst", organization_id=org.id)
    decision = await authorize_product_consumption(session, consumer, version, now=CLAIM_AT)
    assert decision.allowed is False
    assert decision.reason_code == "entitlement_not_provisioned"

    # A retryable failure leaves it visibly retrying, with an attempt row.
    failing = RecordingTransport(error=TransportError("HTTP 503", retryable=True, status_code=503))
    await run_delivery_worker_pass(
        settings,
        now=CLAIM_AT,
        resolvers={KIND_ENTITLEMENT: _resolve_to(failing)},
        session=session,
        owner="test-worker",
    )
    await session.refresh(intent)
    assert intent.state == STATE_RETRYING
    assert intent.attempt_count == 1
    attempts = (await session.scalars(select(DeliveryAttempt))).all()
    assert len(attempts) == 1

    settled = await settle_entitlement_deliveries(session, now=CLAIM_AT)
    assert settled == type(settled)()  # nothing settles while it is still retrying
    await session.refresh(access_request)
    assert access_request.fulfillment_status == "PENDING"

    # The retry succeeds; settlement turns the delivery into the grant.
    succeeding = RecordingTransport()
    await run_delivery_worker_pass(
        settings,
        now=CLAIM_AT + timedelta(hours=1),
        resolvers={KIND_ENTITLEMENT: _resolve_to(succeeding)},
        session=session,
        owner="test-worker",
    )
    await session.refresh(intent)
    assert intent.state == STATE_DELIVERED
    assert succeeding.sent and succeeding.sent[0]["action"] == "PROVISION"

    settled = await settle_entitlement_deliveries(session, now=CLAIM_AT + timedelta(hours=1))
    assert settled.provisioned == 1
    await session.refresh(access_request)
    assert access_request.fulfillment_status == "PROVISIONED"

    consumption = await consume_marketplace_product(version.id, context=consumer, session=session)
    assert consumption.basis == "ENTITLEMENT"

    # The settlement wrote its own receipt, attributed to the service.
    actions = await _audit_actions(session, str(access_request.id))
    assert ("marketplace.entitlement.provision", "SUCCESS") in actions


async def test_a_dead_lettered_delivery_settles_as_failed_and_still_denies(
    session: AsyncSession,
) -> None:
    """A delivery that ran out of retries must never read as a grant."""
    org, version = await _seed(session)
    access_request = await _request_access(session, org, version)
    settings = _webhook_settings(delivery_max_attempts=1)
    await _approve_through_the_review_queue(session, org, access_request, now=QUEUED_AT)

    access_request.fulfillment_status = "PENDING"
    result = plan_entitlement(session, settings, access_request, "PROVISION", now=QUEUED_AT)
    access_request.fulfillment_status = result.status
    access_request.fulfillment_reference = result.reference
    await session.commit()

    failing = RecordingTransport(error=TransportError("HTTP 500", retryable=True, status_code=500))
    await run_delivery_worker_pass(
        settings,
        now=CLAIM_AT,
        resolvers={KIND_ENTITLEMENT: _resolve_to(failing)},
        session=session,
        owner="test-worker",
    )
    intent = await session.scalar(
        select(DeliveryIntent).where(DeliveryIntent.kind == KIND_ENTITLEMENT)
    )
    assert intent is not None
    assert intent.state == STATE_DEAD_LETTER

    settled = await settle_entitlement_deliveries(session, now=CLAIM_AT)
    assert settled.failed == 1
    await session.refresh(access_request)
    assert access_request.fulfillment_status == "FAILED"
    assert access_request.fulfillment_error

    consumer = _context(CONSUMER, "Analyst", organization_id=org.id)
    with pytest.raises(HTTPException) as refusal:
        await consume_marketplace_product(version.id, context=consumer, session=session)
    assert refusal.value.status_code == 403

    # The failure is auditable, not just logged.
    actions = await _audit_actions(session, str(access_request.id))
    assert ("marketplace.entitlement.provision", "FAILURE") in actions


async def test_a_failed_fulfilment_is_retryable_through_the_operator_endpoint(
    session: AsyncSession,
) -> None:
    org, version = await _seed(session)
    access_request = await _request_access(session, org, version)
    await _approve_through_the_review_queue(session, org, access_request, now=CLAIM_AT)
    await session.refresh(access_request)

    # Drive it into the failed state a misconfigured webhook produces.
    access_request.fulfillment_status = "FAILED"
    access_request.fulfillment_error = "entitlement webhook is not configured"
    await session.commit()

    operator = _context("ops@tenant.example", "Operations", organization_id=org.id)
    retried = await fulfill_marketplace_entitlement(
        access_request.id,
        EntitlementOperation(action="PROVISION"),
        context=operator,
        session=session,
        settings=_settings(),
    )
    assert retried.fulfillment_status == "PROVISIONED"
    assert retried.fulfillment_error is None

    consumer = _context(CONSUMER, "Analyst", organization_id=org.id)
    consumption = await consume_marketplace_product(version.id, context=consumer, session=session)
    assert consumption.basis == "ENTITLEMENT"


async def test_the_fulfilment_pass_is_a_no_op_under_the_local_provider(
    session: AsyncSession,
) -> None:
    """The callable the scheduler would wire is safe to call unconditionally.

    Under the default `outbox` provider there is nothing to deliver, because
    provisioning already happened in the approving transaction.
    """
    org, version = await _seed(session)
    access_request = await _request_access(session, org, version)
    await _approve_through_the_review_queue(session, org, access_request, now=CLAIM_AT)

    delivered, settled = await run_entitlement_fulfilment_pass(
        _settings(delivery_worker_enabled=True), now=CLAIM_AT, session=session
    )
    assert delivered.claimed == 0
    assert settled.provisioned == 0
    await session.refresh(access_request)
    assert access_request.fulfillment_status == "PROVISIONED"
