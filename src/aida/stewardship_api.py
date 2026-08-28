from dataclasses import replace
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    AssetCertification,
    AssetDocumentation,
    AssetDocumentationVersion,
    AssetTermLink,
    BulkStewardshipOperation,
    BusinessDomain,
    CoverageSnapshot,
    DataQualityPolicy,
    DataSource,
    GlossaryCategory,
    GlossaryConflict,
    GlossaryLinkProposal,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernanceReview,
    LineOfBusiness,
    MetadataBusinessAnnotation,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    OwnershipAssignment,
    OwnershipRule,
    Project,
)
from aida.schemas import (
    BulkStewardshipOperationCreate,
    BulkStewardshipOperationRead,
    CoverageDimensionRead,
    CoverageSnapshotRead,
    GlossaryCategoryCreate,
    GlossaryCategoryRead,
    GlossaryConflictCreate,
    GlossaryConflictRead,
    GlossaryConflictResolution,
    GlossaryLinkProposalGenerate,
    GlossaryLinkProposalRead,
    GlossaryTermDeprecationRequest,
    GovernanceReviewRead,
    OwnershipAssignmentRead,
    OwnershipRuleCreate,
    OwnershipRuleRead,
    Page,
    StewardshipCoverageRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["glossary-stewardship"])

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


def _audit_context(context: SecurityContext, organization_id: UUID) -> SecurityContext:
    return replace(context, organization_id=organization_id)


async def _new_review(
    session: AsyncSession,
    *,
    context: SecurityContext,
    organization_id: UUID,
    object_type: str,
    object_id: UUID,
    requested_action: str,
) -> GovernanceReview:
    review = GovernanceReview(
        organization_id=organization_id,
        object_type=object_type,
        object_id=str(object_id),
        requested_action=requested_action,
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    record_audit(
        session,
        _audit_context(context, organization_id),
        action="governance.review.request",
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
            "requested_action": requested_action,
        },
    )
    return review


async def _validate_subjects(
    session: AsyncSession,
    organization_id: UUID,
    subject_type: str,
    subject_ids: list[UUID],
) -> None:
    model = MetadataTable if subject_type == "TABLE" else GlossaryTerm
    count = await session.scalar(
        select(func.count())
        .select_from(model)
        .where(
            model.organization_id == organization_id,
            model.id.in_(subject_ids),
        )
    )
    if count != len(subject_ids):
        raise HTTPException(
            status_code=404,
            detail="one or more stewardship subjects were not found",
        )


async def _create_bulk_operation(
    session: AsyncSession,
    *,
    organization_id: UUID,
    body: BulkStewardshipOperationCreate,
    context: SecurityContext,
) -> BulkStewardshipOperation:
    allowed_subject = {
        "ASSIGN_OWNERSHIP": {"TABLE", "TERM"},
        "LINK_TERM": {"TABLE"},
        "DEPRECATE_TERM": {"TERM"},
        "CERTIFY_ASSET": {"TABLE"},
    }
    if body.subject_type not in allowed_subject[body.operation_type]:
        raise HTTPException(status_code=422, detail="operation and subject type are incompatible")
    await _validate_subjects(session, organization_id, body.subject_type, body.subject_ids)
    if body.term_id is not None:
        approved_term = await session.scalar(
            select(GlossaryTerm)
            .join(GlossaryTermVersion, GlossaryTermVersion.term_id == GlossaryTerm.id)
            .where(
                GlossaryTerm.id == body.term_id,
                GlossaryTerm.organization_id == organization_id,
                GlossaryTerm.lifecycle_status == "ACTIVE",
                GlossaryTermVersion.status == "APPROVED",
            )
        )
        if approved_term is None:
            raise HTTPException(status_code=409, detail="linking requires an approved active term")
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="certification expiry must be in the future")
    parameters = {
        key: value
        for key, value in {
            "owner_type": body.owner_type,
            "owner_principal": body.owner_principal,
            "term_id": str(body.term_id) if body.term_id else None,
            "rationale": body.rationale,
            "expires_at": body.expires_at.isoformat() if body.expires_at else None,
            "source_rule_id": str(body.source_rule_id) if body.source_rule_id else None,
        }.items()
        if value is not None
    }
    review = GovernanceReview(
        organization_id=organization_id,
        object_type="BULK_STEWARDSHIP_OPERATION",
        object_id="pending",
        requested_action=body.operation_type,
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    operation = BulkStewardshipOperation(
        organization_id=organization_id,
        operation_type=body.operation_type,
        subject_type=body.subject_type,
        subject_ids=[str(value) for value in body.subject_ids],
        parameters=parameters,
        governance_review_id=review.id,
        requested_by=context.principal_id,
    )
    session.add(operation)
    await session.flush()
    review.object_id = str(operation.id)
    record_audit(
        session,
        _audit_context(context, organization_id),
        action="stewardship.bulk.request",
        resource_type="bulk_stewardship_operation",
        resource_id=str(operation.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"operation_type": body.operation_type, "subject_count": len(body.subject_ids)},
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": str(operation.id),
            "requested_action": body.operation_type,
        },
    )
    return operation


