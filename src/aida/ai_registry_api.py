import hashlib
import json
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.ai_registry import compute_ai_trust_score, score_assessment_controls
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    AiAssessment,
    AiAsset,
    AiAssetVersion,
    ContextProductVersion,
    GovernanceReview,
    ModelRouteConfiguration,
    Organization,
)
from aida.platform_schemas import (
    AiAssessmentCreate,
    AiAssessmentRead,
    AiAssetCreate,
    AiAssetDefinition,
    AiAssetVersionRead,
    AiTrustScoreRead,
)
from aida.schemas import GovernanceReviewRead, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["ai-registry"])

AI_AUTHORS = ("PlatformAdmin", "AgentDeveloper", "ModelRiskManager", "DataScientist")
AI_READERS = (*AI_AUTHORS, "Reviewer", "Auditor", "DataSteward", "Viewer")
AI_ASSESSORS = ("PlatformAdmin", "Reviewer", "Auditor", "ModelRiskManager")


def ai_asset_fingerprint(definition: AiAssetDefinition) -> str:
    payload = json.dumps(definition.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _validate_references(
    session: AsyncSession,
    organization_id: UUID,
    definition: AiAssetDefinition,
) -> None:
    if definition.context_product_version_ids:
        count = await session.scalar(
            select(func.count())
            .select_from(ContextProductVersion)
            .where(
                ContextProductVersion.id.in_(definition.context_product_version_ids),
                ContextProductVersion.organization_id == organization_id,
                ContextProductVersion.status == "PUBLISHED",
            )
        )
        if int(count or 0) != len(definition.context_product_version_ids):
            raise HTTPException(
                status_code=422,
                detail="all Context Product dependencies must be published in this organization",
            )
    if definition.model_route_ids:
        count = await session.scalar(
            select(func.count())
            .select_from(ModelRouteConfiguration)
            .where(
                ModelRouteConfiguration.id.in_(definition.model_route_ids),
                ModelRouteConfiguration.organization_id == organization_id,
                ModelRouteConfiguration.status == "APPROVED",
            )
        )
        if int(count or 0) != len(definition.model_route_ids):
            raise HTTPException(
                status_code=422,
                detail="all model route dependencies must be approved in this organization",
            )


def _apply_definition(version: AiAssetVersion, definition: AiAssetDefinition) -> AiAssetVersion:
    version.name = definition.name
    version.description = definition.description
    version.intended_use = definition.intended_use
    version.owner_principal = definition.owner_principal
    version.provider_type = definition.provider_type
    version.risk_tier = definition.risk_tier
    version.documentation_url = definition.documentation_url
    version.context_product_version_ids = [
        str(item) for item in definition.context_product_version_ids
    ]
    version.model_route_ids = [str(item) for item in definition.model_route_ids]
    version.policy_control_ids = list(definition.policy_control_ids)
    version.evaluation_evidence = definition.evaluation_evidence
    version.runtime_evidence = definition.runtime_evidence
    version.fingerprint = ai_asset_fingerprint(definition)
    return version


def _version_read(asset: AiAsset, version: AiAssetVersion) -> AiAssetVersionRead:
    return AiAssetVersionRead(
        id=version.id,
        organization_id=version.organization_id,
        asset_id=version.asset_id,
        asset_key=asset.asset_key,
        asset_kind=asset.asset_kind,
        version=version.version,
        status=version.status,
        name=version.name,
        description=version.description,
        intended_use=version.intended_use,
        owner_principal=version.owner_principal,
        provider_type=version.provider_type,
        risk_tier=version.risk_tier,
        documentation_url=version.documentation_url,
        context_product_version_ids=[UUID(item) for item in version.context_product_version_ids],
        model_route_ids=[UUID(item) for item in version.model_route_ids],
        policy_control_ids=version.policy_control_ids,
        evaluation_evidence=version.evaluation_evidence,
        runtime_evidence=version.runtime_evidence,
        fingerprint=version.fingerprint,
        created_by=version.created_by,
        approved_by=version.approved_by,
        approved_at=version.approved_at,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


async def _version_scope(
    session: AsyncSession, version_id: UUID, context: SecurityContext
) -> tuple[AiAsset, AiAssetVersion]:
    version = await session.get(AiAssetVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="AI asset version not found")
    enforce_organization(context, version.organization_id)
    asset = await session.get(AiAsset, version.asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="AI asset not found")
    return asset, version


@router.post(
    "/organizations/{organization_id}/ai-assets",
    response_model=AiAssetVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_ai_asset(
    organization_id: UUID,
    body: AiAssetCreate,
    context: SecurityContext = Depends(require_roles(*AI_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> AiAssetVersionRead:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    await _validate_references(session, organization_id, body)
    asset = AiAsset(
        organization_id=organization_id,
        asset_key=body.asset_key,
        asset_kind=body.asset_kind,
        created_by=context.principal_id,
    )
    session.add(asset)
    try:
        await session.flush()
        version = _apply_definition(
            AiAssetVersion(
                organization_id=organization_id,
                asset_id=asset.id,
                version=1,
                created_by=context.principal_id,
            ),
            body,
        )
        session.add(version)
        await session.flush()
        record_audit(
            session,
            replace(context, organization_id=organization_id),
            action="ai_registry.asset.create",
            resource_type="ai_asset_version",
            resource_id=str(version.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={"asset_key": asset.asset_key, "asset_kind": asset.asset_kind},
        )
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="ai_asset_version",
            aggregate_id=str(version.id),
            event_type="ai_registry.asset_draft_created.v1",
            payload={"ai_asset_id": str(asset.id), "asset_kind": asset.asset_kind},
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="AI asset key already exists") from exc
    return _version_read(asset, version)


@router.post(
    "/ai-assets/{asset_id}/versions",
    response_model=AiAssetVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_ai_asset_version(
    asset_id: UUID,
    body: AiAssetDefinition,
    context: SecurityContext = Depends(require_roles(*AI_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> AiAssetVersionRead:
    asset = await session.get(AiAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="AI asset not found")
    enforce_organization(context, asset.organization_id)
    if asset.lifecycle_status == "RETIRED":
        raise HTTPException(status_code=409, detail="retired AI assets cannot be versioned")
    await _validate_references(session, asset.organization_id, body)
    latest = await session.scalar(
        select(func.max(AiAssetVersion.version)).where(AiAssetVersion.asset_id == asset.id)
    )
    version = _apply_definition(
        AiAssetVersion(
            organization_id=asset.organization_id,
            asset_id=asset.id,
            version=int(latest or 0) + 1,
            created_by=context.principal_id,
        ),
        body,
    )
    session.add(version)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="AI asset version conflict") from exc
    return _version_read(asset, version)


@router.get("/organizations/{organization_id}/ai-assets", response_model=Page)
async def list_ai_assets(
    organization_id: UUID,
    asset_kind: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*AI_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    latest = (
        select(func.max(AiAssetVersion.version))
        .where(AiAssetVersion.asset_id == AiAsset.id)
        .correlate(AiAsset)
        .scalar_subquery()
    )
    filters: list[Any] = [
        AiAsset.organization_id == organization_id,
        AiAssetVersion.version == latest,
    ]
    if asset_kind:
        filters.append(AiAsset.asset_kind == asset_kind.upper())
    rows = (
        await session.execute(
            select(AiAsset, AiAssetVersion)
            .join(AiAssetVersion, AiAssetVersion.asset_id == AiAsset.id)
            .where(*filters)
            .order_by(AiAsset.asset_key)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total_filters: list[Any] = [AiAsset.organization_id == organization_id]
    if asset_kind:
        total_filters.append(AiAsset.asset_kind == asset_kind.upper())
    total = await session.scalar(select(func.count()).select_from(AiAsset).where(*total_filters))
    return Page(
        items=[_version_read(asset, version) for asset, version in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/ai-asset-versions/{version_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_ai_asset_version(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*AI_AUTHORS)),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    _, version = await _version_scope(session, version_id, context)
    if version.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only draft AI asset versions can be submitted")
    review = GovernanceReview(
        organization_id=version.organization_id,
        object_type="AI_ASSET_VERSION",
        object_id=str(version.id),
        requested_action="APPROVE",
        requested_by=context.principal_id,
    )
    version.status = "REVIEW_REQUIRED"
    session.add(review)
    await session.commit()
    return review


@router.post(
    "/ai-asset-versions/{version_id}/assessments",
    response_model=AiAssessmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def assess_ai_asset_version(
    version_id: UUID,
    body: AiAssessmentCreate,
    context: SecurityContext = Depends(require_roles(*AI_ASSESSORS)),
    session: AsyncSession = Depends(get_session),
) -> AiAssessment:
    _, version = await _version_scope(session, version_id, context)
    control_results = [item.model_dump(mode="json") for item in body.control_results]
    score, assessment_status, findings = score_assessment_controls(control_results)
    assessment = AiAssessment(
        organization_id=version.organization_id,
        ai_asset_version_id=version.id,
        framework=body.framework,
        framework_version=body.framework_version,
        status=assessment_status,
        score=score,
        control_results=control_results,
        findings=findings,
        assessed_by=context.principal_id,
    )
    session.add(assessment)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action="ai_registry.assessment.create",
        resource_type="ai_assessment",
        resource_id=str(assessment.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"framework": assessment.framework, "score": score, "status": assessment_status},
    )
    record_outbox(
        session,
        organization_id=version.organization_id,
        aggregate_type="ai_assessment",
        aggregate_id=str(assessment.id),
        event_type="ai_registry.assessment_completed.v1",
        payload={
            "ai_asset_version_id": str(version.id),
            "score": score,
            "status": assessment_status,
        },
    )
    await session.commit()
    return assessment


@router.get("/ai-asset-versions/{version_id}/trust", response_model=AiTrustScoreRead)
async def get_ai_asset_trust(
    version_id: UUID,
    context: SecurityContext = Depends(require_roles(*AI_READERS)),
    session: AsyncSession = Depends(get_session),
) -> AiTrustScoreRead:
    _, version = await _version_scope(session, version_id, context)
    assessment = await session.scalar(
        select(AiAssessment)
        .where(AiAssessment.ai_asset_version_id == version.id)
        .order_by(AiAssessment.created_at.desc())
        .limit(1)
    )
    score = compute_ai_trust_score(version, assessment, computed_at=datetime.now().astimezone())
    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action="ai_registry.trust.read",
        resource_type="ai_asset_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"score": score.score, "grade": score.grade, "blockers": score.blockers},
    )
    await session.commit()
    return score
