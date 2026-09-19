"""R11-GQL02 (design 13B): execute an approved tool version once per caller-scoped key.

GraphQL gains exactly one way to run something against a source: an approved, published tool
version, with typed parameters, an explicit row limit and an optional context product. There is
no arbitrary SQL here, no Cypher and no native procedure call, and nothing a `query` can reach
executes -- a status read returns a receipt, never rows.

**One execution path.** The execution itself is `tool_api.execute_tool_version`, the function
the REST route and persisted tool plans already call: role binding, the agent contract and kill
switch, datasource admission, quality and source-definition holds, parameter binding, the
gateway's masking, cost gate and audit. This module adds only what a retried mutation needs and
what product scope asks of a tool.

**Idempotency, caller-scoped.** The caller names each request with a key. The first request with
that key claims a `GovernedExecutionRequest` row (a unique constraint on organization, caller and
key decides races) before anything executes. A later request with the same key and the same
inputs is answered from that row -- `replayed` -- and executes nothing: not while the first is
still running, and not when the first ended without the platform learning its outcome, which
stays `PENDING` for a person to reconcile (design section 13B: never retry an ambiguous outcome
as a new execution). The same key with different inputs is refused, because answering it with
another request's receipt would be a lie.

**Product scope.** Asked through a context product, the tool version must be one the product
declares eligible, its declared dependencies must lie inside the product's tables (the same
contract Ask applies, `GOVERNED_TOOL_DEPENDENCY_CONTRACT`), and the rendered statement is held
to the product at the gateway as well.

**Value-free (INV-6).** The record holds an HMAC of the request, not its parameter values, and
refusals carry stable codes. Result rows go back to the caller once, in the executing response,
and are not retained.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context_product_execution_scope import (
    CONTEXT_PRODUCT_TOOL_DEPENDENCY_OUT_OF_SCOPE,
    ContextProductExecutionScope,
    load_execution_scope,
    resolve_scope_names,
)
from aida.events import record_audit
from aida.governed_execution_models import GovernedExecutionRequest
from aida.models import ContextProductVersion, DataSource, GovernedToolVersion
from aida.schemas import ToolExecutionRequest, ToolExecutionResponse
from aida.security_types import SecurityContext
from aida.signing import sign_value
from aida.tool_api import execute_tool_version
from atlas.platform.config import Settings

#: The roles `execute_tool_version` itself admits. Checked here first so a caller who may not
#: execute never claims an idempotency key.
TOOL_EXECUTION_ROLES: Final = ("PlatformAdmin", "Analyst", "AgentDeveloper", "ToolConsumer")
#: Roles that may read another caller's execution receipt in the same organization, as the
#: REST lineage read of a query execution allows.
RECEIPT_OVERSIGHT_ROLES: Final = frozenset({"PlatformAdmin", "Auditor"})
#: Who may read receipts at all: anyone who can execute (their own) and oversight (any).
RECEIPT_READ_ROLES: Final = tuple(sorted({*TOOL_EXECUTION_ROLES, *RECEIPT_OVERSIGHT_ROLES}))

SURFACE_GRAPHQL: Final = "GRAPHQL"

STATUS_PENDING: Final = "PENDING"
STATUS_COMPLETED: Final = "COMPLETED"
STATUS_REJECTED: Final = "REJECTED"
STATUS_FAILED: Final = "FAILED"

#: Printable, bounded, and free of anything a log or a URL would need escaping for.
_KEY_PATTERN: Final = re.compile(r"[A-Za-z0-9._:-]{8,128}")

IDEMPOTENCY_KEY_INVALID: Final = "IDEMPOTENCY_KEY_INVALID"
IDEMPOTENCY_KEY_REUSED: Final = "IDEMPOTENCY_KEY_REUSED"
TOOL_VERSION_NOT_FOUND: Final = "TOOL_VERSION_NOT_FOUND"
CONTEXT_PRODUCT_NOT_FOUND: Final = "CONTEXT_PRODUCT_NOT_FOUND"
CONTEXT_PRODUCT_TOOL_NOT_ELIGIBLE: Final = "CONTEXT_PRODUCT_TOOL_NOT_ELIGIBLE"
EXECUTION_RECEIPT_NOT_FOUND: Final = "EXECUTION_RECEIPT_NOT_FOUND"


class ExecutionRefused(Exception):
    """Refused with a stable `code` (the GraphQL error code) and a value-free `reason`."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """The record, and -- only for the request that executed -- the gateway's response."""

    record: GovernedExecutionRequest
    replayed: bool
    response: ToolExecutionResponse | None


