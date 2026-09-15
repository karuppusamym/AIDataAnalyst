"""R11-FP06: read how a proposed join is supported, before and after it is decided.

The decision endpoints in ``aida.intelligence_api`` refuse an approval the evidence does not
support. These reads show a reviewer the same validation beforehand, and compare an approved
join's validation today with the one recorded when it was approved, so a lost key or new nulls
show up as drift instead of silently standing.
"""

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.db import get_session
from aida.models import RelationshipCandidate, RelationshipCandidateGroup
from aida.relationship_validation import (
    RelationshipColumnsMissingError,
    RelationshipValidation,
    validate_composite_relationship_candidate,
    validate_relationship_candidate,
    validation_drift,
)
from aida.schemas import RelationshipValidationRead
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["relationship-validation"])

_READER_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "MetadataReviewer",
    "DataSteward",
    "Auditor",
    "Viewer",
)


def _read(
    validation: RelationshipValidation,
    *,
    subject_type: Literal["RELATIONSHIP_CANDIDATE", "COMPOSITE_RELATIONSHIP_CANDIDATE"],
    subject_id: UUID,
    status: str,
    evidence: dict[str, Any] | None,
) -> RelationshipValidationRead:
    recorded = (evidence or {}).get("validation")
    recorded = recorded if isinstance(recorded, dict) else {}
    return RelationshipValidationRead.model_validate(
        {
            **validation.as_evidence(),
            "subject_type": subject_type,
            "subject_id": subject_id,
            "status": status,
            "approvable": validation.approvable,
            "recorded_fingerprint": recorded.get("fingerprint"),
            "recorded_at": recorded.get("validated_at"),
            "drift": validation_drift(evidence, validation),
        }
    )


@router.get(
    "/relationship-candidates/{candidate_id}/validation",
    response_model=RelationshipValidationRead,
)
async def get_relationship_candidate_validation(
    candidate_id: UUID,
    context: SecurityContext = Depends(require_roles(*_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> RelationshipValidationRead:
    candidate = await session.get(RelationshipCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="relationship candidate not found")
    enforce_organization(context, candidate.organization_id)
    try:
        validation = await validate_relationship_candidate(session, candidate)
    except RelationshipColumnsMissingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _read(
        validation,
        subject_type="RELATIONSHIP_CANDIDATE",
        subject_id=candidate.id,
        status=candidate.status,
        evidence=candidate.evidence,
    )


@router.get(
    "/composite-relationship-candidates/{group_id}/validation",
    response_model=RelationshipValidationRead,
)
async def get_composite_relationship_candidate_validation(
    group_id: UUID,
    context: SecurityContext = Depends(require_roles(*_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> RelationshipValidationRead:
    group = await session.get(RelationshipCandidateGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="composite relationship candidate not found")
    enforce_organization(context, group.organization_id)
    try:
        validation = await validate_composite_relationship_candidate(session, group)
    except RelationshipColumnsMissingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _read(
        validation,
        subject_type="COMPOSITE_RELATIONSHIP_CANDIDATE",
        subject_id=group.id,
        status=group.status,
        evidence=group.evidence,
    )
