from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_annotation_versions import current_version_alias
from aida.catalog_read_model import (
    _business_annotations,
    _description,
    _latest_approved_documentation,
    _latest_pending_drafts,
)
from aida.config import Settings, get_settings
from aida.consumption_lineage import get_consumption_by_resource_counts
from aida.context import get_correlation_id
from aida.db import get_session
from aida.documentation_worklist import (
    DocumentationWorklistEntry,
    TableQuerySignal,
    WorklistRanking,
    rank_documentation_worklist,
)
from aida.events import record_audit, record_outbox
from aida.glossary_owner_routing import TableFacts, sync_unowned_asset_backlog
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
    NotificationRuleRecord,
    OwnershipAssignment,
    OwnershipRule,
    Project,
    QueryExecution,
    UnownedAssetEscalation,
)
from aida.quality_coupling import resolve_table_ids
from aida.schemas import (
    BulkStewardshipOperationCreate,
    BulkStewardshipOperationRead,
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
    LeaverReassignmentRequest,
    OwnershipAssignmentBulkReaffirmItemResult,
    OwnershipAssignmentBulkReaffirmRequest,
    OwnershipAssignmentBulkReaffirmResult,
    OwnershipAssignmentRead,
    OwnershipRuleCreate,
    OwnershipRuleRead,
    Page,
    StewardshipCoverageRead,
    UnownedAssetBacklogRouteRequest,
    UnownedAssetBacklogRouteResult,
    UnownedAssetEscalationRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.stewardship_service import active_certified_table_ids, build_stewardship_coverage
from aida.stewardship_worklist import enrich_tables

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

# GL-6: matches the 500-row bound coverage scoring already applies to the
# unowned-table backlog it returns.
UNOWNED_BACKLOG_ROUTE_LIMIT = 500


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


# GL-7: matches the 500-item cap every other bulk stewardship contract in
# this module already enforces (`BulkStewardshipOperationCreate.subject_ids`,
# CT-1's own `CATALOG_BULK_ACTION_MAX_ITEMS`).
LEAVER_REASSIGNMENT_MAX_ITEMS = 500


@router.post(
    "/organizations/{organization_id}/stewardship/leaver-reassignment",
    response_model=BulkStewardshipOperationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_leaver_reassignment(
    organization_id: UUID,
    body: LeaverReassignmentRequest,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> BulkStewardshipOperation:
    """GL-7: reassign a leaving principal's *whole* active ownership
    portfolio -- every ACTIVE `OwnershipAssignment` row it holds, table and
    glossary-term stewardship alike (any subject_type GL-2's ownership model
    covers) -- to a successor in one governed action, reusing the exact
    `BulkStewardshipOperation` / `GovernanceReview` maker-checker contract
    GL-2/GL-5/GL-8 already established (module 08 SS7) rather than a
    bespoke mechanism: `_apply_governance_review_decision`'s existing
    `BULK_STEWARDSHIP_OPERATION` dispatch (`semantic_api.py`) requires no
    change at all -- it already calls `apply_bulk_operation` generically for
    whatever `operation_type` the row carries.

    Selection mirrors CT-1's explicit-vs-filter split: an explicit
    `assignment_ids` list must name only ACTIVE assignments currently owned
    by `leaving_principal` (409 otherwise), and is capped at
    `LEAVER_REASSIGNMENT_MAX_ITEMS` by the request schema itself (a 422 over
    the limit, exactly CT-1's explicit-selection behavior); omitting it
    discovers the leaving principal's whole current portfolio server-side,
    capped at the same limit with `truncated=True` recorded in the
    operation's own parameters -- never silently dropped, CT-1's
    filter-selection behavior.

    A validated `OwnershipAssignment.id` list is exactly what
    `BulkStewardshipOperation.subject_ids` holds here
    (`subject_type="OWNERSHIP_ASSIGNMENT"`), which is what lets one operation
    span every asset kind the leaver owned -- table *and* term -- in a
    single governed decision; the other three operation types this module
    defines are each constrained to one subject_type per operation because
    their subjects are bare catalog/glossary ids, not already-typed
    assignment rows.

    Certifications (`AssetCertification.certified_by`) are a historical
    attestation, not an ownership assignment, and are deliberately out of
    scope -- consistent with this module's "never last-write-wins"
    retained-evidence principle (module 08 SS6): reassigning who currently
    owns a table must never rewrite the historical record of who certified
    it.
    """
    enforce_organization(context, organization_id)
    if body.assignment_ids is not None:
        rows = (
            await session.scalars(
                select(OwnershipAssignment).where(
                    OwnershipAssignment.organization_id == organization_id,
                    OwnershipAssignment.id.in_(body.assignment_ids),
                    OwnershipAssignment.owner_type == body.owner_type,
                    OwnershipAssignment.owner_principal == body.leaving_principal,
                    OwnershipAssignment.status == "ACTIVE",
                )
            )
        ).all()
        found_ids = {row.id for row in rows}
        missing = set(body.assignment_ids) - found_ids
        if missing:
            raise HTTPException(
                status_code=409,
                detail=(
                    "one or more assignment_ids are not active ownership assignments "
                    "currently held by leaving_principal"
                ),
            )
        subject_ids = [row.id for row in rows]
        selection_mode = "EXPLICIT"
        truncated = False
    else:
        candidate_ids = (
            await session.scalars(
                select(OwnershipAssignment.id)
                .where(
                    OwnershipAssignment.organization_id == organization_id,
                    OwnershipAssignment.owner_type == body.owner_type,
                    OwnershipAssignment.owner_principal == body.leaving_principal,
                    OwnershipAssignment.status == "ACTIVE",
                )
                .order_by(OwnershipAssignment.id)
                .limit(LEAVER_REASSIGNMENT_MAX_ITEMS + 1)
            )
        ).all()
        truncated = len(candidate_ids) > LEAVER_REASSIGNMENT_MAX_ITEMS
        subject_ids = list(candidate_ids[:LEAVER_REASSIGNMENT_MAX_ITEMS])
        selection_mode = "FILTER"
        if not subject_ids:
            raise HTTPException(
                status_code=409,
                detail="leaving_principal has no active ownership assignments to reassign",
            )

    review = GovernanceReview(
        organization_id=organization_id,
        object_type="BULK_STEWARDSHIP_OPERATION",
        object_id="pending",
        requested_action="REASSIGN_LEAVER",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    operation = BulkStewardshipOperation(
        organization_id=organization_id,
        operation_type="REASSIGN_LEAVER",
        subject_type="OWNERSHIP_ASSIGNMENT",
        subject_ids=[str(value) for value in subject_ids],
        parameters={
            "leaving_principal": body.leaving_principal,
            "successor_principal": body.successor_principal,
            "owner_type": body.owner_type,
            "rationale": body.rationale,
            "selection_mode": selection_mode,
            "selection_truncated": truncated,
        },
        governance_review_id=review.id,
        requested_by=context.principal_id,
    )
    session.add(operation)
    await session.flush()
    review.object_id = str(operation.id)
    record_audit(
        session,
        _audit_context(context, organization_id),
        action="stewardship.leaver_reassignment.request",
        resource_type="bulk_stewardship_operation",
        resource_id=str(operation.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "leaving_principal": body.leaving_principal,
            "successor_principal": body.successor_principal,
            "subject_count": len(subject_ids),
            "selection_mode": selection_mode,
            "selection_truncated": truncated,
        },
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
            "requested_action": "REASSIGN_LEAVER",
        },
    )
    await session.commit()
    return operation


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


# -- P2-07: OwnershipAssignment re-affirmation -----------------------------
#
# `POST /v1/ownership-assignments/{id}/reaffirm` is the single-item entry;
# `POST /v1/ownership-assignments/bulk-reaffirm` is the maker-checker-friendly
# per-item SAVEPOINT bulk shape used by every other bulk write in this
# module (`bulk_decide_relationship_candidates`, `bulk_reject_link_proposals`).
# A caller may reaffirm only their own ownership; PlatformAdmin/MetadataAdmin
# can reaffirm on behalf of any principal.


# `_OWNERSHIP_ADMIN_ROLES`: roles that may reaffirm an assignment they do NOT
# themselves own. Kept narrow -- broadening this would defeat the "the owner
# must actively re-attest" property the reaffirm endpoint exists to enforce.
_OWNERSHIP_ADMIN_ROLES = frozenset({"PlatformAdmin", "MetadataAdmin"})


async def _reaffirm_one(
    session: AsyncSession,
    *,
    context: SecurityContext,
    assignment: OwnershipAssignment,
    now: datetime,
    reaffirm_days: int,
) -> None:
    """Core: stamps `reaffirmed_at`/`reaffirmed_by`, extends `expires_at`, and
    writes one audit + outbox row. Shared by the single-item endpoint and
    each SAVEPOINT of the bulk endpoint.

    Callers are responsible for checking caller-owns-or-is-admin BEFORE this;
    the row-status ACTIVE gate is checked here to keep the single- and
    bulk-item paths behaviorally identical.
    """
    if assignment.status != "ACTIVE":
        raise HTTPException(
            status_code=409,
            detail="only ACTIVE ownership assignments can be reaffirmed",
        )
    assignment.reaffirmed_at = now
    assignment.reaffirmed_by = context.principal_id
    assignment.expires_at = now + timedelta(days=reaffirm_days)
    # A reaffirmation clears the warning stamp so the row can warn again in
    # its next cycle (the warning was for the previous expiry horizon).
    assignment.expiry_warning_emitted_at = None
    details = {
        "subject_type": assignment.subject_type,
        "subject_id": assignment.subject_id,
        "owner_type": assignment.owner_type,
        "owner_principal": assignment.owner_principal,
        "reaffirm_days": reaffirm_days,
        "new_expires_at": assignment.expires_at.isoformat(),
    }
    record_audit(
        session,
        _audit_context(context, assignment.organization_id),
        action="OWNERSHIP_ASSIGNMENT_REAFFIRMED",
        resource_type="ownership_assignment",
        resource_id=str(assignment.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details=details,
    )
    record_outbox(
        session,
        organization_id=assignment.organization_id,
        aggregate_type="ownership_assignment",
        aggregate_id=str(assignment.id),
        event_type="ownership.assignment.reaffirmed.v1",
        payload={"assignment_id": str(assignment.id), **details},
    )


def _caller_may_reaffirm(context: SecurityContext, assignment: OwnershipAssignment) -> bool:
    """Reaffirm is either self-service (caller is the owner) or admin-driven.

    A GROUP-owned assignment is reaffirmable by any admin or by a caller
    whose principal_id equals the group id (in that deployment a group is
    itself a principal; see security.SecurityContext principal model).
    """
    if any(role in _OWNERSHIP_ADMIN_ROLES for role in context.roles):
        return True
    return context.principal_id == assignment.owner_principal


@router.post(
    "/ownership-assignments/{assignment_id}/reaffirm",
    response_model=OwnershipAssignmentRead,
)
async def reaffirm_ownership_assignment(
    assignment_id: UUID,
    settings: Settings = Depends(get_settings),
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> OwnershipAssignment:
    """P2-07: the owner (or admin) reaffirms an ACTIVE assignment. Extends
    `expires_at` by `settings.ownership_reaffirm_days`, records audit +
    outbox.
    """
    assignment = await session.get(OwnershipAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="ownership assignment not found")
    enforce_organization(context, assignment.organization_id)
    if not _caller_may_reaffirm(context, assignment):
        raise HTTPException(
            status_code=403,
            detail="only the owner or an admin may reaffirm this ownership assignment",
        )
    now = datetime.now(UTC)
    await _reaffirm_one(
        session,
        context=context,
        assignment=assignment,
        now=now,
        reaffirm_days=settings.ownership_reaffirm_days,
    )
    await session.commit()
    await session.refresh(assignment)
    return assignment


@router.post(
    "/ownership-assignments/bulk-reaffirm",
    response_model=OwnershipAssignmentBulkReaffirmResult,
)
async def bulk_reaffirm_ownership_assignments(
    body: OwnershipAssignmentBulkReaffirmRequest,
    settings: Settings = Depends(get_settings),
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> OwnershipAssignmentBulkReaffirmResult:
    """P2-07: the owner (or admin) reaffirms up to 100 ACTIVE assignments in
    one call. Per-item SAVEPOINT -- one item failing (missing, wrong org,
    not-owner, already-LAPSED) doesn't roll the rest back. Same
    partial-success shape as `bulk_decide_relationship_candidates` and
    `bulk_reject_link_proposals`.
    """
    now = datetime.now(UTC)
    items: list[OwnershipAssignmentBulkReaffirmItemResult] = []
    reaffirmed = 0
    skipped = 0
    for assignment_id in body.assignment_ids:
        savepoint = await session.begin_nested()
        try:
            assignment = await session.get(OwnershipAssignment, assignment_id)
            if assignment is None:
                await savepoint.rollback()
                items.append(
                    OwnershipAssignmentBulkReaffirmItemResult(
                        assignment_id=assignment_id,
                        outcome="NOT_FOUND",
                        detail="ownership assignment not found",
                    )
                )
                skipped += 1
                continue
            if context.organization_id != assignment.organization_id:
                await savepoint.rollback()
                items.append(
                    OwnershipAssignmentBulkReaffirmItemResult(
                        assignment_id=assignment_id,
                        outcome="FORBIDDEN",
                        detail="assignment belongs to a different organization",
                    )
                )
                skipped += 1
                continue
            if not _caller_may_reaffirm(context, assignment):
                await savepoint.rollback()
                items.append(
                    OwnershipAssignmentBulkReaffirmItemResult(
                        assignment_id=assignment_id,
                        outcome="FORBIDDEN",
                        detail="only the owner or an admin may reaffirm",
                    )
                )
                skipped += 1
                continue
            try:
                await _reaffirm_one(
                    session,
                    context=context,
                    assignment=assignment,
                    now=now,
                    reaffirm_days=settings.ownership_reaffirm_days,
                )
            except HTTPException as exc:
                await savepoint.rollback()
                items.append(
                    OwnershipAssignmentBulkReaffirmItemResult(
                        assignment_id=assignment_id,
                        outcome="ERROR",
                        detail=str(exc.detail),
                    )
                )
                skipped += 1
                continue
            await savepoint.commit()
            items.append(
                OwnershipAssignmentBulkReaffirmItemResult(
                    assignment_id=assignment_id,
                    outcome="REAFFIRMED",
                )
            )
            reaffirmed += 1
        except Exception as exc:  # noqa: BLE001 -- SAVEPOINT catches all
            await savepoint.rollback()
            items.append(
                OwnershipAssignmentBulkReaffirmItemResult(
                    assignment_id=assignment_id,
                    outcome="ERROR",
                    detail=type(exc).__name__,
                )
            )
            skipped += 1
    await session.commit()
    return OwnershipAssignmentBulkReaffirmResult(
        reaffirmed=reaffirmed, skipped=skipped, items=items
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
    # AT-6: content lives on the current `MetadataBusinessAnnotationVersion`,
    # not on `MetadataBusinessAnnotation` -- see `business_annotation_versions.py`.
    annotation_version_alias, annotation_version_ranked = current_version_alias()
    annotation_rows = (
        await session.execute(
            select(MetadataBusinessAnnotation, annotation_version_alias)
            .join(
                annotation_version_alias,
                annotation_version_alias.annotation_id == MetadataBusinessAnnotation.id,
            )
            .where(
                MetadataBusinessAnnotation.organization_id == organization_id,
                annotation_version_ranked.c.rn == 1,
            )
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
    for annotation, content_version in annotation_rows:
        annotation_labels = [(content_version.business_name, "BUSINESS_NAME")]
        annotation_labels.extend(
            (value, "ANNOTATION_SYNONYM") for value in content_version.synonyms
        )
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
                    "annotation_version": content_version.version,
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
            "annotations_scanned": len(annotation_rows),
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


async def _scope_table_ids(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID | None,
    domain_id: UUID | None,
    line_of_business_id: UUID | None,
) -> set[UUID]:
    """Active table IDs within an organization/source/domain/LOB coverage scope."""
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
    return {table.id for table in tables}


async def _owned_table_ids(
    session: AsyncSession, *, organization_id: UUID, table_ids: set[UUID]
) -> set[UUID]:
    """Table IDs within ``table_ids`` that have an active owner, by assignment or
    approved documentation naming one -- the GL-4 "owned" coverage dimension."""
    if not table_ids:
        return set()
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
    return owned


async def _coverage(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID | None,
    domain_id: UUID | None,
    line_of_business_id: UUID | None,
) -> StewardshipCoverageRead:
    table_ids = await _scope_table_ids(
        session,
        organization_id=organization_id,
        datasource_id=datasource_id,
        domain_id=domain_id,
        line_of_business_id=line_of_business_id,
    )
    if not table_ids:
        return build_stewardship_coverage(
            organization_id=organization_id,
            datasource_id=datasource_id,
            domain_id=domain_id,
            line_of_business_id=line_of_business_id,
            table_ids=set(),
            evidence_sets={},
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
    owned = await _owned_table_ids(session, organization_id=organization_id, table_ids=table_ids)
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
    certification_rows = (
        await session.scalars(
            select(AssetCertification).where(
                AssetCertification.table_id.in_(table_ids),
                # CT-5: certification is now also column-scoped; a column's
                # certification denormalizes its parent table_id but must not
                # count toward the table's own "certified" coverage dimension.
                AssetCertification.asset_type != "COLUMN",
            )
        )
    ).all()
    certified = active_certified_table_ids(list(certification_rows), now=datetime.now(UTC))
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
    if source_wide:
        table_datasources = (
            await session.execute(
                select(MetadataTable.id, MetadataTable.datasource_id).where(
                    MetadataTable.id.in_(table_ids)
                )
            )
        ).all()
        quality_monitored.update(
            table_id
            for table_id, datasource_id in table_datasources
            if datasource_id in source_wide
        )
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
    return build_stewardship_coverage(
        organization_id=organization_id,
        datasource_id=datasource_id,
        domain_id=domain_id,
        line_of_business_id=line_of_business_id,
        table_ids=table_ids,
        evidence_sets=evidence_sets,
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


# --- GL-6: unowned-asset backlog routing and escalation --------------------


async def _unowned_asset_table_facts(
    session: AsyncSession, *, organization_id: UUID, table_ids: list[UUID]
) -> dict[UUID, TableFacts]:
    if not table_ids:
        return {}
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, MetadataBusinessAnnotation, BusinessDomain)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .outerjoin(
                MetadataBusinessAnnotation,
                MetadataBusinessAnnotation.table_id == MetadataTable.id,
            )
            .outerjoin(BusinessDomain, BusinessDomain.id == MetadataBusinessAnnotation.domain_id)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.id.in_(table_ids),
            )
        )
    ).all()
    facts: dict[UUID, TableFacts] = {}
    for table, schema, annotation, domain in rows:
        facts[table.id] = TableFacts(
            table_id=table.id,
            datasource_id=table.datasource_id,
            table_name=table.name,
            schema_name=schema.name,
            domain_key=domain.domain_key if domain is not None else None,
            tags=tuple(annotation.tags) if annotation is not None else (),
        )
    return facts


@router.get(
    "/organizations/{organization_id}/stewardship/unowned-backlog",
    response_model=Page,
)
async def list_unowned_asset_backlog(
    organization_id: UUID,
    backlog_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """The current unowned-asset backlog and where each entry stands in routing."""
    enforce_organization(context, organization_id)
    filters = [UnownedAssetEscalation.organization_id == organization_id]
    if backlog_status:
        filters.append(UnownedAssetEscalation.status == backlog_status.upper())
    else:
        filters.append(UnownedAssetEscalation.status != "RESOLVED")
    total = await session.scalar(
        select(func.count()).select_from(UnownedAssetEscalation).where(*filters)
    )
    rows = (
        await session.scalars(
            select(UnownedAssetEscalation)
            .where(*filters)
            .order_by(UnownedAssetEscalation.first_detected_unowned_at.asc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[UnownedAssetEscalationRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/organizations/{organization_id}/stewardship/unowned-backlog/route",
    response_model=UnownedAssetBacklogRouteResult,
)
async def route_unowned_asset_backlog(
    organization_id: UUID,
    body: UnownedAssetBacklogRouteRequest = UnownedAssetBacklogRouteRequest(),  # noqa: B008
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> UnownedAssetBacklogRouteResult:
    """Reconcile the unowned-asset backlog: route aged entries to a candidate
    owner or stewardship-lead contact, escalate ones still unaddressed, and
    resolve entries whose table has since been owned.

    Reuses DQ-1's notification-routing engine end to end (see
    ``aida.glossary_owner_routing``) against this organization's own
    ``notification-rules`` -- a rule scoped to unowned-asset routing (e.g.
    ``{"domain": "unowned_asset_backlog"}`` in its conditions) is what makes
    routing actually dispatch; without one, tables are still tracked and
    escalation-aged but nothing is routed anywhere.

    Escalation is two-tier (GL-6): ``ROUTED`` -> ``ESCALATED`` against the
    originally matched rule's own channel, then, if still unaddressed a
    further ``DEFAULT_ESCALATE_TIER2_AFTER`` later, ``ESCALATED`` ->
    ``ESCALATED_TIER_2`` -- which always produces an ITSM payload, whether or
    not a rule matches, since tier 2 exists to reach an operational queue
    once the first channel has not resolved it.
    """
    enforce_organization(context, organization_id)
    await _validate_coverage_scope(
        session,
        organization_id=organization_id,
        datasource_id=body.datasource_id,
        domain_id=body.domain_id,
        line_of_business_id=body.line_of_business_id,
    )
    table_ids = await _scope_table_ids(
        session,
        organization_id=organization_id,
        datasource_id=body.datasource_id,
        domain_id=body.domain_id,
        line_of_business_id=body.line_of_business_id,
    )
    owned = await _owned_table_ids(session, organization_id=organization_id, table_ids=table_ids)
    unowned_table_ids = table_ids - owned

    existing_rows = (
        await session.scalars(
            select(UnownedAssetEscalation).where(
                UnownedAssetEscalation.organization_id == organization_id,
                UnownedAssetEscalation.status != "RESOLVED",
            )
        )
    ).all()
    existing_entries = {row.table_id: row for row in existing_rows}

    ownership_rules = list(
        await session.scalars(
            select(OwnershipRule).where(
                OwnershipRule.organization_id == organization_id,
                OwnershipRule.status == "ACTIVE",
            )
        )
    )
    notification_rules = list(
        await session.scalars(
            select(NotificationRuleRecord).where(
                NotificationRuleRecord.organization_id == organization_id,
                NotificationRuleRecord.enabled.is_(True),
            )
        )
    )

    route_candidates = sorted(unowned_table_ids, key=str)[:UNOWNED_BACKLOG_ROUTE_LIMIT]
    table_facts = await _unowned_asset_table_facts(
        session, organization_id=organization_id, table_ids=route_candidates
    )

    result = sync_unowned_asset_backlog(
        organization_id=organization_id,
        unowned_table_ids=unowned_table_ids,
        existing_entries=existing_entries,
        table_facts=table_facts,
        ownership_rules=ownership_rules,
        notification_rules=notification_rules,
        now=datetime.now(UTC),
        route_limit=UNOWNED_BACKLOG_ROUTE_LIMIT,
    )
    for entry in result.created:
        session.add(entry)
    await session.flush()

    audit_context = _audit_context(context, organization_id)
    for entry in result.routed:
        record_audit(
            session,
            audit_context,
            action="stewardship.unowned_asset.routed",
            resource_type="unowned_asset_escalation",
            resource_id=str(entry.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={"table_id": str(entry.table_id), "candidate_owner": entry.candidate_owner},
        )
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="unowned_asset_escalation",
            aggregate_id=str(entry.id),
            event_type="stewardship.unowned_asset_routed.v1",
            payload={"table_id": str(entry.table_id), "candidate_owner": entry.candidate_owner},
        )
    for entry in result.escalated:
        record_audit(
            session,
            audit_context,
            action="stewardship.unowned_asset.escalated",
            resource_type="unowned_asset_escalation",
            resource_id=str(entry.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={"table_id": str(entry.table_id)},
        )
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="unowned_asset_escalation",
            aggregate_id=str(entry.id),
            event_type="stewardship.unowned_asset_escalated.v1",
            payload={"table_id": str(entry.table_id)},
        )
    for entry in result.escalated_tier2:
        record_audit(
            session,
            audit_context,
            action="stewardship.unowned_asset.escalated_tier2",
            resource_type="unowned_asset_escalation",
            resource_id=str(entry.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={"table_id": str(entry.table_id)},
        )
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="unowned_asset_escalation",
            aggregate_id=str(entry.id),
            event_type="stewardship.unowned_asset_escalated_tier2.v1",
            payload={"table_id": str(entry.table_id)},
        )
    for entry in result.resolved:
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="unowned_asset_escalation",
            aggregate_id=str(entry.id),
            event_type="stewardship.unowned_asset_resolved.v1",
            payload={"table_id": str(entry.table_id)},
        )

    await session.commit()

    return UnownedAssetBacklogRouteResult(
        organization_id=organization_id,
        routed=[UnownedAssetEscalationRead.model_validate(entry) for entry in result.routed],
        escalated=[
            UnownedAssetEscalationRead.model_validate(entry) for entry in result.escalated
        ],
        escalated_tier2=[
            UnownedAssetEscalationRead.model_validate(entry) for entry in result.escalated_tier2
        ],
        resolved_count=len(result.resolved),
    )


# --- AT-5: query-history-ranked documentation worklist ---------------------
#
# Distinct from GL-6's unowned-asset backlog above: GL-6 tracks a *stateful*
# routing lifecycle (an `UnownedAssetEscalation` row per table, with a status
# machine and escalation timestamps this module writes to on
# `route_unowned_asset_backlog`). This worklist has no such state -- it is
# computed fresh on every request from real query-history and documentation
# signals, with no ownership/routing concept at all. Bolting a "backlog kind"
# switch onto the GL-6 endpoint would make one route sometimes read a
# persisted table and sometimes compute an aggregate over `QueryExecution`/
# `ConsumptionRecord`, two response shapes wearing one signature -- a
# genuinely separate concern, so it gets its own endpoint (`ApiModel`
# reused belongs to a local read model, exactly `operational_api.py`'s own
# reasoning for `ConnectorHealthScoreRead` not living in `aida.schemas`).
#
# Real query-volume sources (tracker AT-5's own framing; the two are simply
# added together as `query_volume` -- see `documentation_worklist.py`):
#   - `query_execution_count`: `QueryExecution.referenced_tables` from
#     governed SQL execution (`aida.query_gateway`) -- resolved to table ids
#     per datasource, the same technique RT-6 already uses
#     (`aida.retrieval._table_execution_counts`), bounded by the same
#     `Settings.agent_retrieval_scan_limit` scan budget RT-6 spends on the
#     identically-shaped query.
#   - `consumption_read_count`: `ConsumptionRecord` rows with
#     `resource_type="metadata_table"` (CX-4, MCP/context-product reads) --
#     `resource_id` is already the real table id, so this is a single grouped
#     aggregate query (`consumption_lineage.get_consumption_by_resource_counts`),
#     no name resolution needed.
#
# "Documented" is UX-12's own determination
# (`catalog_read_model._description`), reused verbatim rather than re-derived
# -- see `documentation_worklist.py`'s module docstring for the precedence
# chain and why a `PENDING_APPROVAL` draft alone does not count.


class ApiModel(BaseModel):
    """Local response-model base, same shape as `operational_api.ApiModel` /
    `sql_validation_api.ApiModel` -- a page composed from two other modules'
    persisted rows plus a pure ranking function has no natural home in
    `aida.schemas` and does not need one.
    """

    model_config = ConfigDict(from_attributes=True, extra="forbid")


class DocumentationWorklistEntryRead(ApiModel):
    table_id: UUID
    table_name: str
    schema_name: str
    datasource_name: str
    rank: int
    query_execution_count: int
    consumption_read_count: int
    query_volume: int
    last_queried_at: datetime | None
    last_consumed_at: datetime | None
    description_is_proposed: bool
    # SW-1 factors. Every term of the score is on the row for the same reason
    # `query_volume` always was: a steward asking "why is this first" has to
    # be answerable from the response, not from reading the ranker.
    score: float
    usage: float
    impact: float
    deficit: float
    downstream_count: int
    missing: list[str]


# Mirrors GL-6's own `UNOWNED_BACKLOG_ROUTE_LIMIT` bound: caps both (a) how
# many tables the CX-4 consumption side contributes as ranking candidates,
# and (b) how many additional zero-query-volume tables `include_zero_volume`
# pulls in. The gateway-execution side is bounded separately, by
# `Settings.agent_retrieval_scan_limit` (RT-6's own budget) on *rows scanned*
# per datasource rather than tables returned -- a row-scan bound naturally
# limits the number of distinct tables that can appear from it too.
DOCUMENTATION_WORKLIST_CANDIDATE_LIMIT = 500


async def _query_execution_volume(
    session: AsyncSession,
    *,
    datasources: list[DataSource],
    scan_limit: int,
) -> dict[UUID, tuple[int, datetime]]:
    """How many recent `COMPLETED` `QueryExecution` rows referenced each
    table, aggregated across every datasource in ``datasources``.

    `QueryExecution.referenced_tables` stores SQL-qualified name strings, not
    ids, and a name only resolves unambiguously within one datasource's own
    catalog (two datasources can both have a table named ``customers``), so
    the scan and resolution happen per datasource -- exactly RT-6's own
    `aida.retrieval._table_execution_counts`, reused at the technique level
    (same `aida.quality_coupling.resolve_table_ids` resolver, same
    most-recent-first bounded scan) since AT-5 needs a different aggregate
    (every touched table, not lookup counts for a caller-given set), not a
    fork of RT-6's private, retrieval-scoped helper itself.
    """
    counts: dict[UUID, int] = {}
    last_seen: dict[UUID, datetime] = {}
    for datasource in datasources:
        rows = (
            await session.execute(
                select(QueryExecution.referenced_tables, QueryExecution.created_at)
                .where(
                    QueryExecution.datasource_id == datasource.id,
                    QueryExecution.organization_id == datasource.organization_id,
                    QueryExecution.status == "COMPLETED",
                )
                .order_by(QueryExecution.created_at.desc())
                .limit(scan_limit)
            )
        ).all()
        if not rows:
            continue
        all_names: set[str] = set()
        for referenced_tables, _created_at in rows:
            all_names.update(referenced_tables or [])
        if not all_names:
            continue
        name_to_id = await resolve_table_ids(
            session, datasource=datasource, table_names=sorted(all_names)
        )
        for referenced_tables, created_at in rows:
            # A table referenced twice in one statement counts once for that
            # execution -- this measures how many past *queries* touched the
            # table, the same "queries, not raw name occurrences" rule RT-6
            # applies for the identical reason.
            touched = {
                table_id
                for name in (referenced_tables or [])
                if (table_id := name_to_id.get(name)) is not None
            }
            for table_id in touched:
                counts[table_id] = counts.get(table_id, 0) + 1
                if table_id not in last_seen or created_at > last_seen[table_id]:
                    last_seen[table_id] = created_at
    return {table_id: (count, last_seen[table_id]) for table_id, count in counts.items()}


async def _consumption_volume(
    session: AsyncSession, *, organization_id: UUID, limit: int
) -> dict[UUID, tuple[int, datetime]]:
    """CX-4 consumption-read counts per table, top ``limit`` tables by count.

    `ConsumptionRecord.resource_id` for `resource_type="metadata_table"` is
    already the real `MetadataTable.id` (set by `mcp_server.py`'s
    `record_consumption` call at the point a table is read via MCP), so --
    unlike the gateway-execution side -- no name resolution is needed here.
    """
    rows = await get_consumption_by_resource_counts(
        session,
        organization_id=organization_id,
        resource_type="metadata_table",
        limit=limit,
    )
    result: dict[UUID, tuple[int, datetime]] = {}
    for resource_id, count, last_consumed_at in rows:
        try:
            table_id = UUID(resource_id)
        except ValueError:  # pragma: no cover - defensive, ids are always UUIDs
            continue
        result[table_id] = (count, last_consumed_at)
    return result


async def _documentation_state(
    session: AsyncSession, tables: list[MetadataTable]
) -> dict[UUID, tuple[bool, bool]]:
    """table id -> (is_documented, description_is_proposed), reusing UX-12's
    exact precedence chain (`catalog_read_model._description`) rather than a
    second "is this documented" rule -- see `documentation_worklist.py`'s
    module docstring for why a pending, unapproved draft does not count as
    documented here even though `catalog_read_model` surfaces it as a
    proposal.
    """
    if not tables:
        return {}
    table_ids = [table.id for table in tables]
    documentation = await _latest_approved_documentation(session, table_ids)
    pending_drafts = await _latest_pending_drafts(session, table_ids)
    annotations = await _business_annotations(session, table_ids)
    state: dict[UUID, tuple[bool, bool]] = {}
    for table in tables:
        description, description_is_proposed = _description(
            table,
            documentation=documentation.get(table.id),
            pending_draft=pending_drafts.get(table.id),
            annotation=annotations.get(table.id),
        )
        is_documented = bool(description) and not description_is_proposed
        state[table.id] = (is_documented, description_is_proposed)
    return state


async def _documentation_worklist_signals(
    session: AsyncSession,
    *,
    organization_id: UUID,
    scan_limit: int,
    include_zero_volume: bool,
) -> list[TableQuerySignal]:
    """Gather every DB-touching input `rank_documentation_worklist` needs,
    then hand off to that pure function -- this is the only place in AT-5
    that talks to the database.

    The candidate table set is driven by real activity rather than a full
    catalog scan: a table that appears in neither the bounded
    `QueryExecution` scan nor the top-`DOCUMENTATION_WORKLIST_CANDIDATE_LIMIT`
    consumption reads has, by construction, no real query-volume signal to
    rank it by, so it is simply never fetched -- consistent with
    `rank_documentation_worklist`'s own default of excluding zero-volume
    tables, and a lot cheaper than the alternative (composing documentation
    state for an org's entire active-table catalog on every request, which
    `list_catalog_rows`'s own 1M-table docstring notes is exactly the scale
    this platform's catalog surfaces are built not to assume). Only when a
    caller opts into ``include_zero_volume`` does this reach for an
    additional bounded slice of zero-volume active tables.
    """
    datasources = (
        await session.scalars(
            select(DataSource).where(DataSource.organization_id == organization_id)
        )
    ).all()

    execution_volume = await _query_execution_volume(
        session, datasources=list(datasources), scan_limit=scan_limit
    )
    consumption_volume = await _consumption_volume(
        session,
        organization_id=organization_id,
        limit=DOCUMENTATION_WORKLIST_CANDIDATE_LIMIT,
    )
    candidate_ids = set(execution_volume) | set(consumption_volume)

    if include_zero_volume:
        zero_volume_filters: list[Any] = [
            MetadataTable.organization_id == organization_id,
            MetadataTable.status == "ACTIVE",
        ]
        if candidate_ids:
            zero_volume_filters.append(MetadataTable.id.notin_(candidate_ids))
        zero_volume_ids = (
            await session.scalars(
                select(MetadataTable.id)
                .where(*zero_volume_filters)
                .order_by(MetadataTable.id)
                .limit(DOCUMENTATION_WORKLIST_CANDIDATE_LIMIT)
            )
        ).all()
        candidate_ids |= set(zero_volume_ids)

    if not candidate_ids:
        return []

    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, DataSource)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(DataSource, DataSource.id == MetadataTable.datasource_id)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.id.in_(candidate_ids),
            )
        )
    ).all()
    candidate_rows = [(table, schema, datasource) for table, schema, datasource in rows]
    documentation_state = await _documentation_state(
        session, [table for table, _, _ in candidate_rows]
    )
    # SW-1 adoption: downstream impact and the five-field deficit, from the
    # same `enrich_tables` `compute_worklist` uses -- so "documented" has one
    # definition on this platform rather than one per surface. AT-5's own
    # UX-12 precedence chain still decides the description field; SW-1 is
    # handed that answer rather than computing a weaker one of its own.
    enrichment = await enrich_tables(
        session,
        organization_id,
        [table.id for table, _, _ in candidate_rows],
        descriptions={
            table.id: documentation_state.get(table.id, (False, False))[0]
            for table, _, _ in candidate_rows
        },
    )

    signals: list[TableQuerySignal] = []
    for table, schema, datasource in candidate_rows:
        exec_count, last_queried_at = execution_volume.get(table.id, (0, None))
        consumption_count, last_consumed_at = consumption_volume.get(table.id, (0, None))
        is_documented, description_is_proposed = documentation_state.get(
            table.id, (False, False)
        )
        deficit = enrichment.get(table.id)
        signals.append(
            TableQuerySignal(
                table_id=table.id,
                table_name=table.name,
                schema_name=schema.name,
                datasource_name=datasource.name,
                query_execution_count=exec_count,
                consumption_read_count=consumption_count,
                last_queried_at=last_queried_at,
                last_consumed_at=last_consumed_at,
                is_documented=is_documented,
                description_is_proposed=description_is_proposed,
                downstream_count=deficit.downstream_count if deficit else 0,
                missing=deficit.missing if deficit else (),
            )
        )
    return signals


@router.get(
    "/organizations/{organization_id}/stewardship/documentation-worklist",
    response_model=Page,
)
async def list_documentation_worklist(
    organization_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_zero_volume: bool = Query(
        default=False,
        description=(
            "Include tables with zero real query volume (no gateway execution, no "
            "MCP consumption read), sorted after every real-volume row. Off by "
            "default: this worklist ranks by real usage, and a zero-volume table "
            "has none to rank it by (see `documentation_worklist.py`)."
        ),
    ),
    ranking: WorklistRanking = Query(
        default="priority",
        description=(
            "`priority` (default) is SW-1's usage x impact x deficit: among "
            "comparably used tables it puts the hub missing four of five "
            "documentation fields ahead of the leaf missing one. Usage is still "
            "a term, so a table nobody queries still cannot reach the top. "
            "`query_volume` restores the pre-adoption order exactly."
        ),
    ),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page:
    """AT-5: undocumented/under-described tables ranked by real query volume.

    Distinct from RT-6's `usage_popularity` retrieval signal
    (`aida.retrieval.hybrid_retrieve_enhanced`) -- that ranks *candidate
    tables for an agent's next SQL statement*; this ranks *tables a human
    steward should document next*, and excludes any table that already has a
    real (non-proposed) description rather than merely weighting it down.

    Paginated with the same bounded offset/limit `Page` contract GL-6's own
    backlog endpoint above uses, not CT-2's keyset convention: keyset
    pagination continues a page via a `WHERE` predicate over an indexed,
    stored ordering column, and there is no such column here -- `query_volume`
    is a runtime aggregate over a bounded `QueryExecution`/`ConsumptionRecord`
    scan (`_documentation_worklist_signals`), recomputed and re-sorted by the
    pure `rank_documentation_worklist` on every request. `Page.total` still
    reports the full ranked-candidate count, independent of `limit`.
    """
    enforce_organization(context, organization_id)
    signals = await _documentation_worklist_signals(
        session,
        organization_id=organization_id,
        scan_limit=settings.agent_retrieval_scan_limit,
        include_zero_volume=include_zero_volume,
    )
    entries: list[DocumentationWorklistEntry]
    entries, total = rank_documentation_worklist(
        signals,
        limit=limit,
        offset=offset,
        include_zero_volume=include_zero_volume,
        ranking=ranking,
    )
    return Page(
        items=[DocumentationWorklistEntryRead.model_validate(entry) for entry in entries],
        limit=limit,
        offset=offset,
        total=total,
    )
