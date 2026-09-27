"""R11-C8: a reversal is decided by someone else, and an undecided one is not done.

Two clauses the row left open, both about what happens *after* a correction is
filed.

**Maker-checker on a reversal was inherited, not tested.** A reversal is an
ordinary bulk operation behind an ordinary `GovernanceReview`, so the shared
decision guards applied to it -- but nothing proved it, and a reversal is the
one decision where self-approval is most tempting: the steward who disputes a
change is the one most sure it should be undone. These drive both doors a
decision can come through -- the single-review endpoint and the service the
batch endpoint and the reviewer agent use -- with the reversal's own requester,
and with a delegate acting for them.

**Nothing checked that a filed reversal is ever decided.** A sample resolved as
DISAGREED read as resolved the moment the verdict landed, even though the change
the human disputed still stood until someone decided the correction. It now
counts as unresolved until the reversal is decided, so both oversight bounds --
the count and the oldest age -- reach it.
"""

from dataclasses import replace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contract_api import ResolveSampleRequest, resolve_sample
from aida.governance_decision_contracts import GovernanceDecisionRefused
from aida.governance_decision_service import decide_review
from aida.models import (
    AssetTag,
    BulkStewardshipOperation,
    GovernanceReview,
    MetadataTable,
    Organization,
)
from aida.reviewer_agent import oldest_unresolved_sample_age_hours, unresolved_audit_samples
from aida.semantic_api import GovernanceDecisionRequest, decide_governance_review
from aida.stewardship_service import apply_bulk_operation, request_bulk_operation_reversal
from tests import test_ar11_correction_traceability as ar11

# The AR-11 suite's in-memory database, bound as a module attribute for pytest.
session = ar11.session

APPROVE = GovernanceDecisionRequest(decision="APPROVE")


async def _tag_reversal(
    db: AsyncSession,
) -> tuple[Organization, MetadataTable, BulkStewardshipOperation, GovernanceReview]:
    """A TAG that overwrote `low` with `high`, and steward-b's reversal of it."""
    org = await ar11._org(db)
    [table] = await ar11._tables(db, org, 1)
    db.add(ar11._tag(org, table, "low"))
    await db.flush()
    operation = await ar11._operation(
        db,
        org,
        operation_type="TAG",
        subject_ids=[table.id],
        parameters={"tag_key": "pii", "tag_value": "high"},
    )
    await apply_bulk_operation(db, operation, reviewer="steward-a", now=ar11.NOW)
    reversal, review = await request_bulk_operation_reversal(
        db, operation, reason="the agent was wrong", requested_by="steward-b"
    )
    await db.commit()
    return org, table, reversal, review


async def _tag_value(db: AsyncSession, table: MetadataTable) -> str | None:
    tag = await db.scalar(select(AssetTag).where(AssetTag.table_id == table.id))
    return tag.tag_value if tag is not None else None


# --- maker-checker, on both doors ----------------------------------------------


async def test_whoever_raised_a_reversal_cannot_approve_it(session: AsyncSession) -> None:
    org, table, reversal, review = await _tag_reversal(session)

    with pytest.raises(HTTPException) as refused:
        await decide_governance_review(
            review.id, APPROVE, context=ar11._context(org, "steward-b"), session=session
        )

    assert (refused.value.status_code, refused.value.detail) == (
        409,
        "maker-checker separation is required",
    )
    assert reversal.status == "REVIEW_REQUIRED"
    assert await _tag_value(session, table) == "high"


async def test_a_delegate_acting_for_the_requester_cannot_approve_it_either(
    session: AsyncSession,
) -> None:
    """Self-approval by proxy: a delegation must not become the way round."""
    org, table, reversal, review = await _tag_reversal(session)
    delegate = replace(
        ar11._context(org, "steward-d"), active_delegator_principal_id="steward-b"
    )

    with pytest.raises(HTTPException) as refused:
        await decide_governance_review(review.id, APPROVE, context=delegate, session=session)

    assert refused.value.status_code == 409
    assert reversal.status == "REVIEW_REQUIRED"
    assert await _tag_value(session, table) == "high"


