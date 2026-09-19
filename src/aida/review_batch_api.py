"""R11-REV01: the change-focused review queue and frozen review batches.

    GET  /v1/governance/reviews/change-queue            filtered keyset page of the queue
    GET  /v1/governance/reviews/change-queue/details    full evidence for rows opened
    POST /v1/governance/review-batches                  freeze a selection (ids + versions)
    GET  /v1/governance/review-batches/{id}             the batch and its preview counts
    GET  /v1/governance/review-batches/{id}/items       its members, paged
    POST /v1/governance/review-batches/{id}/decision    decide it: re-check, apply, report

The decision route adds no decision path: every member is decided by
`governance_decision_service.decide_review`, the same maker-checker guard and
compare-and-set claim `POST /v1/governance/reviews/{id}/decision` and the PG-3 bulk endpoint
use. See `aida.review_batches` for the re-checks and reason codes.

Roles are the existing ones. Reading the queue is the reviewer-facing read population of
`GET /v1/governance/reviews/queue`; freezing and deciding is the population
`POST /v1/governance/reviews/bulk-decision` admits, including a delegated role (PG-4), whose
delegator is then held to maker-checker by the decision service.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.db import get_session
from aida.review_batch_models import ReviewBatch, ReviewBatchItem
from aida.review_batch_schemas import (
    ChangeQueueDetailRead,
    ChangeQueueDetailsRead,
    ChangeQueueFilterRead,
    ChangeQueueItemRead,
    ChangeQueuePageRead,
    ReviewBatchCorrectionRead,
    ReviewBatchCreate,
    ReviewBatchDecisionCreate,
    ReviewBatchDecisionMemberRead,
    ReviewBatchDecisionRead,
    ReviewBatchItemPageRead,
    ReviewBatchItemRead,
    ReviewBatchRead,
)
from aida.review_batches import (
    BATCH_ITEMS_PAGE_MAX,
    DETAIL_MAX_IDS,
    QUEUE_PAGE_DEFAULT,
    QUEUE_PAGE_MAX,
    ComposedMember,
    Correction,
    QueueFilter,
    ReviewBatchError,
    Selection,
    approve_gate,
    decide_blocker,
    decide_review_batch,
    freeze_review_batch,
    get_review_batch,
    http_error,
    list_change_queue,
    list_review_batch_items,
    load_queue_details,
    outcome_correction,
    preview_evidence,
    review_batch_counts,
    review_family_for,
)
from aida.review_risk_tiers import risk_tier_for
from aida.security import SecurityContext, require_roles, require_roles_or_delegated

router = APIRouter(prefix="/v1", tags=["governance-review-batches"])

#: Same population as `review_queue_api._REVIEW_QUEUE_READ_ROLES`.
_QUEUE_READ_ROLES = ("PlatformAdmin", "SemanticAdmin", "DataSteward", "Reviewer")
#: Same population as `semantic_api.bulk_decide_governance_reviews`.
_DECIDE_ROLES = ("PlatformAdmin", "DataSteward", "Reviewer")


def _item_read(member: ComposedMember, context: SecurityContext) -> ChangeQueueItemRead:
    review = member.review
    proposal = member.proposal
    return ChangeQueueItemRead(
        review_id=review.id,
        object_type=review.object_type,
        object_id=review.object_id,
        review_family=review_family_for(review.object_type),
        change_kind=review.requested_action,
        status=review.status,
        requested_by=review.requested_by,
        created_at=review.created_at,
        risk_tier=review.risk_tier or risk_tier_for(review.object_type),
        confidence=proposal.confidence if proposal is not None else None,
        diffable=bool(proposal is not None and proposal.diff.diffable),
        evidence_count=len(member.evidence),
        evidence_preview=preview_evidence(member),
        evidence_fingerprint=member.fingerprint,
        decide_blocker=decide_blocker(member, context),
        approve_gate=approve_gate(member),
        target_unavailable=member.target_unavailable,
    )


def _correction_read(correction: Correction) -> ReviewBatchCorrectionRead:
    return ReviewBatchCorrectionRead(
        kind=correction.kind,
        available=correction.available,
        method=correction.method,
        path=correction.path,
        subject_type=correction.subject_type,
        subject_id=correction.subject_id,
        reason_code=correction.reason_code,
    )


def _batch_item_read(item: ReviewBatchItem, decision: str | None) -> ReviewBatchItemRead:
    return ReviewBatchItemRead(
        review_id=item.review_id,
        position=item.position,
        object_type=item.object_type,
        review_family=item.review_family,
        frozen_status=item.frozen_status,
        evidence_fingerprint=item.evidence_fingerprint,
        eligibility=item.eligibility,
        exclusion_code=item.exclusion_code,
        approve_gate_code=item.approve_gate_code,
        outcome=item.outcome,
        reason_code=item.reason_code,
        decided_at=item.decided_at,
        correction=_correction_read(outcome_correction(item, decision)),
    )


async def _batch_read(session: AsyncSession, batch: ReviewBatch) -> ReviewBatchRead:
    counts = await review_batch_counts(session, batch)
    return ReviewBatchRead(
        id=batch.id,
        organization_id=batch.organization_id,
        created_by=batch.created_by,
        selection_mode=batch.selection_mode,
        selection_truncated=batch.selection_truncated,
        status=batch.status,
        item_count=batch.item_count,
        eligible_count=batch.eligible_count,
        selection_fingerprint=batch.selection_fingerprint,
        decision=batch.decision,
        decided_at=batch.decided_at,
        created_at=batch.created_at,
        exclusion_counts=counts.exclusion_counts,
        approve_gate_counts=counts.approve_gate_counts,
        outcome_counts=counts.outcome_counts,
    )


@router.get("/governance/reviews/change-queue", response_model=ChangeQueuePageRead)
async def get_change_queue(
    review_status: str | None = Query(default="PENDING", alias="status", max_length=30),
    object_type: list[str] = Query(default=[], max_length=50),
    family: list[str] = Query(default=[], max_length=10),
    change_kind: list[str] = Query(default=[], max_length=50),
    object_id: str | None = Query(default=None, max_length=100),
    table_id: UUID | None = Query(default=None),
    decidable_only: bool = Query(default=False),
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=QUEUE_PAGE_DEFAULT, ge=1, le=QUEUE_PAGE_MAX),
    include_total: bool = Query(default=True),
    context: SecurityContext = Depends(require_roles(*_QUEUE_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ChangeQueuePageRead:
    """One keyset page of the governance review queue, narrowed by change kind, object type,
    review family, object id or table scope, each row carrying the evidence fingerprint a
    batch selection binds to. Pass `status=` (empty) for every status.

    `table_id` scopes to the description drafts of one table and its columns -- the
    1,000-column-table case -- resolved in two statements, whatever the table's width.
    """
    filt = QueueFilter(
        status=review_status or None,
        object_types=tuple(object_type),
        families=tuple(family),
        change_kinds=tuple(change_kind),
        object_id=object_id,
        table_id=table_id,
        decidable_only=decidable_only,
    )
    try:
        page = await list_change_queue(
            session,
            context=context,
            filt=filt,
            cursor=cursor,
            limit=limit,
            include_total=include_total,
        )
    except ReviewBatchError as error:
        raise http_error(error) from error
    return ChangeQueuePageRead(
        organization_id=page.organization_id,
        filters=ChangeQueueFilterRead(
            status=filt.status.upper() if filt.status else None,
            object_types=[value.upper() for value in filt.object_types],
            families=[value.upper() for value in filt.families],
            change_kinds=[value.upper() for value in filt.change_kinds],
            object_id=filt.object_id,
            table_id=filt.table_id,
            decidable_only=filt.decidable_only,
        ),
        generated_at=datetime.now(UTC),
        limit=page.limit,
        next_cursor=page.next_cursor,
        total=page.total,
        items=[_item_read(member, context) for member in page.members],
    )


@router.get("/governance/reviews/change-queue/details", response_model=ChangeQueueDetailsRead)
async def get_change_queue_details(
    review_id: list[UUID] = Query(min_length=1, max_length=DETAIL_MAX_IDS),
    context: SecurityContext = Depends(require_roles(*_QUEUE_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ChangeQueueDetailsRead:
    """Full evidence and diff for the rows a reviewer opened -- loaded on demand, at most
    `DETAIL_MAX_IDS` at a time, so the queue page itself stays small."""
    try:
        members = await load_queue_details(session, context=context, review_ids=review_id)
    except ReviewBatchError as error:
        raise http_error(error) from error
    return ChangeQueueDetailsRead(
        items=[
            ChangeQueueDetailRead(
                item=_item_read(member, context),
                evidence=member.evidence,
                diff=member.proposal.diff if member.proposal is not None else None,
            )
            for member in members
        ]
    )


@router.post(
    "/governance/review-batches",
    response_model=ReviewBatchRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_review_batch(
    body: ReviewBatchCreate,
    context: SecurityContext = Depends(require_roles_or_delegated(*_DECIDE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ReviewBatchRead:
    """Freeze a selection: explicit review ids (accumulated across queue pages, optionally
    with the fingerprints the reviewer saw) or a capped snapshot of a filter. Nothing is
    decided. The response previews how many members are eligible and why the rest are not.
    """
    selections = (
        [Selection(item.review_id, item.evidence_fingerprint) for item in body.items]
        if body.items is not None
        else None
    )
    filt = (
        QueueFilter(
            status=body.filter.status or None,
            object_types=tuple(body.filter.object_types),
            families=tuple(body.filter.families),
            change_kinds=tuple(body.filter.change_kinds),
            object_id=body.filter.object_id,
            table_id=body.filter.table_id,
            decidable_only=body.filter.decidable_only,
        )
        if body.filter is not None
        else None
    )
    try:
        batch = await freeze_review_batch(
            session, context=context, selections=selections, filt=filt
        )
    except ReviewBatchError as error:
        raise http_error(error) from error
    read = await _batch_read(session, batch)
    await session.commit()
    return read


@router.get("/governance/review-batches/{batch_id}", response_model=ReviewBatchRead)
async def read_review_batch(
    batch_id: UUID,
    context: SecurityContext = Depends(require_roles(*_QUEUE_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ReviewBatchRead:
    try:
        batch = await get_review_batch(session, context=context, batch_id=batch_id)
    except ReviewBatchError as error:
        raise http_error(error) from error
    return await _batch_read(session, batch)


@router.get("/governance/review-batches/{batch_id}/items", response_model=ReviewBatchItemPageRead)
async def read_review_batch_items(
    batch_id: UUID,
    cursor: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=100, ge=1, le=BATCH_ITEMS_PAGE_MAX),
    outcome: str | None = Query(default=None, max_length=20),
    eligibility: str | None = Query(default=None, max_length=20),
    context: SecurityContext = Depends(require_roles(*_QUEUE_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ReviewBatchItemPageRead:
    """Every member, in selection order, with its frozen version, eligibility, outcome and
    correction -- so a reviewer can inspect each one before and after deciding."""
    try:
        batch = await get_review_batch(session, context=context, batch_id=batch_id)
        page = await list_review_batch_items(
            session,
            context=context,
            batch_id=batch_id,
            cursor=cursor,
            limit=limit,
            outcome=outcome,
            eligibility=eligibility,
        )
    except ReviewBatchError as error:
        raise http_error(error) from error
    return ReviewBatchItemPageRead(
        batch_id=batch.id,
        next_cursor=page.next_cursor,
        items=[_batch_item_read(item, batch.decision) for item in page.items],
    )


@router.post(
    "/governance/review-batches/{batch_id}/decision",
    response_model=ReviewBatchDecisionRead,
)
async def decide_frozen_review_batch(
    batch_id: UUID,
    body: ReviewBatchDecisionCreate,
    context: SecurityContext = Depends(require_roles_or_delegated(*_DECIDE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ReviewBatchDecisionRead:
    """Decide a frozen batch once. Every eligible member is re-checked against the version it
    was frozen at and then decided through the shared decision service; the response reports
    each member's outcome, reason code and correction. Partial success is the normal case,
    not an error: a member another checker decided first is `ALREADY_DECIDED`, one whose
    evidence moved is `STALE_EVIDENCE`, and the rest still apply.
    """
    try:
        result = await decide_review_batch(
            session,
            context=context,
            batch_id=batch_id,
            decision=body.decision,
            reason=body.reason,
            rationale_by_review_id=body.rationale_by_review_id,
        )
    except ReviewBatchError as error:
        raise http_error(error) from error
    batch_read = await _batch_read(session, result.batch)
    members = [
        ReviewBatchDecisionMemberRead(
            **_batch_item_read(outcome.item, body.decision).model_dump(exclude={"correction"}),
            correction=_correction_read(outcome.correction),
            detail=outcome.detail,
        )
        for outcome in result.outcomes
    ]
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="REVIEW_BATCH_DECISION_CONFLICT") from exc
    return ReviewBatchDecisionRead(
        batch=batch_read,
        overall=result.overall,
        applied_count=result.applied_count,
        refused_count=result.refused_count,
        skipped_count=result.skipped_count,
        members=members,
    )
