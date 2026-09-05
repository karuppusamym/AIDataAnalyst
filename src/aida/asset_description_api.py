"""GL-9: draft table descriptions from catalog evidence, reviewed through the
shared `governance_review` queue. See `aida.asset_description_service` for
the deterministic evidence gathering, scoring, and composition; this module
only exposes it as a tenant-scoped, audited API.
"""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import (
    MINIMUM_EVIDENCE_FOR_REVIEW,
    compose_draft_text,
    ensure_reviewable,
    evidence_payload,
    gather_evidence,
    score_evidence,
    text_fingerprint,
)
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import AssetDescriptionDraft, GovernanceReview, MetadataTable
from aida.sampling_review import (
    MAX_SEED,
    draw_reproducible_sample,
    generate_seed,
    resolve_sample_size,
)
from aida.schemas import (
    GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS,
    AssetDescriptionDraftGenerate,
    AssetDescriptionDraftRead,
    GovernanceReviewRead,
    Page,
)
from aida.security import (
    SecurityContext,
    enforce_organization,
    require_roles,
    require_roles_or_delegated,
)
from aida.semantic_api import _apply_governance_review_decision

router = APIRouter(prefix="/v1", tags=["asset-description-drafting"])

READ_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "SemanticAdmin",
    "DataSteward",
    "Reviewer",
    "Analyst",
    "Viewer",
    "Auditor",
)
WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "SemanticAdmin", "DataSteward")

_GENERATE_BATCH_LIMIT = 100

# AT-14: a sampling-review batch is a set of `GovernanceReview` ids under the
# hood (one per PENDING_APPROVAL draft) -- reuse PG-3's own bulk-decision
# cap rather than inventing a second, unrelated bound for the same
# underlying resource.
SAMPLE_REVIEW_BATCH_MAX_ITEMS = GOVERNANCE_REVIEW_BULK_DECISION_MAX_ITEMS


class _ApiModel(BaseModel):
    """Local request/response-model base, same shape as
    `stewardship_api.ApiModel` -- these sampling-review models compose two
    other modules' pure functions (`aida.sampling_review`) with this one's
    own persisted rows and have no natural home in `aida.schemas`."""

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class AssetDescriptionSampleDraw(_ApiModel):
    """AT-14: draw a reproducible sample from a batch of PENDING_APPROVAL
    drafts without deciding anything -- so a steward can see exactly which
    drafts a sample-based decision would cover, and read their text, before
    committing to one. Safe to call repeatedly with the same seed (e.g. to
    recall a prior draw): it is a preview, and mutates no draft."""

    draft_ids: list[UUID] = Field(min_length=1, max_length=SAMPLE_REVIEW_BATCH_MAX_ITEMS)
    sample_size: int | None = Field(default=None, ge=1)
    sample_fraction: float | None = Field(default=None, gt=0.0, le=1.0)
    seed: int | None = Field(default=None, ge=0, le=MAX_SEED)

    @model_validator(mode="after")
    def _validate(self) -> "AssetDescriptionSampleDraw":
        if len(set(self.draft_ids)) != len(self.draft_ids):
            raise ValueError("draft_ids must be unique")
        if (self.sample_size is None) == (self.sample_fraction is None):
            raise ValueError("exactly one of sample_size or sample_fraction is required")
        return self


class AssetDescriptionSampleDrawRead(_ApiModel):
    seed: int
    batch_size: int
    sample_size: int
    drawn_draft_ids: list[UUID]
    drawn_drafts: list[AssetDescriptionDraftRead]


