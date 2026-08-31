"""GL-7: leaver reassignment -- a departing principal's whole active
ownership portfolio (table ownership, glossary-term stewardship, any other
`OwnershipAssignment` subject_type GL-2's model covers) reassigned to a
successor in one governed action.

Follows CT-1's own real-database test shape
(`tests/test_catalog_bulk_actions_endpoints.py`) rather than a
hand-simulated session: a real in-memory sqlite engine, rows seeded through
the ORM, the actual endpoint/service functions called directly, and the
result asserted against what actually persisted -- not against a mocked
return value.

`OwnershipAssignment.subject_id` carries no FK constraint (subject_type
selects which table it logically refers to), so this file exercises the
reassignment mechanics against bare UUIDs standing in for table and term
subjects, exactly the way `tests/test_bulk_governance_decisions.py` uses a
free-standing UUID for `TermSemanticBinding.semantic_object_id`.
"""

import itertools
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.db import Base
from aida.models import (
    AuditEvent,
    GovernanceReview,
    Organization,
    OwnershipAssignment,
)
from aida.schemas import GovernanceDecisionRequest, LeaverReassignmentRequest
from aida.security_types import SecurityContext
from aida.semantic_api import decide_governance_review
from aida.stewardship_api import LEAVER_REASSIGNMENT_MAX_ITEMS, request_leaver_reassignment

pytestmark = pytest.mark.asyncio

# Same sqlite BigInteger-autoincrement workaround as
# tests/test_catalog_bulk_actions_endpoints.py and
# tests/test_bulk_governance_decisions.py.
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


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


def _context(org: Organization, principal: str, *roles: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset(roles or {"DataSteward"}),
    )


def _assignment(
    org: Organization,
    *,
    subject_type: str,
    owner_principal: str,
    status: str = "ACTIVE",
    owner_type: str = "INDIVIDUAL",
) -> OwnershipAssignment:
    return OwnershipAssignment(
        organization_id=org.id,
        subject_type=subject_type,
        subject_id=str(uuid4()),
        owner_type=owner_type,
        owner_principal=owner_principal,
        assignment_kind="MANUAL",
        status=status,
        assigned_by="admin@bank.com",
    )


async def test_whole_portfolio_across_table_and_term_reassigned_in_one_action(
    session: AsyncSession,
) -> None:
    """The core GL-7 exit condition: table ownership *and* glossary-term
    stewardship -- two different asset kinds -- both reassigned by a single
    governed action (one BulkStewardshipOperation, one GovernanceReview),
    not two separate bespoke calls.
    """
    org = await _org(session)
    table_assignments = [
        _assignment(org, subject_type="TABLE", owner_principal="alice@bank.com") for _ in range(3)
    ]
    term_assignments = [
        _assignment(org, subject_type="TERM", owner_principal="alice@bank.com") for _ in range(2)
    ]
    other_owner = _assignment(org, subject_type="TABLE", owner_principal="charlie@bank.com")
    session.add_all([*table_assignments, *term_assignments, other_owner])
    await session.flush()

    hr_context = _context(org, "hr-admin@bank.com", "DataSteward")
    operation = await request_leaver_reassignment(
        org.id,
        LeaverReassignmentRequest(
            leaving_principal="alice@bank.com",
            successor_principal="bob@bank.com",
            rationale="Alice left the organization; Bob is the successor steward.",
        ),
        context=hr_context,
        session=session,
    )

    assert operation.status == "REVIEW_REQUIRED"
    assert operation.operation_type == "REASSIGN_LEAVER"
    assert operation.subject_type == "OWNERSHIP_ASSIGNMENT"
    assert len(operation.subject_ids) == 5  # 3 TABLE + 2 TERM, never the other owner's row
    assert operation.parameters["selection_mode"] == "FILTER"
    assert operation.parameters["selection_truncated"] is False

    review = await session.get(GovernanceReview, operation.governance_review_id)
    assert review is not None
    assert review.status == "PENDING"
    assert review.object_type == "BULK_STEWARDSHIP_OPERATION"
    assert review.requested_action == "REASSIGN_LEAVER"

    # Maker != checker: a different principal decides.
    reviewer_context = _context(org, "reviewer@bank.com", "DataSteward")
    decided = await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=reviewer_context,
        session=session,
    )
    assert decided.status == "APPROVED"

    await session.refresh(operation)
    assert operation.status == "APPLIED"
    assert operation.applied_count == 5

    # Every one of alice's original rows is vacated (never deleted -- retained
    # as evidence), and bob now holds an ACTIVE row for every one of them,
    # across both subject_types.
    original_ids = {row.id for row in [*table_assignments, *term_assignments]}
    vacated = (
        await session.scalars(
            select(OwnershipAssignment).where(OwnershipAssignment.id.in_(original_ids))
        )
    ).all()
    assert len(vacated) == 5
    assert all(row.status == "REASSIGNED" for row in vacated)
    assert all(row.owner_principal == "alice@bank.com" for row in vacated)

    bob_rows = (
        await session.scalars(
            select(OwnershipAssignment).where(
                OwnershipAssignment.organization_id == org.id,
                OwnershipAssignment.owner_principal == "bob@bank.com",
            )
        )
    ).all()
    assert len(bob_rows) == 5
    assert {row.subject_type for row in bob_rows} == {"TABLE", "TERM"}
    assert all(row.status == "ACTIVE" for row in bob_rows)
    assert all(row.assignment_kind == "REASSIGNED" for row in bob_rows)
    assert all(row.assigned_by == "reviewer@bank.com" for row in bob_rows)
    bob_subject_ids = {(row.subject_type, row.subject_id) for row in bob_rows}
    original_subject_ids = {(row.subject_type, row.subject_id) for row in vacated}
    assert bob_subject_ids == original_subject_ids

    # The unrelated owner's assignment was never touched by this action.
    await session.refresh(other_owner)
    assert other_owner.status == "ACTIVE"
    assert other_owner.owner_principal == "charlie@bank.com"


