"""R11-C8: an agent-approved description can be withdrawn from its sample.

The reviewer agent may approve an `ASSET_DESCRIPTION_DRAFT` or a
`COLUMN_DESCRIPTION_DRAFT`, and each publishes a description version. A human
who disputed that decision could say so, but asking for the correction was
refused by name: a steward had to raise the withdrawal separately, with no edge
back to the sample, so the sample read as resolved while the disputed
description stood.

The correction is the withdrawal a steward already had. These pin what makes it
correct rather than merely available:

* disputing the sample files a withdrawal of exactly the version the draft
  published, naming the sample, as a T2 review a person decides;
* approving it moves that version to `WITHDRAWN` and keeps its text;
* a description a person published since is not withdrawn -- it is theirs;
* a decision that published nothing is refused rather than filed;
* while the withdrawal waits, the disputed sample still counts as unresolved.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contract_api import ResolveSampleRequest, resolve_sample
from aida.asset_description_service import publish_asset_documentation_version
from aida.column_documentation import publish_column_description
from aida.description_withdrawal import current_description_version
from aida.models import (
    AssetDescriptionDraft,
    AssetDocumentationVersion,
    ColumnDescriptionDraft,
    ColumnDocumentationVersion,
    DescriptionWithdrawal,
    GovernanceReview,
    MetadataColumn,
    MetadataTable,
    Organization,
    ReviewAuditSample,
)
from aida.reviewer_agent import unresolved_audit_samples
from aida.semantic_api import GovernanceDecisionRequest, decide_governance_review
from tests import test_ar11_correction_traceability as ar11

# The AR-11 suite's in-memory database, bound as a module attribute for pytest.
session = ar11.session

AGENT = "agent:reviewer"
AGENT_TEXT = "A settled trade, as the agent described it."
DRAFT_TYPES = ("ASSET_DESCRIPTION_DRAFT", "COLUMN_DESCRIPTION_DRAFT")
SUBJECT_TYPE = {"ASSET_DESCRIPTION_DRAFT": "TABLE", "COLUMN_DESCRIPTION_DRAFT": "COLUMN"}

Subject = MetadataTable | MetadataColumn
Version = AssetDocumentationVersion | ColumnDocumentationVersion


async def _publish(
    db: AsyncSession, subject: Subject, *, text: str, approved_by: str, at: datetime
) -> Version:
    """Publish a description onto a table or a column, as a draft's approval does."""
    if isinstance(subject, MetadataColumn):
        return await publish_column_description(
            db,
            organization_id=subject.organization_id,
            table_id=subject.table_id,
            column_id=subject.id,
            description=text,
            created_by="drafter",
            approved_by=approved_by,
            approved_at=at,
        )
    return await publish_asset_documentation_version(
        db,
        organization_id=subject.organization_id,
        table_id=subject.id,
        readme=text,
        created_by="drafter",
        approved_by=approved_by,
        approved_at=at,
    )


async def _agent_decided(
    db: AsyncSession, draft_type: str, *, published: bool = True
) -> tuple[Organization, Subject, Version | None, ReviewAuditSample]:
    """A description draft the agent decided, the version its approval
    published, and the sample that audits the decision."""
    org = await ar11._org(db)
    [table] = await ar11._tables(db, org, 1)
    subject: Subject = table
    if draft_type == "COLUMN_DESCRIPTION_DRAFT":
        subject = MetadataColumn(
            id=uuid4(),
            organization_id=org.id,
            table_id=table.id,
            name="trade_id",
            ordinal_position=0,
            physical_type="uuid",
            nullable=False,
            status="ACTIVE",
            fingerprint="f",
        )
        db.add(subject)
        await db.flush()
    review = GovernanceReview(
        organization_id=org.id,
        object_type=draft_type,
        object_id=str(uuid4()),
        requested_action="APPROVE",
        requested_by="drafter",
    )
    db.add(review)
    await db.flush()
    version = (
        await _publish(db, subject, text=AGENT_TEXT, approved_by=AGENT, at=ar11.NOW)
        if published
        else None
    )
    fields = {
        "organization_id": org.id,
        "table_id": table.id,
        "drafted_text": AGENT_TEXT,
        "text_fingerprint": "f" * 64,
        "accuracy_score": 0.9,
        "clarity_score": 0.9,
        "style_score": 0.9,
        "completeness_score": 0.9,
        "overall_score": 0.9,
        "evidence": {},
        "status": "APPROVED" if published else "REJECTED",
        "governance_review_id": review.id,
        "published_version_id": version.id if version is not None else None,
        "created_by": "drafter",
        "reviewed_by": AGENT,
        "reviewed_at": ar11.NOW,
    }
    draft = (
        ColumnDescriptionDraft(column_id=subject.id, **fields)
        if isinstance(subject, MetadataColumn)
        else AssetDescriptionDraft(**fields)
    )
    db.add(draft)
    await db.flush()
    review.object_id = str(draft.id)
    sample = ReviewAuditSample(
        organization_id=org.id,
        governance_review_id=review.id,
        agent_principal_id=AGENT,
        object_type=draft_type,
        risk_tier="T0",
        decision="APPROVED" if published else "REJECTED",
        sampled_at=ar11.NOW - timedelta(hours=2),
        human_outcome="PENDING",
    )
    db.add(sample)
    await db.commit()
    return org, subject, version, sample


async def _dispute(db: AsyncSession, org: Organization, sample: ReviewAuditSample) -> None:
    await resolve_sample(
        org.id,
        sample.id,
        ResolveSampleRequest(
            human_outcome="DISAGREED",
            rationale="this describes orders, not trades",
            reverse_applied_changes=True,
        ),
        context=ar11._context(org, "reviewer-h"),
        session=db,
    )


