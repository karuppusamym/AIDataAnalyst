import hashlib
import json
from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from aida.context import get_correlation_id
from aida.context_product_policy import (
    evaluate_context_product_purpose,
    evaluate_context_product_quality_from_db,
)
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    ContextProduct,
    ContextProductConsumptionEdge,
    ContextProductRoleBinding,
    ContextProductVersion,
    DataSource,
    GlossaryTermVersion,
    GovernanceReview,
    GovernedTool,
    GovernedToolVersion,
    MetadataTable,
    Project,
    SemanticModelVersion,
)
from aida.schemas import (
    ContextProductCreate,
    ContextProductDefinition,
    ContextProductRead,
    ContextProductVersionCreate,
    ContextProductVersionRead,
    ContextProductVersionUpdate,
    GovernanceReviewRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["context-products"])

CONTEXT_PRODUCT_AUTHORS = ("PlatformAdmin", "SemanticAdmin", "DataSteward")
CONTEXT_PRODUCT_READERS = (
    "PlatformAdmin",
    "SemanticAdmin",
    "DataSteward",
    "Reviewer",
    "Analyst",
    "Auditor",
    "Viewer",
)
CONTEXT_PRODUCT_LIFECYCLE_READERS = frozenset(
    {*CONTEXT_PRODUCT_AUTHORS, "Reviewer", "Auditor"}
)


def context_product_fingerprint(body: ContextProductDefinition) -> str:
    payload = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _can_read_context_product_version(
    context: SecurityContext, version: ContextProductVersion
) -> bool:
    if not context.roles.isdisjoint(CONTEXT_PRODUCT_LIFECYCLE_READERS):
        return True
    return version.status == "PUBLISHED" and not context.roles.isdisjoint(
        version.allowed_consumer_roles
    )


def _can_read_lifecycle(context: SecurityContext) -> bool:
    return not context.roles.isdisjoint(CONTEXT_PRODUCT_LIFECYCLE_READERS)


async def _replace_role_bindings(
    session: AsyncSession, version: ContextProductVersion
) -> None:
    await session.execute(
        delete(ContextProductRoleBinding).where(
            ContextProductRoleBinding.context_product_version_id == version.id
        )
    )
    for role_name in version.allowed_consumer_roles:
        session.add(
            ContextProductRoleBinding(
                organization_id=version.organization_id,
                context_product_version_id=version.id,
                role_name=role_name,
            )
        )


def _definition_from_version(version: ContextProductVersion) -> ContextProductDefinition:
    return ContextProductDefinition.model_validate(
        {
            "name": version.name,
            "description": version.description,
            "purpose": version.purpose,
            "owner_principal": version.owner_principal,
            "table_ids": version.table_ids,
            "semantic_model_version_ids": version.semantic_model_version_ids,
            "glossary_term_version_ids": version.glossary_term_version_ids,
            "eligible_tool_version_ids": version.eligible_tool_version_ids,
            "allowed_consumer_roles": version.allowed_consumer_roles,
            "lineage_depth": version.lineage_depth,
            "quality_requirements": version.quality_requirements,
            "policy_summary": version.policy_summary,
        }
    )


def _apply_definition(
    version: ContextProductVersion, body: ContextProductDefinition
) -> ContextProductVersion:
    payload = body.model_dump(mode="json")
    version.name = body.name
    version.description = body.description
    version.purpose = body.purpose
    version.owner_principal = body.owner_principal
    version.table_ids = payload["table_ids"]
    version.semantic_model_version_ids = payload["semantic_model_version_ids"]
    version.glossary_term_version_ids = payload["glossary_term_version_ids"]
    version.eligible_tool_version_ids = payload["eligible_tool_version_ids"]
    version.allowed_consumer_roles = list(body.allowed_consumer_roles)
    version.lineage_depth = body.lineage_depth
    version.quality_requirements = payload["quality_requirements"]
    version.policy_summary = payload["policy_summary"]
    version.fingerprint = context_product_fingerprint(body)
    return version


