"""R11-C8: an agent-approved business annotation can be taken back.

The reviewer agent may approve a `METADATA_ENRICHMENT_PROPOSAL`, which writes a
business annotation version. Until now a human who disputed that decision had no
correction to file: requesting one was refused by name, and the only remedy was
to propose a better annotation, which does not work when the right answer is
that the platform should say nothing about the table.

The correction is the same one a description already had: withdraw the version,
through a maker-checker review, keeping its content for any run grounded on it.
These pin what makes that correct rather than merely available:

* disputing the sample files the withdrawal, naming the sample, as a T2 review a
  person decides -- the agent that approved the annotation cannot decide it;
* approving it moves exactly the version the agent approved to `WITHDRAWN`;
* a version a person approved since is not withdrawn -- it is theirs, and the
  refusal says to propose instead;
* while the withdrawal waits, the disputed sample still counts as unresolved.
"""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contract_api import ResolveSampleRequest, resolve_sample
from aida.business_annotation_versions import AnnotationVersionContent, write_annotation_version
from aida.models import (
    DescriptionWithdrawal,
    GovernanceReview,
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    Organization,
    ReviewAuditSample,
)
from aida.reviewer_agent import unresolved_audit_samples
from aida.semantic_api import GovernanceDecisionRequest, decide_governance_review
from tests import test_ar11_correction_traceability as ar11

# The AR-11 suite's in-memory database, bound as a module attribute for pytest.
session = ar11.session

AGENT = "agent:reviewer"


def _content(description: str) -> AnnotationVersionContent:
    return AnnotationVersionContent(
        business_name="Orders",
        business_description=description,
        table_role="FACT",
        grain_statement="One row per order.",
        synonyms=[],
        suggested_questions=[],
        tags=[],
        confidence=0.9,
    )


async def _agent_approved_annotation(
    db: AsyncSession,
) -> tuple[
    Organization, MetadataBusinessAnnotation, MetadataBusinessAnnotationVersion, ReviewAuditSample
]:
    """An enrichment proposal the agent approved, the annotation version it
    wrote, and the sample that audits that decision."""
    org = await ar11._org(db)
    [table] = await ar11._tables(db, org, 1)
    review = GovernanceReview(
        organization_id=org.id,
        object_type="METADATA_ENRICHMENT_PROPOSAL",
        object_id=str(uuid4()),
        requested_action="APPROVE",
        requested_by="rules-engine",
    )
    db.add(review)
    await db.flush()
    annotation = MetadataBusinessAnnotation(
        organization_id=org.id,
        datasource_id=table.datasource_id,
        table_id=table.id,
        domain_id=uuid4(),
        entity_id=uuid4(),
        source_proposal_id=UUID(review.object_id),
    )
    db.add(annotation)
    await db.flush()
    version = await write_annotation_version(
        db,
        organization_id=org.id,
        annotation_id=annotation.id,
        content=_content("A settled trade, as the agent described it."),
        approved_by=AGENT,
        approved_at=ar11.NOW,
    )
    sample = ReviewAuditSample(
        organization_id=org.id,
        governance_review_id=review.id,
        agent_principal_id=AGENT,
        object_type="METADATA_ENRICHMENT_PROPOSAL",
        risk_tier="T0",
        decision="APPROVED",
        sampled_at=ar11.NOW - timedelta(hours=2),
        human_outcome="PENDING",
    )
    db.add(sample)
    await db.commit()
    return org, annotation, version, sample


async def _dispute(db: AsyncSession, org: Organization, sample: ReviewAuditSample) -> None:
    await resolve_sample(
        org.id,
        sample.id,
        ResolveSampleRequest(
            human_outcome="DISAGREED",
            rationale="this is an order table, not trades",
            reverse_applied_changes=True,
        ),
        context=ar11._context(org, "reviewer-h"),
        session=db,
    )


async def _withdrawal(
    db: AsyncSession, annotation: MetadataBusinessAnnotation
) -> DescriptionWithdrawal | None:
    return await db.scalar(
        select(DescriptionWithdrawal).where(DescriptionWithdrawal.subject_id == str(annotation.id))
    )


async def test_disputing_the_decision_files_a_withdrawal_that_names_its_sample(
    session: AsyncSession,
) -> None:
    org, annotation, version, sample = await _agent_approved_annotation(session)

    await _dispute(session, org, sample)

    withdrawal = await _withdrawal(session, annotation)
    assert withdrawal is not None
    assert (withdrawal.subject_type, withdrawal.version_id, withdrawal.status) == (
        "ANNOTATION",
        version.id,
        "PENDING_REVIEW",
    )
    assert withdrawal.review_audit_sample_id == sample.id
    review = await session.get(GovernanceReview, withdrawal.governance_review_id)
    assert review is not None
    assert (review.object_type, review.requested_action) == (
        "DESCRIPTION_WITHDRAWAL",
        "WITHDRAW_ANNOTATION",
    )


async def test_approving_it_withdraws_the_agents_version_and_keeps_its_content(
    session: AsyncSession,
) -> None:
    org, annotation, version, sample = await _agent_approved_annotation(session)
    await _dispute(session, org, sample)
    withdrawal = await _withdrawal(session, annotation)
    assert withdrawal is not None and withdrawal.governance_review_id is not None

    await decide_governance_review(
        withdrawal.governance_review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=ar11._context(org, "steward-c"),
        session=session,
    )

    await session.refresh(version)
    assert version.status == "WITHDRAWN"
    assert version.business_description == "A settled trade, as the agent described it."
    approved = await session.scalar(
        select(MetadataBusinessAnnotationVersion).where(
            MetadataBusinessAnnotationVersion.annotation_id == annotation.id,
            MetadataBusinessAnnotationVersion.status == "APPROVED",
        )
    )
    assert approved is None


async def test_a_version_a_person_approved_since_is_not_withdrawn(
    session: AsyncSession,
) -> None:
    """The agent's version is superseded by a steward's. Withdrawing the current
    one would take back the steward's decision, not the agent's."""
    org, annotation, _version, sample = await _agent_approved_annotation(session)
    await write_annotation_version(
        session,
        organization_id=org.id,
        annotation_id=annotation.id,
        content=_content("An order, corrected by a steward."),
        approved_by="steward-h",
        approved_at=ar11.NOW + timedelta(hours=1),
    )
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await _dispute(session, org, sample)

    assert refused.value.status_code == 409
    assert "new proposal" in str(refused.value.detail)
    assert await _withdrawal(session, annotation) is None


async def test_the_disputed_sample_stays_unresolved_until_the_withdrawal_is_decided(
    session: AsyncSession,
) -> None:
    org, annotation, _version, sample = await _agent_approved_annotation(session)
    await _dispute(session, org, sample)
    withdrawal = await _withdrawal(session, annotation)
    assert withdrawal is not None and withdrawal.governance_review_id is not None

    assert await unresolved_audit_samples(session, org.id) == 1

    await decide_governance_review(
        withdrawal.governance_review_id,
        GovernanceDecisionRequest(decision="REJECT", reason="the agent was right after all"),
        context=ar11._context(org, "steward-c"),
        session=session,
    )

    assert await unresolved_audit_samples(session, org.id) == 0
