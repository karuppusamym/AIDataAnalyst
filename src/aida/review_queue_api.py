"""UX-17: `GET /v1/governance/reviews/queue`.

See `aida.review_queue_read_model` for how the response is composed and for
the scoping note on why this is "whatever batch of reviews the caller
selected" (organization, status, object type, and -- for the one proposal
type with a real persisted run, `MetadataEnrichmentProposal` -- an optional
`inference_run_id`) rather than a single uniform "run" concept the data model
does not have across every proposal type in the governance queue.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.db import get_session
from aida.models import GovernanceReview, MetadataEnrichmentProposal
from aida.review_queue_read_model import compose_review_queue
from aida.review_queue_schemas import ReviewQueueRead
from aida.security import SecurityContext, require_roles

router = APIRouter(prefix="/v1", tags=["governance-review-queue"])

# Same reviewer-facing read population `GET /v1/governance/reviews` (list)
# and `GET /v1/governance/reviews/{id}/diff` (SM-7) already use.
_REVIEW_QUEUE_READ_ROLES = ("PlatformAdmin", "SemanticAdmin", "DataSteward", "Reviewer")

_MAX_QUEUE_ROWS = 1000


@router.get("/governance/reviews/queue", response_model=ReviewQueueRead)
async def get_review_queue(
    review_status: str | None = Query(default="PENDING", alias="status", max_length=30),
    object_type: str | None = Query(default=None, max_length=100),
    inference_run_id: UUID | None = Query(default=None),
    limit: int = Query(default=_MAX_QUEUE_ROWS, ge=1, le=_MAX_QUEUE_ROWS),
    context: SecurityContext = Depends(require_roles(*_REVIEW_QUEUE_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ReviewQueueRead:
    """One request returns a batch of governance-review-queue proposals --
    each carrying its rendered diff (SM-7, reused verbatim), numeric
    confidence and the evidence its rationale cites. Every count on the
    response (`total_proposals`, `by_status`, `by_object_type`,
    `diffable_count`) is a `computed_field` derived from `proposals` itself,
    never a separately queried number that could disagree with what was
    actually returned.

    `status` (default `PENDING`, matching `GET /v1/governance/reviews`) and
    `object_type` narrow the batch the same way that list endpoint's own
    `status` filter does; pass `status=` (empty) for every status.
    `inference_run_id` additionally scopes to one `SemanticInferenceRun`'s
    `METADATA_ENRICHMENT_PROPOSAL` reviews -- the one proposal type the data
    model genuinely groups into a batch from one inference pass (see
    `aida.review_queue_read_model` module docstring).
    """
    organization_id = context.require_organization()
    filters = [GovernanceReview.organization_id == organization_id]
    normalized_status = review_status.upper() if review_status else None
    if normalized_status:
        filters.append(GovernanceReview.status == normalized_status)
    normalized_object_type = object_type.upper() if object_type else None
    if normalized_object_type:
        filters.append(GovernanceReview.object_type == normalized_object_type)
    if inference_run_id is not None:
        run_review_ids = (
            await session.scalars(
                select(MetadataEnrichmentProposal.governance_review_id).where(
                    MetadataEnrichmentProposal.inference_run_id == inference_run_id,
                    MetadataEnrichmentProposal.organization_id == organization_id,
                )
            )
        ).all()
        filters.append(GovernanceReview.id.in_(run_review_ids))

    reviews = (
        await session.scalars(
            select(GovernanceReview)
            .where(*filters)
            .order_by(GovernanceReview.created_at)
            .limit(limit)
        )
    ).all()
    proposals = await compose_review_queue(session, reviews)
    return ReviewQueueRead(
        organization_id=organization_id,
        status_filter=normalized_status,
        object_type_filter=normalized_object_type,
        inference_run_id_filter=inference_run_id,
        generated_at=datetime.now(UTC),
        proposals=proposals,
    )