def _version_read(
    product: ContextProduct, version: ContextProductVersion
) -> ContextProductVersionRead:
    return ContextProductVersionRead(
        **_definition_from_version(version).model_dump(),
        id=version.id,
        organization_id=version.organization_id,
        product_id=version.product_id,
        product_key=product.product_key,
        version=version.version,
        status=version.status,
        fingerprint=version.fingerprint,
        created_by=version.created_by,
        approved_by=version.approved_by,
        approved_at=version.approved_at,
        published_at=version.published_at,
        based_on_version_id=version.based_on_version_id,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


def _product_read(
    product: ContextProduct, latest_version: ContextProductVersion
) -> ContextProductRead:
    return ContextProductRead(
        id=product.id,
        organization_id=product.organization_id,
        project_id=product.project_id,
        product_key=product.product_key,
        lifecycle_status=product.lifecycle_status,
        created_by=product.created_by,
        latest_version=_version_read(product, latest_version),
        created_at=product.created_at,
        updated_at=product.updated_at,
    )


async def _project_scope(
    session: AsyncSession, project_id: UUID, context: SecurityContext
) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    return project


async def _product_scope(
    session: AsyncSession, product_id: UUID, context: SecurityContext
) -> ContextProduct:
    product = await session.get(ContextProduct, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="context product not found")
    enforce_organization(context, product.organization_id)
    return product


async def _version_scope(
    session: AsyncSession, version_id: UUID, context: SecurityContext
) -> tuple[ContextProduct, ContextProductVersion]:
    version = await session.get(ContextProductVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="context product version not found")
    enforce_organization(context, version.organization_id)
    product = await session.get(ContextProduct, version.product_id)
    if product is None or product.organization_id != version.organization_id:
        raise HTTPException(status_code=409, detail="context product identity is unavailable")
    return product, version


async def _require_exact_ids(
    session: AsyncSession,
    statement: Select[tuple[UUID]],
    expected: list[UUID],
    label: str,
) -> None:
    if not expected:
        return
    found = set((await session.scalars(statement)).all())
    if found != set(expected):
        raise HTTPException(
            status_code=422,
            detail=f"one or more {label} are not available in the approved product scope",
        )


async def _validate_references(
    session: AsyncSession,
    project: Project,
    body: ContextProductDefinition,
) -> None:
    await _require_exact_ids(
        session,
        select(MetadataTable.id)
        .join(DataSource, DataSource.id == MetadataTable.datasource_id)
        .where(
            MetadataTable.id.in_(body.table_ids),
            MetadataTable.organization_id == project.organization_id,
            MetadataTable.status == "ACTIVE",
            DataSource.project_id == project.id,
        ),
        body.table_ids,
        "tables",
    )
    await _require_exact_ids(
        session,
        select(SemanticModelVersion.id).where(
            SemanticModelVersion.id.in_(body.semantic_model_version_ids),
            SemanticModelVersion.organization_id == project.organization_id,
            SemanticModelVersion.project_id == project.id,
            SemanticModelVersion.status == "PUBLISHED",
        ),
        body.semantic_model_version_ids,
        "semantic model versions",
    )
    await _require_exact_ids(
        session,
        select(GlossaryTermVersion.id).where(
            GlossaryTermVersion.id.in_(body.glossary_term_version_ids),
            GlossaryTermVersion.organization_id == project.organization_id,
            GlossaryTermVersion.status == "APPROVED",
        ),
        body.glossary_term_version_ids,
        "glossary term versions",
    )
    await _require_exact_ids(
        session,
        select(GovernedToolVersion.id)
        .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
        .where(
            GovernedToolVersion.id.in_(body.eligible_tool_version_ids),
            GovernedToolVersion.organization_id == project.organization_id,
            GovernedToolVersion.status == "PUBLISHED",
            GovernedTool.project_id == project.id,
        ),
        body.eligible_tool_version_ids,
        "governed tool versions",
    )


@router.post(
    "/projects/{project_id}/context-products",
    response_model=ContextProductRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_context_product(
    project_id: UUID,
    body: ContextProductCreate,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductRead:
    project = await _project_scope(session, project_id, context)
    await _validate_references(session, project, body)
    existing = await session.scalar(
        select(ContextProduct.id).where(
            ContextProduct.organization_id == project.organization_id,
            ContextProduct.product_key == body.product_key,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="context product key already exists")

    product = ContextProduct(
        organization_id=project.organization_id,
        project_id=project.id,
        product_key=body.product_key,
        created_by=context.principal_id,
    )
    session.add(product)
    await session.flush()
    version = _apply_definition(
        ContextProductVersion(
            organization_id=project.organization_id,
            product_id=product.id,
            version=1,
            created_by=context.principal_id,
        ),
        body,
    )
    session.add(version)
    await session.flush()
    await _replace_role_bindings(session, version)
    audit_context = replace(context, organization_id=project.organization_id)
    record_audit(
        session,
        audit_context,
        action="context_product.create",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"product_key": product.product_key, "version": version.version},
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="context_product_version",
        aggregate_id=str(version.id),
        event_type="context.product_draft_created.v1",
        payload={
            "context_product_id": str(product.id),
            "context_product_version_id": str(version.id),
            "version": version.version,
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="context product allocation conflict") from exc
    return _product_read(product, version)


@router.get("/projects/{project_id}/context-products", response_model=Page)
async def list_context_products(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    project = await _project_scope(session, project_id, context)
    filters = (
        ContextProduct.organization_id == project.organization_id,
        ContextProduct.project_id == project.id,
    )
    statement = select(ContextProduct, ContextProductVersion).join(
        ContextProductVersion,
        ContextProductVersion.product_id == ContextProduct.id,
    )
    count_statement = select(func.count(func.distinct(ContextProduct.id))).select_from(
        ContextProduct
    ).join(ContextProductVersion, ContextProductVersion.product_id == ContextProduct.id)
    if _can_read_lifecycle(context):
        latest_version = (
            select(func.max(ContextProductVersion.version))
            .where(ContextProductVersion.product_id == ContextProduct.id)
            .correlate(ContextProduct)
            .scalar_subquery()
        )
        visibility: tuple[ColumnElement[bool], ...] = (
            ContextProductVersion.version == latest_version,
        )
    else:
        statement = statement.join(
            ContextProductRoleBinding,
            ContextProductRoleBinding.context_product_version_id
            == ContextProductVersion.id,
        ).distinct()
        count_statement = count_statement.join(
            ContextProductRoleBinding,
            ContextProductRoleBinding.context_product_version_id
            == ContextProductVersion.id,
        )
        visibility = (
            ContextProductVersion.status == "PUBLISHED",
            ContextProductRoleBinding.organization_id == project.organization_id,
            ContextProductRoleBinding.role_name.in_(context.roles),
        )
    rows = (
        await session.execute(
            statement
            .where(*filters, *visibility)
            .order_by(ContextProduct.product_key)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(count_statement.where(*filters, *visibility))
    return Page(
        items=[_product_read(product, version) for product, version in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/context-products/{product_id}/versions", response_model=Page)
async def list_context_product_versions(
    product_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    product = await _product_scope(session, product_id, context)
    statement = select(ContextProductVersion)
    count_statement = select(func.count()).select_from(ContextProductVersion)
    visibility: tuple[ColumnElement[bool], ...] = ()
    if not _can_read_lifecycle(context):
        statement = statement.join(
            ContextProductRoleBinding,
            ContextProductRoleBinding.context_product_version_id
            == ContextProductVersion.id,
        ).distinct()
        count_statement = count_statement.join(
            ContextProductRoleBinding,
            ContextProductRoleBinding.context_product_version_id
            == ContextProductVersion.id,
        )
        visibility = (
            ContextProductVersion.status == "PUBLISHED",
            ContextProductRoleBinding.organization_id == product.organization_id,
            ContextProductRoleBinding.role_name.in_(context.roles),
        )
    versions = (
        await session.scalars(
            statement
            .where(ContextProductVersion.product_id == product.id, *visibility)
            .order_by(ContextProductVersion.version.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(
        count_statement.where(ContextProductVersion.product_id == product.id, *visibility)
    )
    return Page(
        items=[_version_read(product, version) for version in versions],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/context-product-versions/{version_id}", response_model=ContextProductVersionRead
)
async def get_context_product_version(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_READERS)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductVersionRead:
    product, version = await _version_scope(session, version_id, context)
    if not _can_read_context_product_version(context, version):
        raise HTTPException(status_code=404, detail="context product version not found")
    if not _can_read_lifecycle(context):
        purpose_decision = evaluate_context_product_purpose(
            context.business_purpose, version.policy_summary
        )
        if not purpose_decision.allowed:
            record_audit(
                session,
                context,
                action="context_product.read.purpose_denied",
                resource_type="context_product_version",
                resource_id=str(version.id),
                outcome="DENIED",
                correlation_id=get_correlation_id(),
                details={"purpose": purpose_decision.snapshot()},
            )
            await session.commit()
            raise HTTPException(status_code=404, detail="context product version not found")
        quality_decision = await evaluate_context_product_quality_from_db(
            session,
            organization_id=version.organization_id,
            table_id_values=version.table_ids,
            requirements=version.quality_requirements,
        )
        if not quality_decision.allowed:
            record_audit(
                session,
                context,
                action="context_product.read.quality_denied",
                resource_type="context_product_version",
                resource_id=str(version.id),
                outcome="DENIED",
                correlation_id=get_correlation_id(),
                details={"quality": quality_decision.snapshot()},
            )
            await session.commit()
            raise HTTPException(status_code=404, detail="context product version not found")
        correlation_id = get_correlation_id()
        session.add(
            ContextProductConsumptionEdge(
                organization_id=version.organization_id,
                context_product_version_id=version.id,
                principal_id=context.principal_id,
                principal_type=context.principal_type,
                channel="REST",
                correlation_id=correlation_id,
                product_fingerprint=version.fingerprint,
                policy_decision="ALLOW",
                quality_snapshot=quality_decision.snapshot(),
            )
        )
        record_audit(
            session,
            context,
            action="context_product.read",
            resource_type="context_product_version",
            resource_id=str(version.id),
            outcome="SUCCESS",
            correlation_id=correlation_id,
            details={"fingerprint": version.fingerprint},
        )
        record_outbox(
            session,
            organization_id=version.organization_id,
            aggregate_type="context_product_version",
            aggregate_id=str(version.id),
            event_type="context.product_consumed.v1",
            payload={
                "product_key": product.product_key,
                "version": version.version,
                "fingerprint": version.fingerprint,
                "principal_id": context.principal_id,
                "channel": "REST",
            },
        )
        await session.commit()
    return _version_read(product, version)


@router.post(
    "/context-products/{product_id}/versions",
    response_model=ContextProductVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_context_product_version(
    product_id: UUID,
    body: ContextProductVersionCreate,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductVersionRead:
    product = await _product_scope(session, product_id, context)
    if product.lifecycle_status != "ACTIVE":
        raise HTTPException(status_code=409, detail="context product is not active")
    project = await _project_scope(session, product.project_id, context)
    await _validate_references(session, project, body)
    if body.based_on_version_id is not None:
        base = await session.get(ContextProductVersion, body.based_on_version_id)
        if base is None or base.product_id != product.id:
            raise HTTPException(status_code=422, detail="base context product version is invalid")
    latest = await session.scalar(
        select(func.max(ContextProductVersion.version)).where(
            ContextProductVersion.product_id == product.id
        )
    )
    version = _apply_definition(
        ContextProductVersion(
            organization_id=product.organization_id,
            product_id=product.id,
            version=(latest or 0) + 1,
            created_by=context.principal_id,
            based_on_version_id=body.based_on_version_id,
        ),
        body,
    )
    session.add(version)
    await session.flush()
    await _replace_role_bindings(session, version)
    record_audit(
        session,
        replace(context, organization_id=product.organization_id),
        action="context_product.version.create",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"product_key": product.product_key, "version": version.version},
    )
    record_outbox(
        session,
        organization_id=product.organization_id,
        aggregate_type="context_product_version",
        aggregate_id=str(version.id),
        event_type="context.product_draft_created.v1",
        payload={
            "context_product_id": str(product.id),
            "context_product_version_id": str(version.id),
            "version": version.version,
        },
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="context product version conflict") from exc
    return _version_read(product, version)


@router.put(
    "/context-product-versions/{version_id}", response_model=ContextProductVersionRead
)
async def update_context_product_version(
    version_id: UUID,
    body: ContextProductVersionUpdate,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> ContextProductVersionRead:
    product, version = await _version_scope(session, version_id, context)
    if version.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only draft context products can be changed")
    project = await _project_scope(session, product.project_id, context)
    await _validate_references(session, project, body)
    _apply_definition(version, body)
    await _replace_role_bindings(session, version)
    record_audit(
        session,
        replace(context, organization_id=product.organization_id),
        action="context_product.version.update",
        resource_type="context_product_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"fingerprint": version.fingerprint, "version": version.version},
    )
    await session.commit()
    return _version_read(product, version)


@router.post(
    "/context-product-versions/{version_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_context_product_version(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    product, version = await _version_scope(session, version_id, context)
    if version.status == "REVIEW_REQUIRED":
        existing = await session.scalar(
            select(GovernanceReview).where(
                GovernanceReview.object_type == "CONTEXT_PRODUCT_VERSION",
                GovernanceReview.object_id == str(version.id),
                GovernanceReview.status == "PENDING",
            )
        )
        if existing is not None:
            return existing
    if version.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only a draft context product can be submitted")
    project = await _project_scope(session, product.project_id, context)
    await _validate_references(session, project, _definition_from_version(version))
    review = GovernanceReview(
        organization_id=product.organization_id,
        object_type="CONTEXT_PRODUCT_VERSION",
        object_id=str(version.id),
        requested_action="PUBLISH",
        requested_by=context.principal_id,
    )
    session.add(review)
    version.status = "REVIEW_REQUIRED"
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=product.organization_id),
        action="context_product.version.submit",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"context_product_version_id": str(version.id)},
    )
    record_outbox(
        session,
        organization_id=product.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": review.object_id,
        },
    )
    await session.commit()
    return review


@router.post(
    "/context-product-versions/{version_id}/deprecate",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_context_product_deprecation(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*CONTEXT_PRODUCT_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    product, version = await _version_scope(session, version_id, context)
    existing = await session.scalar(
        select(GovernanceReview).where(
            GovernanceReview.object_type == "CONTEXT_PRODUCT_VERSION",
            GovernanceReview.object_id == str(version.id),
            GovernanceReview.requested_action == "DEPRECATE",
            GovernanceReview.status == "PENDING",
        )
    )
    if existing is not None:
        return existing
    if version.status != "PUBLISHED":
        raise HTTPException(status_code=409, detail="only a published context product can retire")
    review = GovernanceReview(
        organization_id=product.organization_id,
        object_type="CONTEXT_PRODUCT_VERSION",
        object_id=str(version.id),
        requested_action="DEPRECATE",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=product.organization_id),
        action="context_product.version.deprecation_request",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"context_product_version_id": str(version.id)},
    )
    record_outbox(
        session,
        organization_id=product.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": review.object_id,
            "requested_action": review.requested_action,
        },
    )
    await session.commit()
    return review
