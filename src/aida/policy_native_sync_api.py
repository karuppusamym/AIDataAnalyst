"""HTTP surface for source-native row/column policy synchronization (QG-2).

One operation: preview.

* `POST .../native-policy-sync/preview` -- generate the native DDL for one table
  and return it. Nothing is persisted and nothing is applied, so any
  steward-tier role may call it freely while designing a policy.

The request/decision/apply trio that used to sit beside it was removed in the
2026-09-11 review (defect D1): approving a request executed the generated DDL
over this process's own driver connection to the source, a second SQL
execution path that INV-2 / ADR-0004 reserve for `aida.query_gateway`. The
governed statements are still generated here; running them belongs to whoever
owns DDL change control on that source. Reinstating in-platform execution
needs an ADR, not a pull request.

This is a separate router rather than more routes in `api.py`, for the same reason
`sql_validation_api.py` is one: it is its own reviewable surface, and keeping it out
of `api.py` keeps this branch's single largest shared file out of this change's
diff entirely, which matters more than usual on a branch this many concurrent
sessions are editing.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_graph import load_policies
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled
from aida.models import (
    DataSource,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
)
from aida.policy_native_sync import (
    NativeSyncPlan,
    PolicyNativeSyncError,
    build_native_sync_plan,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["policy-native-sync"])

#: A preview costs nothing but a read and generates no obligation, so the same
#: steward-tier roles that may request a profiling exception may preview freely.
NATIVE_POLICY_SYNC_PREVIEW_ROLES = ("PlatformAdmin", "DataAdmin", "DataSteward")
NATIVE_POLICY_SYNC_READ_ROLES = (
    "PlatformAdmin",
    "DataAdmin",
    "DataSteward",
    "Reviewer",
    "Viewer",
)



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