async def request_fingerprint(
    settings: Settings,
    *,
    tool_version_id: UUID,
    parameters: dict[str, Any],
    max_rows: int,
    context_product_key: str | None,
) -> str:
    """HMAC of every input that decides what runs. Parameters are caller values, so the
    record keeps this and never them (INV-6)."""
    canonical = json.dumps(
        {
            "tool_version_id": str(tool_version_id),
            "parameters": parameters,
            "max_rows": max_rows,
            "context_product_key": context_product_key,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return await sign_value(settings, canonical)


async def _existing(
    session: AsyncSession, context: SecurityContext, organization_id: UUID, key: str
) -> GovernedExecutionRequest | None:
    return (
        await session.execute(
            select(GovernedExecutionRequest).where(
                GovernedExecutionRequest.organization_id == organization_id,
                GovernedExecutionRequest.principal_type == context.principal_type,
                GovernedExecutionRequest.principal_id == context.principal_id,
                GovernedExecutionRequest.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()


def _replay(record: GovernedExecutionRequest, fingerprint: str) -> ExecutionOutcome:
    if record.request_fingerprint != fingerprint:
        raise ExecutionRefused("CONFLICT", IDEMPOTENCY_KEY_REUSED)
    return ExecutionOutcome(record=record, replayed=True, response=None)


async def execute_governed_tool(
    session: AsyncSession,
    settings: Settings,
    *,
    context: SecurityContext,
    organization_id: UUID,
    tool_version_id: UUID,
    parameters: dict[str, Any],
    max_rows: int,
    context_product_key: str | None,
    idempotency_key: str,
    correlation_id: str,
    surface: str = SURFACE_GRAPHQL,
) -> ExecutionOutcome:
    """Execute once for this caller and key, or answer from the record of the time it did."""
    if not _KEY_PATTERN.fullmatch(idempotency_key):
        raise ExecutionRefused("INVALID_ARGUMENT", IDEMPOTENCY_KEY_INVALID)
    if context.roles.isdisjoint(TOOL_EXECUTION_ROLES):
        raise ExecutionRefused("FORBIDDEN", "ROLE_REQUIRED")
    fingerprint = await request_fingerprint(
        settings,
        tool_version_id=tool_version_id,
        parameters=parameters,
        max_rows=max_rows,
        context_product_key=context_product_key,
    )
    existing = await _existing(session, context, organization_id, idempotency_key)
    if existing is not None:
        return _replay(existing, fingerprint)

    version = await session.get(GovernedToolVersion, tool_version_id)
    if version is None or version.organization_id != organization_id:
        # Another organization's version is "not found", not "forbidden" (INV-5).
        raise ExecutionRefused("NOT_FOUND", TOOL_VERSION_NOT_FOUND)

    record = GovernedExecutionRequest(
        organization_id=organization_id,
        principal_id=context.principal_id,
        principal_type=context.principal_type,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        surface=surface,
        tool_version_id=version.id,
        status=STATUS_PENDING,
    )
    session.add(record)
    try:
        # Committed before anything executes: from here on a retry finds this row.
        await session.commit()
    except IntegrityError:
        # A concurrent request with the same key claimed it first.
        await session.rollback()
        claimed = await _existing(session, context, organization_id, idempotency_key)
        if claimed is None:  # pragma: no cover - the constraint fired, so the row exists
            raise
        return _replay(claimed, fingerprint)

    scoped_context = replace(context, organization_id=organization_id)
    scope: ContextProductExecutionScope | None = None
    if context_product_key is not None:
        scope = await _product_scope(
            session,
            scoped_context,
            record,
            version,
            product_key=context_product_key,
            correlation_id=correlation_id,
        )

    try:
        response = await execute_tool_version(
            version.id,
            ToolExecutionRequest(parameters=parameters, max_rows=max_rows),
            scoped_context,
            session,
            settings,
            context_product_scope=scope,
        )
    except HTTPException as refused:
        code, reason, status = _classify(refused)
        await _finish(session, record, status=status, outcome_code=reason)
        raise ExecutionRefused(code, reason) from refused
    # Anything else leaves the record PENDING: the platform does not know what happened at the
    # source, and a retry must not guess.
    record.status = STATUS_COMPLETED
    record.tool_execution_id = response.tool_execution_id
    record.query_execution_id = response.execution.execution_id
    record.row_count = response.execution.row_count
    record.completed_at = datetime.now(UTC)
    await session.commit()
    return ExecutionOutcome(record=record, replayed=False, response=response)


async def _product_scope(
    session: AsyncSession,
    context: SecurityContext,
    record: GovernedExecutionRequest,
    version: GovernedToolVersion,
    *,
    product_key: str,
    correlation_id: str,
) -> ContextProductExecutionScope:
    """The product this execution is held to, or a recorded, audited refusal."""
    scope = await load_execution_scope(
        session,
        organization_id=record.organization_id,
        product_key=product_key,
        roles=context.roles,
    )
    if scope is None:
        # Unknown, unpublished and role-forbidden products answer alike, as on REST (F01).
        await _refuse_before_execution(
            session, context, record, correlation_id, "NOT_FOUND", CONTEXT_PRODUCT_NOT_FOUND
        )
    assert scope is not None
    record.context_product_version_id = scope.version_id
    product_version = await session.get(ContextProductVersion, scope.version_id)
    eligible = (
        {str(value) for value in (product_version.eligible_tool_version_ids or ())}
        if (product_version is not None)
        else set()
    )
    if str(version.id) not in eligible:
        await _refuse_before_execution(
            session,
            context,
            record,
            correlation_id,
            "FORBIDDEN",
            CONTEXT_PRODUCT_TOOL_NOT_ELIGIBLE,
        )
    datasource = await session.get(DataSource, version.datasource_id)
    if datasource is not None:
        dependencies = await resolve_scope_names(
            session, datasource, version.referenced_tables, table_ids=scope.table_ids
        )
        if not dependencies.admitted:
            await _refuse_before_execution(
                session,
                context,
                record,
                correlation_id,
                "FORBIDDEN",
                CONTEXT_PRODUCT_TOOL_DEPENDENCY_OUT_OF_SCOPE,
            )
    return scope


async def _refuse_before_execution(
    session: AsyncSession,
    context: SecurityContext,
    record: GovernedExecutionRequest,
    correlation_id: str,
    code: str,
    reason: str,
) -> None:
    """Close the record, audit the refusal as the REST path audits its own, and raise."""
    record_audit(
        session,
        context,
        action="tool.execute",
        resource_type="governed_tool_version",
        resource_id=str(record.tool_version_id),
        outcome="DENIED",
        correlation_id=correlation_id,
        details={
            "reason": reason,
            "surface": record.surface,
            "governed_execution_request_id": str(record.id),
            "context_product_version_id": (
                str(record.context_product_version_id)
                if record.context_product_version_id
                else None
            ),
        },
    )
    await _finish(session, record, status=STATUS_REJECTED, outcome_code=reason)
    raise ExecutionRefused(code, reason)


async def _finish(
    session: AsyncSession, record: GovernedExecutionRequest, *, status: str, outcome_code: str
) -> None:
    record.status = status
    record.outcome_code = outcome_code
    record.completed_at = datetime.now(UTC)
    await session.commit()


def _classify(refused: HTTPException) -> tuple[str, str, str]:
    """(GraphQL code, value-free reason, record status) for a refusal of the shared path.

    The REST path's `detail` is prose for some refusals and may name a parameter or a
    table, so it is never forwarded: only a detail that is itself a stable code (an
    authorization reason code such as the contract's) passes through as the reason.
    """
    detail = refused.detail if isinstance(refused.detail, str) else ""
    stable = detail if re.fullmatch(r"[A-Za-z][A-Za-z0-9_:]{2,99}", detail) else None
    if refused.status_code == 403:
        return "FORBIDDEN", stable or "EXECUTION_FORBIDDEN", STATUS_REJECTED
    if refused.status_code == 404:
        return "NOT_FOUND", stable or TOOL_VERSION_NOT_FOUND, STATUS_REJECTED
    if refused.status_code == 409:
        reason = (
            "QUALITY_HOLD"
            if "quality incident" in detail.lower()
            else (stable or "NOT_EXECUTABLE")
        )
        return "CONFLICT", reason, STATUS_REJECTED
    if refused.status_code == 422:
        return "REJECTED", "EXECUTION_REJECTED", STATUS_REJECTED
    return "EXECUTION_FAILED", "SOURCE_EXECUTION_FAILED", STATUS_FAILED


async def load_receipt(
    session: AsyncSession,
    context: SecurityContext,
    *,
    organization_id: UUID,
    record_id: UUID,
) -> GovernedExecutionRequest:
    """One execution receipt: the caller's own, or any in the organization for oversight roles.

    Anything else -- another organization's, or another caller's without oversight -- is
    "not found", so a receipt id discloses nothing about whose it is.
    """
    if context.roles.isdisjoint(RECEIPT_READ_ROLES):
        raise ExecutionRefused("FORBIDDEN", "ROLE_REQUIRED")
    record = await session.get(GovernedExecutionRequest, record_id)
    if record is None or record.organization_id != organization_id:
        raise ExecutionRefused("NOT_FOUND", EXECUTION_RECEIPT_NOT_FOUND)
    own = (
        record.principal_id == context.principal_id
        and record.principal_type == context.principal_type
    )
    if not own and context.roles.isdisjoint(RECEIPT_OVERSIGHT_ROLES):
        raise ExecutionRefused("NOT_FOUND", EXECUTION_RECEIPT_NOT_FOUND)
    return record
