from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    AssetTermLink,
    GlossaryCategory,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    MetadataTable,
)
from aida.schemas import (
    AssetDocumentationVersionCreate,
    AssetDocumentationVersionRead,
    AssetTermLinkCreate,
    AssetTermLinkRead,
    GlossaryTermCreate,
    GlossaryTermVersionCreate,
    GlossaryTermVersionRead,
    GovernanceReviewRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["glossary-and-documentation"])

GLOSSARY_READ_ROLES = (
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
GLOSSARY_WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "SemanticAdmin", "DataSteward")


def _term_read(term: GlossaryTerm, version: GlossaryTermVersion) -> GlossaryTermVersionRead:
    return GlossaryTermVersionRead(
        id=version.id,
        organization_id=version.organization_id,
        term_id=term.id,
        term_key=term.term_key,
        category_id=term.category_id,
        lifecycle_status=term.lifecycle_status,
        version=version.version,
        status=version.status,
        display_name=version.display_name,
        definition=version.definition,
        synonyms=version.synonyms,
        owner_principal=version.owner_principal,
        created_by=version.created_by,
        approved_by=version.approved_by,
        approved_at=version.approved_at,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


def _documentation_read(
    documentation: AssetDocumentation,
    version: AssetDocumentationVersion,
) -> AssetDocumentationVersionRead:
    return AssetDocumentationVersionRead(
        id=version.id,
        organization_id=version.organization_id,
        documentation_id=documentation.id,
        table_id=documentation.table_id,
        version=version.version,
        status=version.status,
        aliases=version.aliases,
        readme=version.readme,
        owner_principal=version.owner_principal,
        created_by=version.created_by,
        approved_by=version.approved_by,
        approved_at=version.approved_at,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


async def _table(session: AsyncSession, table_id: UUID, context: SecurityContext) -> MetadataTable:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="metadata table not found")
    enforce_organization(context, table.organization_id)
    return table


async def _category(
    session: AsyncSession, category_id: UUID | None, organization_id: UUID
) -> GlossaryCategory | None:
    if category_id is None:
        return None
    category = await session.get(GlossaryCategory, category_id)
    if (
        category is None
        or category.organization_id != organization_id
        or category.status != "ACTIVE"
    ):
        raise HTTPException(status_code=404, detail="active glossary category not found")
    return category


async def _submit_review(
    session: AsyncSession,
    *,
    context: SecurityContext,
    organization_id: UUID,
    object_type: str,
    object_id: UUID,
    action: str,
) -> GovernanceReview:
    existing = await session.scalar(
        select(GovernanceReview).where(
            GovernanceReview.object_type == object_type,
            GovernanceReview.object_id == str(object_id),
            GovernanceReview.status == "PENDING",
        )
    )
    if existing is not None:
        return existing
    review = GovernanceReview(
        organization_id=organization_id,
        object_type=object_type,
        object_id=str(object_id),
        requested_action="PUBLISH",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    audit_context = replace(context, organization_id=organization_id)
    record_audit(
        session,
        audit_context,
        action=action,
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"object_type": object_type, "object_id": str(object_id)},
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": object_type,
            "object_id": str(object_id),
        },
    )
    return review


@router.post(
    "/organizations/{organization_id}/glossary-terms",
    response_model=GlossaryTermVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_glossary_term(
    organization_id: UUID,
    body: GlossaryTermCreate,
    context: SecurityContext = Depends(require_roles(*GLOSSARY_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GlossaryTermVersionRead:
    enforce_organization(context, organization_id)
    existing = await session.scalar(
        select(GlossaryTerm).where(
            GlossaryTerm.organization_id == organization_id,
            GlossaryTerm.term_key == body.term_key,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="glossary term key already exists")
    await _category(session, body.category_id, organization_id)
    term = GlossaryTerm(
        organization_id=organization_id,
        term_key=body.term_key,
        category_id=body.category_id,
    )
    session.add(term)
    await session.flush()
    version = GlossaryTermVersion(
        organization_id=organization_id,
        term_id=term.id,
        version=1,
        display_name=body.display_name,
        definition=body.definition,
        synonyms=body.synonyms,
        owner_principal=body.owner_principal,
        created_by=context.principal_id,
    )
    session.add(version)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="glossary.term.create",
        resource_type="glossary_term_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"term_id": str(term.id), "term_key": term.term_key, "version": 1},
    )
    await session.commit()
    return _term_read(term, version)


@router.get("/organizations/{organization_id}/glossary-terms", response_model=Page)
async def list_glossary_terms(
    organization_id: UUID,
    query: str | None = Query(default=None, alias="q", max_length=200),
    term_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*GLOSSARY_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    latest = (
        select(
            GlossaryTermVersion.term_id,
            func.max(GlossaryTermVersion.version).label("latest_version"),
        )
        .group_by(GlossaryTermVersion.term_id)
        .subquery()
    )
    filters = [GlossaryTerm.organization_id == organization_id]
    if term_status:
        filters.append(GlossaryTermVersion.status == term_status.upper())
    if query:
        pattern = f"%{query.strip()}%"
        filters.append(
            GlossaryTerm.term_key.ilike(pattern)
            | GlossaryTermVersion.display_name.ilike(pattern)
            | GlossaryTermVersion.definition.ilike(pattern)
        )
    base = (
        select(GlossaryTerm, GlossaryTermVersion)
        .join(latest, latest.c.term_id == GlossaryTerm.id)
        .join(
            GlossaryTermVersion,
            (GlossaryTermVersion.term_id == GlossaryTerm.id)
            & (GlossaryTermVersion.version == latest.c.latest_version),
        )
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(GlossaryTermVersion.display_name).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[_term_read(term, version) for term, version in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/glossary-terms/{term_id}/versions",
    response_model=GlossaryTermVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_glossary_term_version(
    term_id: UUID,
    body: GlossaryTermVersionCreate,
    context: SecurityContext = Depends(require_roles(*GLOSSARY_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GlossaryTermVersionRead:
    term = await session.get(GlossaryTerm, term_id)
    if term is None:
        raise HTTPException(status_code=404, detail="glossary term not found")
    enforce_organization(context, term.organization_id)
    if term.lifecycle_status == "DEPRECATED":
        raise HTTPException(status_code=409, detail="deprecated glossary terms cannot be versioned")
    await _category(session, body.category_id, term.organization_id)
    open_version = await session.scalar(
        select(GlossaryTermVersion).where(
            GlossaryTermVersion.term_id == term.id,
            GlossaryTermVersion.status.in_(("DRAFT", "REVIEW_REQUIRED")),
        )
    )
    if open_version is not None:
        raise HTTPException(status_code=409, detail="glossary term already has an open version")
    latest = await session.scalar(
        select(func.max(GlossaryTermVersion.version)).where(GlossaryTermVersion.term_id == term.id)
    )
    version = GlossaryTermVersion(
        organization_id=term.organization_id,
        term_id=term.id,
        version=(latest or 0) + 1,
        display_name=body.display_name,
        definition=body.definition,
        synonyms=body.synonyms,
        owner_principal=body.owner_principal,
        created_by=context.principal_id,
    )
    term.category_id = body.category_id
    session.add(version)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=term.organization_id),
        action="glossary.term.version.create",
        resource_type="glossary_term_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"term_id": str(term.id), "term_key": term.term_key, "version": version.version},
    )
    await session.commit()
    return _term_read(term, version)


@router.post(
    "/glossary-term-versions/{version_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_glossary_term_version(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*GLOSSARY_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    version = await session.get(GlossaryTermVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="glossary term version not found")
    enforce_organization(context, version.organization_id)
    if version.status == "REVIEW_REQUIRED":
        review = await _submit_review(
            session,
            context=context,
            organization_id=version.organization_id,
            object_type="GLOSSARY_TERM_VERSION",
            object_id=version.id,
            action="glossary.term.submit",
        )
        await session.commit()
        return review
    if version.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only a draft term can be submitted")
    version.status = "REVIEW_REQUIRED"
    review = await _submit_review(
        session,
        context=context,
        organization_id=version.organization_id,
        object_type="GLOSSARY_TERM_VERSION",
        object_id=version.id,
        action="glossary.term.submit",
    )
    await session.commit()
    return review


@router.get(
    "/metadata/tables/{table_id}/documentation",
    response_model=AssetDocumentationVersionRead,
)
async def get_asset_documentation(
    table_id: UUID,
    context: SecurityContext = Depends(require_roles(*GLOSSARY_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> AssetDocumentationVersionRead:
    table = await _table(session, table_id, context)
    documentation = await session.scalar(
        select(AssetDocumentation).where(AssetDocumentation.table_id == table.id)
    )
    if documentation is None:
        raise HTTPException(status_code=404, detail="asset documentation not found")
    version = await session.scalar(
        select(AssetDocumentationVersion)
        .where(AssetDocumentationVersion.documentation_id == documentation.id)
        .order_by(AssetDocumentationVersion.version.desc())
        .limit(1)
    )
    if version is None:
        raise HTTPException(status_code=404, detail="asset documentation version not found")
    return _documentation_read(documentation, version)


@router.post(
    "/metadata/tables/{table_id}/documentation-versions",
    response_model=AssetDocumentationVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_asset_documentation_version(
    table_id: UUID,
    body: AssetDocumentationVersionCreate,
    context: SecurityContext = Depends(require_roles(*GLOSSARY_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> AssetDocumentationVersionRead:
    table = await _table(session, table_id, context)
    documentation = await session.scalar(
        select(AssetDocumentation).where(AssetDocumentation.table_id == table.id)
    )
    if documentation is None:
        documentation = AssetDocumentation(
            organization_id=table.organization_id,
            table_id=table.id,
        )
        session.add(documentation)
        await session.flush()
    open_version = await session.scalar(
        select(AssetDocumentationVersion).where(
            AssetDocumentationVersion.documentation_id == documentation.id,
            AssetDocumentationVersion.status.in_(("DRAFT", "REVIEW_REQUIRED")),
        )
    )
    if open_version is not None:
        raise HTTPException(
            status_code=409,
            detail="asset documentation already has an open version",
        )
    latest = await session.scalar(
        select(func.max(AssetDocumentationVersion.version)).where(
            AssetDocumentationVersion.documentation_id == documentation.id
        )
    )
    version = AssetDocumentationVersion(
        organization_id=table.organization_id,
        documentation_id=documentation.id,
        version=(latest or 0) + 1,
        aliases=body.aliases,
        readme=body.readme,
        owner_principal=body.owner_principal,
        created_by=context.principal_id,
    )
    session.add(version)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=table.organization_id),
        action="asset.documentation.create",
        resource_type="asset_documentation_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"table_id": str(table.id), "version": version.version},
    )
    await session.commit()
    return _documentation_read(documentation, version)


@router.post(
    "/asset-documentation-versions/{version_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_asset_documentation_version(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*GLOSSARY_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    version = await session.get(AssetDocumentationVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="asset documentation version not found")
    enforce_organization(context, version.organization_id)
    if version.status == "REVIEW_REQUIRED":
        review = await _submit_review(
            session,
            context=context,
            organization_id=version.organization_id,
            object_type="ASSET_DOCUMENTATION_VERSION",
            object_id=version.id,
            action="asset.documentation.submit",
        )
        await session.commit()
        return review
    if version.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only draft documentation can be submitted")
    version.status = "REVIEW_REQUIRED"
    review = await _submit_review(
        session,
        context=context,
        organization_id=version.organization_id,
        object_type="ASSET_DOCUMENTATION_VERSION",
        object_id=version.id,
        action="asset.documentation.submit",
    )
    await session.commit()
    return review


@router.get("/metadata/tables/{table_id}/glossary-links", response_model=Page)
async def list_asset_term_links(
    table_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*GLOSSARY_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    table = await _table(session, table_id, context)
    base = (
        select(AssetTermLink, GlossaryTerm, GlossaryTermVersion)
        .join(GlossaryTerm, GlossaryTerm.id == AssetTermLink.term_id)
        .join(
            GlossaryTermVersion,
            (GlossaryTermVersion.term_id == GlossaryTerm.id)
            & (GlossaryTermVersion.status == "APPROVED"),
        )
        .where(AssetTermLink.table_id == table.id)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(GlossaryTermVersion.display_name).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[
            AssetTermLinkRead(
                id=link.id,
                organization_id=link.organization_id,
                table_id=link.table_id,
                term_id=term.id,
                term_key=term.term_key,
                display_name=version.display_name,
                definition=version.definition,
                linked_by=link.linked_by,
                link_type=link.link_type,
                confidence=link.confidence,
                source_annotation_id=link.source_annotation_id,
                created_at=link.created_at,
            )
            for link, term, version in rows
        ],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/metadata/tables/{table_id}/glossary-links",
    response_model=AssetTermLinkRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_asset_term_link(
    table_id: UUID,
    body: AssetTermLinkCreate,
    context: SecurityContext = Depends(require_roles(*GLOSSARY_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> AssetTermLinkRead:
    table = await _table(session, table_id, context)
    term = await session.get(GlossaryTerm, body.term_id)
    if term is None or term.organization_id != table.organization_id:
        raise HTTPException(status_code=404, detail="glossary term not found")
    approved = await session.scalar(
        select(GlossaryTermVersion)
        .where(
            GlossaryTermVersion.term_id == term.id,
            GlossaryTermVersion.status == "APPROVED",
        )
        .order_by(GlossaryTermVersion.version.desc())
        .limit(1)
    )
    if approved is None:
        raise HTTPException(status_code=409, detail="only approved glossary terms can be linked")
    existing = await session.scalar(
        select(AssetTermLink).where(
            AssetTermLink.table_id == table.id,
            AssetTermLink.term_id == term.id,
        )
    )
    if existing is not None:
        return AssetTermLinkRead(
            id=existing.id,
            organization_id=existing.organization_id,
            table_id=existing.table_id,
            term_id=term.id,
            term_key=term.term_key,
            display_name=approved.display_name,
            definition=approved.definition,
            linked_by=existing.linked_by,
            link_type=existing.link_type,
            confidence=existing.confidence,
            source_annotation_id=existing.source_annotation_id,
            created_at=existing.created_at,
        )
    link = AssetTermLink(
        organization_id=table.organization_id,
        table_id=table.id,
        term_id=term.id,
        linked_by=context.principal_id,
    )
    session.add(link)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=table.organization_id),
        action="asset.glossary.link",
        resource_type="asset_term_link",
        resource_id=str(link.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"table_id": str(table.id), "term_id": str(term.id)},
    )
    await session.commit()
    return AssetTermLinkRead(
        id=link.id,
        organization_id=link.organization_id,
        table_id=link.table_id,
        term_id=term.id,
        term_key=term.term_key,
        display_name=approved.display_name,
        definition=approved.definition,
        linked_by=link.linked_by,
        link_type=link.link_type,
        confidence=link.confidence,
        source_annotation_id=link.source_annotation_id,
        created_at=link.created_at,
    )


@router.delete("/asset-term-links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_asset_term_link(
    link_id: UUID,
    context: SecurityContext = Depends(require_roles(*GLOSSARY_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Response:
    link = await session.get(AssetTermLink, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="asset glossary link not found")
    enforce_organization(context, link.organization_id)
    await session.delete(link)
    record_audit(
        session,
        replace(context, organization_id=link.organization_id),
        action="asset.glossary.unlink",
        resource_type="asset_term_link",
        resource_id=str(link.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"table_id": str(link.table_id), "term_id": str(link.term_id)},
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