@router.post(
    "/organizations/{organization_id}/glossary-categories",
    response_model=GlossaryCategoryRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_glossary_category(
    organization_id: UUID,
    body: GlossaryCategoryCreate,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GlossaryCategory:
    enforce_organization(context, organization_id)
    if body.parent_id is not None:
        parent = await session.get(GlossaryCategory, body.parent_id)
        if parent is None or parent.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="parent glossary category not found")
    existing = await session.scalar(
        select(GlossaryCategory).where(
            GlossaryCategory.organization_id == organization_id,
            GlossaryCategory.category_key == body.category_key,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="glossary category key already exists")
    category = GlossaryCategory(
        organization_id=organization_id,
        parent_id=body.parent_id,
        category_key=body.category_key,
        display_name=body.display_name,
        description=body.description,
        created_by=context.principal_id,
    )
    session.add(category)
    await session.flush()
    record_audit(
        session,
        _audit_context(context, organization_id),
        action="glossary.category.create",
        resource_type="glossary_category",
        resource_id=str(category.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"category_key": category.category_key},
    )
    await session.commit()
    return category


@router.get("/organizations/{organization_id}/glossary-categories", response_model=Page)
async def list_glossary_categories(
    organization_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = (
        GlossaryCategory.organization_id == organization_id,
        GlossaryCategory.status == "ACTIVE",
    )
    total = await session.scalar(select(func.count()).select_from(GlossaryCategory).where(*filters))
    rows = (
        await session.scalars(
            select(GlossaryCategory)
            .where(*filters)
            .order_by(GlossaryCategory.display_name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[GlossaryCategoryRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/organizations/{organization_id}/stewardship/bulk-operations",
    response_model=BulkStewardshipOperationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_bulk_stewardship_operation(
    organization_id: UUID,
    body: BulkStewardshipOperationCreate,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> BulkStewardshipOperation:
    enforce_organization(context, organization_id)
    operation = await _create_bulk_operation(
        session,
        organization_id=organization_id,
        body=body,
        context=context,
    )
    await session.commit()
    return operation


@router.get("/organizations/{organization_id}/stewardship/bulk-operations", response_model=Page)
async def list_bulk_stewardship_operations(
    organization_id: UUID,
    operation_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = [BulkStewardshipOperation.organization_id == organization_id]
    if operation_status:
        filters.append(BulkStewardshipOperation.status == operation_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(BulkStewardshipOperation).where(*filters)
    )
    rows = (
        await session.scalars(
            select(BulkStewardshipOperation)
            .where(*filters)
            .order_by(BulkStewardshipOperation.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[BulkStewardshipOperationRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/glossary-terms/{term_id}/deprecate",
    response_model=BulkStewardshipOperationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def deprecate_glossary_term(
    term_id: UUID,
    body: GlossaryTermDeprecationRequest,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> BulkStewardshipOperation:
    term = await session.get(GlossaryTerm, term_id)
    if term is None:
        raise HTTPException(status_code=404, detail="glossary term not found")
    enforce_organization(context, term.organization_id)
    if term.lifecycle_status == "DEPRECATED":
        raise HTTPException(status_code=409, detail="glossary term is already deprecated")
    operation = await _create_bulk_operation(
        session,
        organization_id=term.organization_id,
        body=BulkStewardshipOperationCreate(
            operation_type="DEPRECATE_TERM",
            subject_type="TERM",
            subject_ids=[term.id],
            rationale=body.reason,
        ),
        context=context,
    )
    await session.commit()
    return operation


@router.post(
    "/organizations/{organization_id}/ownership-rules",
    response_model=OwnershipRuleRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_ownership_rule(
    organization_id: UUID,
    body: OwnershipRuleCreate,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> OwnershipRule:
    enforce_organization(context, organization_id)
    existing = await session.scalar(
        select(OwnershipRule).where(
            OwnershipRule.organization_id == organization_id,
            OwnershipRule.rule_key == body.rule_key,
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="ownership rule key already exists")
    rule = OwnershipRule(
        organization_id=organization_id,
        created_by=context.principal_id,
        **body.model_dump(),
    )
    session.add(rule)
    await session.flush()
    record_audit(
        session,
        _audit_context(context, organization_id),
        action="ownership.rule.create",
        resource_type="ownership_rule",
        resource_id=str(rule.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"match_field": rule.match_field, "owner_type": rule.owner_type},
    )
    await session.commit()
    return rule


@router.get("/organizations/{organization_id}/ownership-rules", response_model=Page)
async def list_ownership_rules(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    rows = (
        await session.scalars(
            select(OwnershipRule)
            .where(
                OwnershipRule.organization_id == organization_id,
                OwnershipRule.status == "ACTIVE",
            )
            .order_by(OwnershipRule.display_name)
            .limit(500)
        )
    ).all()
    return Page(
        items=[OwnershipRuleRead.model_validate(row) for row in rows],
        limit=500,
        offset=0,
        total=len(rows),
    )


@router.post(
    "/ownership-rules/{rule_id}/apply",
    response_model=BulkStewardshipOperationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def apply_ownership_rule(
    rule_id: UUID,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> BulkStewardshipOperation:
    rule = await session.get(OwnershipRule, rule_id)
    if rule is None or rule.status != "ACTIVE":
        raise HTTPException(status_code=404, detail="active ownership rule not found")
    enforce_organization(context, rule.organization_id)
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataBusinessAnnotation, BusinessDomain)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .outerjoin(
                MetadataBusinessAnnotation,
                MetadataBusinessAnnotation.table_id == MetadataTable.id,
            )
            .outerjoin(
                BusinessDomain, BusinessDomain.id == MetadataBusinessAnnotation.domain_id
            )
            .where(
                MetadataTable.organization_id == rule.organization_id,
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataTable.id)
            .limit(10_000)
        )
    ).all()
    pattern = rule.match_pattern.casefold()
    matched: list[UUID] = []
    for table, schema, annotation, domain in rows:
        if rule.match_field == "TAG":
            tags = annotation.tags if annotation is not None else []
            is_match = any(fnmatchcase(tag.casefold(), pattern) for tag in tags)
        else:
            candidates = {
                "TABLE_NAME": table.name,
                "SCHEMA_NAME": schema.name,
                "QUALIFIED_NAME": f"{schema.name}.{table.name}",
                "DOMAIN_KEY": domain.domain_key if domain is not None else None,
            }
            value = candidates[rule.match_field]
            is_match = value is not None and fnmatchcase(value.casefold(), pattern)
        if is_match:
            matched.append(table.id)
        if len(matched) == 500:
            break
    if not matched:
        raise HTTPException(status_code=409, detail="ownership rule matched no active tables")
    operation = await _create_bulk_operation(
        session,
        organization_id=rule.organization_id,
        body=BulkStewardshipOperationCreate(
            operation_type="ASSIGN_OWNERSHIP",
            subject_type="TABLE",
            subject_ids=matched,
            owner_type=rule.owner_type,
            owner_principal=rule.owner_principal,
            source_rule_id=rule.id,
        ),
        context=context,
    )
    await session.commit()
    return operation


@router.get("/organizations/{organization_id}/ownership-assignments", response_model=Page)
async def list_ownership_assignments(
    organization_id: UUID,
    subject_type: str | None = Query(default=None, max_length=30),
    subject_id: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = [
        OwnershipAssignment.organization_id == organization_id,
        OwnershipAssignment.status == "ACTIVE",
    ]
    if subject_type:
        filters.append(OwnershipAssignment.subject_type == subject_type.upper())
    if subject_id:
        filters.append(OwnershipAssignment.subject_id == subject_id)
    total = await session.scalar(
        select(func.count()).select_from(OwnershipAssignment).where(*filters)
    )
    rows = (
        await session.scalars(
            select(OwnershipAssignment)
            .where(*filters)
            .order_by(OwnershipAssignment.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[OwnershipAssignmentRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/organizations/{organization_id}/glossary-conflicts",
    response_model=GlossaryConflictRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_glossary_conflict(
    organization_id: UUID,
    body: GlossaryConflictCreate,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GlossaryConflict:
    enforce_organization(context, organization_id)
    if body.term_id is not None:
        term = await session.get(GlossaryTerm, body.term_id)
        if term is None or term.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="glossary term not found")
    conflict = GlossaryConflict(
        organization_id=organization_id,
        term_id=body.term_id,
        conflict_type=body.conflict_type,
        position_a=body.position_a,
        position_b=body.position_b,
        assigned_owner=body.assigned_owner,
        raised_by=context.principal_id,
    )
    session.add(conflict)
    await session.flush()
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="glossary_conflict",
        aggregate_id=str(conflict.id),
        event_type="glossary.conflict_raised.v1",
        payload={"conflict_id": str(conflict.id), "conflict_type": conflict.conflict_type},
    )
    record_audit(
        session,
        _audit_context(context, organization_id),
        action="glossary.conflict.raise",
        resource_type="glossary_conflict",
        resource_id=str(conflict.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"conflict_type": conflict.conflict_type},
    )
    await session.commit()
    return conflict


@router.post("/organizations/{organization_id}/glossary-conflicts/detect", response_model=Page)
async def detect_glossary_conflicts(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    rows = (
        await session.execute(
            select(GlossaryTerm, GlossaryTermVersion)
            .join(GlossaryTermVersion, GlossaryTermVersion.term_id == GlossaryTerm.id)
            .where(
                GlossaryTerm.organization_id == organization_id,
                GlossaryTerm.lifecycle_status == "ACTIVE",
                GlossaryTermVersion.status == "APPROVED",
            )
            .limit(5000)
        )
    ).all()
    labels: dict[str, list[tuple[GlossaryTerm, GlossaryTermVersion, str]]] = {}
    for term, version in rows:
        for label in [version.display_name, *version.synonyms]:
            labels.setdefault(label.strip().casefold(), []).append((term, version, label))
    existing_rows = (
        await session.scalars(
            select(GlossaryConflict).where(
                GlossaryConflict.organization_id == organization_id,
                GlossaryConflict.status.in_(("OPEN", "REVIEW_REQUIRED")),
                GlossaryConflict.conflict_type == "SYNONYM_COLLISION",
            )
        )
    ).all()
    existing_pairs = {
        tuple(sorted((row.position_a.get("term_id", ""), row.position_b.get("term_id", ""))))
        for row in existing_rows
    }
    created: list[GlossaryConflict] = []
    for label, matches in labels.items():
        if len(matches) < 2:
            continue
        first = matches[0]
        for second in matches[1:]:
            pair = tuple(sorted((str(first[0].id), str(second[0].id))))
            same_definition = first[1].definition.casefold() == second[1].definition.casefold()
            if pair in existing_pairs or same_definition:
                continue
            conflict = GlossaryConflict(
                organization_id=organization_id,
                term_id=first[0].id,
                conflict_type="SYNONYM_COLLISION",
                position_a={
                    "term_id": str(first[0].id),
                    "display_name": first[1].display_name,
                    "definition": first[1].definition,
                    "colliding_label": label,
                },
                position_b={
                    "term_id": str(second[0].id),
                    "display_name": second[1].display_name,
                    "definition": second[1].definition,
                    "colliding_label": label,
                },
                assigned_owner=first[1].owner_principal,
                raised_by=context.principal_id,
            )
            session.add(conflict)
            created.append(conflict)
            existing_pairs.add(pair)
            if len(created) == 100:
                break
        if len(created) == 100:
            break
    await session.flush()
    for conflict in created:
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="glossary_conflict",
            aggregate_id=str(conflict.id),
            event_type="glossary.conflict_raised.v1",
            payload={"conflict_id": str(conflict.id), "conflict_type": conflict.conflict_type},
        )
    record_audit(
        session,
        _audit_context(context, organization_id),
        action="glossary.conflict.detect",
        resource_type="glossary_conflict",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"approved_terms_scanned": len(rows), "conflicts_created": len(created)},
    )
    await session.commit()
    return Page(
        items=[GlossaryConflictRead.model_validate(row) for row in created],
        limit=100,
        offset=0,
        total=len(created),
    )


@router.get("/organizations/{organization_id}/glossary-conflicts", response_model=Page)
async def list_glossary_conflicts(
    organization_id: UUID,
    conflict_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = [GlossaryConflict.organization_id == organization_id]
    if conflict_status:
        filters.append(GlossaryConflict.status == conflict_status.upper())
    total = await session.scalar(select(func.count()).select_from(GlossaryConflict).where(*filters))
    rows = (
        await session.scalars(
            select(GlossaryConflict)
            .where(*filters)
            .order_by(GlossaryConflict.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[GlossaryConflictRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/glossary-conflicts/{conflict_id}/resolution",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_conflict_resolution(
    conflict_id: UUID,
    body: GlossaryConflictResolution,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    conflict = await session.get(GlossaryConflict, conflict_id)
    if conflict is None:
        raise HTTPException(status_code=404, detail="glossary conflict not found")
    enforce_organization(context, conflict.organization_id)
    if conflict.status != "OPEN":
        raise HTTPException(status_code=409, detail="only open conflicts can be resolved")
    conflict.status = "REVIEW_REQUIRED"
    conflict.proposed_resolution = body.resolution
    conflict.proposed_definition = body.resolved_definition
    conflict.resolution_rationale = body.rationale
    review = await _new_review(
        session,
        context=context,
        organization_id=conflict.organization_id,
        object_type="GLOSSARY_CONFLICT",
        object_id=conflict.id,
        requested_action="RESOLVE",
    )
    await session.commit()
    return review


def _proposal_read(
    proposal: GlossaryLinkProposal,
    term: GlossaryTermVersion,
    table: MetadataTable,
) -> GlossaryLinkProposalRead:
    return GlossaryLinkProposalRead(
        id=proposal.id,
        organization_id=proposal.organization_id,
        table_id=proposal.table_id,
        term_id=proposal.term_id,
        term_display_name=term.display_name,
        table_name=table.name,
        source_annotation_id=proposal.source_annotation_id,
        confidence=proposal.confidence,
        evidence=proposal.evidence,
        status=proposal.status,
        governance_review_id=proposal.governance_review_id,
        created_by=proposal.created_by,
        reviewed_by=proposal.reviewed_by,
        reviewed_at=proposal.reviewed_at,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
    )


@router.post(
    "/organizations/{organization_id}/glossary-link-proposals/generate",
    response_model=Page,
)
async def generate_glossary_link_proposals(
    organization_id: UUID,
    body: GlossaryLinkProposalGenerate,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    term_rows = (
        await session.execute(
            select(GlossaryTerm, GlossaryTermVersion)
            .join(GlossaryTermVersion, GlossaryTermVersion.term_id == GlossaryTerm.id)
            .where(
                GlossaryTerm.organization_id == organization_id,
                GlossaryTerm.lifecycle_status == "ACTIVE",
                GlossaryTermVersion.status == "APPROVED",
            )
            .limit(5000)
        )
    ).all()
    label_index: dict[str, list[tuple[GlossaryTerm, GlossaryTermVersion, str]]] = {}
    for term, version in term_rows:
        labels = [(version.display_name, "DISPLAY_NAME"), (term.term_key, "TERM_KEY")]
        labels.extend((synonym, "SYNONYM") for synonym in version.synonyms)
        for label, kind in labels:
            label_index.setdefault(label.strip().casefold(), []).append((term, version, kind))
    annotations = (
        await session.scalars(
            select(MetadataBusinessAnnotation)
            .where(MetadataBusinessAnnotation.organization_id == organization_id)
            .order_by(MetadataBusinessAnnotation.id)
            .limit(10_000)
        )
    ).all()
    link_rows = (
        await session.execute(
            select(AssetTermLink.table_id, AssetTermLink.term_id).where(
                AssetTermLink.organization_id == organization_id
            )
        )
    ).all()
    existing_links = {(row[0], row[1]) for row in link_rows}
    proposal_rows = (
        await session.execute(
            select(
                GlossaryLinkProposal.table_id,
                GlossaryLinkProposal.term_id,
                GlossaryLinkProposal.source_annotation_id,
            ).where(GlossaryLinkProposal.organization_id == organization_id)
        )
    ).all()
    existing_proposals = {(row[0], row[1], row[2]) for row in proposal_rows}
    created: list[tuple[GlossaryLinkProposal, GlossaryTermVersion, MetadataTable]] = []
    tables = {
        row.id: row
        for row in (
            await session.scalars(
                select(MetadataTable).where(MetadataTable.organization_id == organization_id)
            )
        ).all()
    }
    for annotation in annotations:
        annotation_labels = [(annotation.business_name, "BUSINESS_NAME")]
        annotation_labels.extend((value, "ANNOTATION_SYNONYM") for value in annotation.synonyms)
        candidates: dict[UUID, tuple[GlossaryTerm, GlossaryTermVersion, float, str, str]] = {}
        for annotation_label, annotation_kind in annotation_labels:
            normalized = annotation_label.strip().casefold()
            for term, version, term_kind in label_index.get(normalized, []):
                is_primary_match = (
                    annotation_kind == "BUSINESS_NAME" and term_kind == "DISPLAY_NAME"
                )
                confidence = 1.0 if is_primary_match else 0.92
                current = candidates.get(term.id)
                if current is None or confidence > current[2]:
                    candidates[term.id] = (
                        term,
                        version,
                        confidence,
                        annotation_label,
                        term_kind,
                    )
        for term, version, confidence, matched_label, term_kind in candidates.values():
            key = (annotation.table_id, term.id, annotation.id)
            if (
                confidence < body.minimum_confidence
                or (annotation.table_id, term.id) in existing_links
                or key in existing_proposals
            ):
                continue
            table = tables.get(annotation.table_id)
            if table is None:
                continue
            proposal = GlossaryLinkProposal(
                organization_id=organization_id,
                table_id=annotation.table_id,
                term_id=term.id,
                source_annotation_id=annotation.id,
                confidence=confidence,
                evidence={
                    "strategy": "APPROVED_LABEL_EXACT_MATCH",
                    "matched_label": matched_label,
                    "term_label_kind": term_kind,
                    "annotation_version": annotation.version,
                },
                created_by=context.principal_id,
            )
            session.add(proposal)
            created.append((proposal, version, table))
            existing_proposals.add(key)
            if len(created) == body.limit:
                break
        if len(created) == body.limit:
            break
    await session.flush()
    record_audit(
        session,
        _audit_context(context, organization_id),
        action="glossary.link_proposal.generate",
        resource_type="glossary_link_proposal",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "annotations_scanned": len(annotations),
            "approved_terms_scanned": len(term_rows),
            "proposals_created": len(created),
        },
    )
    await session.commit()
    return Page(
        items=[_proposal_read(proposal, term, table) for proposal, term, table in created],
        limit=body.limit,
        offset=0,
        total=len(created),
    )


@router.get("/organizations/{organization_id}/glossary-link-proposals", response_model=Page)
async def list_glossary_link_proposals(
    organization_id: UUID,
    proposal_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = [GlossaryLinkProposal.organization_id == organization_id]
    if proposal_status:
        filters.append(GlossaryLinkProposal.status == proposal_status.upper())
    base = (
        select(GlossaryLinkProposal, GlossaryTermVersion, MetadataTable)
        .join(
            GlossaryTermVersion,
            (GlossaryTermVersion.term_id == GlossaryLinkProposal.term_id)
            & (GlossaryTermVersion.status == "APPROVED"),
        )
        .join(MetadataTable, MetadataTable.id == GlossaryLinkProposal.table_id)
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.order_by(GlossaryLinkProposal.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[_proposal_read(proposal, term, table) for proposal, term, table in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/glossary-link-proposals/{proposal_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_glossary_link_proposal(
    proposal_id: UUID,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    proposal = await session.get(GlossaryLinkProposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="glossary link proposal not found")
    enforce_organization(context, proposal.organization_id)
    if proposal.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only draft link proposals can be submitted")
    proposal.status = "REVIEW_REQUIRED"
    review = await _new_review(
        session,
        context=context,
        organization_id=proposal.organization_id,
        object_type="GLOSSARY_LINK_PROPOSAL",
        object_id=proposal.id,
        requested_action="APPROVE_LINK",
    )
    proposal.governance_review_id = review.id
    await session.commit()
    return review


async def _coverage(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID | None,
    domain_id: UUID | None,
    line_of_business_id: UUID | None,
) -> StewardshipCoverageRead:
    filters = [
        MetadataTable.organization_id == organization_id,
        MetadataTable.status == "ACTIVE",
    ]
    if datasource_id is not None:
        filters.append(MetadataTable.datasource_id == datasource_id)
    if domain_id is not None:
        domain_table_ids = select(MetadataBusinessAnnotation.table_id).where(
            MetadataBusinessAnnotation.organization_id == organization_id,
            MetadataBusinessAnnotation.domain_id == domain_id,
        )
        filters.append(MetadataTable.id.in_(domain_table_ids))
    if line_of_business_id is not None:
        lob_source_ids = (
            select(DataSource.id)
            .join(Project, Project.id == DataSource.project_id)
            .where(
                DataSource.organization_id == organization_id,
                Project.line_of_business_id == line_of_business_id,
            )
        )
        filters.append(MetadataTable.datasource_id.in_(lob_source_ids))
    tables = (await session.scalars(select(MetadataTable).where(*filters).limit(10_000))).all()
    table_ids = {table.id for table in tables}
    if not table_ids:
        empty = CoverageDimensionRead(covered=0, total=0, percentage=0.0)
        return StewardshipCoverageRead(
            organization_id=organization_id,
            datasource_id=datasource_id,
            domain_id=domain_id,
            line_of_business_id=line_of_business_id,
            table_count=0,
            overall_score=0.0,
            dimensions={
                name: empty
                for name in (
                    "documented",
                    "owned",
                    "classified",
                    "certified",
                    "quality_monitored",
                    "semantically_mapped",
                )
            },
            unowned_table_ids=[],
            computed_at=datetime.now(UTC),
        )
    documented = set(
        await session.scalars(
            select(AssetDocumentation.table_id)
            .join(
                AssetDocumentationVersion,
                AssetDocumentationVersion.documentation_id == AssetDocumentation.id,
            )
            .where(
                AssetDocumentation.table_id.in_(table_ids),
                AssetDocumentationVersion.status == "APPROVED",
            )
        )
    )
    owned = {
        UUID(value)
        for value in await session.scalars(
            select(OwnershipAssignment.subject_id).where(
                OwnershipAssignment.organization_id == organization_id,
                OwnershipAssignment.subject_type == "TABLE",
                OwnershipAssignment.status == "ACTIVE",
                OwnershipAssignment.subject_id.in_([str(value) for value in table_ids]),
            )
        )
    }
    owned.update(
        await session.scalars(
            select(AssetDocumentation.table_id)
            .join(
                AssetDocumentationVersion,
                AssetDocumentationVersion.documentation_id == AssetDocumentation.id,
            )
            .where(
                AssetDocumentation.table_id.in_(table_ids),
                AssetDocumentationVersion.status == "APPROVED",
                AssetDocumentationVersion.owner_principal.is_not(None),
            )
        )
    )
    classified = set(
        await session.scalars(
            select(MetadataColumn.table_id)
            .where(
                MetadataColumn.table_id.in_(table_ids),
                MetadataColumn.classification != "UNCLASSIFIED",
            )
            .distinct()
        )
    )
    certified = set(
        await session.scalars(
            select(AssetCertification.table_id).where(
                AssetCertification.table_id.in_(table_ids),
                AssetCertification.status == "ACTIVE",
                AssetCertification.expires_at > datetime.now(UTC),
            )
        )
    )
    policies = (
        await session.scalars(
            select(DataQualityPolicy).where(
                DataQualityPolicy.organization_id == organization_id,
                DataQualityPolicy.enabled.is_(True),
                or_(
                    DataQualityPolicy.table_id.in_(table_ids),
                    DataQualityPolicy.table_id.is_(None),
                ),
            )
        )
    ).all()
    quality_monitored = {policy.table_id for policy in policies if policy.table_id in table_ids}
    source_wide = {policy.datasource_id for policy in policies if policy.table_id is None}
    quality_monitored.update(table.id for table in tables if table.datasource_id in source_wide)
    semantically_mapped = set(
        await session.scalars(
            select(MetadataBusinessAnnotation.table_id).where(
                MetadataBusinessAnnotation.table_id.in_(table_ids)
            )
        )
    )
    evidence_sets = {
        "documented": documented,
        "owned": owned,
        "classified": classified,
        "certified": certified,
        "quality_monitored": quality_monitored,
        "semantically_mapped": semantically_mapped,
    }
    dimensions: dict[str, CoverageDimensionRead] = {}
    for name, evidence in evidence_sets.items():
        count = len(evidence & table_ids)
        dimensions[name] = CoverageDimensionRead(
            covered=count,
            total=len(table_ids),
            percentage=round(count * 100 / len(table_ids), 2),
        )
    overall = round(sum(value.percentage for value in dimensions.values()) / len(dimensions), 2)
    return StewardshipCoverageRead(
        organization_id=organization_id,
        datasource_id=datasource_id,
        domain_id=domain_id,
        line_of_business_id=line_of_business_id,
        table_count=len(table_ids),
        overall_score=overall,
        dimensions=dimensions,
        unowned_table_ids=sorted(table_ids - owned, key=str)[:500],
        computed_at=datetime.now(UTC),
    )


async def _validate_coverage_scope(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID | None,
    domain_id: UUID | None,
    line_of_business_id: UUID | None,
) -> None:
    if datasource_id is not None:
        datasource = await session.get(DataSource, datasource_id)
        if datasource is None or datasource.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="data source not found")
    if domain_id is not None:
        domain = await session.get(BusinessDomain, domain_id)
        if domain is None or domain.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="business domain not found")
    if line_of_business_id is not None:
        line_of_business = await session.get(LineOfBusiness, line_of_business_id)
        if line_of_business is None or line_of_business.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="line of business not found")


@router.get(
    "/organizations/{organization_id}/stewardship/coverage",
    response_model=StewardshipCoverageRead,
)
async def get_stewardship_coverage(
    organization_id: UUID,
    datasource_id: UUID | None = None,
    domain_id: UUID | None = None,
    line_of_business_id: UUID | None = None,
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StewardshipCoverageRead:
    enforce_organization(context, organization_id)
    await _validate_coverage_scope(
        session,
        organization_id=organization_id,
        datasource_id=datasource_id,
        domain_id=domain_id,
        line_of_business_id=line_of_business_id,
    )
    return await _coverage(
        session,
        organization_id=organization_id,
        datasource_id=datasource_id,
        domain_id=domain_id,
        line_of_business_id=line_of_business_id,
    )


@router.post(
    "/organizations/{organization_id}/stewardship/coverage/snapshots",
    response_model=StewardshipCoverageRead,
)
async def snapshot_stewardship_coverage(
    organization_id: UUID,
    datasource_id: UUID | None = None,
    domain_id: UUID | None = None,
    line_of_business_id: UUID | None = None,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> StewardshipCoverageRead:
    enforce_organization(context, organization_id)
    await _validate_coverage_scope(
        session,
        organization_id=organization_id,
        datasource_id=datasource_id,
        domain_id=domain_id,
        line_of_business_id=line_of_business_id,
    )
    coverage = await _coverage(
        session,
        organization_id=organization_id,
        datasource_id=datasource_id,
        domain_id=domain_id,
        line_of_business_id=line_of_business_id,
    )
    snapshot = CoverageSnapshot(
        organization_id=organization_id,
        datasource_id=datasource_id,
        domain_id=domain_id,
        line_of_business_id=line_of_business_id,
        table_count=coverage.table_count,
        dimensions={key: value.model_dump() for key, value in coverage.dimensions.items()},
        overall_score=coverage.overall_score,
        computed_by=context.principal_id,
    )
    session.add(snapshot)
    await session.flush()
    record_audit(
        session,
        _audit_context(context, organization_id),
        action="stewardship.coverage.snapshot",
        resource_type="coverage_snapshot",
        resource_id=str(snapshot.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"table_count": coverage.table_count, "overall_score": coverage.overall_score},
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="coverage_snapshot",
        aggregate_id=str(snapshot.id),
        event_type="stewardship.coverage_computed.v1",
        payload={
            "snapshot_id": str(snapshot.id),
            "table_count": coverage.table_count,
            "overall_score": coverage.overall_score,
        },
    )
    await session.commit()
    return coverage


@router.get(
    "/organizations/{organization_id}/stewardship/coverage/snapshots",
    response_model=Page,
)
async def list_stewardship_coverage_snapshots(
    organization_id: UUID,
    datasource_id: UUID | None = None,
    domain_id: UUID | None = None,
    line_of_business_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = [CoverageSnapshot.organization_id == organization_id]
    for column, value in (
        (CoverageSnapshot.datasource_id, datasource_id),
        (CoverageSnapshot.domain_id, domain_id),
        (CoverageSnapshot.line_of_business_id, line_of_business_id),
    ):
        filters.append(column == value if value is not None else column.is_(None))
    total = await session.scalar(select(func.count()).select_from(CoverageSnapshot).where(*filters))
    rows = (
        await session.scalars(
            select(CoverageSnapshot)
            .where(*filters)
            .order_by(CoverageSnapshot.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[CoverageSnapshotRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )
