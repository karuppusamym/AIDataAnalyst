"""connectivity -- HTTP routes, mounted by the app entrypoint (`aida.main`).

Status: real content, populated 2026-09-03 under tracker ST-07 Commit C
(Phase 5 of `Docs/40-engineering/06-refactor-plan.md`). Endpoints move here
verbatim from `aida.api`; each preserves its path, method, response model,
tag placement (none) and required roles, so `openapi.json` is byte-identical
after the move. Only the source module for each handler changes.

The router deliberately keeps `APIRouter(prefix="/v1")` with NO `tags=`
argument -- the `aida.api` router it inherits from also carries no tags,
and adding a "connectivity" tag here would give the moved endpoints an
OpenAPI tag they didn't have before. Grouping in Swagger UI stays exactly
as it was. Same convention `atlas.modules.catalog.router` established the
same session.

Endpoints moved (per session addendum
`Docs/60-delivery/09-session-2026-09-03-addendum.md`):

* `GET  /v1/projects/{project_id}/datasources`            -- list_datasources
* `POST /v1/projects/{project_id}/datasources`             -- create_datasource
* `POST /v1/projects/{project_id}/datasources/bulk-onboard` -- bulk_onboard_datasources
* `PATCH /v1/datasources/{datasource_id}`                  -- update_datasource
* `PUT  /v1/datasources/{datasource_id}/scan-policy`       -- upsert_scan_policy
* `GET  /v1/datasources/{datasource_id}/scan-policy`       -- get_scan_policy
* `POST /v1/datasources/{datasource_id}/test`              -- test_datasource

plus three private helpers used only by `create_datasource` and
`bulk_onboard_datasources` (`_validate_datasource_create`,
`_build_datasource`, `_record_datasource_registration_events`).

Deliberately NOT moved here (stay in `aida.api`, not this module's
domain):

* Anything under `/datasources/{id}/tables`, `/datasources/{id}/agent-runs`,
  `/datasources/{id}/analysis-runs` -- catalog / agent-runtime territory.
* `ingest_datasource_classification_feed`
  (`/datasources/{id}/classification-feed/ingest`) -- classification module
  territory, even though it hangs off a datasource id.

`_commit_or_conflict` (used by `create_datasource`) stays defined in
`aida.api` -- it is shared by four non-connectivity endpoints there
(organization/LOB/data-domain/project creation) as well, so moving it
here would make those four import it back from this protected module for
no reason. Importing it from `aida.api` is the correct direction (atlas
depends on aida, never the reverse) and needs no import-linter change: the
`connectivity module privacy` contract only restricts who may import
*this* module's protected files, not what they themselves import.

`ScanPolicy` (the model) and `ScanPolicyRead`/`ScanPolicyUpsert` (the
schemas) are imported from `aida.models` / `aida.schemas` rather than
`atlas.modules.connectivity.{models,schemas}` because that is where the
moved endpoints imported them from before the move -- `ScanPolicy` was
never part of the ST-05 model migration into this module (only
`DataSource` and `ConnectorCertificationRun` were), so it still lives in
`aida.models` directly, not as a re-export shim. Migrating `ScanPolicy`
itself into this module's owned models is a separate, later decision, not
part of this Commit C router move.

Import-linter: this module is in the `connectivity module privacy`
contract's `protected_modules` list; it can be imported by
`atlas.modules.connectivity.api` (and by the app-assembly file
`aida.main`, which needs to `include_router` it) plus, as of this move,
`aida.api` itself -- three symbols (`create_datasource`,
`bulk_onboard_datasources`, `test_datasource`) are re-exported from there
for two existing test modules that import them by name
(`tests/test_bulk_source_onboarding.py`,
`tests/test_datasource_lifecycle.py`). New sibling modules that need to
reach handlers from outside should call them via HTTP or via a service
function, not via a router import.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.api import _commit_or_conflict
from aida.config import Settings, get_settings
from aida.connectors.registry import connector_registry
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import DataSource, Project, ScanPolicy
from aida.schemas import (
    DataSourceBulkOnboardItemRead,
    DataSourceBulkOnboardRequest,
    DataSourceBulkOnboardResultRead,
    DataSourceCreate,
    DataSourceRead,
    DataSourceSummaryRead,
    DataSourceUpdate,
    Page,
    ScanPolicyRead,
    ScanPolicyUpsert,
)
from aida.secrets import SecretResolver
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1")


def _validate_datasource_create(body: DataSourceCreate, settings: Settings) -> None:
    """Shared, DB-free registration validation for a single datasource spec.

    Used by both `create_datasource` and `bulk_onboard_datasources` so the two
    paths cannot drift: credential-reference provider check and connector-type
    support are exactly the same rule either way (IN-1).
    """
    approved_reference_prefix = f"{settings.credential_provider}://"
    if not body.credential_reference.startswith(approved_reference_prefix):
        raise HTTPException(
            status_code=422,
            detail=(
                "credential_reference must use the configured secret provider, "
                "never a connection string or unapproved provider"
            ),
        )
    if body.connector_type not in connector_registry.supported_types:
        raise HTTPException(
            status_code=422,
            detail=f"unsupported connector type: {body.connector_type}",
        )


def _build_datasource(project: Project, body: DataSourceCreate) -> DataSource:
    return DataSource(
        organization_id=project.organization_id,
        line_of_business_id=project.line_of_business_id,
        data_domain_id=project.data_domain_id,
        project_id=project.id,
        **body.model_dump(),
    )


def _record_datasource_registration_events(
    session: AsyncSession,
    audit_context: SecurityContext,
    project: Project,
    datasource: DataSource,
) -> None:
    record_audit(
        session,
        audit_context,
        action="datasource.register",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "connector_type": datasource.connector_type,
            "network_zone": datasource.network_zone,
        },
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="datasource",
        aggregate_id=str(datasource.id),
        event_type="datasource.registered.v1",
        payload={
            "datasource_id": str(datasource.id),
            "project_id": str(project.id),
            "connector_type": datasource.connector_type,
        },
    )


@router.get("/projects/{project_id}/datasources", response_model=Page)
async def list_datasources(
    project_id: UUID,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "OrganizationAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> Page:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    filters = (DataSource.project_id == project.id,)
    total = await session.scalar(select(func.count()).select_from(DataSource).where(*filters))
    rows = (
        await session.scalars(
            select(DataSource).where(*filters).order_by(DataSource.name).limit(limit).offset(offset)
        )
    ).all()
    return Page(
        items=[DataSourceSummaryRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/projects/{project_id}/datasources",
    response_model=DataSourceRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_datasource(
    project_id: UUID,
    body: DataSourceCreate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataAdmin")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DataSource:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)
    _validate_datasource_create(body, settings)
    datasource = _build_datasource(project, body)
    session.add(datasource)
    await session.flush()
    audit_context = replace(context, organization_id=project.organization_id)
    _record_datasource_registration_events(session, audit_context, project, datasource)
    await _commit_or_conflict(session, "datasource name already exists in this project")
    return datasource


@router.post(
    "/projects/{project_id}/datasources/bulk-onboard",
    response_model=DataSourceBulkOnboardResultRead,
)
async def bulk_onboard_datasources(
    project_id: UUID,
    body: DataSourceBulkOnboardRequest,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataAdmin")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DataSourceBulkOnboardResultRead:
    """IN-1: register up to DATASOURCE_BULK_ONBOARD_MAX_ITEMS datasources in one call.

    Every item goes through exactly the same registration path a single
    `create_datasource` call would -- `_validate_datasource_create` and
    `_build_datasource` are the identical functions that endpoint calls, so
    there is no bulk-only shortcut on credential-reference validation,
    connector-type support, or per-project name uniqueness. A bad item (an
    unapproved credential reference, an unsupported connector type, or a name
    that collides with an existing datasource or an earlier item in this same
    batch) fails only that item -- CT-1/RL-6's partial-success precedent, not
    an all-or-nothing transaction. Each item's insert runs inside its own
    SAVEPOINT (`session.begin_nested()`) so a `DataSource.project_id+name`
    uniqueness violation caught at flush time rolls back only that item, never
    the datasources already staged from earlier in the batch.

    No connectivity probe runs here, in or out of a Temporal workflow: the
    single-item path doesn't run one either at registration time (that is
    `test_datasource`, POST `/datasources/{id}/test`, a separate step the
    caller invokes per source after registration), so there is nothing
    per-item-slow to defer for the bulk path either -- 200 items is 200 bounded
    DB writes, not 200 outbound connections.
    """
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    enforce_organization(context, project.organization_id)

    existing_names = set(
        await session.scalars(select(DataSource.name).where(DataSource.project_id == project.id))
    )
    audit_context = replace(context, organization_id=project.organization_id)

    results: list[DataSourceBulkOnboardItemRead] = []
    succeeded = 0
    for index, item in enumerate(body.datasources):
        try:
            _validate_datasource_create(item, settings)
            if item.name in existing_names:
                raise HTTPException(
                    status_code=422,
                    detail="datasource name already exists in this project",
                )
        except HTTPException as exc:
            results.append(
                DataSourceBulkOnboardItemRead(
                    index=index,
                    name=item.name,
                    status="FAILED",
                    reason=str(exc.detail),
                )
            )
            continue

        datasource = _build_datasource(project, item)
        try:
            async with session.begin_nested():
                session.add(datasource)
                await session.flush()
        except IntegrityError:
            results.append(
                DataSourceBulkOnboardItemRead(
                    index=index,
                    name=item.name,
                    status="FAILED",
                    reason="datasource name already exists in this project",
                )
            )
            continue

        existing_names.add(item.name)
        _record_datasource_registration_events(session, audit_context, project, datasource)
        results.append(
            DataSourceBulkOnboardItemRead(
                index=index,
                name=item.name,
                status="SUCCEEDED",
                datasource_id=datasource.id,
                reason=None,
            )
        )
        succeeded += 1

    failed = len(results) - succeeded
    record_audit(
        session,
        audit_context,
        action="datasource.bulk_register",
        resource_type="datasource",
        resource_id=None,
        outcome="SUCCESS" if not failed else "PARTIAL_SUCCESS" if succeeded else "FAILURE",
        correlation_id=get_correlation_id(),
        details={
            "project_id": str(project.id),
            "requested_count": len(results),
            "succeeded_count": succeeded,
            "failed_count": failed,
        },
    )
    await session.commit()
    return DataSourceBulkOnboardResultRead(
        requested_count=len(results),
        succeeded_count=succeeded,
        failed_count=failed,
        results=results,
    )


@router.patch("/datasources/{datasource_id}", response_model=DataSourceRead)
async def update_datasource(
    datasource_id: UUID,
    body: DataSourceUpdate,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataAdmin")),
    session: AsyncSession = Depends(get_session),
) -> DataSource:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    changes = body.model_dump(exclude_unset=True)
    enabled = changes.pop("enabled", None)
    if enabled is not None:
        datasource.status = (
            "CONNECTION_VERIFIED" if enabled and datasource.capabilities else "REGISTERED"
        )
        if not enabled:
            datasource.status = "DISABLED"
    for field, value in changes.items():
        setattr(datasource, field, value)
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="datasource.update",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"updated_fields": sorted(body.model_fields_set)},
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="datasource",
        aggregate_id=str(datasource.id),
        event_type="datasource.updated.v1",
        payload={
            "datasource_id": str(datasource.id),
            "status": datasource.status,
            "max_concurrency": datasource.max_concurrency,
        },
    )
    await session.commit()
    return datasource


@router.put("/datasources/{datasource_id}/scan-policy", response_model=ScanPolicyRead)
async def upsert_scan_policy(
    datasource_id: UUID,
    body: ScanPolicyUpsert,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin")
    ),
    session: AsyncSession = Depends(get_session),
) -> ScanPolicy:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    now = datetime.now(UTC)
    if body.start_at is not None and body.start_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="start_at must include a timezone")
    next_run_at = body.start_at.astimezone(UTC) if body.start_at else now
    policy = await session.scalar(
        select(ScanPolicy).where(ScanPolicy.datasource_id == datasource.id)
    )
    values = body.model_dump(exclude={"start_at"})
    # base_priority tracks the admin's own explicit choice separately from the
    # scheduler-visible `priority` column, so a later usage-weighted rebalance
    # (workflows/scheduler.rebalance_usage_weighted_priorities) always computes
    # from what the admin actually asked for, never from a previously-boosted
    # value (ADR-0017 SS8).
    values["base_priority"] = body.priority
    if policy is None:
        policy = ScanPolicy(
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            next_run_at=next_run_at,
            created_by=context.principal_id,
            **values,
        )
        session.add(policy)
    else:
        for field, value in values.items():
            setattr(policy, field, value)
        if body.start_at is not None or policy.next_run_at < now:
            policy.next_run_at = next_run_at
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="scan_policy.upsert",
        resource_type="scan_policy",
        resource_id=str(policy.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"enabled": policy.enabled, "interval_minutes": policy.interval_minutes},
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="scan_policy",
        aggregate_id=str(policy.id),
        event_type="scan_policy.updated.v1",
        payload={
            "scan_policy_id": str(policy.id),
            "datasource_id": str(datasource.id),
            "enabled": policy.enabled,
        },
    )
    await session.commit()
    return policy


@router.get("/datasources/{datasource_id}/scan-policy", response_model=ScanPolicyRead)
async def get_scan_policy(
    datasource_id: UUID,
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> ScanPolicy:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    policy = await session.scalar(
        select(ScanPolicy).where(ScanPolicy.datasource_id == datasource.id)
    )
    if policy is None:
        raise HTTPException(status_code=404, detail="scan policy not found")
    return policy


@router.post("/datasources/{datasource_id}/test", response_model=DataSourceRead)
async def test_datasource(
    datasource_id: UUID,
    context: SecurityContext = Depends(require_roles("PlatformAdmin", "DataAdmin")),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> DataSource:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        dsn = SecretResolver(settings).resolve(datasource.credential_reference)
        connector = connector_registry.create(datasource.connector_type, dsn)
        await connector.test_connection()
        if datasource.status != "ACTIVE":
            datasource.status = "CONNECTION_VERIFIED"
        datasource.capabilities = asdict(connector.capabilities)
        outcome = "SUCCESS"
    except Exception as exc:
        datasource.status = "CONNECTION_FAILED"
        outcome = "FAILURE"
        record_audit(
            session,
            replace(context, organization_id=datasource.organization_id),
            action="datasource.test",
            resource_type="datasource",
            resource_id=str(datasource.id),
            outcome=outcome,
            correlation_id=get_correlation_id(),
            details={"error_class": type(exc).__name__},
        )
        await session.commit()
        raise HTTPException(status_code=424, detail="datasource connection test failed") from exc
    record_audit(
        session,
        replace(context, organization_id=datasource.organization_id),
        action="datasource.test",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome=outcome,
        correlation_id=get_correlation_id(),
    )
    await session.commit()
    return datasource
