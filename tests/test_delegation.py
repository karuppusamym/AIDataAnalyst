"""PG-4: delegation and reassignment of governance authority.

Proves both halves of PG-4's exit condition ("time-bounded, audited")
against a real (in-memory sqlite) database, mirroring the existing
governance-review test harnesses (`tests/test_bulk_governance_decisions.py`,
PG-3) rather than a hand-simulated session:

  * time-bounded -- a delegate is denied governance-review decision
    authority before any delegation exists, permitted while an active
    delegation's window covers "now", and denied again once that window has
    elapsed. This exercises the real enforcement point,
    `aida.security.require_roles_or_delegated` -- the same dependency
    `semantic_api.decide_governance_review` /
    `bulk_decide_governance_reviews` depend on -- not a mock of it, proving
    the delegation actually widens who is *permitted* there rather than
    sitting inert next to the check.
  * audited -- granting and revoking a delegation each write a real
    `AuditEvent` + `OutboxEvent` (`delegation.grant`/`delegation.granted`,
    `delegation.revoke`/`delegation.revoked`); *exercising* a delegation to
    decide a real governance review is audited too, at the point of use,
    with `via_delegation_id`/`via_delegator_principal_id` recorded on that
    decision's own `AuditEvent`.

Also covers the two guardrails delegation must not defeat: a principal can
only delegate roles it actually holds itself, and a delegate acting under a
delegated role still cannot approve something the *delegator* itself
proposed (self-approval by proxy).
"""

import itertools
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.db import Base
from aida.delegation_api import DelegationCreate, grant_delegation, revoke_delegation
from aida.models import (
    AuditEvent,
    Delegation,
    GlossaryTerm,
    GovernanceReview,
    Organization,
    OutboxEvent,
    TermSemanticBinding,
)
from aida.schemas import GovernanceDecisionRequest
from aida.security import require_roles_or_delegated
from aida.security_types import SecurityContext
from aida.semantic_api import decide_governance_review

pytestmark = pytest.mark.asyncio

_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as active:
        yield active
    await engine.dispose()


GOVERNANCE_ROLES = ("PlatformAdmin", "DataSteward", "Reviewer")


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _term(session: AsyncSession, org: Organization) -> GlossaryTerm:
    term = GlossaryTerm(organization_id=org.id, term_key=f"term-{uuid4().hex[:8]}")
    session.add(term)
    await session.flush()
    return term


def _context(org: Organization, principal: str, *roles: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset(roles),
    )


async def _seed_binding_and_review(
    session: AsyncSession,
    org: Organization,
    term: GlossaryTerm,
    *,
    requested_by: str,
) -> GovernanceReview:
    """The leanest governed object type the queue supports (TERM_SEMANTIC_BINDING,
    SM-2/PG-3's own choice) -- `semantic_object_id` carries no FK of its own,
    so a fresh random UUID stands in without needing a real SemanticMetric.
    """
    binding = TermSemanticBinding(
        organization_id=org.id,
        term_id=term.id,
        semantic_object_type="SEMANTIC_METRIC",
        semantic_object_id=uuid4(),
        status="PENDING_APPROVAL",
        requested_by=requested_by,
    )
    session.add(binding)
    await session.flush()
    review = GovernanceReview(
        organization_id=org.id,
        object_type="TERM_SEMANTIC_BINDING",
        object_id=str(binding.id),
        requested_action="APPROVE",
        requested_by=requested_by,
        status="PENDING",
    )
    session.add(review)
    await session.flush()
    return review


async def test_delegate_denied_then_permitted_during_window_then_denied_after_expiry(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    dependency = require_roles_or_delegated(*GOVERNANCE_ROLES)
    bob = _context(org, "bob@bank.com", "Analyst")  # holds none of GOVERNANCE_ROLES directly

    # 1. Before any delegation exists: denied.
    with pytest.raises(HTTPException) as exc_before:
        await dependency(context=bob, session=session)
    assert exc_before.value.status_code == 403

    # 2. Alice, a real DataSteward, delegates that authority to bob for a
    # bounded two-week window -- audited at grant.
    alice = _context(org, "alice@bank.com", "DataSteward")
    now = datetime.now(UTC)
    grant = await grant_delegation(
        org.id,
        DelegationCreate(
            delegate_principal_id="bob@bank.com",
            delegated_roles=["DataSteward"],
            reason="Alice is on medical leave for two weeks; Bob covers her review queue.",
            starts_at=now,
            expires_at=now + timedelta(days=14),
        ),
        context=alice,
        session=session,
    )
    assert grant.status == "ACTIVE"
    assert grant.delegator_principal_id == "alice@bank.com"
    assert grant.delegate_principal_id == "bob@bank.com"

    grant_audit_rows = (
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "delegation.grant"))
    ).all()
    assert len(grant_audit_rows) == 1
    assert grant_audit_rows[0].resource_id == str(grant.id)
    assert grant_audit_rows[0].principal_id == "alice@bank.com"

    grant_outbox_rows = (
        await session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "delegation.granted")
        )
    ).all()
    assert len(grant_outbox_rows) == 1
    assert grant_outbox_rows[0].aggregate_id == str(grant.id)

    # 3. During the active window: bob is permitted, and the widened context
    # carries which delegation authorized it.
    resolved = await dependency(context=bob, session=session)
    assert "DataSteward" in resolved.roles
    assert resolved.active_delegation_id == grant.id
    assert resolved.active_delegator_principal_id == "alice@bank.com"

    # Exercise it for real: bob decides a live governance review someone else
    # (not alice, not bob) proposed, using the widened context exactly as
    # decide_governance_review's own `Depends(require_roles_or_delegated(...))`
    # would hand it in on a real request.
    term = await _term(session, org)
    review = await _seed_binding_and_review(session, org, term, requested_by="maker@bank.com")
    decided = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=resolved,
        session=session,
    )
    assert decided.status == "APPROVED"
    assert decided.decided_by == "bob@bank.com"

    # Audited at use too, not only at grant.
    use_audit_rows = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "governance.review.decide")
        )
    ).all()
    assert len(use_audit_rows) == 1
    assert use_audit_rows[0].details["via_delegation_id"] == str(grant.id)
    assert use_audit_rows[0].details["via_delegator_principal_id"] == "alice@bank.com"

    # 4. The window elapses (same grant; wall-clock has simply moved past
    # its expires_at) -- denied again. Time-bounding is enforced at
    # evaluation time, not just recorded as inert stored data.
    grant.starts_at = now - timedelta(days=30)
    grant.expires_at = now - timedelta(days=1)
    await session.flush()
    with pytest.raises(HTTPException) as exc_after:
        await dependency(context=bob, session=session)
    assert exc_after.value.status_code == 403


