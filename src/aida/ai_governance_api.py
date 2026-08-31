import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.model_gateway import GLOBAL_KILL_SWITCH_SCOPE, route_adapter_available
from aida.models import GovernanceReview, KillSwitchState, ModelRouteConfiguration, Organization
from aida.schemas import (
    GovernanceReviewRead,
    KillSwitchEngageRequest,
    KillSwitchReleaseRequest,
    KillSwitchStateRead,
    ModelRouteConfigurationCreate,
    ModelRouteConfigurationRead,
    Page,
)
from aida.secrets import SecretResolutionError, SecretResolver
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["ai-governance"])


def _configuration_fingerprint(body: ModelRouteConfigurationCreate) -> str:
    payload = body.model_dump(mode="json")
    reference = payload.pop("credential_reference", None)
    payload["credential_reference_fingerprint"] = (
        hashlib.sha256(reference.encode()).hexdigest() if reference else None
    )
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _route_read(route: ModelRouteConfiguration, settings: Settings) -> ModelRouteConfigurationRead:
    selected = settings.model_route == route.route_key
    adapter_available = route_adapter_available(
        provider_type=route.provider_type,
        credential_reference=route.credential_reference,
        settings=settings,
    )
    if route.status != "APPROVED":
        activation_status = route.status
    elif not selected:
        activation_status = "APPROVED_NOT_SELECTED"
    elif not settings.model_generation_enabled:
        activation_status = "GENERATION_DISABLED"
    elif not adapter_available:
        activation_status = "ADAPTER_REGISTRATION_REQUIRED"
    else:
        activation_status = "READY"
    return ModelRouteConfigurationRead(
        id=route.id,
        organization_id=route.organization_id,
        route_key=route.route_key,
        version=route.version,
        status=route.status,
        display_name=route.display_name,
        provider_type=route.provider_type,
        model_id=route.model_id,
        endpoint_alias=route.endpoint_alias,
        uses_credential_reference=route.credential_reference is not None,
        data_residency=route.data_residency,
        retention_policy=route.retention_policy,
        capabilities=route.capabilities,
        max_input_tokens=route.max_input_tokens,
        max_output_tokens=route.max_output_tokens,
        timeout_seconds=route.timeout_seconds,
        fingerprint=route.fingerprint,
        created_by=route.created_by,
        approved_by=route.approved_by,
        approved_at=route.approved_at,
        selected_by_runtime=selected,
        adapter_available=adapter_available,
        activation_status=activation_status,
        created_at=route.created_at,
        updated_at=route.updated_at,
    )


@router.post(
    "/organizations/{organization_id}/model-routes",
    response_model=ModelRouteConfigurationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_route(
    organization_id: UUID,
    body: ModelRouteConfigurationCreate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "AgentDeveloper")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ModelRouteConfigurationRead:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    if body.credential_reference:
        try:
            SecretResolver(settings).validate_reference(body.credential_reference)
        except SecretResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    latest_version = await session.scalar(
        select(func.max(ModelRouteConfiguration.version)).where(
            ModelRouteConfiguration.organization_id == organization_id,
            ModelRouteConfiguration.route_key == body.route_key,
        )
    )
    route = ModelRouteConfiguration(
        organization_id=organization_id,
        route_key=body.route_key,
        version=int(latest_version or 0) + 1,
        display_name=body.display_name,
        provider_type=body.provider_type,
        model_id=body.model_id,
        endpoint_alias=body.endpoint_alias,
        credential_reference=body.credential_reference,
        data_residency=body.data_residency,
        retention_policy=body.retention_policy,
        capabilities=list(dict.fromkeys(body.capabilities)),
        max_input_tokens=body.max_input_tokens,
        max_output_tokens=body.max_output_tokens,
        timeout_seconds=body.timeout_seconds,
        fingerprint=_configuration_fingerprint(body),
        created_by=context.principal_id,
    )
    session.add(route)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="model route version already exists") from exc
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="model_route.create",
        resource_type="model_route_configuration",
        resource_id=str(route.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "route_key": route.route_key,
            "version": route.version,
            "provider_type": route.provider_type,
            "uses_credential_reference": route.credential_reference is not None,
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="model_route_configuration",
        aggregate_id=str(route.id),
        event_type="model_route.created.v1",
        payload={"model_route_id": str(route.id), "route_key": route.route_key},
    )
    await session.commit()
    return _route_read(route, settings)


