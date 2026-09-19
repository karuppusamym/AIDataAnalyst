"""R11-REV01: the change-focused review queue and frozen review batches.

What these tests pin, in the row's own words: a filtered, change-focused queue with server
pagination; frozen batch ids and versions; per-item evidence gates and stale/concurrent-change
refusals; partial-outcome reporting and correction links -- while preserving the existing
maker-checker rules, review families and the refusal of unattended agent approval.

Every decision these tests observe is made by `governance_decision_service.decide_review`:
the batch path is asserted to *route through* it (maker-checker, compare-and-set claim,
adapter gates), never to re-implement it. Real in-memory SQLite, real adapters.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.review_batches as review_batches_module
from aida import review_batch_models  # noqa: F401 -- registers the batch tables
from aida.column_documentation import publish_column_description
from aida.db import Base
from aida.description_withdrawal import WITHDRAWN, request_description_withdrawal
from aida.models import (
    AssetDescriptionDraft,
    AssetDocumentationVersion,
    AuditEvent,
    ColumnDescriptionDraft,
    GovernanceReview,
    OutboxEvent,
)
from aida.review_batch_api import (
    create_review_batch,
    decide_frozen_review_batch,
    get_change_queue,
    get_change_queue_details,
    read_review_batch,
    read_review_batch_items,
)
from aida.review_batch_models import ReviewBatch, ReviewBatchItem
from aida.review_batch_schemas import (
    ReviewBatchCreate,
    ReviewBatchDecisionCreate,
    ReviewBatchFilterWrite,
    ReviewBatchSelectionWrite,
)
from aida.review_batches import (
    ComposedMember,
    QueueFilter,
    ReviewBatchError,
    Selection,
    approve_gate,
    compose_members,
    correction_for,
    decide_review_batch,
    freeze_review_batch,
    list_change_queue,
    review_family_for,
)
from aida.review_queue_read_model import compose_review_queue
from aida.review_queue_schemas import ReviewQueueProposalRead
from aida.schemas import EvidenceItemRead, GovernanceDecisionRequest
from aida.semantic_api import GovernanceReviewDiffRead, decide_governance_review
from tests.support.review_batch_estate import (
    CHECKER,
    MAKER,
    OTHER_CHECKER,
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


def _queue_kwargs(**overrides: object) -> dict[str, object]:
    """The keyword arguments `get_change_queue` takes when called directly (FastAPI would
    otherwise supply the `Query` defaults)."""
    base: dict[str, object] = {
        "review_status": "PENDING",
        "object_type": [],
        "family": [],
        "change_kind": [],
        "object_id": None,
        "table_id": None,
        "decidable_only": False,
        "cursor": None,
        "limit": 50,
        "include_total": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# The queue: filters and keyset pagination
# ---------------------------------------------------------------------------


async def test_keyset_pages_cover_the_queue_exactly_once(session: AsyncSession) -> None:
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 25)
    reviews = await add_table_draft_reviews(session, estate, tables)
    context = reviewer(estate.organization_id)

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await get_change_queue(
            **_queue_kwargs(cursor=cursor, limit=10), context=context, session=session
        )
        pages += 1
        assert page.total == 25
        seen.extend(str(item.review_id) for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert pages == 3
    assert seen == [str(review.id) for review in reviews]


async def test_keyset_is_stable_when_rows_are_decided_between_pages(
    session: AsyncSession,
) -> None:
    """The reason there is no offset: deciding rows on page 1 while page 2 is read must not
    make page 2 skip the rows that slid forward."""
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 20)
    reviews = await add_table_draft_reviews(session, estate, tables)
    context = reviewer(estate.organization_id)

    first = await list_change_queue(
        session, context=context, filt=QueueFilter(), cursor=None, limit=10
    )
    for review in reviews[:5]:  # decided elsewhere while the reviewer reads on
        review.status = "APPROVED"
    await session.flush()
    second = await list_change_queue(
        session, context=context, filt=QueueFilter(), cursor=first.next_cursor, limit=10
    )
    assert [m.review.id for m in second.members] == [r.id for r in reviews[10:20]]
    assert second.next_cursor is None


async def test_filters_narrow_by_family_kind_type_scope_and_decidability(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 3)
    wide = tables[0]
    columns = await add_columns(session, wide, 4)
    table_reviews = await add_table_draft_reviews(session, estate, tables)
    column_reviews = await add_column_draft_reviews(session, estate, wide, columns)
    own = await add_table_draft_reviews(
        session, estate, await add_tables(session, estate, 1, prefix="own"), requested_by=CHECKER
    )
    conflict = await add_bare_review(
        session, estate, "GLOSSARY_CONFLICT", requested_action="RESOLVE"
    )
    unknown = await add_bare_review(session, estate, "SOMETHING_NEW")
    context = reviewer(estate.organization_id)

    async def ids(filt: QueueFilter) -> set[str]:
        page = await list_change_queue(session, context=context, filt=filt, cursor=None, limit=200)
        return {str(member.review.id) for member in page.members}

    everything = {str(r.id) for r in (*table_reviews, *column_reviews, *own, conflict, unknown)}
    assert await ids(QueueFilter()) == everything
    assert await ids(QueueFilter(families=("SEMANTIC",))) == {str(conflict.id)}
    assert await ids(QueueFilter(families=("OTHER",))) == {str(unknown.id)}
    assert await ids(QueueFilter(change_kinds=("RESOLVE",))) == {str(conflict.id)}
    assert await ids(QueueFilter(object_types=("COLUMN_DESCRIPTION_DRAFT",))) == {
        str(r.id) for r in column_reviews
    }
    # Table scope: the wide table's own draft and its four column drafts, nothing else.
    assert await ids(QueueFilter(table_id=wide.id)) == {
        str(table_reviews[0].id),
        *(str(r.id) for r in column_reviews),
    }
    assert await ids(QueueFilter(object_id=conflict.object_id)) == {str(conflict.id)}
    # decidable_only drops what this reviewer authored -- maker-checker, paged by the server.
    assert str(own[0].id) not in await ids(QueueFilter(decidable_only=True))
    assert review_family_for("SOMETHING_NEW") == "OTHER"

    with pytest.raises(ReviewBatchError) as refused:
        await ids(QueueFilter(families=("NOT_A_FAMILY",)))
    assert refused.value.code == "UNKNOWN_REVIEW_FAMILY"


async def test_queue_rows_report_blockers_gates_and_fingerprints(session: AsyncSession) -> None:
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 2)
    [theirs] = await add_table_draft_reviews(session, estate, tables[:1])
    [mine] = await add_table_draft_reviews(session, estate, tables[1:], requested_by=CHECKER)
    bare = await add_bare_review(session, estate, "GLOSSARY_CONFLICT")
    context = reviewer(estate.organization_id)

    page = await get_change_queue(**_queue_kwargs(), context=context, session=session)
    rows = {item.review_id: item for item in page.items}
    assert rows[theirs.id].decide_blocker is None
    assert rows[theirs.id].approve_gate is None
    assert rows[theirs.id].review_family == "DESCRIPTION"
    assert rows[theirs.id].evidence_preview[0].claim.startswith("proposed_description:")
    assert len(rows[theirs.id].evidence_fingerprint) == 64
    assert rows[mine.id].decide_blocker == "MAKER_CHECKER"
    # Nothing composed for a glossary conflict: rejectable in a batch, not approvable.
    assert rows[bare.id].approve_gate == "EVIDENCE_NOT_SHOWN"

    details = await get_change_queue_details(
        review_id=[theirs.id, bare.id], context=context, session=session
    )
    assert [d.item.review_id for d in details.items] == [theirs.id, bare.id]
    assert details.items[0].evidence[0].category == "DESCRIPTION_DRAFT"


async def test_invalid_cursor_is_a_422(session: AsyncSession) -> None:
    estate = await build_estate(session)
    with pytest.raises(HTTPException) as refused:
        await get_change_queue(
            **_queue_kwargs(cursor="not-a-cursor"),
            context=reviewer(estate.organization_id),
            session=session,
        )
    assert refused.value.status_code == 422
    assert refused.value.detail == "INVALID_CURSOR"


async def test_fingerprint_moves_with_reviewed_content_and_nothing_else(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    [table] = await add_tables(session, estate, 1)
    [review] = await add_table_draft_reviews(session, estate, [table])
    before = (await compose_members(session, estate.organization_id, [review]))[review.id]

    # A pre-review recommendation is not the evidence under review.
    review.pre_review_recommendation = "APPROVE"
    review.pre_review_confidence = 0.99
    await session.flush()
    touched = (await compose_members(session, estate.organization_id, [review]))[review.id]
    assert touched.fingerprint == before.fingerprint

    draft = await session.get(AssetDescriptionDraft, UUID(review.object_id))
    assert draft is not None
    draft.drafted_text = "Edited after the reviewer looked."
    await session.flush()
    edited = (await compose_members(session, estate.organization_id, [review]))[review.id]
    assert edited.fingerprint != before.fingerprint


# ---------------------------------------------------------------------------
# Freezing: explicit ids and versions, across pages; exclusions
# ---------------------------------------------------------------------------


async def test_cross_page_selection_freezes_ids_and_the_versions_seen(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 30)
    await add_table_draft_reviews(session, estate, tables)
    context = reviewer(estate.organization_id)

    selected: list[ReviewBatchSelectionWrite] = []
    cursor: str | None = None
    while True:
        page = await get_change_queue(
            **_queue_kwargs(cursor=cursor, limit=10), context=context, session=session
        )
        # Take every other row on every page: a selection no single page contains.
        selected.extend(
            ReviewBatchSelectionWrite(
                review_id=item.review_id, evidence_fingerprint=item.evidence_fingerprint
            )
            for item in page.items[::2]
        )
        cursor = page.next_cursor
        if cursor is None:
            break

    batch = await create_review_batch(
        ReviewBatchCreate(items=selected), context=context, session=session
    )
    assert batch.item_count == 15
    assert batch.eligible_count == 15
    assert batch.excluded_count == 0
    assert batch.status == "FROZEN"
    members = await read_review_batch_items(
        batch.id, cursor=None, limit=100, outcome=None, eligibility=None,
        context=context, session=session,
    )
    assert [m.review_id for m in members.items] == [s.review_id for s in selected]
    assert [m.evidence_fingerprint for m in members.items] == [
        s.evidence_fingerprint for s in selected
    ]


async def test_freeze_excludes_unauthorized_changed_and_decided_members(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    other = await build_estate(session, name="Other")
    tables = await add_tables(session, estate, 4)
    fresh, changed, decided, delegated = await add_table_draft_reviews(session, estate, tables)
    [mine] = await add_table_draft_reviews(
        session, estate, await add_tables(session, estate, 1, prefix="mine"), requested_by=CHECKER
    )
    [foreign] = await add_table_draft_reviews(
        session, other, await add_tables(session, other, 1, prefix="foreign")
    )
    delegated.requested_by = "delegator@example.com"
    await session.flush()
    context = reviewer(estate.organization_id, delegator="delegator@example.com")

    page = await list_change_queue(
        session, context=context, filt=QueueFilter(status=None), cursor=None, limit=50
    )
    seen = {m.review.id: m.fingerprint for m in page.members}
    assert foreign.id not in seen  # another organization's review is not even listed

    # Between viewing and freezing: one draft is edited, one review is decided elsewhere.
    draft = await session.scalar(
        select(AssetDescriptionDraft).where(
            AssetDescriptionDraft.governance_review_id == changed.id
        )
    )
    assert draft is not None
    draft.drafted_text = "A different text than the one the reviewer read."
    decided.status = "APPROVED"
    await session.flush()

    batch = await freeze_review_batch(
        session,
        context=context,
        selections=[
            Selection(fresh.id, seen[fresh.id]),
            Selection(changed.id, seen[changed.id]),
            Selection(decided.id, seen[decided.id]),
            Selection(delegated.id, seen[delegated.id]),
            Selection(mine.id, seen[mine.id]),
            Selection(foreign.id, "0" * 64),
            Selection(uuid4(), None),
            Selection(fresh.id, seen[fresh.id]),  # duplicates collapse
        ],
        filt=None,
    )
    items = (
        await session.scalars(
            select(ReviewBatchItem)
            .where(ReviewBatchItem.batch_id == batch.id)
            .order_by(ReviewBatchItem.position)
        )
    ).all()
    assert [(i.eligibility, i.exclusion_code) for i in items] == [
        ("ELIGIBLE", None),
        ("EXCLUDED", "STALE_EVIDENCE"),
        ("EXCLUDED", "NOT_PENDING"),
        # PG-4: the delegate may not decide what the delegator proposed.
        ("EXCLUDED", "MAKER_CHECKER"),
        ("EXCLUDED", "MAKER_CHECKER"),
        ("EXCLUDED", "NOT_FOUND"),  # other organization: indistinguishable from absent
        ("EXCLUDED", "NOT_FOUND"),
    ]
    assert batch.item_count == 7
    assert batch.eligible_count == 1
    read = await read_review_batch(batch.id, context=context, session=session)
    assert read.exclusion_counts == {
        "STALE_EVIDENCE": 1,
        "NOT_PENDING": 1,
        "MAKER_CHECKER": 2,
        "NOT_FOUND": 2,
    }


async def test_freeze_by_filter_snapshots_and_reports_truncation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 12)
    reviews = await add_table_draft_reviews(session, estate, tables)
    monkeypatch.setattr(review_batches_module, "REVIEW_BATCH_MAX_ITEMS", 10)
    context = reviewer(estate.organization_id)

    batch = await create_review_batch(
        ReviewBatchCreate(filter=ReviewBatchFilterWrite(families=["DESCRIPTION"])),
        context=context,
        session=session,
    )
    assert batch.selection_mode == "FILTER"
    assert batch.selection_truncated is True
    assert batch.item_count == 10
    members = await read_review_batch_items(
        batch.id, cursor=None, limit=100, outcome=None, eligibility=None,
        context=context, session=session,
    )
    assert [m.review_id for m in members.items] == [r.id for r in reviews[:10]]


async def test_agents_cannot_freeze_or_decide_batches(session: AsyncSession) -> None:
    """This addition does not enable unattended approval: a non-human principal is refused
    before anything is read, whatever roles it holds."""
    estate = await build_estate(session)
    [review] = await add_table_draft_reviews(session, estate, await add_tables(session, estate, 1))
    human = reviewer(estate.organization_id)
    batch = await freeze_review_batch(
        session, context=human, selections=[Selection(review.id)], filt=None
    )
    agent = reviewer(estate.organization_id, "agent:reviewer", principal_type="AGENT")
    with pytest.raises(ReviewBatchError) as frozen:
        await freeze_review_batch(
            session, context=agent, selections=[Selection(review.id)], filt=None
        )
    assert frozen.value.code == "AGENT_PRINCIPAL_REFUSED"
    with pytest.raises(ReviewBatchError) as decided:
        await decide_review_batch(
            session, context=agent, batch_id=batch.id, decision="APPROVE", reason=None
        )
    assert decided.value.code == "AGENT_PRINCIPAL_REFUSED"
    assert review.status == "PENDING"


# ---------------------------------------------------------------------------
# Deciding: re-checks, partial outcomes, the shared decision path
# ---------------------------------------------------------------------------


async def test_decision_rechecks_every_member_and_reports_partial_outcomes(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    tables = await add_tables(session, estate, 4)
    ok_a, ok_b, edited, raced = await add_table_draft_reviews(session, estate, tables)
    wide = (await add_tables(session, estate, 1, prefix="wide"))[0]
    [column] = await add_columns(session, wide, 1)
    [moved_target] = await add_column_draft_reviews(session, estate, wide, [column])
    unshown = await add_bare_review(session, estate, "GLOSSARY_CONFLICT")
    await session.commit()
    context = reviewer(estate.organization_id)

    page = await get_change_queue(**_queue_kwargs(), context=context, session=session)
    batch = await create_review_batch(
        ReviewBatchCreate(
            items=[
                ReviewBatchSelectionWrite(
                    review_id=item.review_id, evidence_fingerprint=item.evidence_fingerprint
                )
                for item in page.items
            ]
        ),
        context=context,
        session=session,
    )
    assert batch.eligible_count == 6
    assert batch.approve_gate_counts == {"EVIDENCE_NOT_SHOWN": 1}

    # After the freeze: a draft is edited (changed member), another checker decides one
    # (concurrent decision), and the column's description is published underneath its
    # draft (a change the fingerprint cannot see -- the adapter's own gate refuses it).
    draft = await session.scalar(
        select(AssetDescriptionDraft).where(AssetDescriptionDraft.governance_review_id == edited.id)
    )
    assert draft is not None
    draft.drafted_text = "Changed after the batch was frozen."
    await session.commit()
    await decide_governance_review(
        raced.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        reviewer(estate.organization_id, OTHER_CHECKER),
        session,
    )
    await publish_column_description(
        session,
        organization_id=estate.organization_id,
        table_id=wide.id,
        column_id=column.id,
        description="Published by someone else in the meantime.",
        created_by=OTHER_CHECKER,
        approved_by=MAKER,
        approved_at=datetime.now(UTC),
    )
    await session.commit()

    result = await decide_frozen_review_batch(
        batch.id,
        ReviewBatchDecisionCreate(decision="APPROVE", reason="evidence read"),
        context=context,
        session=session,
    )
    by_review = {member.review_id: member for member in result.members}
    assert by_review[ok_a.id].outcome == "APPLIED"
    assert by_review[ok_b.id].outcome == "APPLIED"
    assert (by_review[edited.id].outcome, by_review[edited.id].reason_code) == (
        "REFUSED",
        "STALE_EVIDENCE",
    )
    assert (by_review[raced.id].outcome, by_review[raced.id].reason_code) == (
        "REFUSED",
        "ALREADY_DECIDED",
    )
    assert (by_review[moved_target.id].outcome, by_review[moved_target.id].reason_code) == (
        "REFUSED",
        "TARGET_REFUSED",
    )
    assert "changed after the draft was composed" in (by_review[moved_target.id].detail or "")
    assert (by_review[unshown.id].outcome, by_review[unshown.id].reason_code) == (
        "REFUSED",
        "EVIDENCE_NOT_SHOWN",
    )
    assert result.overall == "PARTIAL_SUCCESS"
    assert (result.applied_count, result.refused_count, result.skipped_count) == (2, 4, 0)
    assert result.batch.status == "DECIDED"
    assert result.batch.outcome_counts == {
        "APPLIED": 2,
        "REFUSED:STALE_EVIDENCE": 1,
        "REFUSED:ALREADY_DECIDED": 1,
        "REFUSED:TARGET_REFUSED": 1,
        "REFUSED:EVIDENCE_NOT_SHOWN": 1,
    }

    # Applied members went through the shared service: claimed, published, audited once.
    await session.refresh(ok_a)
    assert (ok_a.status, ok_a.decided_by, ok_a.decision_reason) == (
        "APPROVED",
        CHECKER,
        "evidence read",
    )
    # A refused member's savepoint left nothing behind: still pending, no outbox, no audit.
    await session.refresh(moved_target)
    assert moved_target.status == "PENDING"
    refused_outbox = await session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.aggregate_id == moved_target.object_id)
    )
    assert refused_outbox == 0
    batch_audits = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "governance.review.batch_decide")
        )
    ).all()
    assert sorted(a.resource_id for a in batch_audits) == sorted([str(ok_a.id), str(ok_b.id)])

    # The stored members agree with the response, codes only.
    stored = await read_review_batch_items(
        batch.id, cursor=None, limit=100, outcome="REFUSED", eligibility=None,
        context=context, session=session,
    )
    assert {m.reason_code for m in stored.items} == {
        "STALE_EVIDENCE",
        "ALREADY_DECIDED",
        "TARGET_REFUSED",
        "EVIDENCE_NOT_SHOWN",
    }


async def test_a_batch_is_decided_once_and_only_by_whoever_froze_it(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    reviews = await add_table_draft_reviews(session, estate, await add_tables(session, estate, 2))
    owner = reviewer(estate.organization_id)
    batch = await freeze_review_batch(
        session, context=owner, selections=[Selection(r.id) for r in reviews], filt=None
    )
    await session.commit()

    with pytest.raises(ReviewBatchError) as not_owned:
        await decide_review_batch(
            session,
            context=reviewer(estate.organization_id, OTHER_CHECKER),
            batch_id=batch.id,
            decision="APPROVE",
            reason=None,
        )
    assert (not_owned.value.code, not_owned.value.http_status) == ("REVIEW_BATCH_NOT_OWNED", 403)

    first = await decide_review_batch(
        session, context=owner, batch_id=batch.id, decision="APPROVE", reason=None
    )
    await session.commit()
    assert first.applied_count == 2
    with pytest.raises(ReviewBatchError) as again:
        await decide_review_batch(
            session, context=owner, batch_id=batch.id, decision="REJECT", reason="changed mind"
        )
    assert (again.value.code, again.value.http_status) == ("REVIEW_BATCH_ALREADY_DECIDED", 409)

    # A batch in another organization is simply not found.
    stranger = await build_estate(session, name="Stranger")
    with pytest.raises(ReviewBatchError) as foreign:
        await decide_review_batch(
            session,
            context=reviewer(stranger.organization_id),
            batch_id=batch.id,
            decision="APPROVE",
            reason=None,
        )
    assert foreign.value.code == "REVIEW_BATCH_NOT_FOUND"


async def test_rejection_requires_a_rationale_per_member(session: AsyncSession) -> None:
    estate = await build_estate(session)
    first, second = await add_table_draft_reviews(
        session, estate, await add_tables(session, estate, 2)
    )
    context = reviewer(estate.organization_id)
    batch = await freeze_review_batch(
        session, context=context, selections=[Selection(first.id), Selection(second.id)], filt=None
    )
    result = await decide_review_batch(
        session,
        context=context,
        batch_id=batch.id,
        decision="REJECT",
        reason=None,
        rationale_by_review_id={first.id: "cites a column the table does not have"},
    )
    outcomes = {o.item.review_id: (o.outcome, o.reason_code) for o in result.outcomes}
    assert outcomes == {
        first.id: ("APPLIED", None),
        second.id: ("REFUSED", "RATIONALE_REQUIRED"),
    }
    await session.refresh(first)
    assert (first.status, first.decision_reason) == (
        "REJECTED",
        "cites a column the table does not have",
    )
    # A rejection publishes nothing, so there is nothing to withdraw.
    assert result.outcomes[0].correction.kind == "REPROPOSE"
    assert result.outcomes[0].correction.available is False


async def test_approve_gate_holds_t3_changes_to_individual_decisions() -> None:
    """No composed evidence exists yet for any trust-boundary type, so the second arm of the
    gate is pinned directly: even with evidence shown, a T3 change is never batch-approved."""
    review = GovernanceReview(
        id=uuid4(),
        organization_id=uuid4(),
        object_type="CROSS_BOUNDARY_GRANT",
        object_id=str(uuid4()),
        requested_action="GRANT",
        status="PENDING",
        requested_by=MAKER,
    )
    shown = ComposedMember(
        review=review,
        proposal=None,
        supplement=[EvidenceItemRead(category="X", claim="y", source="z")],
        fingerprint="0" * 64,
    )
    assert approve_gate(shown) == "EVIDENCE_NOT_SHOWN"  # no proposal composed at all
    composed = await _compose_nothing_for(review)
    assert approve_gate(composed) == "INDIVIDUAL_DECISION_REQUIRED"


async def _compose_nothing_for(review: GovernanceReview) -> ComposedMember:
    """A member with a composed (empty) proposal plus shown evidence, built without a DB."""
    proposal = ReviewQueueProposalRead(
        review_id=review.id,
        organization_id=review.organization_id,
        object_type=review.object_type,
        object_id=review.object_id,
        requested_action=review.requested_action,
        status=review.status,
        requested_by=review.requested_by,
        decided_by=None,
        decision_reason=None,
        decided_at=None,
        created_at=datetime.now(UTC),
        evidence=[EvidenceItemRead(category="GRANT", claim="domain A -> domain B", source="s")],
        diff=GovernanceReviewDiffRead(
            review_id=review.id,
            object_type=review.object_type,
            object_id=review.object_id,
            diffable=False,
        ),
    )
    return ComposedMember(review=review, proposal=proposal, supplement=[], fingerprint="0" * 64)


# ---------------------------------------------------------------------------
# Corrections: the applied member's governed undo, end to end
# ---------------------------------------------------------------------------


async def test_correction_journey_withdraws_a_batch_approved_description(
    session: AsyncSession,
) -> None:
    estate = await build_estate(session)
    [table] = await add_tables(session, estate, 1)
    [review] = await add_table_draft_reviews(session, estate, [table])
    context = reviewer(estate.organization_id)
    batch = await freeze_review_batch(
        session, context=context, selections=[Selection(review.id)], filt=None
    )
    result = await decide_review_batch(
        session, context=context, batch_id=batch.id, decision="APPROVE", reason=None
    )
    await session.commit()
    [applied] = result.outcomes
    correction = applied.correction
    assert (correction.kind, correction.available) == ("WITHDRAW_DESCRIPTION", True)
    assert (correction.method, correction.path) == ("POST", "/v1/descriptions/withdrawals")
    assert (correction.subject_type, correction.subject_id) == ("TABLE", str(table.id))
    draft = await session.get(AssetDescriptionDraft, UUID(review.object_id))
    assert draft is not None and draft.published_version_id is not None
    published = await session.get(AssetDocumentationVersion, draft.published_version_id)
    assert published is not None and published.status == "APPROVED"

    # Follow the link: a steward requests the withdrawal it names...
    withdrawal, withdrawal_review = await request_description_withdrawal(
        session,
        organization_id=estate.organization_id,
        subject_type=correction.subject_type,
        subject_id=UUID(correction.subject_id or ""),
        reason="approved in a batch; the text overstates the grain",
        requested_by=MAKER,
    )
    await session.commit()
    # ...which is itself reviewed, and is a queue row a batch may reject but not approve.
    queue = await list_change_queue(
        session,
        context=context,
        filt=QueueFilter(object_types=("DESCRIPTION_WITHDRAWAL",)),
        cursor=None,
        limit=10,
    )
    [row] = queue.members
    assert row.review.id == withdrawal_review.id
    assert review_family_for(row.review.object_type) == "DESCRIPTION"
    assert approve_gate(row) == "EVIDENCE_NOT_SHOWN"

    # A different checker decides it on the ordinary single-review path.
    await decide_governance_review(
        withdrawal_review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        reviewer(estate.organization_id, OTHER_CHECKER),
        session,
    )
    await session.refresh(published)
    assert published.status == WITHDRAWN
    assert withdrawal.version_id == published.id

    # The stored member still names its correction after the fact.
    stored = await read_review_batch_items(
        batch.id, cursor=None, limit=10, outcome=None, eligibility=None,
        context=context, session=session,
    )
    assert stored.items[0].correction.kind == "WITHDRAW_DESCRIPTION"
    assert stored.items[0].correction.subject_id == str(table.id)


async def test_bulk_operation_corrections_are_named_but_not_implied(
    session: AsyncSession,
) -> None:
    reversal = correction_for(
        "BULK_STEWARDSHIP_OPERATION", "APPROVE", "BULK_STEWARDSHIP_OPERATION", "op"
    )
    assert (reversal.kind, reversal.available, reversal.reason_code) == (
        "REVERSE_BULK_OPERATION",
        False,
        "NO_HUMAN_REVERSAL_ROUTE",
    )
    other = correction_for("TERM_SEMANTIC_BINDING", "APPROVE", None, None)
    assert (other.kind, other.available) == ("NONE_DEFINED", False)


# ---------------------------------------------------------------------------
# What must not change, and what must not be stored
# ---------------------------------------------------------------------------


async def test_value_free_rows_hold_codes_ids_and_hashes_only(session: AsyncSession) -> None:
    estate = await build_estate(session)
    reviews = await add_table_draft_reviews(session, estate, await add_tables(session, estate, 3))
    context = reviewer(estate.organization_id)
    batch = await freeze_review_batch(
        session, context=context, selections=[Selection(r.id) for r in reviews], filt=None
    )
    await decide_review_batch(
        session,
        context=context,
        batch_id=batch.id,
        decision="REJECT",
        reason="a sentence that must only land on governance_review.decision_reason",
    )
    await session.flush()
    drafts = (await session.scalars(select(AssetDescriptionDraft))).all()
    texts = {d.drafted_text for d in drafts} | {
        "a sentence that must only land on governance_review.decision_reason"
    }
    rows = [
        *(await session.scalars(select(ReviewBatch))).all(),
        *(await session.scalars(select(ReviewBatchItem))).all(),
    ]
    stored = repr([{c.key: getattr(r, c.key) for c in r.__table__.columns} for r in rows])
    for text in texts:
        assert text not in stored
    batch_audits = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.resource_type == "review_batch")
        )
    ).all()
    for audit in batch_audits:
        for text in texts:
            assert text not in repr(audit.details)


async def test_existing_queue_composition_is_untouched(session: AsyncSession) -> None:
    """The change queue composes through the shared read model; the embedded evidence is the
    very list `GET /v1/governance/reviews/queue` returns for the same review."""
    estate = await build_estate(session)
    [review] = await add_table_draft_reviews(session, estate, await add_tables(session, estate, 1))
    [shared] = await compose_review_queue(session, [review])
    member = (await compose_members(session, estate.organization_id, [review]))[review.id]
    assert member.proposal == shared
    assert member.evidence == shared.evidence


async def test_column_drafts_in_a_wide_table_are_one_scope(session: AsyncSession) -> None:
    estate = await build_estate(session)
    [wide] = await add_tables(session, estate, 1)
    columns = await add_columns(session, wide, 12)
    reviews = await add_column_draft_reviews(session, estate, wide, columns)
    context = reviewer(estate.organization_id)
    batch = await create_review_batch(
        ReviewBatchCreate(filter=ReviewBatchFilterWrite(table_id=wide.id)),
        context=context,
        session=session,
    )
    assert batch.eligible_count == 12
    result = await decide_frozen_review_batch(
        batch.id,
        ReviewBatchDecisionCreate(decision="APPROVE"),
        context=context,
        session=session,
    )
    assert result.overall == "SUCCESS"
    assert {m.correction.subject_type for m in result.members} == {"COLUMN"}
    assert {m.correction.subject_id for m in result.members} == {str(c.id) for c in columns}
    approved = await session.scalar(
        select(func.count())
        .select_from(ColumnDescriptionDraft)
        .where(ColumnDescriptionDraft.status == "APPROVED")
    )
    assert approved == len(reviews)


async def test_a_claim_lost_after_the_recheck_is_a_concurrent_decision(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The narrow race: the member passes every re-check (still PENDING in this session's
    view, same fingerprint), and another checker's commit lands before the claim. It is the
    shared service's compare-and-set that refuses it -- the batch path adds no check of its
    own that could be raced -- and the member's savepoint leaves nothing behind."""
    from sqlalchemy import update

    from aida.governance_decision_service import decide_review as real_decide_review

    estate = await build_estate(session)
    first, second = await add_table_draft_reviews(
        session, estate, await add_tables(session, estate, 2)
    )
    context = reviewer(estate.organization_id)
    batch = await freeze_review_batch(
        session, context=context, selections=[Selection(first.id), Selection(second.id)], filt=None
    )

    async def racing_decide_review(session_, review, **kwargs):  # type: ignore[no-untyped-def]
        if review.id == second.id:
            await session_.execute(
                update(GovernanceReview)
                .where(GovernanceReview.id == review.id)
                .values(status="APPROVED", decided_by=OTHER_CHECKER)
                .execution_options(synchronize_session=False)
            )
        return await real_decide_review(session_, review, **kwargs)

    monkeypatch.setattr(review_batches_module, "decide_review", racing_decide_review)
    result = await decide_review_batch(
        session, context=context, batch_id=batch.id, decision="APPROVE", reason=None
    )
    outcomes = {o.item.review_id: (o.outcome, o.reason_code) for o in result.outcomes}
    assert outcomes == {
        first.id: ("APPLIED", None),
        second.id: ("REFUSED", "CONCURRENT_DECISION"),
    }
    draft = await session.scalar(
        select(AssetDescriptionDraft).where(
            AssetDescriptionDraft.governance_review_id == second.id
        )
    )
    assert draft is not None and draft.status == "PENDING_APPROVAL"