async def test_explicit_assignment_ids_reassign_only_the_chosen_subset(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    keep = _assignment(org, subject_type="TABLE", owner_principal="alice@bank.com")
    reassign = _assignment(org, subject_type="TERM", owner_principal="alice@bank.com")
    session.add_all([keep, reassign])
    await session.flush()

    operation = await request_leaver_reassignment(
        org.id,
        LeaverReassignmentRequest(
            leaving_principal="alice@bank.com",
            successor_principal="bob@bank.com",
            assignment_ids=[reassign.id],
            rationale="Only the glossary term needs to move today.",
        ),
        context=_context(org, "hr-admin@bank.com"),
        session=session,
    )
    assert operation.parameters["selection_mode"] == "EXPLICIT"
    assert [str(reassign.id)] == operation.subject_ids

    review = await session.get(GovernanceReview, operation.governance_review_id)
    assert review is not None
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=_context(org, "reviewer@bank.com"),
        session=session,
    )

    await session.refresh(keep)
    await session.refresh(reassign)
    assert keep.status == "ACTIVE"
    assert keep.owner_principal == "alice@bank.com"
    assert reassign.status == "REASSIGNED"


async def test_explicit_assignment_id_not_owned_by_leaver_is_rejected(
    session: AsyncSession,
) -> None:
    org = await _org(session)
    someone_elses = _assignment(org, subject_type="TABLE", owner_principal="charlie@bank.com")
    session.add(someone_elses)
    await session.flush()

    with pytest.raises(Exception) as exc_info:
        await request_leaver_reassignment(
            org.id,
            LeaverReassignmentRequest(
                leaving_principal="alice@bank.com",
                successor_principal="bob@bank.com",
                assignment_ids=[someone_elses.id],
                rationale="Attempting to reassign a row alice does not own.",
            ),
            context=_context(org, "hr-admin@bank.com"),
            session=session,
        )
    assert getattr(exc_info.value, "status_code", None) == 409


