"""PR-1: composite key inference API.

This is a standalone router (not folded into `aida.api` / `aida.intelligence_api`)
so it can land without touching either hub module. It is the persistence and
HTTP half of the feature; `aida.composite_key_inference` holds the pure,
DB-free scoring logic this module fetches evidence for and calls.

Endpoints:
- `POST /v1/tables/{table_id}/composite-key-candidates/discover` -- compute
  candidates for one table from its latest `TableProfile`/`ColumnProfile`
  evidence and persist new `PENDING` rows (idempotent: candidates whose exact
  column set already exists and is not `REJECTED` are skipped).
- `GET /v1/datasources/{datasource_id}/composite-key-candidates` -- list
  candidates for a datasource, optionally narrowed to one table, with the
  usual `Page`/`limit`/`offset` pagination and a status filter.
- `POST /v1/composite-key-candidates/{candidate_id}/decision` -- the
  maker-checker decision endpoint, mirroring
  `aida.intelligence_api.decide_relationship_candidate` exactly: 404 if
  missing, 409 if the maker reviews their own candidate, 409 if it is no
  longer `PENDING`, otherwise APPROVE/REJECT with reviewer + timestamp
  stamped and an audit + outbox record written.
"""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.composite_key_inference import ColumnKeyEvidence, infer_composite_key_candidates
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    ColumnProfile,
    CompositeKeyCandidate,
    DataSource,
    MetadataColumn,
    MetadataConstraint,
    MetadataTable,
    TableProfile,
)
from aida.schemas import CompositeKeyCandidateDecision, CompositeKeyCandidateRead, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["composite-key-inference"])

_DECLARED_KEY_CONSTRAINT_TYPES = ("PRIMARY_KEY", "UNIQUE")


async def _declared_key_column_ids(session: AsyncSession, table: MetadataTable) -> set[UUID]:
    """Column ids already covered by a declared PRIMARY_KEY/UNIQUE constraint."""
    constraints = (
        await session.scalars(
            select(MetadataConstraint).where(
                MetadataConstraint.table_id == table.id,
                MetadataConstraint.status == "ACTIVE",
                MetadataConstraint.constraint_type.in_(_DECLARED_KEY_CONSTRAINT_TYPES),
            )
        )
    ).all()
    if not constraints:
        return set()
    columns = (
        await session.scalars(select(MetadataColumn).where(MetadataColumn.table_id == table.id))
    ).all()
    columns_by_name = {column.name.lower(): column for column in columns}
    declared: set[UUID] = set()
    for constraint in constraints:
        for name in constraint.columns:
            column = columns_by_name.get(name.lower())
            if column is not None:
                declared.add(column.id)
    return declared


