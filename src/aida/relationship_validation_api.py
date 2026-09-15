"""R11-FP06: read how a proposed join is supported, before and after it is decided.

The decision endpoints in ``aida.intelligence_api`` refuse an approval the evidence does not
support. These reads show a reviewer the same validation beforehand, and compare an approved
join's validation today with the one recorded when it was approved, so a lost key or new nulls
show up as drift instead of silently standing.

A validation names columns and carries profile counts from both sides of the join, so it is read
under the catalog's own rule for each side: the caller's organization owns the datasource and the
authorization gate allows reading its metadata. A join whose two sides sit in different data
domains is also read only under an ACTIVE cross-boundary grant (ADR-0017), checked per read.
Organization membership alone is not enough.
"""

from collections.abc import Iterable
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import gate_read
from aida.config import Settings, get_settings
from aida.db import get_session
from aida.domain_service import check_cross_boundary_grant
from aida.models import DataSource, RelationshipCandidate, RelationshipCandidateGroup
from aida.relationship_validation import (
    RelationshipColumnsMissingError,
    RelationshipValidation,
    validate_composite_relationship_candidate,
    validate_relationship_candidate,
    validation_drift,
)
from aida.resource_scope import load_datasource_in_scope
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
NO_CROSS_BOUNDARY_GRANT = "NO_CROSS_BOUNDARY_GRANT"


async def _authorize_sides(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    *,
    source_datasource_id: UUID,
    target_datasource_ids: Iterable[UUID],
) -> None:
    """Gate every datasource the validation reads, then the domain boundary between them."""
    source = await load_datasource_in_scope(session, context, source_datasource_id)
    sides: list[DataSource] = [source]
    for datasource_id in dict.fromkeys(target_datasource_ids):
        if datasource_id != source.id:
            sides.append(await load_datasource_in_scope(session, context, datasource_id))
    for datasource in sides:
        await gate_read(
            session,
            context,
            settings,
            action="READ_METADATA",
            resource_type="datasource",
            resource_id=str(datasource.id),
            datasource_id=datasource.id,
        )
    for target in sides[1:]:
        if target.data_domain_id == source.data_domain_id:
            continue
        # The source side's domain must be granted sight into the target side's domain.
        allowed = await check_cross_boundary_grant(
            session, source.organization_id, target.data_domain_id, source.data_domain_id
        )
        if not allowed:
            raise HTTPException(status_code=403, detail=NO_CROSS_BOUNDARY_GRANT)


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
    settings: Settings = Depends(get_settings),
) -> RelationshipValidationRead:
    candidate = await session.get(RelationshipCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="relationship candidate not found")
    enforce_organization(context, candidate.organization_id)
    await _authorize_sides(
        session,
        context,
        settings,
        source_datasource_id=candidate.datasource_id,
        target_datasource_ids=[candidate.target_datasource_id],
    )
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
    settings: Settings = Depends(get_settings),
) -> RelationshipValidationRead:
    group = await session.get(RelationshipCandidateGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="composite relationship candidate not found")
    enforce_organization(context, group.organization_id)
    await _authorize_sides(
        session,
        context,
        settings,
        source_datasource_id=group.datasource_id,
        target_datasource_ids=[],
    )
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
