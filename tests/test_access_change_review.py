"""R11-AUD02: access policies and workspace memberships go through a governance review.

INV-8 says the identity that proposes a governed change can never be the identity that
approves it, for any object type. `ACCESS_POLICY` and `WORKSPACE_MEMBERSHIP` were classed
risk tier T3 and had no review adapter, so `create_access_policy` and `add_member` did the
whole change in one call by one principal (`Docs/60-delivery/03-tracker.md`, R11-AUD02).

Every test here drives the real handlers -- `create_access_policy` and `add_member` for the
proposal, `decide_governance_review` for the decision -- against a real SQLite schema, and
reads the outcome back out of the database rather than out of the objects the handlers
returned. Two assertions carry the point of the change and are worth naming:

* **A proposal enforces nothing.** `load_policies` (the policy engine's only loader) does not
  return a draft or a rejected policy, and `membership_roles` / `authorize` do not see a
  pending or rejected member.
* **The gate is the real one.** The proposer is refused by `check_decision_permitted`
  (409), a non-human decider by the reviewer-agent oversight guard (403, whatever the
  configured ceiling), and only then does a different human holding a role the decide route
  admits move the object.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.reviewer_agent as reviewer_agent  # noqa: F401  -- registers the agent oversight guard
from aida.access_change_review import (
    ACCESS_POLICY_REVIEW_TYPE,
    WORKSPACE_MEMBERSHIP_REVIEW_TYPE,
)
from aida.business_graph import load_policies
from aida.db import Base
from aida.governance_decision_service import registered_object_types
from aida.models import (
    AccessPolicy,
    AuditEvent,
    GovernanceReview,
    Organization,
    OutboxEvent,
    WorkspaceMembership,
)
from aida.review_risk_tiers import TIER_T3, agent_decidable_object_types, risk_tier_for
from aida.schemas import (
    AccessPolicyCreate,
    AccessPolicyProposalRead,
    GovernanceDecisionRequest,
    GovernanceReviewBulkDecisionRequest,
    WorkspaceMembershipCreate,
    WorkspaceMembershipProposalRead,
)
from aida.security_types import SecurityContext
from aida.semantic_api import bulk_decide_governance_reviews, decide_governance_review
from aida.workspace_api import add_member, create_access_policy, list_access_policies, list_members
from aida.workspace_service import authorize, create_workspace, membership_roles

# `AuditEvent.id` is a BigInteger autoincrement primary key that relies on PostgreSQL identity
# generation; SQLite only auto-populates a bare INTEGER PRIMARY KEY. Same workaround as
# `test_governance_decision_concurrency.py`.
_audit_event_ids = itertools.count(10_000)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


# --- fixtures ----------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


@dataclass(frozen=True)
class _Tenant:
    """An organization's id, and nothing a rollback can expire.

    A test that provokes a refusal has to `rollback()` (the handler leaves the failed claim
    for the request's session teardown to discard), and a rollback expires every instance in
    the session -- an `Organization` included, whose `.id` cannot then be read outside an
    await. The contexts these tests build only ever need the id.
    """

    id: UUID


async def _org(session: AsyncSession) -> _Tenant:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return _Tenant(org.id)


def _ctx(
    org: _Tenant, principal: str, *roles: str, principal_type: str = "USER"
) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type=principal_type,
        organization_id=org.id,
        roles=frozenset(roles),
    )


def _admin(org: _Tenant, principal: str = "alice") -> SecurityContext:
    return _ctx(org, principal, "OrganizationAdmin")


def _reviewer(org: _Tenant, principal: str = "bob") -> SecurityContext:
    return _ctx(org, principal, "Reviewer")


def _policy_body(code: str = "mask-pii", **overrides: object) -> AccessPolicyCreate:
    fields: dict[str, object] = {"code": code, "name": "Mask PII", "effect": "MASK"}
    fields.update(overrides)
    return AccessPolicyCreate.model_validate(fields)


async def _propose_policy(
    session: AsyncSession, org: _Tenant, *, maker: str = "alice", code: str = "mask-pii"
) -> AccessPolicyProposalRead:
    return await create_access_policy(
        org.id,
        _policy_body(code),
        context=_admin(org, maker),
        session=session,
        correlation_id="corr-policy",
    )


async def _workspace_id(session: AsyncSession, org: _Tenant) -> UUID:
    workspace = await create_workspace(
        session,
        organization_id=org.id,
        name="Risk",
        slug=f"risk-{uuid4().hex[:6]}",
        purpose="p",
        owner_principal="founder",
    )
    return workspace.id


async def _propose_member(
    session: AsyncSession,
    org: _Tenant,
    workspace_id: UUID,
    *,
    member: str = "carol",
    role: str = "workspace_owner",
    maker: str = "alice",
    expires_at: datetime | None = None,
) -> WorkspaceMembershipProposalRead:
    return await add_member(
        workspace_id,
        WorkspaceMembershipCreate.model_validate(
            {"principal_id": member, "role": role, "expires_at": expires_at}
        ),
        context=_admin(org, maker),
        session=session,
        correlation_id="corr-member",
    )


async def _decide(
    session: AsyncSession,
    review_id: UUID,
    context: SecurityContext,
    decision: str = "APPROVE",
) -> GovernanceReview:
    return await decide_governance_review(
        review_id,
        GovernanceDecisionRequest(decision=decision, reason="checked"),  # type: ignore[arg-type]
        context=context,
        session=session,
    )


async def _fresh[T](session: AsyncSession, model: type[T], identity: UUID) -> T:
    """The row as the database has it now, not as this session last saw it.

    `populate_existing` rather than `session.expire_all()`, which would expire every instance
    and leave any later attribute read outside an await a `MissingGreenlet` in async
    SQLAlchemy.
    """
    row = await session.get(model, identity, populate_existing=True)
    assert row is not None
    return row


async def _review(session: AsyncSession, review_id: UUID) -> GovernanceReview:
    return await _fresh(session, GovernanceReview, review_id)


async def _audits(session: AsyncSession, action: str) -> list[AuditEvent]:
    return list(await session.scalars(select(AuditEvent).where(AuditEvent.action == action)))


async def _events(session: AsyncSession, event_type: str) -> list[OutboxEvent]:
    return list(
        await session.scalars(select(OutboxEvent).where(OutboxEvent.event_type == event_type))
    )


# --- the registry and the tier -----------------------------------------------


def test_both_types_are_decidable_t3_and_no_agent_may_decide_them() -> None:
    """The tier already said T3 and the registry now backs it: a type that is classified but
    has no adapter is a type nobody can decide, and `decided_elsewhere` in
    `test_governance_decision_concurrency` no longer lists either."""
    for object_type in (ACCESS_POLICY_REVIEW_TYPE, WORKSPACE_MEMBERSHIP_REVIEW_TYPE):
        assert object_type in registered_object_types()
        assert risk_tier_for(object_type) == TIER_T3
        assert object_type not in agent_decidable_object_types()
        assert object_type not in agent_decidable_object_types(TIER_T3)


# --- access policy: the proposal ---------------------------------------------


async def test_creating_a_policy_files_a_draft_and_opens_a_review(session: AsyncSession) -> None:
    org = await _org(session)

    proposal = await _propose_policy(session, org, maker="alice")

    assert isinstance(proposal, AccessPolicyProposalRead)
    assert proposal.status == "DRAFT"
    assert proposal.version == 1
    assert proposal.created_by == "alice"

    stored = await session.get(AccessPolicy, proposal.id)
    assert stored is not None and stored.status == "DRAFT"
    review = await _review(session, proposal.governance_review_id)
    assert (review.object_type, review.object_id, review.status) == (
        "ACCESS_POLICY",
        str(proposal.id),
        "PENDING",
    )
    assert (review.requested_action, review.requested_by) == ("ACTIVATE", "alice")

    created = await _audits(session, "ACCESS_POLICY_CREATED")
    assert [(row.principal_id, row.resource_id) for row in created] == [("alice", "mask-pii")]
    assert created[0].details["status"] == "DRAFT"
    assert created[0].details["governance_review_id"] == str(review.id)
    requested = await _events(session, "governance.review_requested.v1")
    assert [row.aggregate_id for row in requested] == [str(review.id)]

    # A reviewer needs to be able to see what they are about to activate.
    listed = await list_access_policies(
        org.id, context=_reviewer(org), session=session
    )
    assert [(item.code, item.status) for item in listed.items] == [("mask-pii", "DRAFT")]


async def test_a_request_for_status_active_is_refused_and_creates_nothing(
    session: AsyncSession,
) -> None:
    """422, not a quiet downgrade to DRAFT: a caller who believes a DENY policy is in force
    when it is not has been told something false."""
    org = await _org(session)

    with pytest.raises(HTTPException) as refused:
        await create_access_policy(
            org.id,
            _policy_body("deny-all", effect="DENY", status="ACTIVE"),
            context=_admin(org),
            session=session,
            correlation_id="corr",
        )

    assert refused.value.status_code == 422
    assert "cannot be created ACTIVE" in str(refused.value.detail)
    assert await session.scalar(select(func.count()).select_from(AccessPolicy)) == 0
    assert await session.scalar(select(func.count()).select_from(GovernanceReview)) == 0


async def test_a_second_create_under_a_code_is_the_next_version_with_its_own_review(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    first = await _propose_policy(session, org)
    second = await _propose_policy(session, org)

    assert (first.version, second.version) == (1, 2)
    assert first.governance_review_id != second.governance_review_id


async def test_two_creates_that_race_to_the_same_version_are_a_409_not_a_500(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both requests computed version 1 before either committed. The loser must be told, and
    must leave neither a second policy nor a review that points at one."""
    org = await _org(session)
    await _propose_policy(session, org)

    async def read_before_the_other_commit(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(session, "scalar", read_before_the_other_commit)
    with pytest.raises(HTTPException) as clash:
        await _propose_policy(session, org, maker="dave")
    monkeypatch.undo()

    assert (clash.value.status_code, clash.value.detail) == (
        409,
        "an access policy with this code and version already exists",
    )
    assert await session.scalar(select(func.count()).select_from(AccessPolicy)) == 1
    assert await session.scalar(select(func.count()).select_from(GovernanceReview)) == 1


async def test_the_evaluator_never_loads_a_draft_or_a_rejected_policy(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    proposal = await _propose_policy(session, org)
    assert await load_policies(session, org.id) == ()

    await _decide(session, proposal.governance_review_id, _reviewer(org), "REJECT")
    stored = await session.get(AccessPolicy, proposal.id)
    assert stored is not None and stored.status == "REJECTED"
    assert await load_policies(session, org.id) == ()


# --- access policy: the decision ---------------------------------------------


async def test_the_proposer_cannot_approve_their_own_policy(session: AsyncSession) -> None:
    org = await _org(session)
    proposal = await _propose_policy(session, org, maker="alice")

    # Even holding every role the decide route admits: maker != checker is by principal id.
    with pytest.raises(HTTPException) as refused:
        await _decide(session, proposal.governance_review_id, _ctx(org, "alice", "PlatformAdmin"))

    assert refused.value.status_code == 409
    assert "maker-checker" in str(refused.value.detail)
    await session.rollback()
    stored = await session.get(AccessPolicy, proposal.id)
    assert stored is not None and stored.status == "DRAFT"
    assert (await _review(session, proposal.governance_review_id)).status == "PENDING"


async def test_a_different_reviewer_approves_and_the_policy_becomes_active(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    proposal = await _propose_policy(session, org, maker="alice")

    decided = await _decide(session, proposal.governance_review_id, _reviewer(org, "bob"))

    assert (decided.status, decided.decided_by) == ("APPROVED", "bob")
    stored = await _fresh(session, AccessPolicy, proposal.id)
    assert stored.status == "ACTIVE"
    assert [policy.code for policy in await load_policies(session, org.id)] == ["mask-pii"]

    # The ledger names both people: the proposer when it was proposed, the decider twice --
    # once as the generic review decision and once against the policy itself.
    assert [row.principal_id for row in await _audits(session, "ACCESS_POLICY_CREATED")] == [
        "alice"
    ]
    activated = await _audits(session, "ACCESS_POLICY_ACTIVATED")
    assert [(row.principal_id, row.resource_id) for row in activated] == [("bob", "mask-pii")]
    assert activated[0].details["proposed_by"] == "alice"
    decisions = await _audits(session, "governance.review.decide")
    assert [(row.principal_id, row.resource_id) for row in decisions] == [
        ("bob", str(proposal.governance_review_id))
    ]
    assert [row.aggregate_id for row in await _events(session, "access_policy.activated.v1")] == [
        str(proposal.id)
    ]


async def test_a_rejection_retires_the_proposal_and_activates_nothing(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    proposal = await _propose_policy(session, org)

    decided = await _decide(session, proposal.governance_review_id, _reviewer(org), "REJECT")

    assert decided.status == "REJECTED"
    stored = await _fresh(session, AccessPolicy, proposal.id)
    assert stored.status == "REJECTED"
    assert await load_policies(session, org.id) == ()
    assert len(await _audits(session, "ACCESS_POLICY_REJECTED")) == 1
    assert len(await _events(session, "access_policy.rejected.v1")) == 1
    assert await _audits(session, "ACCESS_POLICY_ACTIVATED") == []


async def test_a_non_human_decider_is_refused_for_the_t3_policy_type(
    session: AsyncSession,
) -> None:
    """ADR-0027: T3 is never agent-decidable, whatever an identity provider says the
    principal is called. Keyed on the authenticated principal type, not on a role."""
    org = await _org(session)
    proposal = await _propose_policy(session, org)

    agent = _ctx(org, "reviewer-bot", "Reviewer", principal_type="AGENT")
    with pytest.raises(HTTPException) as refused:
        await _decide(session, proposal.governance_review_id, agent)

    assert refused.value.status_code == 403
    await session.rollback()
    stored = await session.get(AccessPolicy, proposal.id)
    assert stored is not None and stored.status == "DRAFT"
    assert (await _review(session, proposal.governance_review_id)).status == "PENDING"


async def test_a_policy_that_is_no_longer_a_draft_cannot_be_approved(
    session: AsyncSession,
) -> None:
    """The adapter's own precondition: it refuses, the claim unwinds, and the review stays
    pending rather than spending its one decision."""
    org = await _org(session)
    proposal = await _propose_policy(session, org)
    stored = await session.get(AccessPolicy, proposal.id)
    assert stored is not None
    stored.status = "ACTIVE"
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await _decide(session, proposal.governance_review_id, _reviewer(org))

    assert refused.value.status_code == 409
    assert refused.value.detail == "access policy is no longer a draft"
    await session.rollback()
    assert (await _review(session, proposal.governance_review_id)).status == "PENDING"


# --- workspace membership: the proposal --------------------------------------


@pytest.mark.parametrize("role", ["workspace_owner", "reviewer", "auditor", "analyst", "viewer"])
async def test_adding_a_member_in_any_role_is_a_pending_proposal(
    session: AsyncSession, role: str
) -> None:
    """Every role, not only `workspace_owner`: the tier is on the type, and `analyst` already
    carries READ_DATA. Nothing takes effect until the review is approved."""
    org = await _org(session)
    workspace_id = await _workspace_id(session, org)

    proposal = await _propose_member(session, org, workspace_id, role=role)

    assert isinstance(proposal, WorkspaceMembershipProposalRead)
    assert (proposal.status, proposal.role, proposal.granted_by) == (
        "PENDING_APPROVAL",
        role,
        "alice",
    )
    review = await _review(session, proposal.governance_review_id)
    assert (review.object_type, review.object_id, review.status) == (
        "WORKSPACE_MEMBERSHIP",
        str(proposal.id),
        "PENDING",
    )
    assert (review.requested_action, review.requested_by) == ("GRANT", "alice")
    proposed = await _audits(session, "WORKSPACE_MEMBER_PROPOSED")
    assert [(row.principal_id, row.resource_id) for row in proposed] == [
        ("alice", str(workspace_id))
    ]
    assert proposed[0].details["member_principal_id"] == "carol"
    assert await _audits(session, "WORKSPACE_MEMBER_ADDED") == []

    # Grants nothing: no live role, and the authorization gate cannot find a membership.
    assert await membership_roles(session, workspace_id, "carol") == frozenset()
    result = await authorize(
        session,
        _ctx(org, "carol", "Analyst"),
        workspace_id=workspace_id,
        action="READ_METADATA",
        resource_type="TABLE",
    )
    assert (result.allowed, result.reason_code) == (False, "NO_WORKSPACE_MEMBERSHIP")
    # ...but a member listing shows the proposal honestly, as pending.
    listed = await list_members(workspace_id, context=_reviewer(org), session=session)
    assert {(item.principal_id, item.status) for item in listed.items} == {
        ("founder", "ACTIVE"),
        ("carol", "PENDING_APPROVAL"),
    }


async def test_a_member_is_proposed_once_while_a_proposal_or_membership_exists(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    workspace_id = await _workspace_id(session, org)
    proposal = await _propose_member(session, org, workspace_id)

    with pytest.raises(HTTPException) as pending:
        await _propose_member(session, org, workspace_id, role="viewer")
    assert pending.value.status_code == 409
    assert pending.value.detail == "principal already has a membership awaiting approval"

    await _decide(session, proposal.governance_review_id, _reviewer(org))
    with pytest.raises(HTTPException) as active:
        await _propose_member(session, org, workspace_id)
    assert (active.value.status_code, active.value.detail) == (
        409,
        "principal already has a membership",
    )
    assert await session.scalar(select(func.count()).select_from(GovernanceReview)) == 1


async def test_two_proposals_that_race_for_one_principal_are_a_409_not_a_500(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = await _org(session)
    workspace_id = await _workspace_id(session, org)
    await _propose_member(session, org, workspace_id)

    async def read_before_the_other_commit(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(session, "scalar", read_before_the_other_commit)
    with pytest.raises(HTTPException) as clash:
        await _propose_member(session, org, workspace_id, role="viewer", maker="dave")
    monkeypatch.undo()

    assert (clash.value.status_code, clash.value.detail) == (
        409,
        "principal already has a membership",
    )
    assert await session.scalar(select(func.count()).select_from(GovernanceReview)) == 1


# --- workspace membership: the decision --------------------------------------


async def test_the_proposer_cannot_approve_their_own_membership_proposal(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    workspace_id = await _workspace_id(session, org)
    proposal = await _propose_member(session, org, workspace_id, maker="alice")

    with pytest.raises(HTTPException) as refused:
        await _decide(
            session, proposal.governance_review_id, _ctx(org, "alice", "PlatformAdmin")
        )

    assert refused.value.status_code == 409
    assert "maker-checker" in str(refused.value.detail)
    await session.rollback()
    assert await membership_roles(session, workspace_id, "carol") == frozenset()


async def test_a_different_reviewer_approves_and_the_member_takes_effect(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    workspace_id = await _workspace_id(session, org)
    session.add(
        AccessPolicy(
            organization_id=org.id,
            code="rbac-parity",
            name="RBAC parity",
            effect="ALLOW",
            subject_match={"roles": ["workspace_owner"]},
            action_match=[],
            created_by="test",
        )
    )
    await session.flush()
    proposal = await _propose_member(session, org, workspace_id, maker="alice")

    decided = await _decide(session, proposal.governance_review_id, _reviewer(org, "bob"))

    assert (decided.status, decided.decided_by) == ("APPROVED", "bob")
    stored = await _fresh(session, WorkspaceMembership, proposal.id)
    assert (stored.status, stored.role) == ("ACTIVE", "workspace_owner")
    # `granted_by` is the last principal to act on the grant: the approver, once it is active.
    assert stored.granted_by == "bob"
    assert await membership_roles(session, workspace_id, "carol") == frozenset(
        {"workspace_owner"}
    )
    result = await authorize(
        session,
        _ctx(org, "carol", "Analyst"),
        workspace_id=workspace_id,
        action="APPROVE",
        resource_type="TABLE",
    )
    assert result.allowed is True

    assert [row.principal_id for row in await _audits(session, "WORKSPACE_MEMBER_PROPOSED")] == [
        "alice"
    ]
    added = await _audits(session, "WORKSPACE_MEMBER_ADDED")
    assert [(row.principal_id, row.resource_id) for row in added] == [
        ("bob", str(workspace_id))
    ]
    assert (added[0].details["proposed_by"], added[0].details["member_principal_id"]) == (
        "alice",
        "carol",
    )
    assert [
        (row.principal_id, row.resource_id)
        for row in await _audits(session, "governance.review.decide")
    ] == [("bob", str(proposal.governance_review_id))]
    assert [
        row.aggregate_id for row in await _events(session, "workspace_membership.approved.v1")
    ] == [str(proposal.id)]


async def test_the_beneficiary_cannot_approve_their_own_membership(
    session: AsyncSession,
) -> None:
    """`check_decision_permitted` compares the decider only with the proposer, so a member
    proposed by someone else could otherwise sign off on their own access."""
    org = await _org(session)
    workspace_id = await _workspace_id(session, org)
    proposal = await _propose_member(session, org, workspace_id, member="bob", maker="alice")

    with pytest.raises(HTTPException) as refused:
        await _decide(session, proposal.governance_review_id, _reviewer(org, "bob"))

    assert refused.value.status_code == 409
    assert refused.value.detail == "a principal cannot approve their own workspace membership"
    await session.rollback()
    assert await membership_roles(session, workspace_id, "bob") == frozenset()
    assert (await _review(session, proposal.governance_review_id)).status == "PENDING"

    # Rejecting your own grant is not a way round anything, so it is allowed.
    rejected = await _decide(
        session, proposal.governance_review_id, _reviewer(org, "bob"), "REJECT"
    )
    assert rejected.status == "REJECTED"


async def test_a_rejected_member_holds_nothing_and_can_be_proposed_again(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    workspace_id = await _workspace_id(session, org)
    first = await _propose_member(session, org, workspace_id, role="workspace_owner")

    await _decide(session, first.governance_review_id, _reviewer(org), "REJECT")

    stored = await _fresh(session, WorkspaceMembership, first.id)
    assert stored.status == "REJECTED"
    assert await membership_roles(session, workspace_id, "carol") == frozenset()
    assert len(await _audits(session, "WORKSPACE_MEMBER_REJECTED")) == 1

    # `(workspace, principal)` is unique, so a rejection must not be permanent: the rejected
    # row is reused for the new proposal, under a new review.
    again = await _propose_member(
        session, org, workspace_id, role="analyst", maker="dave"
    )
    assert again.id == first.id
    assert (again.status, again.role, again.granted_by) == ("PENDING_APPROVAL", "analyst", "dave")
    assert again.governance_review_id != first.governance_review_id
    assert (await _review(session, first.governance_review_id)).status == "REJECTED"
    assert (await _review(session, again.governance_review_id)).status == "PENDING"

    await _decide(session, again.governance_review_id, _reviewer(org))
    assert await membership_roles(session, workspace_id, "carol") == frozenset({"analyst"})


async def test_a_non_human_decider_is_refused_for_the_t3_membership_type(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    workspace_id = await _workspace_id(session, org)
    proposal = await _propose_member(session, org, workspace_id)

    agent = _ctx(org, "reviewer-bot", "Reviewer", principal_type="AGENT")
    with pytest.raises(HTTPException) as refused:
        await _decide(session, proposal.governance_review_id, agent)

    assert refused.value.status_code == 403
    await session.rollback()
    assert await membership_roles(session, workspace_id, "carol") == frozenset()
    assert (await _review(session, proposal.governance_review_id)).status == "PENDING"


async def test_a_membership_that_has_already_expired_is_not_approved_but_can_be_rejected(
    session: AsyncSession,
) -> None:
    """Approving it would record a grant that grants nothing -- `membership_roles` treats an
    expired row as absent -- in the ledger an access review reads."""
    org = await _org(session)
    workspace_id = await _workspace_id(session, org)
    proposal = await _propose_member(
        session, org, workspace_id, expires_at=datetime.now(UTC) - timedelta(days=1)
    )

    with pytest.raises(HTTPException) as refused:
        await _decide(session, proposal.governance_review_id, _reviewer(org))
    assert refused.value.status_code == 409
    assert "already expired" in str(refused.value.detail)
    await session.rollback()
    assert (await _review(session, proposal.governance_review_id)).status == "PENDING"

    rejected = await _decide(session, proposal.governance_review_id, _reviewer(org), "REJECT")
    assert rejected.status == "REJECTED"


# --- the other decision surface -------------------------------------------------


async def test_the_bulk_decision_route_applies_the_same_adapters_and_the_same_maker_check(
    session: AsyncSession,
) -> None:
    """`bulk_decide_governance_reviews` reaches the same registry, so it needs no second
    implementation of either rule -- and a batch that includes the caller's own proposal
    fails that item, not the batch."""
    org = await _org(session)
    workspace_id = await _workspace_id(session, org)
    policy = await _propose_policy(session, org, maker="alice")
    member = await _propose_member(session, org, workspace_id, maker="alice")
    own = await _propose_policy(session, org, maker="bob", code="deny-export")

    result = await bulk_decide_governance_reviews(
        GovernanceReviewBulkDecisionRequest(
            review_ids=[
                policy.governance_review_id,
                member.governance_review_id,
                own.governance_review_id,
            ],
            decision="APPROVE",
        ),
        context=_reviewer(org, "bob"),
        session=session,
    )

    outcomes = {item.review_id: item.outcome for item in result.results}
    assert outcomes == {
        str(policy.governance_review_id): "APPLIED",
        str(member.governance_review_id): "APPLIED",
        str(own.governance_review_id): "NOT_PERMITTED",
    }
    assert [item.code for item in await load_policies(session, org.id)] == ["mask-pii"]
    assert await membership_roles(session, workspace_id, "carol") == frozenset(
        {"workspace_owner"}
    )
    # Each applied item still leaves its own domain audit row, batch or not.
    assert len(await _audits(session, "ACCESS_POLICY_ACTIVATED")) == 1
    assert len(await _audits(session, "WORKSPACE_MEMBER_ADDED")) == 1