async def _withdrawal(db: AsyncSession, subject: Subject) -> DescriptionWithdrawal | None:
    return await db.scalar(
        select(DescriptionWithdrawal).where(DescriptionWithdrawal.subject_id == str(subject.id))
    )


@pytest.mark.parametrize("draft_type", DRAFT_TYPES)
async def test_disputing_the_decision_files_a_withdrawal_of_what_the_draft_published(
    session: AsyncSession, draft_type: str
) -> None:
    org, subject, version, sample = await _agent_decided(session, draft_type)
    assert version is not None

    await _dispute(session, org, sample)

    withdrawal = await _withdrawal(session, subject)
    assert withdrawal is not None
    assert (withdrawal.subject_type, withdrawal.version_id, withdrawal.status) == (
        SUBJECT_TYPE[draft_type],
        version.id,
        "PENDING_REVIEW",
    )
    assert withdrawal.review_audit_sample_id == sample.id
    review = await session.get(GovernanceReview, withdrawal.governance_review_id)
    assert review is not None
    assert (review.object_type, review.requested_action) == (
        "DESCRIPTION_WITHDRAWAL",
        "WITHDRAW_DESCRIPTION",
    )


@pytest.mark.parametrize("draft_type", DRAFT_TYPES)
async def test_approving_it_withdraws_the_published_version_and_keeps_its_text(
    session: AsyncSession, draft_type: str
) -> None:
    org, subject, version, sample = await _agent_decided(session, draft_type)
    assert version is not None
    await _dispute(session, org, sample)
    withdrawal = await _withdrawal(session, subject)
    assert withdrawal is not None and withdrawal.governance_review_id is not None

    await decide_governance_review(
        withdrawal.governance_review_id,
        GovernanceDecisionRequest(decision="APPROVE"),
        context=ar11._context(org, "steward-c"),
        session=session,
    )

    await session.refresh(version)
    assert version.status == "WITHDRAWN"
    text = (
        version.description if isinstance(version, ColumnDocumentationVersion) else version.readme
    )
    assert text == AGENT_TEXT
    assert await current_description_version(session, SUBJECT_TYPE[draft_type], subject.id) is None


@pytest.mark.parametrize("draft_type", DRAFT_TYPES)
async def test_a_description_a_person_published_since_is_not_withdrawn(
    session: AsyncSession, draft_type: str
) -> None:
    """The agent's version is superseded by a steward's. Withdrawing the current
    one would take back the steward's decision, not the agent's."""
    org, subject, _version, sample = await _agent_decided(session, draft_type)
    await _publish(
        session,
        subject,
        text="An order, described by a steward.",
        approved_by="steward-h",
        at=ar11.NOW + timedelta(hours=1),
    )
    await session.commit()

    with pytest.raises(HTTPException) as refused:
        await _dispute(session, org, sample)

    assert refused.value.status_code == 409
    assert "new description" in str(refused.value.detail)
    assert await _withdrawal(session, subject) is None


async def test_a_decision_that_published_nothing_is_refused_rather_than_filed(
    session: AsyncSession,
) -> None:
    """A rejected draft changed nothing, so there is nothing to withdraw -- and
    filing a withdrawal of whatever the table says now would be a change nobody
    disputed."""
    org, subject, _version, sample = await _agent_decided(
        session, "ASSET_DESCRIPTION_DRAFT", published=False
    )

    with pytest.raises(HTTPException) as refused:
        await _dispute(session, org, sample)

    assert refused.value.status_code == 409
    assert "published no description" in str(refused.value.detail)
    assert await _withdrawal(session, subject) is None


async def test_the_disputed_sample_stays_unresolved_until_the_withdrawal_is_decided(
    session: AsyncSession,
) -> None:
    org, subject, _version, sample = await _agent_decided(session, "COLUMN_DESCRIPTION_DRAFT")
    await _dispute(session, org, sample)
    withdrawal = await _withdrawal(session, subject)
    assert withdrawal is not None and withdrawal.governance_review_id is not None

    assert await unresolved_audit_samples(session, org.id) == 1

    await decide_governance_review(
        withdrawal.governance_review_id,
        GovernanceDecisionRequest(decision="REJECT", reason="the agent was right after all"),
        context=ar11._context(org, "steward-c"),
        session=session,
    )

    assert await unresolved_audit_samples(session, org.id) == 0


async def test_answers_that_consulted_the_described_table_are_found(
    session: AsyncSession,
) -> None:
    """The row's last clause, for a description: a run that consulted the table
    while the agent's description stood is listed, and one on another table is
    not. See `tests/test_r11c8_downstream_impact.py` for the window and bounds."""
    from aida.correction_impact import ASSET_IN_CONTEXT, downstream_impact
    from tests.test_r11c8_downstream_impact import hit, make_agent_run

    org, subject, _version, sample = await _agent_decided(session, "ASSET_DESCRIPTION_DRAFT")
    review = await session.get(GovernanceReview, sample.governance_review_id)
    assert review is not None
    review.decided_at = ar11.NOW
    consulted = make_agent_run(
        org.id, created_at=ar11.NOW + timedelta(hours=1), retrieval=[hit("TABLE", subject.id)]
    )
    unrelated = make_agent_run(
        org.id, created_at=ar11.NOW + timedelta(hours=1), retrieval=[hit("TABLE", uuid4())]
    )
    session.add_all([consulted, unrelated])
    await session.commit()

    impact = await downstream_impact(session, sample)

    assert [run.agent_run_id for run in impact.affected_runs] == [consulted.id]
    assert impact.affected_runs[0].bases == (ASSET_IN_CONTEXT,)
