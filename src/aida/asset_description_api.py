"""GL-9: draft table descriptions from catalog evidence, reviewed through the
shared `governance_review` queue. See `aida.asset_description_service` for
the deterministic evidence gathering, scoring, and composition; this module
only exposes it as a tenant-scoped, audited API.
"""

from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
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
from aida.schemas import (
    AssetDescriptionDraftGenerate,
    AssetDescriptionDraftRead,
    GovernanceReviewRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

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


__all__ = ["router", "MINIMUM_EVIDENCE_FOR_REVIEW"]