@router.get("/organizations/{organization_id}/model-routes", response_model=Page)
async def list_model_routes(
    organization_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "AgentDeveloper", "DataSteward", "Reviewer", "Auditor", "Viewer"
        )
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page:
    enforce_organization(context, organization_id)
    filters = (ModelRouteConfiguration.organization_id == organization_id,)
    total = await session.scalar(
        select(func.count()).select_from(ModelRouteConfiguration).where(*filters)
    )
    routes = (
        await session.scalars(
            select(ModelRouteConfiguration)
            .where(*filters)
            .order_by(
                ModelRouteConfiguration.route_key,
                ModelRouteConfiguration.version.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[_route_read(route, settings) for route in routes],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/model-routes/{route_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_201_CREATED,
)
async def submit_model_route(
    route_id: UUID,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "AgentDeveloper")),
    session: AsyncSession = Depends(get_session),
) -> GovernanceReview:
    route = await session.get(ModelRouteConfiguration, route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="model route not found")
    enforce_organization(context, route.organization_id)
    if route.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only draft model routes can be submitted")
    route.status = "PENDING_REVIEW"
    review = GovernanceReview(
        organization_id=route.organization_id,
        object_type="MODEL_ROUTE_CONFIGURATION",
        object_id=str(route.id),
        requested_action="APPROVE_MODEL_ROUTE",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=route.organization_id),
        action="model_route.submit",
        resource_type="model_route_configuration",
        resource_id=str(route.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"review_id": str(review.id)},
    )
    record_outbox(
        session,
        organization_id=route.organization_id,
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


# --- Kill switch (MG-2) -----------------------------------------------------------
#
# Deliberately NOT the ModelRouteConfiguration maker-checker lifecycle: job P5
# ("stop AI immediately") and the module-15 kill-switch contract (`20-modules/
# 15-model-gateway.md` §7) call for a single-operator, immediately-effective action,
# audited rather than dual-controlled -- the opposite failure mode from a route
# approval (where premature activation is the risk to guard against, an unreviewed
# kill is not). Reversal requires the same PlatformAdmin authorization and is
# audited identically.


def _kill_switch_scope(route_key: str | None) -> str:
    return route_key or GLOBAL_KILL_SWITCH_SCOPE


def _kill_switch_read(state: KillSwitchState) -> KillSwitchStateRead:
    return KillSwitchStateRead(
        id=state.id,
        organization_id=state.organization_id,
        route_key=state.route_key,
        scope="ORGANIZATION" if state.route_key == GLOBAL_KILL_SWITCH_SCOPE else "ROUTE",
        engaged=state.engaged,
        reason=state.reason,
        engaged_by=state.engaged_by,
        engaged_at=state.engaged_at,
        released_by=state.released_by,
        released_at=state.released_at,
        created_at=state.created_at,
        updated_at=state.updated_at,
    )


async def _get_or_create_kill_switch_state(
    session: AsyncSession, organization_id: UUID, route_key: str
) -> KillSwitchState:
    state = await session.scalar(
        select(KillSwitchState).where(
            KillSwitchState.organization_id == organization_id,
            KillSwitchState.route_key == route_key,
        )
    )
    if state is None:
        state = KillSwitchState(organization_id=organization_id, route_key=route_key)
        session.add(state)
        await session.flush()
    return state


@router.post(
    "/organizations/{organization_id}/kill-switch/engage",
    response_model=KillSwitchStateRead,
)
async def engage_kill_switch(
    organization_id: UUID,
    body: KillSwitchEngageRequest,
    context: SecurityContext = Depends(require_roles("PlatformAdmin")),
    session: AsyncSession = Depends(get_session),
) -> KillSwitchStateRead:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    scope = _kill_switch_scope(body.route_key)
    state = await _get_or_create_kill_switch_state(session, organization_id, scope)
    now = datetime.now(UTC)
    state.engaged = True
    state.reason = body.reason
    state.engaged_by = context.principal_id
    state.engaged_at = now
    state.released_by = None
    state.released_at = None
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="model.kill_switch_engage",
        resource_type="kill_switch_state",
        resource_id=str(state.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"scope": scope, "route_key": body.route_key, "reason": body.reason},
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="kill_switch_state",
        aggregate_id=str(state.id),
        event_type="model.kill_switch_engaged",
        payload={
            "scope": scope,
            "route_key": body.route_key,
            "actor": context.principal_id,
            "reason": body.reason,
            "engaged_at": now.isoformat(),
        },
    )
    await session.commit()
    return _kill_switch_read(state)


@router.post(
    "/organizations/{organization_id}/kill-switch/release",
    response_model=KillSwitchStateRead,
)
async def release_kill_switch(
    organization_id: UUID,
    body: KillSwitchReleaseRequest,
    context: SecurityContext = Depends(require_roles("PlatformAdmin")),
    session: AsyncSession = Depends(get_session),
) -> KillSwitchStateRead:
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    scope = _kill_switch_scope(body.route_key)
    state = await session.scalar(
        select(KillSwitchState).where(
            KillSwitchState.organization_id == organization_id,
            KillSwitchState.route_key == scope,
        )
    )
    if state is None or not state.engaged:
        raise HTTPException(status_code=409, detail="kill switch is not currently engaged")
    now = datetime.now(UTC)
    state.engaged = False
    state.released_by = context.principal_id
    state.released_at = now
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="model.kill_switch_release",
        resource_type="kill_switch_state",
        resource_id=str(state.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"scope": scope, "route_key": body.route_key, "reason": body.reason},
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="kill_switch_state",
        aggregate_id=str(state.id),
        event_type="model.kill_switch_released",
        payload={
            "scope": scope,
            "route_key": body.route_key,
            "actor": context.principal_id,
            "reason": body.reason,
            "released_at": now.isoformat(),
        },
    )
    await session.commit()
    return _kill_switch_read(state)


@router.get(
    "/organizations/{organization_id}/kill-switch",
    response_model=list[KillSwitchStateRead],
)
async def list_kill_switch_state(
    organization_id: UUID,
    context: SecurityContext = Depends(
        require_roles(
            "PlatformAdmin", "AgentDeveloper", "DataSteward", "Reviewer", "Auditor", "Viewer"
        )
    ),
    session: AsyncSession = Depends(get_session),
) -> list[KillSwitchStateRead]:
    enforce_organization(context, organization_id)
    states = (
        await session.scalars(
            select(KillSwitchState)
            .where(KillSwitchState.organization_id == organization_id)
            .order_by(KillSwitchState.route_key)
        )
    ).all()
    return [_kill_switch_read(state) for state in states]
