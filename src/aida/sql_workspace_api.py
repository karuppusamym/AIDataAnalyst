"""HTTP surface for the SQL review workspace (R11-SQL01; lifecycle in `aida.sql_workspace`).

Two routes, and nothing executes on the first:

* `POST /v1/datasources/{datasource_id}/sql-drafts` takes a question *or* a statement. A question
  is drafted by the governed Ask stages up to the SQL (`GovernedAgentOrchestrator.draft`); a
  statement -- pasted, or a draft the person edited -- is taken as the person's own. Either way
  the gateway validates it without executing it and a valid statement gets a receipt.
* `POST /v1/sql-drafts/{receipt_id}/run` sends the statement back with the receipt and runs it
  once, through the gateway's full authorization and validation.
* `GET /v1/datasources/{datasource_id}/sql-drafts` lists the caller's own recent receipts on
  that datasource: the redacted shape, status and execution, never a literal or a row.

A statement may carry `parameters`: `:name` placeholders in the text, each declared with a type
and a value sent beside it, bound by the governed-tool renderer (see `aida.sql_workspace`). Run
sends the same parameters back; any other value is REVALIDATION_REQUIRED.

Roles are the execution route's, `PlatformAdmin` and `Analyst`: a receipt is only worth having to
someone who may run it. An `agent:` identity is held to its contract exactly as on Ask.

Separate from `api.py` for the reason `sql_validation_api` gives: the finding and receipt
vocabulary stays beside the module that defines it.
"""

from dataclasses import replace
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contracts import AgentContractValidationError, load_contract_for_principal
from aida.agent_orchestrator import (
    AgentClarificationRequired,
    AgentPolicyRejected,
    GovernedAgentOrchestrator,
    ModelRouteUnavailable,
)
from aida.context_product_execution_scope import (
    ContextProductExecutionScope,
    load_execution_scope,
)
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled
from aida.models import DataSource
from aida.query_execution_view import query_execution_response
from aida.query_gateway import AuthorizationRejected, QueryExecutionGateway, QueryRejected
from aida.schemas import QueryExecutionResponse
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.sql_validation_api import GatewaySqlValidationResponse, validation_response
from aida.sql_workspace import (
    ORIGIN_GENERATED,
    ORIGIN_PASTED,
    DraftParameter,
    SqlWorkspaceRefused,
    run_receipt,
    validate_draft,
)
from aida.sql_workspace_models import SqlDraftReceipt
from atlas.platform.config import Settings, get_settings
from atlas.platform.context import get_correlation_id
from atlas.platform.db import get_session

router = APIRouter(prefix="/v1", tags=["sql-workspace"])

SQL_WORKSPACE_ROLES = ("PlatformAdmin", "Analyst")


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


#: The governed-tool parameter types (`ToolParameterDefinition.parameter_type`), repeated here
#: because a `Literal` cannot be derived at runtime; a test holds the two equal.
SqlParameterType = Literal["STRING", "INTEGER", "NUMBER", "BOOLEAN", "DATE"]

#: A value as JSON carries it. Strict, so `"5"` stays a string and `true` a boolean: whether a
#: value fits its declared type is the binder's decision, reported as a finding, not a coercion
#: made quietly here. The length bound is transport hygiene; the binder's own, lower one is
#: what a person meets, as PARAMETER_TOO_LONG.
SqlParameterValue = (
    Annotated[str, StringConstraints(strict=True, max_length=10_000)]
    | StrictBool
    | StrictInt
    | StrictFloat
    | None
)