@router.post(
    "/tables/{table_id}/composite-key-candidates/discover",
    response_model=Page,
    status_code=status.HTTP_202_ACCEPTED,
)
async def discover_composite_key_candidates(
    table_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)

    latest_profile = (
        await session.scalars(
            select(TableProfile)
            .where(TableProfile.table_id == table.id, TableProfile.status == "COMPLETED")
            .order_by(TableProfile.created_at.desc())
            .limit(1)
        )
    ).first()
    if latest_profile is None:
        # No profiling evidence at all -- nothing to infer from.
        return Page(items=[], limit=0, offset=0, total=0)

    column_profiles = (
        await session.scalars(
            select(ColumnProfile).where(ColumnProfile.table_profile_id == latest_profile.id)
        )
    ).all()
    if not column_profiles:
        return Page(items=[], limit=0, offset=0, total=0)

    columns = (
        await session.scalars(
            select(MetadataColumn).where(
                MetadataColumn.table_id == table.id,
                MetadataColumn.status == "ACTIVE",
            )
        )
    ).all()
    columns_by_id = {column.id: column for column in columns}
    declared_key_column_ids = await _declared_key_column_ids(session, table)

    evidence_rows = [
        ColumnKeyEvidence(
            column_id=profile.column_id,
            column_name=columns_by_id[profile.column_id].name,
            null_count=profile.null_count,
            non_null_count=profile.non_null_count,
            approximate_distinct_count=profile.approximate_distinct_count,
        )
        for profile in column_profiles
        if profile.column_id in columns_by_id
    ]

    proposals = infer_composite_key_candidates(
        columns=evidence_rows,
        sampled_row_count=latest_profile.sampled_row_count,
        row_count_estimate=latest_profile.row_count_estimate,
        declared_key_column_ids=declared_key_column_ids,
    )

    existing = (
        await session.scalars(
            select(CompositeKeyCandidate).where(
                CompositeKeyCandidate.table_id == table.id,
                CompositeKeyCandidate.status != "REJECTED",
            )
        )
    ).all()
    existing_column_sets = {tuple(sorted(row.column_ids)) for row in existing}

    created: list[CompositeKeyCandidate] = []
    for proposal in proposals:
        column_id_strings = [str(column_id) for column_id in proposal.column_ids]
        key = tuple(sorted(column_id_strings))
        if key in existing_column_sets:
            continue
        candidate = CompositeKeyCandidate(
            organization_id=table.organization_id,
            datasource_id=table.datasource_id,
            table_id=table.id,
            column_ids=column_id_strings,
            detection_rule=proposal.detection_rule,
            confidence=proposal.confidence,
            evidence=proposal.evidence,
            status="PENDING",
            created_by=context.principal_id,
        )
        session.add(candidate)
        created.append(candidate)
        existing_column_sets.add(key)

    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=table.organization_id),
        action="composite_key_candidates.discover",
        resource_type="metadata_table",
        resource_id=str(table.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "created_count": len(created),
            "evaluated_count": len(proposals),
            "columns_profiled": len(evidence_rows),
        },
    )
    await session.commit()
    return Page(
        items=[CompositeKeyCandidateRead.model_validate(candidate) for candidate in created],
        limit=len(created),
        offset=0,
        total=len(created),
    )


@router.get("/datasources/{datasource_id}/composite-key-candidates", response_model=Page)
async def list_composite_key_candidates(
    datasource_id: UUID,
    table_id: UUID | None = Query(default=None),
    candidate_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Auditor", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = [CompositeKeyCandidate.datasource_id == datasource.id]
    if table_id is not None:
        filters.append(CompositeKeyCandidate.table_id == table_id)
    if candidate_status:
        filters.append(CompositeKeyCandidate.status == candidate_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(CompositeKeyCandidate).where(*filters)
    )
    rows = (
        await session.scalars(
            select(CompositeKeyCandidate)
            .where(*filters)
            .order_by(CompositeKeyCandidate.confidence.desc(), CompositeKeyCandidate.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[CompositeKeyCandidateRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/composite-key-candidates/{candidate_id}/decision",
    response_model=CompositeKeyCandidateRead,
)
async def decide_composite_key_candidate(
    candidate_id: UUID,
    body: CompositeKeyCandidateDecision,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataReviewer", "DataSteward")
    ),
    session: AsyncSession = Depends(get_session),
) -> CompositeKeyCandidate:
    candidate = await session.get(CompositeKeyCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="composite key candidate not found")
    enforce_organization(context, candidate.organization_id)
    if candidate.created_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker cannot review their own candidate")
    if candidate.status != "PENDING":
        raise HTTPException(status_code=409, detail="composite key candidate is already decided")
    candidate.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    candidate.reviewed_by = context.principal_id
    candidate.review_reason = body.reason
    candidate.reviewed_at = datetime.now(UTC)
    record_audit(
        session,
        replace(context, organization_id=candidate.organization_id),
        action="composite_key_candidate.decide",
        resource_type="composite_key_candidate",
        resource_id=str(candidate.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": body.decision},
    )
    record_outbox(
        session,
        organization_id=candidate.organization_id,
        aggregate_type="composite_key_candidate",
        aggregate_id=str(candidate.id),
        event_type="composite_key_candidate.decided.v1",
        payload={"candidate_id": str(candidate.id), "status": candidate.status},
    )
    await session.commit()
    return candidate
