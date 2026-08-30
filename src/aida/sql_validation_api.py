"""HTTP surface for the deterministic SQL validator (review item N14).

`POST /v1/datasources/{datasource_id}/sql-validations` runs
`QueryExecutionGateway.validate` -- the same pipeline
`QueryExecutionGateway.execute` runs -- and returns structured findings without
contacting a source for anything but a dry-run estimate. Nothing is executed and
no source value is returned, so the response is safe to hand straight to a
coding agent.

This is deliberately a separate router rather than another route in `api.py`:
the existing `POST /v1/query/validate` is the *guard-only* check (parse,
read-only, structural rules) and has no catalog binding, no per-object
authorisation and no cost estimate. Both are kept -- the cheap one needs no
datasource and no source contact -- so this one lives beside its own module and
carries a datasource in its path.

Response models are declared here rather than in `aida.schemas` to keep the
finding vocabulary next to the module that defines it.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled
from aida.models import DataSource
from aida.query_gateway import AuthorizationRejected, QueryExecutionGateway
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["sql-validation"])

#: Same role binding as the guard-only `POST /v1/query/validate` in `api.py`.
#: An agent identity that may ask the platform to check SQL is an
#: `AgentDeveloper`; validation returns no rows, so it is not gated on the
#: narrower execution roles.
SQL_VALIDATION_ROLES = ("PlatformAdmin", "Analyst", "AgentDeveloper")


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class GatewaySqlValidationRequest(ApiModel):
    sql: str = Field(min_length=1, max_length=200_000)
    max_rows: int | None = Field(default=None, ge=1, le=1_000_000)
    # See `QueryExecutionRequest.workspace_id` (ADR-0018): optional while the estate
    # migrates, required once the unresolved posture flips to DENY.
    workspace_id: UUID | None = None


class SqlFindingRead(ApiModel):
    code: str
    severity: str
    ref: str | None = None
    hint: str
    detail: dict[str, Any] = Field(default_factory=dict)


class QueryEstimateRead(ApiModel):
    plan_cost: float | None = None
    kind: str | None = None
    estimated_rows: float | None = None
    estimated_bytes: int | None = None


class GatewaySqlValidationResponse(ApiModel):
    valid: bool
    dialect: str
    findings: list[SqlFindingRead]
    normalized_sql: str | None = None
    referenced_tables: list[str]
    referenced_columns: list[str]
    applied_row_limit: int | None = None
    column_lineage: list[dict[str, Any]]
    estimate: QueryEstimateRead
    rejection_reason: str | None = None


@router.post(
    "/datasources/{datasource_id}/sql-validations",
    response_model=GatewaySqlValidationResponse,
    summary="Validate SQL through the query gateway without executing it",
)
async def validate_sql(
    datasource_id: UUID,
    body: GatewaySqlValidationRequest,
    context: SecurityContext = Depends(require_roles(*SQL_VALIDATION_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GatewaySqlValidationResponse:
    """Return findings, never rows.

    An invalid statement is a 200 with `valid: false`, not a 4xx: the findings
    *are* the answer an agent asked for, and turning them into an error status
    would make the iterate-against-the-compiler loop harder to consume. 4xx is
    reserved for the request itself being unusable (unknown datasource,
    cross-organization access, a disabled datasource).
    """
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        ensure_datasource_enabled(datasource)
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    gateway = QueryExecutionGateway(settings)
    try:
        report = await gateway.validate(
            session,
            datasource=datasource,
            context=context,
            correlation_id=get_correlation_id(),
            sql=body.sql,
            requested_limit=body.max_rows,
            workspace_id=body.workspace_id,
        )
    except AuthorizationRejected as exc:
        # Ahead of the blanket handler below, which would otherwise report a refusal
        # this platform made deliberately as a failure of the customer's warehouse.
        raise HTTPException(status_code=403, detail=exc.reason_code) from exc
    except Exception as exc:  # pragma: no cover - source dry run failed
        raise HTTPException(status_code=502, detail="source query estimate failed") from exc

    payload = report.as_dict()
    return GatewaySqlValidationResponse(
        valid=report.valid,
        dialect=report.dialect,
        findings=[SqlFindingRead(**item) for item in payload["findings"]],
        normalized_sql=report.normalized_sql,
        referenced_tables=list(report.referenced_tables),
        referenced_columns=list(report.referenced_columns),
        applied_row_limit=report.applied_row_limit,
        column_lineage=[dict(item) for item in report.column_lineage],
        estimate=QueryEstimateRead(
            plan_cost=report.plan_cost,
            kind=report.estimate_kind,
            estimated_rows=report.estimated_rows,
            estimated_bytes=report.estimated_bytes,
        ),
        rejection_reason=report.rejection_reason(),
    )
