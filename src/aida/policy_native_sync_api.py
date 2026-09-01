"""HTTP surface for source-native row/column policy synchronization (QG-2).

Three operations, deliberately separated:

* `POST .../native-policy-sync/preview` -- generate the native DDL for one table
  and return it. Nothing is persisted or applied; safe for any steward-tier role
  to call freely while designing a policy.
* `POST .../native-policy-sync/requests` -- freeze a preview into a durable,
  reviewable request (`PolicyNativeSyncRequest`), `PENDING` a decision.
* `POST /native-policy-sync/requests/{id}/decision` -- maker-checker decide it. A
  *different* principal than the requester must decide (INV-8-shaped, mirroring
  `decide_profiling_exception_policy` in `api.py`); `APPROVE` immediately attempts
  the apply against the live source and records the outcome (`APPLIED` or
  `APPLY_FAILED`) durably, either way. `REJECT` never touches the source.

This is a separate router rather than more routes in `api.py`, for the same reason
`sql_validation_api.py` is one: it is its own reviewable surface, and keeping it out
of `api.py` keeps this branch's single largest shared file out of this change's
diff entirely, which matters more than usual on a branch this many concurrent
sessions are editing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_graph import load_policies
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled
from aida.models import (
    DataSource,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    PolicyNativeSyncRequest,
)
from aida.policy_native_sync import (
    NativeStatement,
    NativeSyncPlan,
    PolicyNativeSyncError,
    apply_native_sync_plan,
    build_native_sync_plan,
)
from aida.schemas import Page
from aida.secrets import SecretResolver
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.signing import resolve_signing_provider

router = APIRouter(prefix="/v1", tags=["policy-native-sync"])

#: A preview costs nothing but a read and generates no obligation, so the same
#: steward-tier roles that may request a profiling exception may preview freely.
NATIVE_POLICY_SYNC_PREVIEW_ROLES = ("PlatformAdmin", "DataAdmin", "DataSteward")
NATIVE_POLICY_SYNC_REQUEST_ROLES = ("PlatformAdmin", "DataAdmin", "DataSteward")
NATIVE_POLICY_SYNC_READ_ROLES = (
    "PlatformAdmin",
    "DataAdmin",
    "DataSteward",
    "Reviewer",
    "Viewer",
)
#: The role tier trusted to write a native `CREATE POLICY`/`ADD MASKED` DDL to a
#: live source. Deliberately narrower than the request roles and identical to
#: `PROFILING_EXCEPTION_DECIDE_ROLES` in `api.py` -- decision authority over a
#: live-source write is not something `DataAdmin` alone should hold here either.
NATIVE_POLICY_SYNC_DECIDE_ROLES = ("PlatformAdmin", "DataSteward", "Reviewer")


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class NativePolicySyncTableRequest(ApiModel):
    schema_name: str = Field(min_length=1, max_length=255)
    table_name: str = Field(min_length=1, max_length=255)


class NativeStatementRead(ApiModel):
    kind: str
    sql: str
    target_schema: str
    target_table: str
    target_column: str | None
    policy_code: str


class NativeSyncPlanRead(ApiModel):
    datasource_id: UUID
    connector_type: str
    schema_name: str
    table_name: str
    row_policy_count: int
    column_policy_count: int
    statements: list[NativeStatementRead]
    unsupported: list[str]


class PolicyNativeSyncRequestCreate(NativePolicySyncTableRequest):
    reason: str = Field(min_length=3, max_length=2000)


class PolicyNativeSyncRequestRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    connector_type: str
    schema_name: str
    table_name: str
    statements: list[dict[str, Any]]
    row_policy_count: int
    column_policy_count: int
    unsupported: list[str]
    status: str
    requested_by: str
    request_reason: str
    decided_by: str | None
    decision_reason: str | None
    decided_at: datetime | None
    applied_at: datetime | None
    apply_error: str | None
    created_at: datetime


class NativePolicySyncDecisionRequest(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_rejection_reason(self) -> NativePolicySyncDecisionRequest:
        if self.decision == "REJECT" and not self.reason:
            raise ValueError("a reason is required when rejecting a native policy sync request")
        return self


async def _load_datasource(session: AsyncSession, datasource_id: UUID) -> DataSource:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    return datasource


async def _load_table_columns(
    session: AsyncSession, datasource: DataSource, schema_name: str, table_name: str
) -> tuple[MetadataTable, list[tuple[str, str]]]:
    """The one table's ACTIVE columns, keyed the way `build_native_sync_plan` wants.

    Same tenancy and ACTIVE-only filters `QueryExecutionGateway._catalog_columns`
    already applies, scoped down to exactly one already-resolved table rather than
    every table a statement references.
    """
    table = await session.scalar(
        select(MetadataTable)
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .where(
            MetadataTable.datasource_id == datasource.id,
            MetadataTable.organization_id == datasource.organization_id,
            MetadataTable.status == "ACTIVE",
            MetadataTable.name == table_name,
            MetadataSchema.name == schema_name,
        )
    )
    if table is None:
        raise HTTPException(
            status_code=404,
            detail=f"no ACTIVE table named {table_name!r} in schema {schema_name!r}",
        )
    rows = (
        await session.execute(
            select(MetadataColumn.name, MetadataColumn.classification).where(
                MetadataColumn.table_id == table.id,
                MetadataColumn.organization_id == datasource.organization_id,
                MetadataColumn.status == "ACTIVE",
            )
        )
    ).all()
    return table, [(str(name), str(classification)) for name, classification in rows]


async def _build_plan(
    session: AsyncSession, datasource: DataSource, schema_name: str, table_name: str
) -> NativeSyncPlan:
    _table, columns = await _load_table_columns(session, datasource, schema_name, table_name)
    policies = await load_policies(session, datasource.organization_id)
    try:
        return build_native_sync_plan(
            policies,
            datasource_id=datasource.id,
            connector_type=datasource.connector_type,
            schema_name=schema_name,
            table_name=table_name,
            columns=columns,
        )
    except PolicyNativeSyncError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _plan_read(plan: NativeSyncPlan) -> NativeSyncPlanRead:
    return NativeSyncPlanRead(
        datasource_id=plan.datasource_id,
        connector_type=plan.connector_type,
        schema_name=plan.schema_name,
        table_name=plan.table_name,
        row_policy_count=len(plan.row_policies),
        column_policy_count=len(plan.column_policies),
        statements=[NativeStatementRead(**statement.as_dict()) for statement in plan.statements],
        unsupported=list(plan.unsupported),
    )


@router.post(
    "/datasources/{datasource_id}/native-policy-sync/preview",
    response_model=NativeSyncPlanRead,
    summary="Generate source-native row/column policy DDL without applying it",
)
async def preview_native_policy_sync(
    datasource_id: UUID,
    body: NativePolicySyncTableRequest,
    context: SecurityContext = Depends(require_roles(*NATIVE_POLICY_SYNC_PREVIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> NativeSyncPlanRead:
    """Dry run: resolve governed policies for one table and generate the matching
    `CREATE POLICY`/`ADD MASKED` DDL, without writing anything to the source or to
    platform state beyond the audit trail below. Safe to call repeatedly while
    iterating on a policy -- nothing here is durable except the audit record of
    having looked.
    """
    datasource = await _load_datasource(session, datasource_id)
    enforce_organization(context, datasource.organization_id)
    try:
        ensure_datasource_enabled(datasource)
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    plan = await _build_plan(session, datasource, body.schema_name, body.table_name)

    audit_context = replace(context, organization_id=datasource.organization_id)
    record_audit(
        session,
        audit_context,
        action="policy_native_sync.preview",
        resource_type="datasource",
        resource_id=str(datasource.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "schema_name": body.schema_name,
            "table_name": body.table_name,
            "connector_type": datasource.connector_type,
            "statement_count": len(plan.statements),
            "row_policy_count": len(plan.row_policies),
            "column_policy_count": len(plan.column_policies),
            "unsupported": list(plan.unsupported),
        },
    )
    await session.commit()
    return _plan_read(plan)


@router.post(
    "/datasources/{datasource_id}/native-policy-sync/requests",
    response_model=PolicyNativeSyncRequestRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_native_policy_sync(
    datasource_id: UUID,
    body: PolicyNativeSyncRequestCreate,
    context: SecurityContext = Depends(require_roles(*NATIVE_POLICY_SYNC_REQUEST_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> PolicyNativeSyncRequest:
    """Freeze a preview into a durable, reviewable request. `PENDING` until a
    *different* principal decides it via `POST /native-policy-sync/requests/{id}/decision`
    -- only that decision, never this one, can result in DDL reaching the source.
    """
    datasource = await _load_datasource(session, datasource_id)
    enforce_organization(context, datasource.organization_id)
    try:
        ensure_datasource_enabled(datasource)
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    plan = await _build_plan(session, datasource, body.schema_name, body.table_name)
    if not plan.statements:
        raise HTTPException(
            status_code=422,
            detail=(
                "no synchronizable native policy obligations were found for this "
                "table -- nothing to request"
            ),
        )

    request = PolicyNativeSyncRequest(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        connector_type=datasource.connector_type,
        schema_name=body.schema_name,
        table_name=body.table_name,
        statements=[statement.as_dict() for statement in plan.statements],
        row_policy_count=len(plan.row_policies),
        column_policy_count=len(plan.column_policies),
        unsupported=list(plan.unsupported),
        status="PENDING",
        requested_by=context.principal_id,
        request_reason=body.reason,
    )
    session.add(request)
    await session.flush()
    audit_context = replace(context, organization_id=datasource.organization_id)
    record_audit(
        session,
        audit_context,
        action="policy_native_sync.request",
        resource_type="policy_native_sync_request",
        resource_id=str(request.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "datasource_id": str(datasource.id),
            "schema_name": body.schema_name,
            "table_name": body.table_name,
            "statement_count": len(plan.statements),
        },
    )
    record_outbox(
        session,
        organization_id=datasource.organization_id,
        aggregate_type="policy_native_sync_request",
        aggregate_id=str(request.id),
        event_type="policy_native_sync.requested.v1",
        payload={
            "request_id": str(request.id),
            "datasource_id": str(datasource.id),
            "schema_name": body.schema_name,
            "table_name": body.table_name,
        },
    )
    await session.commit()
    return request


@router.get(
    "/datasources/{datasource_id}/native-policy-sync/requests",
    response_model=Page,
)
async def list_native_policy_sync_requests(
    datasource_id: UUID,
    request_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*NATIVE_POLICY_SYNC_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    datasource = await _load_datasource(session, datasource_id)
    enforce_organization(context, datasource.organization_id)
    filters = [
        PolicyNativeSyncRequest.organization_id == datasource.organization_id,
        PolicyNativeSyncRequest.datasource_id == datasource.id,
    ]
    if request_status:
        filters.append(PolicyNativeSyncRequest.status == request_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(PolicyNativeSyncRequest).where(*filters)
    )
    rows = (
        await session.scalars(
            select(PolicyNativeSyncRequest)
            .where(*filters)
            .order_by(PolicyNativeSyncRequest.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[PolicyNativeSyncRequestRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


def _statements_from_request(request: PolicyNativeSyncRequest) -> tuple[NativeStatement, ...]:
    return tuple(
        NativeStatement(
            kind=str(item["kind"]),
            sql=str(item["sql"]),
            target_schema=str(item["target_schema"]),
            target_table=str(item["target_table"]),
            target_column=(
                None if item.get("target_column") is None else str(item["target_column"])
            ),
            policy_code=str(item["policy_code"]),
        )
        for item in request.statements
    )


@router.post(
    "/native-policy-sync/requests/{request_id}/decision",
    response_model=PolicyNativeSyncRequestRead,
)
async def decide_native_policy_sync_request(
    request_id: UUID,
    body: NativePolicySyncDecisionRequest,
    context: SecurityContext = Depends(require_roles(*NATIVE_POLICY_SYNC_DECIDE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> PolicyNativeSyncRequest:
    """Decide a pending request. Maker != checker (INV-8-shaped): the deciding
    principal must differ from `requested_by`, enforced the same way
    `decide_profiling_exception_policy` enforces it in `api.py`.

    `REJECT` only records the decision. `APPROVE` immediately attempts the apply
    against the live source and records the real outcome durably either way --
    `APPLIED` with the DDL's HMAC evidence (`aida.signing`, the same evidence
    mechanism `QueryExecutionGateway` uses for executed SQL) on success,
    `APPLY_FAILED` with the exception class (never the raw driver error, which can
    carry source-side values -- INV-6) on failure. A failed apply never raises out
    of this endpoint: the caller gets back the request row showing exactly what
    happened, which is the more useful answer than a 5xx for an operation that
    already ran against an external system.
    """
    request = await session.scalar(
        select(PolicyNativeSyncRequest)
        .where(PolicyNativeSyncRequest.id == request_id)
        .with_for_update()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="policy native sync request not found")
    enforce_organization(context, request.organization_id)
    if request.status != "PENDING":
        raise HTTPException(status_code=409, detail="policy native sync request is already decided")
    if request.requested_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker-checker separation is required")

    now = datetime.now(UTC)
    audit_context = replace(context, organization_id=request.organization_id)

    if body.decision != "APPROVE":
        request.status = "REJECTED"
        request.decided_by = context.principal_id
        request.decision_reason = body.reason
        request.decided_at = now
        record_audit(
            session,
            audit_context,
            action="policy_native_sync.decide",
            resource_type="policy_native_sync_request",
            resource_id=str(request.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={"decision": "REJECT"},
        )
        record_outbox(
            session,
            organization_id=request.organization_id,
            aggregate_type="policy_native_sync_request",
            aggregate_id=str(request.id),
            event_type="policy_native_sync.decided.v1",
            payload={"request_id": str(request.id), "status": request.status},
        )
        await session.commit()
        return request

    request.status = "APPROVED"
    request.decided_by = context.principal_id
    request.decision_reason = body.reason
    request.decided_at = now
    record_audit(
        session,
        audit_context,
        action="policy_native_sync.decide",
        resource_type="policy_native_sync_request",
        resource_id=str(request.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": "APPROVE"},
    )
    record_outbox(
        session,
        organization_id=request.organization_id,
        aggregate_type="policy_native_sync_request",
        aggregate_id=str(request.id),
        event_type="policy_native_sync.decided.v1",
        payload={"request_id": str(request.id), "status": request.status},
    )

    datasource = await session.get(DataSource, request.datasource_id)
    if datasource is None:  # pragma: no cover - FK(ondelete=CASCADE) makes this unreachable
        raise HTTPException(status_code=409, detail="datasource for this request no longer exists")

    statements = _statements_from_request(request)
    plan = NativeSyncPlan(
        datasource_id=request.datasource_id,
        connector_type=request.connector_type,
        schema_name=request.schema_name,
        table_name=request.table_name,
        row_policies=(),
        column_policies=(),
        statements=statements,
        unsupported=tuple(request.unsupported),
    )
    statements_hash = await resolve_signing_provider(settings).sign(
        "\n".join(statement.sql for statement in statements)
    )
    try:
        dsn = SecretResolver(settings).resolve(datasource.credential_reference)
        await apply_native_sync_plan(
            plan, dsn=dsn, timeout_seconds=float(settings.query_timeout_seconds)
        )
    except Exception as exc:
        request.status = "APPLY_FAILED"
        request.apply_error = type(exc).__name__
        record_audit(
            session,
            audit_context,
            action="policy_native_sync.apply",
            resource_type="policy_native_sync_request",
            resource_id=str(request.id),
            outcome="FAILURE",
            correlation_id=get_correlation_id(),
            details={"error_class": type(exc).__name__, "statements_hash": statements_hash},
        )
        await session.commit()
        return request

    request.status = "APPLIED"
    request.applied_at = datetime.now(UTC)
    record_audit(
        session,
        audit_context,
        action="policy_native_sync.apply",
        resource_type="policy_native_sync_request",
        resource_id=str(request.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "datasource_id": str(datasource.id),
            "schema_name": request.schema_name,
            "table_name": request.table_name,
            "statement_count": len(statements),
            "statements_hash": statements_hash,
        },
    )
    record_outbox(
        session,
        organization_id=request.organization_id,
        aggregate_type="policy_native_sync_request",
        aggregate_id=str(request.id),
        event_type="policy_native_sync.applied.v1",
        payload={
            "request_id": str(request.id),
            "datasource_id": str(datasource.id),
            "statements_hash": statements_hash,
        },
    )
    await session.commit()
    return request