class SqlDraftParameter(ApiModel):
    """One named parameter: the type it is declared as and the value bound to it.

    The statement names it as a `:name` placeholder. The value travels here, beside the text,
    and is bound as one typed literal into the parsed statement -- never spliced into the text --
    and never stored: the receipt keeps a keyed digest of it. A missing or null value is refused
    as PARAMETER_VALUE_MISSING.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    parameter_type: SqlParameterType
    value: SqlParameterValue = None


def _distinct_names(parameters: list[SqlDraftParameter]) -> None:
    names = [parameter.name for parameter in parameters]
    if len(names) != len(set(names)):
        raise ValueError("each parameter name may be declared once")


class SqlDraftRequest(ApiModel):
    """A question to draft SQL for, or a statement to validate -- exactly one."""

    question: str | None = Field(default=None, min_length=1, max_length=4_000)
    sql: str | None = Field(default=None, min_length=1, max_length=200_000)
    #: Values for the statement's `:name` placeholders. Only with `sql`: a question drafts a
    #: statement, and its parameters are declared once the person has read it.
    parameters: list[SqlDraftParameter] = Field(default_factory=list, max_length=50)
    max_rows: int | None = Field(default=None, ge=1, le=1_000_000)
    context_product_key: str | None = Field(default=None, min_length=1, max_length=100)
    workspace_id: UUID | None = None

    @model_validator(mode="after")
    def _one_input(self) -> "SqlDraftRequest":
        if (self.question is None) == (self.sql is None):
            raise ValueError("send a question or a sql statement, not both and not neither")
        if self.question is not None and self.parameters:
            raise ValueError("parameters bind a statement you send, not a question")
        _distinct_names(self.parameters)
        return self


class SqlDraftReceiptRead(ApiModel):
    id: UUID
    origin: str
    status: str
    statement_digest: str
    redacted_sql: str | None = None
    context_product_version_id: UUID | None = None
    workspace_id: UUID | None = None
    max_rows: int | None = None
    applied_row_limit: int | None = None
    referenced_tables: list[str]
    agent_run_id: UUID | None = None
    query_execution_id: UUID | None = None
    failure_reason: str | None = None
    created_at: datetime
    expires_at: datetime
    executed_at: datetime | None = None


class SqlDraftResponse(ApiModel):
    origin: str
    #: The statement to review. For a question, what the model wrote; for a statement, None --
    #: the caller already holds it. Never stored server-side (INV-6).
    sql: str | None = None
    agent_run_id: UUID | None = None
    generation_source: str | None = None
    #: Set when no SQL was drafted: `GOVERNED_TOOL_ANSWERS` means an approved governed tool
    #: answers this question, and Ask should be used to run it.
    reason: str | None = None
    selected_tool_version_id: str | None = None
    validation: GatewaySqlValidationResponse | None = None
    #: Present only for a valid statement. Run needs it, and the exact statement, back.
    receipt: SqlDraftReceiptRead | None = None


class SqlDraftRunRequest(ApiModel):
    sql: str = Field(min_length=1, max_length=200_000)
    #: The parameters validated with the statement, sent again: another value, type or name is
    #: REVALIDATION_REQUIRED, exactly as an edited statement is.
    parameters: list[SqlDraftParameter] = Field(default_factory=list, max_length=50)
    max_rows: int | None = Field(default=None, ge=1, le=1_000_000)
    context_product_key: str | None = Field(default=None, min_length=1, max_length=100)
    workspace_id: UUID | None = None

    @model_validator(mode="after")
    def _distinct(self) -> "SqlDraftRunRequest":
        _distinct_names(self.parameters)
        return self


def _draft_parameters(parameters: list[SqlDraftParameter]) -> list[DraftParameter]:
    return [
        DraftParameter(
            name=parameter.name, parameter_type=parameter.parameter_type, value=parameter.value
        )
        for parameter in parameters
    ]


class SqlDraftRunResponse(ApiModel):
    receipt: SqlDraftReceiptRead
    execution: QueryExecutionResponse


async def _product_scope(
    session: AsyncSession,
    context: SecurityContext,
    organization_id: UUID,
    product_key: str | None,
) -> ContextProductExecutionScope | None:
    """The product the statement is held to; 404 for one this caller cannot use (as F01)."""
    if product_key is None:
        return None
    scope = await load_execution_scope(
        session, organization_id=organization_id, product_key=product_key, roles=context.roles
    )
    if scope is None:
        raise HTTPException(status_code=404, detail="context product not found")
    return scope


@router.post(
    "/datasources/{datasource_id}/sql-drafts",
    response_model=SqlDraftResponse,
    summary="Draft or accept SQL and validate it without executing it",
)
async def create_sql_draft(
    datasource_id: UUID,
    body: SqlDraftRequest,
    context: SecurityContext = Depends(require_roles(*SQL_WORKSPACE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SqlDraftResponse:
    """Nothing runs here. An invalid statement is a 200 with findings and no receipt -- and a
    statement whose parameters do not bind is an invalid statement, with `PARAMETER_*` findings."""
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        ensure_datasource_enabled(datasource)
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    scoped_context = replace(context, organization_id=datasource.organization_id)
    scope = await _product_scope(
        session, context, datasource.organization_id, body.context_product_key
    )
    correlation_id = get_correlation_id()
    origin = ORIGIN_PASTED
    sql = body.sql
    agent_run_id: UUID | None = None
    generation_source: str | None = None
    if body.question is not None:
        origin = ORIGIN_GENERATED
        try:
            caller_contract = await load_contract_for_principal(
                session,
                organization_id=datasource.organization_id,
                agent_principal_id=context.principal_id,
                principal_type=context.principal_type,
            )
        except AgentContractValidationError as exc:
            raise HTTPException(status_code=403, detail=exc.code) from exc
        try:
            drafted = await GovernedAgentOrchestrator(settings).draft(
                session,
                datasource=datasource,
                context=scoped_context,
                correlation_id=correlation_id,
                question=body.question,
                requested_limit=body.max_rows,
                agent_asset_version_id=(
                    caller_contract.ai_asset_version_id if caller_contract is not None else None
                ),
                context_product_key=body.context_product_key,
            )
        except AgentClarificationRequired as exc:
            # Same structured 409 as Ask, so one client handles both.
            detail: dict[str, Any] = {
                "code": exc.code,
                "message": str(exc),
                "required_parameters": list(exc.required_parameters),
                "tool_version_id": exc.tool_version_id,
            }
            if exc.candidates:
                detail["candidates"] = list(exc.candidates)
            raise HTTPException(status_code=409, detail=detail) from exc
        except AgentPolicyRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ModelRouteUnavailable as exc:
            if exc.provider_status_code == 429:
                raise HTTPException(status_code=429, detail=str(exc)) from exc
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except QueryRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        agent_run_id = drafted.agent_run.id
        generation_source = drafted.generation_source
        if drafted.sql is None:
            await session.commit()
            return SqlDraftResponse(
                origin=origin,
                agent_run_id=agent_run_id,
                reason=drafted.reason,
                selected_tool_version_id=drafted.selected_tool_version_id,
            )
        sql = drafted.sql
    assert sql is not None  # the request validator admits exactly one of question and sql
    try:
        validated = await validate_draft(
            session,
            QueryExecutionGateway(settings),
            settings,
            datasource=datasource,
            context=scoped_context,
            correlation_id=correlation_id,
            sql=sql,
            max_rows=body.max_rows,
            workspace_id=body.workspace_id,
            scope=scope,
            origin=origin,
            agent_run_id=agent_run_id,
            parameters=_draft_parameters(body.parameters),
        )
    except AuthorizationRejected as exc:
        raise HTTPException(status_code=403, detail=exc.reason_code) from exc
    except Exception as exc:  # pragma: no cover - source dry run failed
        raise HTTPException(status_code=502, detail="source query estimate failed") from exc
    return SqlDraftResponse(
        origin=origin,
        sql=sql if origin == ORIGIN_GENERATED else None,
        agent_run_id=agent_run_id,
        generation_source=generation_source,
        validation=validation_response(validated.report),
        receipt=(
            SqlDraftReceiptRead.model_validate(validated.receipt)
            if validated.receipt is not None
            else None
        ),
    )


@router.post(
    "/sql-drafts/{receipt_id}/run",
    response_model=SqlDraftRunResponse,
    summary="Run a validated SQL draft once, with its receipt",
)
async def run_sql_draft(
    receipt_id: UUID,
    body: SqlDraftRunRequest,
    context: SecurityContext = Depends(require_roles(*SQL_WORKSPACE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> SqlDraftRunResponse:
    """Refusals are `{code, execution_id}`: 404, 403 (not yours), 409 (used, expired, edited)."""
    receipt = await session.get(SqlDraftReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail={"code": "RECEIPT_NOT_FOUND"})
    enforce_organization(context, receipt.organization_id)
    datasource = await session.get(DataSource, receipt.datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail={"code": "RECEIPT_NOT_FOUND"})
    try:
        ensure_datasource_enabled(datasource)
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Resolved again, now: a product unpublished or a role withdrawn since validation is a 404
    # here, and a product republished since then resolves to another version, whose digest
    # cannot match the receipt's.
    scope = await _product_scope(
        session, context, receipt.organization_id, body.context_product_key
    )
    try:
        result, ran = await run_receipt(
            session,
            QueryExecutionGateway(settings),
            receipt_id=receipt_id,
            context=replace(context, organization_id=receipt.organization_id),
            correlation_id=get_correlation_id(),
            sql=body.sql,
            max_rows=body.max_rows,
            workspace_id=body.workspace_id,
            scope=scope,
            parameters=_draft_parameters(body.parameters),
        )
    except SqlWorkspaceRefused as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "execution_id": str(exc.execution_id) if exc.execution_id else None,
            },
        ) from exc
    except AuthorizationRejected as exc:
        raise HTTPException(status_code=403, detail=exc.reason_code) from exc
    except QueryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="source query execution failed") from exc
    return SqlDraftRunResponse(
        receipt=SqlDraftReceiptRead.model_validate(ran),
        execution=query_execution_response(result),
    )


@router.get(
    "/datasources/{datasource_id}/sql-drafts",
    response_model=list[SqlDraftReceiptRead],
    summary="The caller's recent reviewed SQL on this datasource, newest first",
)
async def list_sql_drafts(
    datasource_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    context: SecurityContext = Depends(require_roles(*SQL_WORKSPACE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> list[SqlDraftReceiptRead]:
    """R11-SQL01's history: what this caller validated and ran here, as receipts.

    Only the caller's own -- a receipt is a record of one person's review, and another
    analyst's statements are theirs. Value-free by construction: a receipt holds the redacted
    shape and a digest, never the statement's literals, and never a row.
    """
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    receipts = (
        await session.scalars(
            select(SqlDraftReceipt)
            .where(
                SqlDraftReceipt.organization_id == datasource.organization_id,
                SqlDraftReceipt.datasource_id == datasource.id,
                SqlDraftReceipt.principal_id == context.principal_id,
                SqlDraftReceipt.principal_type == context.principal_type,
            )
            .order_by(SqlDraftReceipt.created_at.desc(), SqlDraftReceipt.id)
            .limit(limit)
        )
    ).all()
    return [SqlDraftReceiptRead.model_validate(receipt) for receipt in receipts]
