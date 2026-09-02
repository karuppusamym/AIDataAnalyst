"""N8: REST surface for the data-dictionary document-ingestion pipeline.

Its own file with its own locally-scoped Pydantic schemas, same reasoning as
`aida.playbooks_api` (AT-1): kept out of the shared, hot `aida.api`/
`aida.schemas` modules under heavy concurrent edit on this branch, since this
row's exit condition does not require touching either.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.document_ingestion import (
    create_document_from_csv,
    extract_description_claims,
    resolve_structural_mappings,
)
from aida.events import record_audit, record_outbox
from aida.models import Document, DocumentClaim, DocumentMapping, DocumentSection, Project
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["document-ingestion"])

DOCUMENT_WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")
DOCUMENT_READ_ROLES = (*DOCUMENT_WRITE_ROLES, "Analyst", "Viewer", "Reviewer")


class DocumentCreate(ApiModel):
    filename: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)


class DocumentRead(ApiModel):
    id: UUID
    organization_id: UUID
    project_id: UUID
    filename: str
    media_type: str
    sha256: str
    status: str
    section_count: int
    parse_error_count: int
    uploaded_by: str
    created_at: datetime
    updated_at: datetime


class DocumentSectionRead(ApiModel):
    id: UUID
    document_id: UUID
    ordinal: int
    raw_schema_name: str | None
    raw_table_name: str
    raw_column_name: str | None
    raw_description: str


class DocumentMappingRead(ApiModel):
    id: UUID
    document_section_id: UUID
    subject_type: str
    subject_id: str | None
    mapping_kind: str
    confidence: float


class DocumentClaimRead(ApiModel):
    id: UUID
    document_section_id: UUID
    subject_type: str
    subject_id: str
    predicate: str
    object_value: str
    confidence: float
    status: str
    governance_review_id: UUID | None
    created_by: str
    reviewed_by: str | None
    reviewed_at: datetime | None


class DocumentMappingSummaryRead(ApiModel):
    document_id: UUID
    matched_count: int
    unmatched_count: int


async def _project_scope(
    session: AsyncSession, project_id: UUID, context: SecurityContext
) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    return project


async def _document_scope(
    session: AsyncSession, document_id: UUID, context: SecurityContext
) -> Document:
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    enforce_organization(context, document.organization_id)
    return document


@router.post(
    "/projects/{project_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    project_id: UUID,
    body: DocumentCreate,
    context: SecurityContext = Depends(require_roles(*DOCUMENT_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Document:
    project = await _project_scope(session, project_id, context)
    document = await create_document_from_csv(
        session,
        organization_id=project.organization_id,
        project_id=project.id,
        filename=body.filename,
        content=body.content,
        uploaded_by=context.principal_id,
    )
    record_audit(
        session,
        context,
        action="document.upload",
        resource_type="document",
        resource_id=str(document.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "filename": document.filename,
            "section_count": document.section_count,
            "parse_error_count": document.parse_error_count,
        },
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="document",
        aggregate_id=str(document.id),
        event_type="document.uploaded.v1",
        payload={
            "document_id": str(document.id),
            "project_id": str(project.id),
            "section_count": document.section_count,
        },
    )
    await session.commit()
    return document


@router.get("/projects/{project_id}/documents", response_model=Page)
async def list_documents(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*DOCUMENT_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    project = await _project_scope(session, project_id, context)
    rows = (
        await session.scalars(
            select(Document)
            .where(Document.project_id == project.id)
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    count = await session.scalar(
        select(func.count()).select_from(
            select(Document.id).where(Document.project_id == project.id).subquery()
        )
    )
    return Page(
        items=[DocumentRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=count,
    )


@router.get("/documents/{document_id}", response_model=DocumentRead)
async def get_document(
    document_id: UUID,
    context: SecurityContext = Depends(require_roles(*DOCUMENT_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Document:
    return await _document_scope(session, document_id, context)


@router.get("/documents/{document_id}/sections", response_model=Page)
async def list_document_sections(
    document_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*DOCUMENT_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    document = await _document_scope(session, document_id, context)
    rows = (
        await session.scalars(
            select(DocumentSection)
            .where(DocumentSection.document_id == document.id)
            .order_by(DocumentSection.ordinal)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    count = await session.scalar(
        select(func.count()).select_from(
            select(DocumentSection.id)
            .where(DocumentSection.document_id == document.id)
            .subquery()
        )
    )
    return Page(
        items=[DocumentSectionRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=count,
    )


@router.post(
    "/documents/{document_id}/map",
    response_model=DocumentMappingSummaryRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def map_document(
    document_id: UUID,
    context: SecurityContext = Depends(require_roles(*DOCUMENT_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> DocumentMappingSummaryRead:
    document = await _document_scope(session, document_id, context)
    if document.status != "PARSED":
        raise HTTPException(
            status_code=409, detail="document must be in PARSED status to be mapped"
        )
    mappings = await resolve_structural_mappings(session, document)
    matched_count = sum(1 for mapping in mappings if mapping.mapping_kind == "STRUCTURAL")
    unmatched_count = len(mappings) - matched_count
    record_audit(
        session,
        context,
        action="document.map",
        resource_type="document",
        resource_id=str(document.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"matched_count": matched_count, "unmatched_count": unmatched_count},
    )
    record_outbox(
        session,
        organization_id=document.organization_id,
        aggregate_type="document",
        aggregate_id=str(document.id),
        event_type="document.mapped.v1",
        payload={
            "document_id": str(document.id),
            "matched_count": matched_count,
            "unmatched_count": unmatched_count,
        },
    )
    await session.commit()
    return DocumentMappingSummaryRead(
        document_id=document.id, matched_count=matched_count, unmatched_count=unmatched_count
    )


@router.get("/documents/{document_id}/mappings", response_model=Page)
async def list_document_mappings(
    document_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*DOCUMENT_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    document = await _document_scope(session, document_id, context)
    section_ids_statement = select(DocumentSection.id).where(
        DocumentSection.document_id == document.id
    )
    rows = (
        await session.scalars(
            select(DocumentMapping)
            .where(DocumentMapping.document_section_id.in_(section_ids_statement))
            .limit(limit)
            .offset(offset)
        )
    ).all()
    count = await session.scalar(
        select(func.count()).select_from(
            select(DocumentMapping.id)
            .where(DocumentMapping.document_section_id.in_(section_ids_statement))
            .subquery()
        )
    )
    return Page(
        items=[DocumentMappingRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=count,
    )


@router.post(
    "/documents/{document_id}/extract-claims",
    response_model=Page,
    status_code=status.HTTP_202_ACCEPTED,
)
async def extract_claims(
    document_id: UUID,
    context: SecurityContext = Depends(require_roles(*DOCUMENT_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    document = await _document_scope(session, document_id, context)
    if document.status != "MAPPED":
        raise HTTPException(
            status_code=409, detail="document must be in MAPPED status before extracting claims"
        )
    claims = await extract_description_claims(session, document, requested_by=context.principal_id)
    record_audit(
        session,
        context,
        action="document.extract_claims",
        resource_type="document",
        resource_id=str(document.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"claim_count": len(claims)},
    )
    record_outbox(
        session,
        organization_id=document.organization_id,
        aggregate_type="document",
        aggregate_id=str(document.id),
        event_type="document.claims_extracted.v1",
        payload={"document_id": str(document.id), "claim_count": len(claims)},
    )
    await session.commit()
    return Page(
        items=[DocumentClaimRead.model_validate(claim) for claim in claims],
        limit=len(claims),
        offset=0,
        total=len(claims),
    )


@router.get("/documents/{document_id}/claims", response_model=Page)
async def list_document_claims(
    document_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*DOCUMENT_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    document = await _document_scope(session, document_id, context)
    section_ids_statement = select(DocumentSection.id).where(
        DocumentSection.document_id == document.id
    )
    rows = (
        await session.scalars(
            select(DocumentClaim)
            .where(DocumentClaim.document_section_id.in_(section_ids_statement))
            .limit(limit)
            .offset(offset)
        )
    ).all()
    count = await session.scalar(
        select(func.count()).select_from(
            select(DocumentClaim.id)
            .where(DocumentClaim.document_section_id.in_(section_ids_statement))
            .subquery()
        )
    )
    return Page(
        items=[DocumentClaimRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=count,
    )
