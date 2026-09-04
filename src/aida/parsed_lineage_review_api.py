"""P1-05 / ADR-0026: review endpoints for the five non-governed
parser-produced lineage edge tables.

Design mirrors `intelligence_api.decide_relationship_candidate` and
`bulk_decide_relationship_candidates` exactly: same maker-checker rule
(the principal who wrote the edge cannot decide it -- 409), same
outbox/audit trail, same per-item SAVEPOINT-guarded bulk endpoint. What
this module does NOT do is share storage with those endpoints -- see
ADR-0026 for why the five edge tables keep their own columns instead of
being folded under a `LineageEdge` supertype.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.parsed_lineage_review_service import (
    EDGE_TYPE_TO_MODEL,
    EDGE_TYPES,
    list_parsed_lineage_review_queue,
)
from aida.schemas import (
    ParsedLineageEdgeBulkDecisionItemRead,
    ParsedLineageEdgeBulkDecisionRequest,
    ParsedLineageEdgeBulkDecisionResultRead,
    ParsedLineageEdgeDecisionRead,
    ParsedLineageEdgeDecisionRequest,
    ParsedLineageEdgeReviewQueueItemRead,
    ParsedLineageEdgeReviewQueueRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["parsed-lineage-review"])

# Match the roles that decide `RelationshipCandidate` (see
# `intelligence_api.decide_relationship_candidate`) -- these edges are
# same-shape review artifacts, so they belong to the same reviewer
# population, not to a new role.
_REVIEWER_ROLES = ("PlatformAdmin", "MetadataReviewer", "DataSteward")
_QUEUE_READER_ROLES = (
    "PlatformAdmin",
    "MetadataReviewer",
    "DataSteward",
    "MetadataAdmin",
    "DataAdmin",
    "Viewer",
)


def _decision_event_type(status: str) -> str:
    return (
        "lineage.parsed_edge.approved.v1"
        if status == "ACTIVE"
        else "lineage.parsed_edge.rejected.v1"
    )


def _decision_payload(edge_type: str, edge: Any) -> dict[str, Any]:
    """The outbox payload for one parsed-edge decision. Kept intentionally
    small -- edge_type + edge_id + review_status + the datasource-hint
    the graph projector keys off, if the table has one. Payload is the
    contract; the edge row itself is the source of truth."""
    payload: dict[str, Any] = {
        "edge_id": str(edge.id),
        "edge_type": edge_type,
        "review_status": edge.review_status,
    }
    datasource_id = getattr(edge, "datasource_id", None)
    if datasource_id is not None:
        payload["datasource_id"] = str(datasource_id)
    return payload


async def _load_edge(
    session: AsyncSession, edge_type: str, edge_id: UUID
) -> Any:
    model = EDGE_TYPE_TO_MODEL.get(edge_type)
    if model is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown parsed lineage edge_type: {edge_type}",
        )
    edge = await session.get(model, edge_id)
    if edge is None:
        raise HTTPException(status_code=404, detail="parsed lineage edge not found")
    return edge


def _apply_decision(
    edge: Any,
    *,
    decision: str,
    principal_id: str,
    reason: str,
    now: datetime,
) -> None:
    """Mutate the edge in-place -- the caller commits."""
    edge.review_status = "ACTIVE" if decision == "APPROVED" else "REJECTED"
    edge.reviewed_by = principal_id
    edge.review_reason = reason
    edge.reviewed_at = now


def _reject_maker_checker(edge: Any, context: SecurityContext) -> None:
    """Same rule as `decide_relationship_candidate`: the principal who
    wrote the edge cannot decide it -- 409, not 403, because the caller
    IS authorized as a reviewer; they just aren't allowed to review
    THIS specific one."""
    created_by = getattr(edge, "created_by", None)
    if created_by is not None and created_by == context.principal_id:
        raise HTTPException(
            status_code=409,
            detail="maker cannot review their own parsed lineage edge",
        )


def _reject_already_decided(edge: Any) -> None:
    if edge.review_status != "PROPOSED":
        raise HTTPException(
            status_code=409,
            detail=(
                f"parsed lineage edge is already {edge.review_status.lower()}"
            ),
        )


@router.get(
    "/lineage/parsed-edges/review-queue",
    response_model=ParsedLineageEdgeReviewQueueRead,
)
async def get_parsed_lineage_review_queue(
    edge_type: str | None = Query(default=None, max_length=40),
    min_confidence: float | None = Query(default=None, ge=0.0, le=1.0),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_QUEUE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ParsedLineageEdgeReviewQueueRead:
    """List PROPOSED parsed-lineage edges across the five tables --
    the review queue itself. Filtered by the caller's own organization,
    ordered newest first; `edge_type` narrows to one table, and
    `min_confidence` filters on the coerced 0..1 confidence (string
    enums FULL/PARTIAL/LOW map to 1.0/0.6/0.3)."""
    if edge_type and edge_type not in EDGE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                "edge_type must be one of "
                + ", ".join(EDGE_TYPES)
                + " (or omitted to span all five)"
            ),
        )
    if context.organization_id is None:
        raise HTTPException(status_code=403, detail="organization scope required")
    items, total = await list_parsed_lineage_review_queue(
        session,
        context.organization_id,
        edge_type=edge_type,
        min_confidence=min_confidence,
        limit=limit,
        offset=offset,
    )
    return ParsedLineageEdgeReviewQueueRead(
        items=[
            ParsedLineageEdgeReviewQueueItemRead(
                edge_id=item.edge_id,
                edge_type=item.edge_type,
                organization_id=item.organization_id,
                created_at=item.created_at,
                created_by=item.created_by,
                confidence=item.confidence,
                source_label=item.source_label,
                target_label=item.target_label,
                transformation_type=item.transformation_type,
                source_sql_reference=item.source_sql_reference,
            )
            for item in items
        ],
        limit=limit,
        offset=offset,
        total=total,
    )


@router.post(
    "/lineage/parsed-edges/{edge_id}/decision",
    response_model=ParsedLineageEdgeDecisionRead,
)
async def decide_parsed_lineage_edge(
    edge_id: UUID,
    body: ParsedLineageEdgeDecisionRequest,
    context: SecurityContext = Depends(require_roles(*_REVIEWER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ParsedLineageEdgeDecisionRead:
    """Approve or reject one PROPOSED parsed lineage edge.

    Same guarantees as `decide_relationship_candidate`: maker-checker
    (creator cannot decide, 409), already-decided rejected (409), audit
    + outbox emit on success. On APPROVE the edge flips to ACTIVE and
    the next unified-lineage read / graph-projector run picks it up;
    on REJECT it flips to REJECTED and stays out of the graph while
    remaining queryable evidence that a reviewer said no.
    """
    edge = await _load_edge(session, body.edge_type, edge_id)
    enforce_organization(context, edge.organization_id)
    _reject_maker_checker(edge, context)
    _reject_already_decided(edge)

    _apply_decision(
        edge,
        decision=body.decision,
        principal_id=context.principal_id,
        reason=body.reason,
        now=datetime.now(UTC),
    )
    record_audit(
        session,
        replace(context, organization_id=edge.organization_id),
        action="LINEAGE_PARSED_EDGE_"
        + ("APPROVED" if edge.review_status == "ACTIVE" else "REJECTED"),
        resource_type="parsed_lineage_edge",
        resource_id=str(edge.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"edge_type": body.edge_type, "decision": body.decision},
    )
    record_outbox(
        session,
        organization_id=edge.organization_id,
        aggregate_type="parsed_lineage_edge",
        aggregate_id=str(edge.id),
        event_type=_decision_event_type(edge.review_status),
        payload=_decision_payload(body.edge_type, edge),
    )
    await session.commit()
    return ParsedLineageEdgeDecisionRead(
        edge_id=edge.id,
        edge_type=body.edge_type,
        review_status=edge.review_status,
        reviewed_by=edge.reviewed_by,
        reviewed_at=edge.reviewed_at,
        review_reason=edge.review_reason,
    )


@router.post(
    "/lineage/parsed-edges/bulk-decide",
    response_model=ParsedLineageEdgeBulkDecisionResultRead,
)
async def bulk_decide_parsed_lineage_edges(
    body: ParsedLineageEdgeBulkDecisionRequest,
    context: SecurityContext = Depends(require_roles(*_REVIEWER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ParsedLineageEdgeBulkDecisionResultRead:
    """Decide up to 100 PROPOSED parsed-lineage edges in one call.

    Mirrors `bulk_decide_relationship_candidates` exactly: same
    maker-checker, same PENDING-only rule (already-decided edges are
    marked FAILED and skipped), and each item runs inside its own
    SAVEPOINT via `session.begin_nested()` so one bad edge does not
    roll back the whole batch. The outbox event is emitted once per
    successful item, not once per batch, so a downstream projector
    reprocesses one edge at a time.
    """
    now = datetime.now(UTC)
    principal_id = context.principal_id
    results: list[ParsedLineageEdgeBulkDecisionItemRead] = []
    succeeded = 0

    for item in body.items:
        try:
            async with session.begin_nested():
                edge = await _load_edge(session, item.edge_type, item.edge_id)
                enforce_organization(context, edge.organization_id)
                _reject_maker_checker(edge, context)
                _reject_already_decided(edge)
                _apply_decision(
                    edge,
                    decision=body.decision,
                    principal_id=principal_id,
                    reason=body.reason,
                    now=now,
                )
                record_outbox(
                    session,
                    organization_id=edge.organization_id,
                    aggregate_type="parsed_lineage_edge",
                    aggregate_id=str(edge.id),
                    event_type=_decision_event_type(edge.review_status),
                    payload=_decision_payload(item.edge_type, edge),
                )
        except HTTPException as exc:
            # One item failed -- record it and continue with the rest.
            # `session.begin_nested()` has already released the SAVEPOINT
            # so the outer session is still usable.
            results.append(
                ParsedLineageEdgeBulkDecisionItemRead(
                    edge_id=item.edge_id,
                    edge_type=item.edge_type,
                    status="FAILED",
                    reason=str(exc.detail),
                )
            )
            continue
        results.append(
            ParsedLineageEdgeBulkDecisionItemRead(
                edge_id=item.edge_id,
                edge_type=item.edge_type,
                status="SUCCEEDED",
                reason=None,
            )
        )
        succeeded += 1

    failed = len(results) - succeeded
    outcome = (
        "SUCCESS" if failed == 0 else "PARTIAL_SUCCESS" if succeeded > 0 else "FAILURE"
    )
    record_audit(
        session,
        context,
        action="LINEAGE_PARSED_EDGE_BULK_DECIDE",
        resource_type="parsed_lineage_edge",
        resource_id=None,
        outcome=outcome,
        correlation_id=get_correlation_id(),
        details={
            "decision": body.decision,
            "requested_count": len(results),
            "succeeded_count": succeeded,
            "failed_count": failed,
        },
    )
    await session.commit()
    return ParsedLineageEdgeBulkDecisionResultRead(
        decision=body.decision,
        requested_count=len(results),
        succeeded_count=succeeded,
        failed_count=failed,
        results=results,
    )


__all__ = ["router"]
