"""R11-REV01: per-object-type evidence gates on batch approval.

The first slice gated batch approval on "some evidence was shown and the change is not T3".
That let a description draft whose text was blank, or whose evidence payload was empty, be
approved in a batch alongside nine hundred good ones. The gate is now a per-type *contract*
(`aida.review_batches.BATCH_APPROVAL_EVIDENCE`): the facts that type's review actually rests
on, read off what the shared read model composes for it. What these tests pin:

* every contracted type, composed through the real read model from real rows, meets its
  contract -- so the contract cannot drift from the composer without failing here;
* every fact in every contract is load-bearing: drop it and approval is refused with
  `REQUIRED_EVIDENCE_MISSING`, naming it;
* types with no contract are reject-only in a batch, explicitly (`NO_EVIDENCE_CONTRACT`),
  and a batch rejection of one is really applied through the shared decision service;
* T3 stays individual-only whatever it composes; contracted types are all decidable.

Real in-memory SQLite (which does not enforce foreign keys here, so proposal rows point at
bare ids where the composer never follows them), real read model, real adapters.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.semantic_api  # noqa: F401 -- registers every object type's decision adapter
from aida import review_batch_models  # noqa: F401 -- registers the batch tables
from aida.db import Base
from aida.description_withdrawal import request_description_withdrawal
from aida.envelope_models import RoutineDescriptionDraft
from aida.governance_decision_service import registered_object_types
from aida.models import (
    AssetDescriptionDraft,
    AssetDocumentationVersion,
    BulkStewardshipOperation,
    GlossaryLinkProposal,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    MetadataEnrichmentProposal,
    SemanticInferenceRun,
    SemanticMetricProposal,
    SemanticModelVersion,
    TermSemanticBinding,
)
from aida.quality_rule_proposal_model import QualityRuleProposal
from aida.review_batch_api import create_review_batch, decide_frozen_review_batch
from aida.review_batch_schemas import (
    ReviewBatchCreate,
    ReviewBatchDecisionCreate,
    ReviewBatchSelectionWrite,
)
from aida.review_batches import (
    _FAMILY_OF,
    BATCH_APPROVAL_EVIDENCE,
    ComposedMember,
    Selection,
    approve_gate,
    compose_members,
    decide_review_batch,
    freeze_review_batch,
    missing_evidence,
    required_evidence,
)
from aida.review_queue_schemas import ReviewQueueProposalRead
from aida.review_risk_tiers import TIER_T3, risk_tier_for
from aida.schemas import EvidenceItemRead
from aida.semantic_api import GovernanceReviewDiffRead
from tests.support.review_batch_estate import (
    MAKER,
    Estate,
    add_bare_review,
    add_column_draft_reviews,
    add_columns,
    add_table_draft_reviews,
    add_tables,
    build_estate,
    reviewer,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


_SCORES = {
    "accuracy_score": 0.8,
    "clarity_score": 0.8,
    "style_score": 0.8,
    "completeness_score": 0.8,
    "overall_score": 0.8,
}


async def _review(
    session: AsyncSession, estate: Estate, object_type: str, object_id: UUID, action: str
) -> GovernanceReview:
    review = GovernanceReview(
        id=uuid4(),
        organization_id=estate.organization_id,
        object_type=object_type,
        object_id=str(object_id),
        requested_action=action,
        status="PENDING",
        requested_by=MAKER,
        created_at=estate.tick(),
    )
    session.add(review)
    await session.flush()
    return review


async def _one_of_each_contracted_type(
    session: AsyncSession, estate: Estate
) -> dict[str, GovernanceReview]:
    """A pending review of every contracted object type, each backed by the row the shared
    read model composes it from, populated the way that type's own producer populates it."""
    org = estate.organization_id
    [table] = await add_tables(session, estate, 1, prefix="gated")
    reviews: dict[str, GovernanceReview] = {}

    [reviews["ASSET_DESCRIPTION_DRAFT"]] = await add_table_draft_reviews(session, estate, [table])
    [column] = await add_columns(session, table, 1)
    [reviews["COLUMN_DESCRIPTION_DRAFT"]] = await add_column_draft_reviews(
        session, estate, table, [column]
    )

    routine_draft_id = uuid4()
    reviews["ROUTINE_DESCRIPTION_DRAFT"] = await _review(
        session, estate, "ROUTINE_DESCRIPTION_DRAFT", routine_draft_id, "PUBLISH_DESCRIPTION"
    )
    text = "Recalculates the daily balance for one account."
    session.add(
        RoutineDescriptionDraft(
            id=routine_draft_id,
            organization_id=org,
            datasource_id=estate.datasource.id,
            routine_id=uuid4(),
            drafted_text=text,
            text_fingerprint=hashlib.sha256(text.encode()).hexdigest(),
            evidence={"routine_type": "PROCEDURE", "body_state": "WITHHELD"},
            governance_review_id=reviews["ROUTINE_DESCRIPTION_DRAFT"].id,
            created_by=MAKER,
            **_SCORES,
        )
    )

    run = SemanticInferenceRun(
        id=uuid4(),
        organization_id=org,
        datasource_id=estate.datasource.id,
        engine_mode="RULES_ONLY",
        engine_version="v1",
        created_by="inference-engine",
    )
    session.add(run)
    await session.flush()
    enrichment_id = uuid4()
    reviews["METADATA_ENRICHMENT_PROPOSAL"] = await _review(
        session, estate, "METADATA_ENRICHMENT_PROPOSAL", enrichment_id, "APPLY_BUSINESS_SEMANTICS"
    )
    session.add(
        MetadataEnrichmentProposal(
            id=enrichment_id,
            organization_id=org,
            datasource_id=estate.datasource.id,
            inference_run_id=run.id,
            table_id=table.id,
            governance_review_id=reviews["METADATA_ENRICHMENT_PROPOSAL"].id,
            engine_type="RULES",
            engine_version="v1",
            confidence=0.7,
            payload={"business_name": "Gated"},
            evidence={"evidence_ids": [f"table:{table.id}"], "rules_version": "v1"},
            fingerprint="fp-enrichment",
            proposed_by="inference-engine",
        )
    )

    link_id = uuid4()
    reviews["GLOSSARY_LINK_PROPOSAL"] = await _review(
        session, estate, "GLOSSARY_LINK_PROPOSAL", link_id, "LINK_TERM"
    )
    session.add(
        GlossaryLinkProposal(
            id=link_id,
            organization_id=org,
            table_id=table.id,
            term_id=uuid4(),
            source_annotation_id=uuid4(),
            confidence=0.9,
            evidence={"matched_label": "balance", "match_strategy": "EXACT"},
            governance_review_id=reviews["GLOSSARY_LINK_PROPOSAL"].id,
            created_by=MAKER,
        )
    )

    metric_id = uuid4()
    reviews["SEMANTIC_METRIC_PROPOSAL"] = await _review(
        session, estate, "SEMANTIC_METRIC_PROPOSAL", metric_id, "PUBLISH_METRIC"
    )
    session.add(
        SemanticMetricProposal(
            id=metric_id,
            organization_id=org,
            project_id=uuid4(),
            table_id=table.id,
            measure_column_id=column.id,
            source_annotation_id=uuid4(),
            proposed_slug="daily_balance",
            proposed_name="Daily balance",
            proposed_description="Sum of balances per day.",
            proposed_aggregation="SUM",
            proposed_grain="day",
            evidence={"measure_column": "balance"},
            governance_review_id=reviews["SEMANTIC_METRIC_PROPOSAL"].id,
            created_by=MAKER,
            **_SCORES,
        )
    )

    binding_id = uuid4()
    reviews["TERM_SEMANTIC_BINDING"] = await _review(
        session, estate, "TERM_SEMANTIC_BINDING", binding_id, "BIND_TERM"
    )
    session.add(
        TermSemanticBinding(
            id=binding_id,
            organization_id=org,
            term_id=uuid4(),
            semantic_object_type="METRIC",
            semantic_object_id=uuid4(),
            requested_by=MAKER,
        )
    )

    rule_id = uuid4()
    reviews["QUALITY_RULE_PROPOSAL"] = await _review(
        session, estate, "QUALITY_RULE_PROPOSAL", rule_id, "PUBLISH_QUALITY_RULE"
    )
    session.add(
        QualityRuleProposal(
            id=rule_id,
            organization_id=org,
            datasource_id=estate.datasource.id,
            table_id=table.id,
            rule_type="TABLE_ROW_COUNT_MIN",
            threshold=100.0,
            name="gated has rows",
            confidence=0.8,
            evidence={"profiles_observed": 5},
            created_by=MAKER,
        )
    )

    model = SemanticModelVersion(
        id=uuid4(),
        organization_id=org,
        project_id=uuid4(),
        version=1,
        name="Model",
        change_summary="first",
        status="REVIEW_REQUIRED",
        created_by=MAKER,
    )
    session.add(model)
    await session.flush()
    reviews["SEMANTIC_MODEL_VERSION"] = await _review(
        session, estate, "SEMANTIC_MODEL_VERSION", model.id, "PUBLISH"
    )

    term = GlossaryTerm(id=uuid4(), organization_id=org, term_key="balance")
    session.add(term)
    await session.flush()
    term_version = GlossaryTermVersion(
        id=uuid4(),
        organization_id=org,
        term_id=term.id,
        version=1,
        display_name="Balance",
        definition="What an account holds at the end of a day.",
        created_by=MAKER,
    )
    session.add(term_version)
    await session.flush()
    reviews["GLOSSARY_TERM_VERSION"] = await _review(
        session, estate, "GLOSSARY_TERM_VERSION", term_version.id, "PUBLISH"
    )

    operation_id = uuid4()
    reviews["BULK_STEWARDSHIP_OPERATION"] = await _review(
        session, estate, "BULK_STEWARDSHIP_OPERATION", operation_id, "BULK_TAG"
    )
    session.add(
        BulkStewardshipOperation(
            id=operation_id,
            organization_id=org,
            operation_type="TAG",
            subject_type="TABLE",
            subject_ids=[str(table.id)],
            parameters={"tag_key": "pii-review", "tag_value": "pending"},
            governance_review_id=reviews["BULK_STEWARDSHIP_OPERATION"].id,
            requested_by=MAKER,
        )
    )
    await session.flush()
    return reviews