async def test_filter_selection_caps_at_500_with_truncated_flag(session: AsyncSession) -> None:
    """Mirrors CT-1's real-scale filter-cap proof: matching more than the
    cap still processes exactly LEAVER_REASSIGNMENT_MAX_ITEMS, never more,
    and reports truncated=True rather than silently dropping the rest.
    """
    org = await _org(session)
    extra = 37
    rows = [
        _assignment(org, subject_type="TABLE", owner_principal="alice@bank.com")
        for _ in range(LEAVER_REASSIGNMENT_MAX_ITEMS + extra)
    ]
    session.add_all(rows)
    await session.flush()

    operation = await request_leaver_reassignment(
        org.id,
        LeaverReassignmentRequest(
            leaving_principal="alice@bank.com",
            successor_principal="bob@bank.com",
            rationale="Bank-scale leaver with a very large table portfolio.",
        ),
        context=_context(org, "hr-admin@bank.com"),
        session=session,
    )
    assert len(operation.subject_ids) == LEAVER_REASSIGNMENT_MAX_ITEMS
    assert operation.parameters["selection_truncated"] is True


async def test_stale_assignment_is_skipped_not_a_hard_failure(session: AsyncSession) -> None:
    """A row captured into the portfolio at request time can become stale by
    decision time (already reassigned or vacated some other way). That must
    be skipped -- reflected in a lower applied_count -- never abort the rest
    of the governed decision.
    """
    org = await _org(session)
    stays_pending = _assignment(org, subject_type="TABLE", owner_principal="alice@bank.com")
    goes_stale = _assignment(org, subject_type="TABLE", owner_principal="alice@bank.com")
    session.add_all([stays_pending, goes_stale])
    await session.flush()

    operation = await request_leaver_reassignment(
        org.id,
        LeaverReassignmentRequest(
            leaving_principal="alice@bank.com",
            successor_principal="bob@bank.com",
            rationale="Two tables, one goes stale before decision.",
        ),
        context=_context(org, "hr-admin@bank.com"),
        session=session,
    )
    assert len(operation.subject_ids) == 2

    # Simulate a concurrent, independent vacate of one row between request
    # and decision.
    goes_stale.status = "REVOKED"
    await session.flush()

    review = await session.get(GovernanceReview, operation.governance_review_id)
    assert review is not None
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=_context(org, "reviewer@bank.com"),
        session=session,
    )

    await session.refresh(operation)
    assert operation.status == "APPLIED"
    assert operation.applied_count == 1

    await session.refresh(stays_pending)
    assert stays_pending.status == "REASSIGNED"
    await session.refresh(goes_stale)
    assert goes_stale.status == "REVOKED"  # untouched by the decision, not silently reassigned

    bob_rows = (
        await session.scalars(
            select(OwnershipAssignment).where(
                OwnershipAssignment.organization_id == org.id,
                OwnershipAssignment.owner_principal == "bob@bank.com",
            )
        )
    ).all()
    assert len(bob_rows) == 1


async def test_successor_reassignment_reactivates_an_existing_inactive_row(
    session: AsyncSession,
) -> None:
    """If the successor already has a (currently inactive) row for the same
    subject -- e.g. a prior reassignment away from them and back -- the
    unique (org, subject_type, subject_id, owner_type, owner_principal)
    constraint means the reassignment must reactivate that row rather than
    insert a duplicate.
    """
    org = await _org(session)
    leaver_row = _assignment(org, subject_type="TABLE", owner_principal="alice@bank.com")
    session.add(leaver_row)
    await session.flush()
    bob_prior = OwnershipAssignment(
        organization_id=org.id,
        subject_type="TABLE",
        subject_id=leaver_row.subject_id,
        owner_type="INDIVIDUAL",
        owner_principal="bob@bank.com",
        assignment_kind="REASSIGNED",
        status="REASSIGNED",
        assigned_by="someone@bank.com",
    )
    session.add(bob_prior)
    await session.flush()

    operation = await request_leaver_reassignment(
        org.id,
        LeaverReassignmentRequest(
            leaving_principal="alice@bank.com",
            successor_principal="bob@bank.com",
            rationale="Bob previously owned this table too.",
        ),
        context=_context(org, "hr-admin@bank.com"),
        session=session,
    )
    review = await session.get(GovernanceReview, operation.governance_review_id)
    assert review is not None
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=_context(org, "reviewer@bank.com"),
        session=session,
    )

    await session.refresh(bob_prior)
    assert bob_prior.status == "ACTIVE"
    bob_rows = (
        await session.scalars(
            select(OwnershipAssignment).where(
                OwnershipAssignment.organization_id == org.id,
                OwnershipAssignment.owner_principal == "bob@bank.com",
            )
        )
    ).all()
    assert len(bob_rows) == 1  # reactivated in place, not duplicated