class AssetDescriptionSampleDecide(_ApiModel):
    """AT-14: apply ONE accept/reject decision to the reproducibly-drawn
    SAMPLE of a batch -- never to the whole batch. `seed` is required (not
    generated here): a controlled review cites a specific, already-known
    seed -- typically the one an earlier `AssetDescriptionSampleDraw` call
    returned -- so the sample this decision is actually applied to is
    guaranteed to be the one the steward read, by recomputing it from the
    same (batch, sample_size, seed) inputs server-side rather than trusting
    a caller-supplied id list."""

    draft_ids: list[UUID] = Field(min_length=1, max_length=SAMPLE_REVIEW_BATCH_MAX_ITEMS)
    sample_size: int | None = Field(default=None, ge=1)
    sample_fraction: float | None = Field(default=None, gt=0.0, le=1.0)
    seed: int = Field(ge=0, le=MAX_SEED)
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate(self) -> "AssetDescriptionSampleDecide":
        if len(set(self.draft_ids)) != len(self.draft_ids):
            raise ValueError("draft_ids must be unique")
        if (self.sample_size is None) == (self.sample_fraction is None):
            raise ValueError("exactly one of sample_size or sample_fraction is required")
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a sample review")
        return self


class AssetDescriptionSampleItemRead(_ApiModel):
    draft_id: UUID
    status: Literal["SUCCEEDED", "FAILED"]
    reason: str | None = None


class AssetDescriptionSampleDecisionResultRead(_ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    seed: int
    batch_size: int
    sample_size: int
    drawn_draft_ids: list[UUID]
    unsampled_draft_ids: list[UUID]
    succeeded_count: int
    failed_count: int
    results: list[AssetDescriptionSampleItemRead]


async def _load_pending_batch(
    session: AsyncSession, *, organization_id: UUID, draft_ids: list[UUID]
) -> list[AssetDescriptionDraft]:
    """Fetch every draft named by `draft_ids`, requiring ALL of them to be a
    PENDING_APPROVAL draft in this organization -- the sampling batch is
    exactly the pending items named, nothing more and nothing silently
    dropped. Order matches `draft_ids` so the caller's batch composition is
    preserved for hashing/echoing back."""
    rows = {
        row.id: row
        for row in (
            await session.scalars(
                select(AssetDescriptionDraft).where(
                    AssetDescriptionDraft.organization_id == organization_id,
                    AssetDescriptionDraft.id.in_(draft_ids),
                )
            )
        ).all()
    }
    missing_or_not_pending = [
        draft_id
        for draft_id in draft_ids
        if draft_id not in rows or rows[draft_id].status != "PENDING_APPROVAL"
    ]
    if missing_or_not_pending:
        raise HTTPException(
            status_code=409,
            detail=(
                "one or more asset description drafts in the batch are not currently pending review"
            ),
        )
    return [rows[draft_id] for draft_id in draft_ids]


def _draft_read(draft: AssetDescriptionDraft, table_name: str) -> AssetDescriptionDraftRead:
    return AssetDescriptionDraftRead(
        id=draft.id,
        organization_id=draft.organization_id,
        table_id=draft.table_id,
        table_name=table_name,
        drafted_text=draft.drafted_text,
        accuracy_score=draft.accuracy_score,
        clarity_score=draft.clarity_score,
        style_score=draft.style_score,
        completeness_score=draft.completeness_score,
        overall_score=draft.overall_score,
        evidence=draft.evidence,
        status=draft.status,
        governance_review_id=draft.governance_review_id,
        published_version_id=draft.published_version_id,
        created_by=draft.created_by,
        reviewed_by=draft.reviewed_by,
        reviewed_at=draft.reviewed_at,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


@router.post(
    "/organizations/{organization_id}/asset-description-drafts/generate",
    response_model=Page,
)
async def generate_asset_description_drafts(
    organization_id: UUID,
    body: AssetDescriptionDraftGenerate,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    table_ids = body.table_ids[:_GENERATE_BATCH_LIMIT]
    tables = (
        await session.scalars(
            select(MetadataTable).where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.id.in_(table_ids),
                MetadataTable.status == "ACTIVE",
            )
        )
    ).all()
    open_table_ids = set(
        await session.scalars(
            select(AssetDescriptionDraft.table_id).where(
                AssetDescriptionDraft.organization_id == organization_id,
                AssetDescriptionDraft.table_id.in_(table_ids),
                AssetDescriptionDraft.status.in_(("DRAFT", "PENDING_APPROVAL")),
            )
        )
    )
    created: list[tuple[AssetDescriptionDraft, str]] = []
    skipped_open = 0
    skipped_duplicate = 0
    for table in tables:
        if table.id in open_table_ids:
            skipped_open += 1
            continue
        evidence = await gather_evidence(session, table)
        drafted_text = compose_draft_text(evidence)
        fingerprint = text_fingerprint(drafted_text)
        duplicate_rejected = await session.scalar(
            select(AssetDescriptionDraft.id).where(
                AssetDescriptionDraft.table_id == table.id,
                AssetDescriptionDraft.status == "REJECTED",
                AssetDescriptionDraft.text_fingerprint == fingerprint,
            )
        )
        if duplicate_rejected is not None:
            skipped_duplicate += 1
            continue
        scores = score_evidence(evidence)
        draft = AssetDescriptionDraft(
            organization_id=organization_id,
            table_id=table.id,
            drafted_text=drafted_text,
            text_fingerprint=fingerprint,
            accuracy_score=scores.accuracy,
            clarity_score=scores.clarity,
            style_score=scores.style,
            completeness_score=scores.completeness,
            overall_score=scores.overall,
            evidence=evidence_payload(evidence),
            created_by=context.principal_id,
        )
        session.add(draft)
        created.append((draft, table.name))
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="asset_description.draft.generate",
        resource_type="asset_description_draft",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "requested": len(table_ids),
            "drafts_created": len(created),
            "skipped_open": skipped_open,
            "skipped_duplicate_rejected": skipped_duplicate,
        },
    )
    await session.commit()
    return Page(
        items=[_draft_read(draft, table_name) for draft, table_name in created],
        limit=_GENERATE_BATCH_LIMIT,
        offset=0,
        total=len(created),
    )