async def test_the_shared_decision_service_refuses_the_requester_too(
    session: AsyncSession,
) -> None:
    """The batch endpoint and the reviewer agent decide through `decide_review`,
    which checks permission before it claims anything."""
    org, table, reversal, review = await _tag_reversal(session)

    with pytest.raises(GovernanceDecisionRefused) as refused:
        await decide_review(
            session,
            review,
            decision="APPROVE",
            reason=None,
            context=ar11._context(org, "steward-b"),
            now=ar11.NOW,
        )

    assert refused.value.http_status == 409
    assert reversal.status == "REVIEW_REQUIRED"
    assert await _tag_value(session, table) == "high"


async def test_another_steward_approving_it_restores_what_was_overwritten(
    session: AsyncSession,
) -> None:
    org, table, reversal, review = await _tag_reversal(session)

    await decide_governance_review(
        review.id, APPROVE, context=ar11._context(org, "steward-c"), session=session
    )

    assert reversal.status == "APPLIED"
    assert await _tag_value(session, table) == "low"


# --- an undecided correction keeps its sample open -------------------------------


async def _disputed_with_reversal(
    db: AsyncSession,
) -> tuple[Organization, BulkStewardshipOperation, GovernanceReview]:
    org = await ar11._org(db)
    operation, sample, _tables = await ar11._sampled_bulk_decision(db, org)
    await db.commit()
    await resolve_sample(
        org.id,
        sample.id,
        ResolveSampleRequest(
            human_outcome="DISAGREED",
            rationale="both tables are staging copies",
            reverse_applied_changes=True,
        ),
        context=ar11._context(org, "reviewer-h"),
        session=db,
    )
    reversal = await db.scalar(
        select(BulkStewardshipOperation).where(
            BulkStewardshipOperation.reverses_operation_id == operation.id
        )
    )
    assert reversal is not None
    review = await db.get(GovernanceReview, reversal.governance_review_id)
    assert review is not None
    return org, reversal, review


async def test_a_disputed_sample_stays_unresolved_until_its_reversal_is_decided(
    session: AsyncSession,
) -> None:
    org, _reversal, review = await _disputed_with_reversal(session)

    assert await unresolved_audit_samples(session, org.id) == 1

    await decide_governance_review(
        review.id, APPROVE, context=ar11._context(org, "steward-c"), session=session
    )

    assert await unresolved_audit_samples(session, org.id) == 0


async def test_rejecting_the_reversal_also_finishes_the_correction(
    session: AsyncSession,
) -> None:
    """A decided correction is finished whichever way it was decided: the
    question the bound asks is whether a person has looked, not what they said."""
    org, _reversal, review = await _disputed_with_reversal(session)

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="REJECT", reason="the links were right after all"),
        context=ar11._context(org, "steward-c"),
        session=session,
    )

    assert await unresolved_audit_samples(session, org.id) == 0


async def test_a_waiting_correction_is_aged_from_when_it_was_filed(
    session: AsyncSession,
) -> None:
    org, reversal, _review = await _disputed_with_reversal(session)
    reversal.created_at = ar11.NOW - ar11.timedelta(hours=30)
    await session.commit()

    assert await oldest_unresolved_sample_age_hours(session, org.id, now=ar11.NOW) == 30.0


async def test_a_verdict_without_a_reversal_resolves_the_sample_at_once(
    session: AsyncSession,
) -> None:
    org = await ar11._org(session)
    _operation, sample, _tables = await ar11._sampled_bulk_decision(session, org)
    await session.commit()

    await resolve_sample(
        org.id,
        sample.id,
        ResolveSampleRequest(human_outcome="DISAGREED", rationale="wrong, but leave it"),
        context=ar11._context(org, "reviewer-h"),
        session=session,
    )

    assert await unresolved_audit_samples(session, org.id) == 0
    assert await oldest_unresolved_sample_age_hours(session, org.id, now=ar11.NOW) is None