# ---------------------------------------------------------------------------
# The contracts match what the read model composes
# ---------------------------------------------------------------------------


async def test_every_contracted_type_meets_its_contract_through_the_real_read_model(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    reviews = await _one_of_each_contracted_type(session, estate)
    assert set(reviews) == set(BATCH_APPROVAL_EVIDENCE)
    composed = await compose_members(session, estate.organization_id, list(reviews.values()))
    for object_type, review in reviews.items():
        member = composed[review.id]
        assert missing_evidence(member) == (), object_type
        assert approve_gate(member) is None, object_type


def _without(member: ComposedMember, fact: str) -> ComposedMember:
    """The same member with one contracted fact taken away: the evidence items that satisfy
    it dropped, or -- for a structured diff -- the diff made non-diffable."""
    [requirement] = [
        item for item in BATCH_APPROVAL_EVIDENCE[member.review.object_type] if item.name == fact
    ]
    proposal = member.proposal
    assert proposal is not None
    if requirement.structured_diff:
        diff = proposal.diff.model_copy(update={"diffable": False})
        return replace(member, proposal=proposal.model_copy(update={"diff": diff}))

    def keeps(item: EvidenceItemRead) -> bool:
        alone = replace(
            member, proposal=proposal.model_copy(update={"evidence": [item]}), supplement=[]
        )
        return not requirement.satisfied_by(alone)

    return replace(
        member,
        proposal=proposal.model_copy(
            update={"evidence": [item for item in proposal.evidence if keeps(item)]}
        ),
        supplement=[item for item in member.supplement if keeps(item)],
    )


_EVERY_FACT = [
    (object_type, requirement.name)
    for object_type, contract in BATCH_APPROVAL_EVIDENCE.items()
    for requirement in contract
]


@pytest.mark.parametrize(("object_type", "fact"), _EVERY_FACT)
async def test_every_contracted_fact_is_load_bearing(
    session: AsyncSession, object_type: str, fact: str
) -> None:
    estate = await build_estate(session)
    reviews = await _one_of_each_contracted_type(session, estate)
    review = reviews[object_type]
    member = (await compose_members(session, estate.organization_id, [review]))[review.id]
    assert approve_gate(member) is None

    lacking = _without(member, fact)
    assert fact in missing_evidence(lacking)
    assert approve_gate(lacking) in ("REQUIRED_EVIDENCE_MISSING", "EVIDENCE_NOT_SHOWN")


# ---------------------------------------------------------------------------
# Real members that lack a fact, end to end through freeze and decide
# ---------------------------------------------------------------------------


async def test_a_blank_draft_or_an_empty_payload_is_refused_approval_by_name(
    session: AsyncSession,
) -> None:
    """The case the first slice let through: a description draft whose text is blank, and one
    whose evidence payload is empty, each composed *something*, so "evidence was shown" was
    true. Now each is refused batch approval with the missing fact named, frozen and at
    decision time -- and a batch rejection of the same two still applies."""
    estate = await build_estate(session)
    good, blank, bare = await add_table_draft_reviews(
        session, estate, await add_tables(session, estate, 3)
    )
    drafts = {
        draft.governance_review_id: draft
        for draft in (await session.scalars(select(AssetDescriptionDraft))).all()
    }
    drafts[blank.id].drafted_text = "   "
    drafts[bare.id].evidence = {}
    await session.commit()
    context = reviewer(estate.organization_id)

    composed = await compose_members(session, estate.organization_id, [good, blank, bare])
    assert missing_evidence(composed[blank.id]) == ("PROPOSED_TEXT",)
    assert missing_evidence(composed[bare.id]) == ("SOURCE_SIGNALS",)
    assert required_evidence("ASSET_DESCRIPTION_DRAFT") == ("PROPOSED_TEXT", "SOURCE_SIGNALS")

    batch = await create_review_batch(
        ReviewBatchCreate(
            items=[
                ReviewBatchSelectionWrite(
                    review_id=review.id, evidence_fingerprint=composed[review.id].fingerprint
                )
                for review in (good, blank, bare)
            ]
        ),
        context=context,
        session=session,
    )
    assert batch.eligible_count == 3
    assert batch.approve_gate_counts == {"REQUIRED_EVIDENCE_MISSING": 2}

    result = await decide_frozen_review_batch(
        batch.id,
        ReviewBatchDecisionCreate(decision="APPROVE", reason="read every member"),
        context=context,
        session=session,
    )
    by_review = {member.review_id: member for member in result.members}
    assert by_review[good.id].outcome == "APPLIED"
    for review, fact in ((blank, "PROPOSED_TEXT"), (bare, "SOURCE_SIGNALS")):
        refused = by_review[review.id]
        assert (refused.outcome, refused.reason_code) == ("REFUSED", "REQUIRED_EVIDENCE_MISSING")
        assert refused.detail == f"missing required evidence: {fact}"
        await session.refresh(review)
        assert review.status == "PENDING"  # the gate refused before the decision service ran

    # Rejection is not gated: publishing nothing needs no contract.
    rejection = await freeze_review_batch(
        session, context=context, selections=[Selection(blank.id), Selection(bare.id)], filt=None
    )
    rejected = await decide_review_batch(
        session,
        context=context,
        batch_id=rejection.id,
        decision="REJECT",
        reason="the draft does not say what it describes",
    )
    assert {o.outcome for o in rejected.outcomes} == {"APPLIED"}


# ---------------------------------------------------------------------------
# Reject-only and individual-only types, explicitly
# ---------------------------------------------------------------------------


def _member_with_everything(object_type: str) -> ComposedMember:
    """A member whose proposal composed evidence *and* a diff -- as much as any type could
    show -- so the only thing left to decide the gate is the type itself."""
    review = GovernanceReview(
        id=uuid4(),
        organization_id=uuid4(),
        object_type=object_type,
        object_id=str(uuid4()),
        requested_action="APPROVE",
        status="PENDING",
        requested_by=MAKER,
    )
    proposal = ReviewQueueProposalRead(
        review_id=review.id,
        organization_id=review.organization_id,
        object_type=object_type,
        object_id=review.object_id,
        requested_action=review.requested_action,
        status="PENDING",
        requested_by=MAKER,
        decided_by=None,
        decision_reason=None,
        decided_at=None,
        created_at=datetime.now(UTC),
        evidence=[EvidenceItemRead(category="ANY", claim="something: shown", source="x:y")],
        diff=GovernanceReviewDiffRead(
            review_id=review.id,
            object_type=object_type,
            object_id=review.object_id,
            diffable=True,
            before={},
            after={"a": 1},
        ),
    )
    return ComposedMember(review=review, proposal=proposal, supplement=[], fingerprint="0" * 64)


def test_contracted_types_are_decidable_and_below_the_trust_boundary() -> None:
    registered = registered_object_types()
    for object_type in BATCH_APPROVAL_EVIDENCE:
        assert object_type in registered, f"{object_type} has a contract but no decision path"
        assert risk_tier_for(object_type) != TIER_T3, f"{object_type} is T3 yet contracted"


def test_every_other_type_is_reject_only_or_individual_by_name() -> None:
    """Every object type the queue knows or the decision service can decide is exactly one of:
    contracted (approvable when its facts are shown), T3 (individual only), or reject-only in
    a batch. No type falls between them, and none becomes approvable by composing *more*."""
    known = set(_FAMILY_OF) | set(registered_object_types())
    reject_only: set[str] = set()
    for object_type in sorted(known):
        gate = approve_gate(_member_with_everything(object_type))
        if risk_tier_for(object_type) == TIER_T3:
            assert gate == "INDIVIDUAL_DECISION_REQUIRED", object_type
        elif object_type in BATCH_APPROVAL_EVIDENCE:
            assert gate in (None, "REQUIRED_EVIDENCE_MISSING"), object_type
        else:
            assert gate == "NO_EVIDENCE_CONTRACT", object_type
            assert required_evidence(object_type) == ()
            reject_only.add(object_type)
    # The reject-only set is real, not empty by accident: withdrawals and glossary conflicts
    # are in it (the read model composes nothing for either).
    assert {"DESCRIPTION_WITHDRAWAL", "GLOSSARY_CONFLICT"} <= reject_only


async def test_a_reject_only_type_is_batch_rejected_but_never_batch_approved(
    session: AsyncSession,
) -> None:
    """A description withdrawal: batch approval is refused `NO_EVIDENCE_CONTRACT` and leaves
    both the withdrawal and the published description untouched; a batch rejection of the
    same withdrawal is decided through the shared decision service."""
    estate = await build_estate(session)
    [table] = await add_tables(session, estate, 1)
    [draft_review] = await add_table_draft_reviews(session, estate, [table])
    context = reviewer(estate.organization_id)
    publish = await freeze_review_batch(
        session, context=context, selections=[Selection(draft_review.id)], filt=None
    )
    await decide_review_batch(
        session, context=context, batch_id=publish.id, decision="APPROVE", reason=None
    )
    draft = await session.get(AssetDescriptionDraft, UUID(draft_review.object_id))
    assert draft is not None and draft.published_version_id is not None
    _withdrawal, withdrawal_review = await request_description_withdrawal(
        session,
        organization_id=estate.organization_id,
        subject_type="TABLE",
        subject_id=table.id,
        reason="overstates the grain",
        requested_by=MAKER,
    )
    await session.commit()

    approve = await freeze_review_batch(
        session, context=context, selections=[Selection(withdrawal_review.id)], filt=None
    )
    approved = await decide_review_batch(
        session, context=context, batch_id=approve.id, decision="APPROVE", reason="looks right"
    )
    [member] = approved.outcomes
    assert (member.outcome, member.reason_code) == ("REFUSED", "NO_EVIDENCE_CONTRACT")
    await session.refresh(withdrawal_review)
    assert withdrawal_review.status == "PENDING"
    published = await session.get(AssetDocumentationVersion, draft.published_version_id)
    assert published is not None and published.status == "APPROVED"

    reject = await freeze_review_batch(
        session, context=context, selections=[Selection(withdrawal_review.id)], filt=None
    )
    rejected = await decide_review_batch(
        session,
        context=context,
        batch_id=reject.id,
        decision="REJECT",
        reason="the grain statement is correct",
    )
    assert [o.outcome for o in rejected.outcomes] == ["APPLIED"]
    await session.refresh(withdrawal_review)
    assert withdrawal_review.status == "REJECTED"
    await session.refresh(published)
    assert published.status == "APPROVED"


async def test_a_type_with_nothing_composed_but_a_contract_is_not_shown(
    session: AsyncSession,
) -> None:
    """A contracted type whose backing row is gone composes nothing at all: that member is
    `EVIDENCE_NOT_SHOWN`, distinct from a type that has no contract."""
    estate = await build_estate(session)
    orphan = await add_bare_review(session, estate, "GLOSSARY_LINK_PROPOSAL")
    member = (await compose_members(session, estate.organization_id, [orphan]))[orphan.id]
    assert approve_gate(member) == "EVIDENCE_NOT_SHOWN"
    assert missing_evidence(member) == ("MATCH_EVIDENCE",)