@router.get(
    "/organizations/{organization_id}/asset-description-drafts",
    response_model=Page,
)
async def list_asset_description_drafts(
    organization_id: UUID,
    draft_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = [AssetDescriptionDraft.organization_id == organization_id]
    if draft_status:
        filters.append(AssetDescriptionDraft.status == draft_status.upper())
    base = (
        select(AssetDescriptionDraft, MetadataTable.name)
        .join(MetadataTable, MetadataTable.id == AssetDescriptionDraft.table_id)
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    # Confidence sets review priority: highest-evidence drafts sort first so
    # a reviewer's queue surfaces the most defensible work first. This is
    # the *only* effect of the score on review order — it never bypasses it.
    rows = (
        await session.execute(
            base.order_by(
                AssetDescriptionDraft.overall_score.desc(),
                AssetDescriptionDraft.created_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[_draft_read(draft, table_name) for draft, table_name in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


class DescriptionDraftEdit(BaseModel):
    drafted_text: str = Field(min_length=10, max_length=50_000)
    expected_text: str = Field(max_length=50_000)


@router.put("/asset-description-drafts/{draft_id}", response_model=AssetDescriptionDraftRead)
async def edit_asset_description_draft(
    draft_id: UUID,
    body: DescriptionDraftEdit,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> AssetDescriptionDraftRead:
    draft = await session.scalar(
        select(AssetDescriptionDraft)
        .where(
            AssetDescriptionDraft.id == draft_id,
        )
        .with_for_update()
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="asset description draft not found")
    enforce_organization(context, draft.organization_id)
    if draft.status != "DRAFT" or draft.drafted_text != body.expected_text:
        raise HTTPException(
            status_code=409, detail="Draft changed or is already in review; reload before editing"
        )
    draft.evidence = {
        **draft.evidence,
        "origin": "METADATA_WITH_HUMAN_EDITS",
        "original_fingerprint": draft.evidence.get("original_fingerprint", draft.text_fingerprint),
        "edited_by": context.principal_id,
    }
    draft.drafted_text = body.drafted_text
    draft.text_fingerprint = text_fingerprint(body.drafted_text)
    record_audit(
        session,
        context,
        action="asset_description.draft.edit",
        resource_type="asset_description_draft",
        resource_id=str(draft.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"fingerprint": draft.text_fingerprint},
    )
    table = await session.get(MetadataTable, draft.table_id)
    await session.commit()
    return _draft_read(draft, table.name if table else str(draft.table_id))


@router.post(
    "/asset-description-drafts/{draft_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_asset_description_draft(
    draft_id: UUID,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    draft = await session.get(AssetDescriptionDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="asset description draft not found")
    enforce_organization(context, draft.organization_id)
    if draft.status == "PENDING_APPROVAL":
        existing = await session.scalar(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "ASSET_DESCRIPTION_DRAFT",
                GovernanceReview.object_id == str(draft.id),
                GovernanceReview.status == "PENDING",
            )
        )
        if existing is not None:
            return existing
    if draft.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only a draft can be submitted for review")
    # The minimum-evidence gate: this is what keeps a near-empty draft from
    # ever reaching a state that could be mistaken for a published
    # description. It is enforced here on the deterministic score alone.
    ensure_reviewable(draft.overall_score)
    draft.status = "PENDING_APPROVAL"
    review = GovernanceReview(
        organization_id=draft.organization_id,
        object_type="ASSET_DESCRIPTION_DRAFT",
        object_id=str(draft.id),
        requested_action="PUBLISH",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    draft.governance_review_id = review.id
    record_audit(
        session,
        replace(context, organization_id=draft.organization_id),
        action="asset_description.draft.submit",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"draft_id": str(draft.id), "overall_score": draft.overall_score},
    )
    record_outbox(
        session,
        organization_id=draft.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": str(draft.id),
            "overall_score": draft.overall_score,
        },
    )
    await session.commit()
    return review


# ---------------------------------------------------------------------------
# AT-14: sampling-based bulk review for drafted prose (language fields only).
#
# GL-9's per-draft `submit`/`decide_governance_review` path above -- and
# PG-3's `bulk_decide_governance_reviews` -- both require every item that
# ends up decided to have actually been named by the caller as one to
# decide. This is a *different* shape: acceptance sampling, where a steward
# reviews a random, reproducible SAMPLE drawn from a much larger batch and
# that decision is applied to exactly the sampled items -- not the whole
# batch. `aida.sampling_review.draw_reproducible_sample` is the pure,
# DB-free core; everything here is the audited, tenant-scoped wiring around
# it.
#
# What a sample decision actually decides (and why): the honest reading of
# "sampling-based bulk review" is that the SAMPLED items are individually
# finalized (published on APPROVE, rejected on REJECT) exactly as if each had
# been decided one at a time through `decide_governance_review` -- this
# calls the identical `_apply_governance_review_decision` core, so there is
# no second, laxer decision path. The UNSAMPLED items are left untouched,
# still PENDING_APPROVAL with their own PENDING `GovernanceReview` --
# `unsampled_draft_ids` in the response names them explicitly. A batch-level
# "the sample passed, so treat the rest as accepted too" outcome was
# considered and rejected: that would let a model's own drafted text become
# authoritative for items nobody -- human or model -- ever actually read,
# which is exactly what the 0.70/no-auto-publish cap this row must not
# relax exists to prevent (`Docs/90-reference/04-analysis-algorithms.md`
# SS4, ADR-0001). What sampling buys instead is real: a steward who wants to
# know whether a batch of 500 model drafts is broadly trustworthy reads and
# decides ~50 of them in one call instead of 500, with the seed and the
# drawn ids recorded so that verdict is reproducible and auditable -- cold
# start speed from targeting, batch-as-unit and sampling, none of which
# requires model output to become authoritative.


async def _table_names(session: AsyncSession, table_ids: list[UUID]) -> dict[UUID, str]:
    if not table_ids:
        return {}
    rows = (
        await session.execute(
            select(MetadataTable.id, MetadataTable.name).where(MetadataTable.id.in_(table_ids))
        )
    ).all()
    return {row[0]: row[1] for row in rows}


@router.post(
    "/organizations/{organization_id}/asset-description-drafts/sample-review/draw",
    response_model=AssetDescriptionSampleDrawRead,
)
async def draw_asset_description_sample_review(
    organization_id: UUID,
    body: AssetDescriptionSampleDraw,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> AssetDescriptionSampleDrawRead:
    """Preview a reproducible sample from a batch of pending drafts -- draws
    and audits the draw, but decides nothing. Mutates no draft; safe to call
    more than once (e.g. with the same seed, to recall a prior draw)."""
    enforce_organization(context, organization_id)
    batch = await _load_pending_batch(
        session, organization_id=organization_id, draft_ids=body.draft_ids
    )
    sample_size = resolve_sample_size(
        len(batch), sample_size=body.sample_size, sample_fraction=body.sample_fraction
    )
    seed = body.seed if body.seed is not None else generate_seed()
    drawn_ids = draw_reproducible_sample(body.draft_ids, sample_size=sample_size, seed=seed)
    drawn_by_id = {draft.id: draft for draft in batch if draft.id in set(drawn_ids)}
    table_names = await _table_names(session, [draft.table_id for draft in drawn_by_id.values()])
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="asset_description.sample_review.draw",
        resource_type="asset_description_draft",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "seed": seed,
            "seed_was_caller_supplied": body.seed is not None,
            "batch_size": len(batch),
            "sample_size": len(drawn_ids),
            "drawn_draft_ids": [str(value) for value in drawn_ids],
        },
    )
    await session.commit()
    return AssetDescriptionSampleDrawRead(
        seed=seed,
        batch_size=len(batch),
        sample_size=len(drawn_ids),
        drawn_draft_ids=drawn_ids,
        drawn_drafts=[
            _draft_read(drawn_by_id[draft_id], table_names.get(drawn_by_id[draft_id].table_id, ""))
            for draft_id in drawn_ids
        ],
    )


@router.post(
    "/organizations/{organization_id}/asset-description-drafts/sample-review/decide",
    response_model=AssetDescriptionSampleDecisionResultRead,
)
async def decide_asset_description_sample_review(
    organization_id: UUID,
    body: AssetDescriptionSampleDecide,
    context: SecurityContext = Depends(
        require_roles_or_delegated("PlatformAdmin", "DataSteward", "Reviewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> AssetDescriptionSampleDecisionResultRead:
    """Apply ONE decision to the reproducibly-drawn SAMPLE of a batch --
    `seed` is required so the sample this decision covers is exactly the one
    an earlier `.../sample-review/draw` call showed a steward (recomputed
    server-side from (batch, sample_size, seed), never trusted from the
    caller as a bare id list). Unsampled items are left PENDING_APPROVAL.

    Reuses `_apply_governance_review_decision` -- the identical core
    `decide_governance_review` and `bulk_decide_governance_reviews` (PG-3)
    call -- per sampled item, inside its own SAVEPOINT (PG-3's own partial-
    success precedent): a precondition failure on one sampled item marks it
    FAILED and continues rather than aborting the whole sample.
    """
    enforce_organization(context, organization_id)
    batch = await _load_pending_batch(
        session, organization_id=organization_id, draft_ids=body.draft_ids
    )
    sample_size = resolve_sample_size(
        len(batch), sample_size=body.sample_size, sample_fraction=body.sample_fraction
    )
    drawn_ids = draw_reproducible_sample(body.draft_ids, sample_size=sample_size, seed=body.seed)
    drawn_set = set(drawn_ids)
    unsampled_ids = [draft_id for draft_id in body.draft_ids if draft_id not in drawn_set]
    drafts_by_id = {draft.id: draft for draft in batch}
    review_ids = [
        drafts_by_id[draft_id].governance_review_id
        for draft_id in drawn_ids
        if drafts_by_id[draft_id].governance_review_id is not None
    ]
    reviews_by_id = {
        row.id: row
        for row in (
            await session.scalars(
                select(GovernanceReview).where(GovernanceReview.id.in_(review_ids))
            )
        ).all()
    }
    now = datetime.now(UTC)
    results: list[AssetDescriptionSampleItemRead] = []
    succeeded = 0
    for draft_id in drawn_ids:
        draft = drafts_by_id[draft_id]
        review = (
            reviews_by_id.get(draft.governance_review_id)
            if draft.governance_review_id is not None
            else None
        )
        if review is None or review.status != "PENDING":
            results.append(
                AssetDescriptionSampleItemRead(
                    draft_id=draft_id,
                    status="FAILED",
                    reason="governance review is not pending",
                )
            )
            continue
        if review.requested_by == context.principal_id or (
            context.active_delegator_principal_id is not None
            and review.requested_by == context.active_delegator_principal_id
        ):
            results.append(
                AssetDescriptionSampleItemRead(
                    draft_id=draft_id,
                    status="FAILED",
                    reason="maker-checker separation is required",
                )
            )
            continue
        try:
            async with session.begin_nested():
                (
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    payload,
                ) = await _apply_governance_review_decision(
                    session,
                    review,
                    decision=body.decision,
                    reason=body.reason,
                    context=context,
                    now=now,
                )
                # Additive JSON evidence on the draft itself (no schema
                # change -- `evidence` is already a free-form JSON column):
                # a reviewer opening this one draft later can see it was
                # decided as part of a sampled batch, not read individually.
                draft.evidence = {
                    **draft.evidence,
                    "sample_review": {
                        "seed": body.seed,
                        "batch_size": len(batch),
                        "sample_size": len(drawn_ids),
                        "drawn_draft_ids": [str(value) for value in drawn_ids],
                    },
                }
                record_outbox(
                    session,
                    organization_id=organization_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    event_type=event_type,
                    payload=payload,
                )
        except HTTPException as exc:
            results.append(
                AssetDescriptionSampleItemRead(
                    draft_id=draft_id, status="FAILED", reason=str(exc.detail)
                )
            )
            continue
        results.append(AssetDescriptionSampleItemRead(draft_id=draft_id, status="SUCCEEDED"))
        succeeded += 1
    failed = len(results) - succeeded
    outcome = "SUCCESS" if not failed else "PARTIAL_SUCCESS" if succeeded else "FAILURE"
    # The audit record, not just the per-draft evidence field, is this row's
    # required provenance: the seed and the exact drawn member ids, so a
    # sampled decision is replayable and defensible after the fact.
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="asset_description.sample_review.decide",
        resource_type="asset_description_draft",
        resource_id=str(organization_id),
        outcome=outcome,
        correlation_id=get_correlation_id(),
        details={
            "decision": body.decision,
            "seed": body.seed,
            "batch_size": len(batch),
            "batch_draft_ids": [str(value) for value in body.draft_ids],
            "sample_size": len(drawn_ids),
            "drawn_draft_ids": [str(value) for value in drawn_ids],
            "unsampled_draft_ids": [str(value) for value in unsampled_ids],
            "succeeded_count": succeeded,
            "failed_count": failed,
            "via_delegation_id": (
                str(context.active_delegation_id) if context.active_delegation_id else None
            ),
            "via_delegator_principal_id": context.active_delegator_principal_id,
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="sample governance decision conflicted with concurrent state",
        ) from exc
    return AssetDescriptionSampleDecisionResultRead(
        decision=body.decision,
        seed=body.seed,
        batch_size=len(batch),
        sample_size=len(drawn_ids),
        drawn_draft_ids=drawn_ids,
        unsampled_draft_ids=unsampled_ids,
        succeeded_count=succeeded,
        failed_count=failed,
        results=results,
    )


__all__ = ["router", "MINIMUM_EVIDENCE_FOR_REVIEW"]
