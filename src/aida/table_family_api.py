"""RL-1: table family / temporal intelligence -- API layer.

This module owns the database fetch, persistence, de-duplication, and
maker-checker review flow around the pure detectors in
``aida.table_family_intelligence``. It follows the same shape as the
relationship-candidate endpoints in ``aida.intelligence_api``
(``discover`` -> ``list`` -> ``decision``) and the same maker-checker rules:
a candidate starts PENDING, the maker who triggered discovery cannot also
review it, and a candidate that has already been decided cannot be decided
again.

Kept as its own router/module (rather than added to ``api.py`` or
``intelligence_api.py``) and wired into the app with a single
``app.include_router(table_family_router)`` line in ``main.py``.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    DataSource,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    TableFamilyCandidate,
)
from aida.schemas import Page, TableFamilyCandidateDecision, TableFamilyCandidateRead
from aida.schemas import TableFamilyDiscoveryRequest as DiscoveryRequest
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.table_family_intelligence import FamilyCandidateDraft, detect_table_families

router = APIRouter(prefix="/v1", tags=["table-family-intelligence"])

# A single discover call fetches at most this many tables/columns from one
# schema -- detection is already bounded to one schema at a time (see
# `table_family_intelligence`'s module docstring), this is an additional,
# defense-in-depth cap on how much of a single very large schema one call
# will scan.
TABLE_FAMILY_SCAN_MAX_TABLES = 5_000
TABLE_FAMILY_SCAN_MAX_COLUMNS = 100_000

_DISCOVER_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin")
_LIST_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Auditor", "Viewer")
_DECISION_ROLES = ("PlatformAdmin", "MetadataReviewer", "DataSteward")


def _existing_member_keys(rows: list[TableFamilyCandidate]) -> set[tuple[str, frozenset[UUID]]]:
    """Identity of already-persisted, non-REJECTED candidates.

    Mirrors `FamilyCandidateDraft.member_key()` so a re-run of discovery can
    skip anything already recorded (PENDING or APPROVED) for the same
    family type + member set, without spamming duplicates. A REJECTED
    candidate does *not* block re-detection -- new evidence (or simply
    re-running after a false rejection) should be able to raise it again.
    """
    keys: set[tuple[str, frozenset[UUID]]] = set()
    for row in rows:
        if row.status == "REJECTED":
            continue
        keys.add((row.family_type, frozenset(UUID(member) for member in row.member_table_ids)))
    return keys


@router.post(
    "/schemas/{schema_id}/table-family-candidates/discover",
    response_model=Page,
    status_code=status.HTTP_202_ACCEPTED,
)
async def discover_table_family_candidates(
    schema_id: UUID,
    body: DiscoveryRequest,
    context: SecurityContext = Depends(require_roles(*_DISCOVER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    metadata_schema = await session.get(MetadataSchema, schema_id)
    if metadata_schema is None:
        raise HTTPException(status_code=404, detail="schema not found")
    enforce_organization(context, metadata_schema.organization_id)

    tables = (
        await session.scalars(
            select(MetadataTable)
            .where(
                MetadataTable.schema_id == metadata_schema.id,
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataTable.name)
            .limit(TABLE_FAMILY_SCAN_MAX_TABLES)
        )
    ).all()
    if not tables:
        return Page(items=[], limit=body.max_candidates, offset=0, total=0)

    table_ids = [table.id for table in tables]
    columns = (
        await session.scalars(
            select(MetadataColumn)
            .where(
                MetadataColumn.table_id.in_(table_ids),
                MetadataColumn.status == "ACTIVE",
            )
            .order_by(MetadataColumn.table_id, MetadataColumn.ordinal_position)
            .limit(TABLE_FAMILY_SCAN_MAX_COLUMNS)
        )
    ).all()
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)

    detector_input = [
        (
            table.id,
            table.name,
            [
                (column.name, column.physical_type, column.nullable)
                for column in columns_by_table.get(table.id, [])
            ],
        )
        for table in tables
    ]
    drafts: list[FamilyCandidateDraft] = detect_table_families(detector_input)

    existing_rows = (
        await session.scalars(
            select(TableFamilyCandidate).where(
                TableFamilyCandidate.schema_id == metadata_schema.id
            )
        )
    ).all()
    existing_keys = _existing_member_keys(list(existing_rows))

    datasource_id = tables[0].datasource_id
    created: list[TableFamilyCandidate] = []
    for draft in drafts:
        if draft.member_key() in existing_keys:
            continue
        candidate = TableFamilyCandidate(
            organization_id=metadata_schema.organization_id,
            datasource_id=datasource_id,
            schema_id=metadata_schema.id,
            family_type=draft.family_type,
            member_table_ids=[str(member_id) for member_id in draft.member_table_ids],
            base_table_id=draft.base_table_id,
            detection_rule=draft.detection_rule,
            confidence=draft.confidence,
            evidence=draft.evidence,
            created_by=context.principal_id,
        )
        session.add(candidate)
        created.append(candidate)
        existing_keys.add(draft.member_key())
        if len(created) >= body.max_candidates:
            break

    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=metadata_schema.organization_id),
        action="table_family_candidates.discover",
        resource_type="metadata_schema",
        resource_id=str(metadata_schema.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "created_candidates": len(created),
            "tables_scanned": len(tables),
            "columns_scanned": len(columns),
        },
    )
    await session.commit()
    return Page(
        items=[TableFamilyCandidateRead.model_validate(item) for item in created],
        limit=body.max_candidates,
        offset=0,
        total=len(created),
    )


@router.get("/schemas/{schema_id}/table-family-candidates", response_model=Page)
async def list_table_family_candidates_for_schema(
    schema_id: UUID,
    candidate_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_LIST_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    metadata_schema = await session.get(MetadataSchema, schema_id)
    if metadata_schema is None:
        raise HTTPException(status_code=404, detail="schema not found")
    enforce_organization(context, metadata_schema.organization_id)
    filters = [TableFamilyCandidate.schema_id == metadata_schema.id]
    if candidate_status:
        filters.append(TableFamilyCandidate.status == candidate_status.upper())
    return await _list_candidates(session, filters, limit, offset)


@router.get("/datasources/{datasource_id}/table-family-candidates", response_model=Page)
async def list_table_family_candidates_for_datasource(
    datasource_id: UUID,
    candidate_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_LIST_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    filters = [TableFamilyCandidate.datasource_id == datasource.id]
    if candidate_status:
        filters.append(TableFamilyCandidate.status == candidate_status.upper())
    return await _list_candidates(session, filters, limit, offset)


async def _list_candidates(
    session: AsyncSession,
    filters: Sequence[ColumnElement[bool]],
    limit: int,
    offset: int,
) -> Page:
    total = await session.scalar(
        select(func.count()).select_from(TableFamilyCandidate).where(*filters)
    )
    rows = (
        await session.scalars(
            select(TableFamilyCandidate)
            .where(*filters)
            .order_by(TableFamilyCandidate.confidence.desc(), TableFamilyCandidate.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[TableFamilyCandidateRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/table-family-candidates/{candidate_id}/decision",
    response_model=TableFamilyCandidateRead,
)
async def decide_table_family_candidate(
    candidate_id: UUID,
    body: TableFamilyCandidateDecision,
    context: SecurityContext = Depends(require_roles(*_DECISION_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> TableFamilyCandidate:
    candidate = await session.get(TableFamilyCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="table family candidate not found")
    enforce_organization(context, candidate.organization_id)
    if candidate.created_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker cannot review their own candidate")
    if candidate.status != "PENDING":
        raise HTTPException(status_code=409, detail="table family candidate is already decided")
    candidate.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    candidate.reviewed_by = context.principal_id
    candidate.review_reason = body.reason
    candidate.reviewed_at = datetime.now(UTC)
    record_audit(
        session,
        replace(context, organization_id=candidate.organization_id),
        action="table_family_candidate.decide",
        resource_type="table_family_candidate",
        resource_id=str(candidate.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": body.decision},
    )
    record_outbox(
        session,
        organization_id=candidate.organization_id,
        aggregate_type="table_family_candidate",
        aggregate_id=str(candidate.id),
        event_type="table_family_candidate.decided.v1",
        payload={"candidate_id": str(candidate.id), "status": candidate.status},
    )
    await session.commit()
    return candidate