async def test_revoked_delegation_is_no_longer_honored_and_is_audited(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    dependency = require_roles_or_delegated(*GOVERNANCE_ROLES)
    alice = _context(org, "alice@bank.com", "DataSteward")
    bob = _context(org, "bob@bank.com", "Analyst")
    now = datetime.now(UTC)

    grant = await grant_delegation(
        org.id,
        DelegationCreate(
            delegate_principal_id="bob@bank.com",
            delegated_roles=["DataSteward"],
            reason="Covering for a week while Alice is at a conference.",
            starts_at=now,
            expires_at=now + timedelta(days=7),
        ),
        context=alice,
        session=session,
    )
    resolved = await dependency(context=bob, session=session)
    assert resolved.active_delegation_id == grant.id

    revoked = await revoke_delegation(grant.id, context=alice, session=session)
    assert revoked.status == "REVOKED"
    assert revoked.revoked_by == "alice@bank.com"

    revoke_audit_rows = (
        await session.scalars(select(AuditEvent).where(AuditEvent.action == "delegation.revoke"))
    ).all()
    assert len(revoke_audit_rows) == 1
    revoke_outbox_rows = (
        await session.scalars(
            select(OutboxEvent).where(OutboxEvent.event_type == "delegation.revoked")
        )
    ).all()
    assert len(revoke_outbox_rows) == 1

    with pytest.raises(HTTPException) as exc_info:
        await dependency(context=bob, session=session)
    assert exc_info.value.status_code == 403


async def test_grant_rejects_a_role_the_delegator_does_not_hold(session: AsyncSession) -> None:
    org = await _org(session)
    # alice only holds "Reviewer" -- attempting to delegate "PlatformAdmin"
    # (authority she does not have) must fail.
    alice = _context(org, "alice@bank.com", "Reviewer")
    with pytest.raises(HTTPException) as exc_info:
        await grant_delegation(
            org.id,
            DelegationCreate(
                delegate_principal_id="bob@bank.com",
                delegated_roles=["PlatformAdmin"],
                reason="Trying to hand off authority alice does not hold.",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
            context=alice,
            session=session,
        )
    assert exc_info.value.status_code == 422

    rows = (await session.scalars(select(Delegation))).all()
    assert rows == []  # nothing persisted from the rejected attempt


async def test_delegate_cannot_approve_what_the_delegator_itself_proposed(
    session: AsyncSession,
) -> None:
    """A delegate acting under a delegated role must not be able to approve
    a review the *delegator* proposed -- otherwise the delegator could
    effectively self-approve through a proxy, defeating maker != checker
    (INV-8) via the delegation mechanism itself.
    """
    org = await _org(session)
    dependency = require_roles_or_delegated(*GOVERNANCE_ROLES)
    alice = _context(org, "alice@bank.com", "DataSteward")
    bob = _context(org, "bob@bank.com", "Analyst")
    now = datetime.now(UTC)

    await grant_delegation(
        org.id,
        DelegationCreate(
            delegate_principal_id="bob@bank.com",
            delegated_roles=["DataSteward"],
            reason="Alice delegates review authority to Bob for a week.",
            starts_at=now,
            expires_at=now + timedelta(days=7),
        ),
        context=alice,
        session=session,
    )
    resolved = await dependency(context=bob, session=session)

    term = await _term(session, org)
    # alice herself is the maker this time.
    review = await _seed_binding_and_review(session, org, term, requested_by="alice@bank.com")

    with pytest.raises(HTTPException) as exc_info:
        await decide_governance_review(
            review.id,
            GovernanceDecisionRequest(decision="APPROVE"),
            context=resolved,
            session=session,
        )
    assert exc_info.value.status_code == 409
