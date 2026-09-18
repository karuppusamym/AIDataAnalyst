"""R11-SQL01 (design 22, items 9/10): SQL a person reviews before it runs.

Generated or pasted, a statement goes through one lifecycle, and nothing in it executes before the
person asks it to:

1. **Draft.** Pasted SQL arrives as text. Generated SQL comes from a generation-only Ask
   (`GovernedAgentOrchestrator.draft`), which stops at GENERATED -- the model wrote a statement
   and nothing ran it.
2. **Validate.** `QueryExecutionGateway.validate` -- the pipeline `execute` runs, as far as the
   dry-run estimate -- returns findings and never rows. A valid statement gets a
   `SqlDraftReceipt`: a digest of the exact statement and its bindings (row limit, context
   product version, workspace), the caller, and an expiry.
3. **Run.** The caller sends the statement again with the receipt. It is refused unless the
   receipt is theirs, unexpired, unused and the digest still matches -- so an *edited* statement
   needs validating again -- and unless the product still resolves to the version validated
   under. Then `QueryExecutionGateway.execute` authorizes and validates it again in full: a
   receipt is a precondition, never a bypass, so a grant revoked or a definition changed after
   validation still stops the Run.

**Value-free (INV-6).** The receipt stores a digest and the redacted shape; the statement text is
the caller's. Audit records carry the receipt id, the digest and counts, never SQL.

**Once.** A receipt moves VALIDATED -> EXECUTING by conditional update, so a duplicate or retried
Run executes nothing and is told which execution the receipt produced. Result rows are not
retained anywhere, as for every other execution: running again means validating again.

The existing `POST /v1/datasources/{id}/query-executions` route is untouched: it is the API an
agent or a tool calls, and this is the reviewed path a person takes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Final, NoReturn
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context_product_execution_scope import ContextProductExecutionScope
from aida.events import record_audit
from aida.models import DataSource
from aida.query_gateway import (
    AuthorizationRejected,
    GatewayResult,
    QueryExecutionGateway,
    QueryRejected,
)
from aida.security import enforce_organization
from aida.security_types import SecurityContext
from aida.sql_redaction import redact_for_storage
from aida.sql_validation import SqlValidationReport
from aida.sql_workspace_models import SqlDraftReceipt
from atlas.platform.config import Settings

ORIGIN_GENERATED: Final = "GENERATED"
ORIGIN_PASTED: Final = "PASTED"

STATUS_VALIDATED: Final = "VALIDATED"
STATUS_EXECUTING: Final = "EXECUTING"
STATUS_EXECUTED: Final = "EXECUTED"
STATUS_FAILED: Final = "FAILED"

#: Refusal codes, each with the status a route answers it with.
RECEIPT_NOT_FOUND: Final = "RECEIPT_NOT_FOUND"
RECEIPT_NOT_YOURS: Final = "RECEIPT_NOT_YOURS"
RECEIPT_ALREADY_USED: Final = "RECEIPT_ALREADY_USED"
RECEIPT_EXPIRED: Final = "RECEIPT_EXPIRED"
REVALIDATION_REQUIRED: Final = "REVALIDATION_REQUIRED"


class SqlWorkspaceRefused(Exception):
    """A Run refused before anything executed. `code` is stable; `status_code` is the HTTP one."""

    def __init__(self, code: str, status_code: int, *, execution_id: UUID | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.execution_id = execution_id


def statement_digest(
    *,
    sql: str,
    max_rows: int | None,
    context_product_version_id: UUID | None,
    workspace_id: UUID | None,
) -> str:
    """The exact statement and every binding a Run must repeat, as one sha256.

    The text is hashed as sent, byte for byte: a changed literal, a reformatted line or a new
    limit is a different statement, and a different statement needs its own validation.
    """
    payload = {
        "sql": sql,
        "max_rows": max_rows,
        "context_product_version_id": (
            str(context_product_version_id) if context_product_version_id else None
        ),
        "workspace_id": str(workspace_id) if workspace_id else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class DraftValidation:
    """The gateway's findings, and the receipt a valid statement earned (None when invalid)."""

    report: SqlValidationReport
    receipt: SqlDraftReceipt | None


def _utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes and PostgreSQL aware ones; compare them as UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def validate_draft(
    session: AsyncSession,
    gateway: QueryExecutionGateway,
    settings: Settings,
    *,
    datasource: DataSource,
    context: SecurityContext,
    correlation_id: str,
    sql: str,
    max_rows: int | None,
    workspace_id: UUID | None,
    scope: ContextProductExecutionScope | None,
    origin: str,
    agent_run_id: UUID | None = None,
    now: datetime | None = None,
) -> DraftValidation:
    """Validate a draft through the gateway without executing it; receipt a valid one."""
    report = await gateway.validate(
        session,
        datasource=datasource,
        context=context,
        correlation_id=correlation_id,
        sql=sql,
        requested_limit=max_rows,
        workspace_id=workspace_id,
        context_product_scope=scope,
    )
    if not report.valid:
        record_audit(
            session,
            context,
            action="sql_draft.validation_refused",
            resource_type="datasource",
            resource_id=str(datasource.id),
            outcome="DENIED",
            correlation_id=correlation_id,
            details={"origin": origin, "finding_codes": list(report.codes())},
        )
        await session.commit()
        return DraftValidation(report=report, receipt=None)
    clock = now or datetime.now(UTC)
    redacted = redact_for_storage(sql, dialect=datasource.dialect)
    receipt = SqlDraftReceipt(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        principal_id=context.principal_id,
        principal_type=context.principal_type,
        origin=origin,
        status=STATUS_VALIDATED,
        statement_digest=statement_digest(
            sql=sql,
            max_rows=max_rows,
            context_product_version_id=scope.version_id if scope else None,
            workspace_id=workspace_id,
        ),
        redacted_sql=redacted.redacted if redacted else None,
        redaction_status=redacted.status if redacted else "UNPARSED",
        context_product_version_id=scope.version_id if scope else None,
        workspace_id=workspace_id,
        max_rows=max_rows,
        applied_row_limit=report.applied_row_limit,
        referenced_tables=list(report.referenced_tables),
        finding_codes=list(report.codes()),
        estimate={
            "plan_cost": report.plan_cost,
            "kind": report.estimate_kind,
            "estimated_rows": report.estimated_rows,
            "estimated_bytes": report.estimated_bytes,
        },
        agent_run_id=agent_run_id,
        expires_at=clock + timedelta(minutes=settings.sql_draft_receipt_ttl_minutes),
    )
    session.add(receipt)
    await session.flush()
    record_audit(
        session,
        context,
        action="sql_draft.validated",
        resource_type="sql_draft_receipt",
        resource_id=str(receipt.id),
        outcome="SUCCESS",
        correlation_id=correlation_id,
        details={
            "origin": origin,
            "statement_digest": receipt.statement_digest,
            "context_product_version_id": (
                str(receipt.context_product_version_id)
                if receipt.context_product_version_id
                else None
            ),
            "referenced_table_count": len(receipt.referenced_tables),
        },
    )
    await session.commit()
    return DraftValidation(report=report, receipt=receipt)


async def run_receipt(
    session: AsyncSession,
    gateway: QueryExecutionGateway,
    *,
    receipt_id: UUID,
    context: SecurityContext,
    correlation_id: str,
    sql: str,
    max_rows: int | None,
    workspace_id: UUID | None,
    scope: ContextProductExecutionScope | None,
    now: datetime | None = None,
) -> tuple[GatewayResult, SqlDraftReceipt]:
    """Run a validated draft once, through the gateway, if its receipt still stands.

    Every refusal is raised before any execution session opens. The order is the order a person
    can act on: someone else's receipt (403), then a spent one (409, naming the execution it
    produced), an expired one, and finally a statement that is not the one validated.
    """
    receipt = await session.get(SqlDraftReceipt, receipt_id)
    if receipt is None:
        raise SqlWorkspaceRefused(RECEIPT_NOT_FOUND, 404)
    enforce_organization(context, receipt.organization_id)
    if (
        receipt.principal_id != context.principal_id
        or receipt.principal_type != context.principal_type
    ):
        await _refuse(session, context, receipt, correlation_id, RECEIPT_NOT_YOURS, 403)
    if receipt.status != STATUS_VALIDATED:
        await _refuse(session, context, receipt, correlation_id, RECEIPT_ALREADY_USED, 409)
    clock = now or datetime.now(UTC)
    if _utc(receipt.expires_at) <= clock:
        await _refuse(session, context, receipt, correlation_id, RECEIPT_EXPIRED, 409)
    presented = statement_digest(
        sql=sql,
        max_rows=max_rows,
        context_product_version_id=scope.version_id if scope else None,
        workspace_id=workspace_id,
    )
    if presented != receipt.statement_digest:
        # An edited statement, another limit or workspace, or a product that now resolves to a
        # different published version: not what was validated.
        await _refuse(session, context, receipt, correlation_id, REVALIDATION_REQUIRED, 409)
    datasource = await session.get(DataSource, receipt.datasource_id)
    if datasource is None:
        raise SqlWorkspaceRefused(RECEIPT_NOT_FOUND, 404)
    claimed = await session.execute(
        update(SqlDraftReceipt)
        .where(
            SqlDraftReceipt.id == receipt.id,
            SqlDraftReceipt.organization_id == receipt.organization_id,
            SqlDraftReceipt.status == STATUS_VALIDATED,
        )
        .values(status=STATUS_EXECUTING)
        .execution_options(synchronize_session=False)
    )
    if getattr(claimed, "rowcount", 0) != 1:
        # Another Run claimed it between the read above and this update.
        await session.refresh(receipt)
        await _refuse(session, context, receipt, correlation_id, RECEIPT_ALREADY_USED, 409)
    await session.refresh(receipt)
    try:
        result = await gateway.execute(
            session,
            datasource=datasource,
            context=replace(context, organization_id=datasource.organization_id),
            correlation_id=correlation_id,
            sql=sql,
            requested_limit=max_rows,
            semantic_version=None,
            workspace_id=workspace_id,
            context_product_scope=scope,
        )
    except QueryRejected as exc:
        receipt.status = STATUS_FAILED
        receipt.failure_reason = (
            exc.reason_code if isinstance(exc, AuthorizationRejected) else "QUERY_REJECTED"
        )[:200]
        receipt.query_execution_id = exc.execution_id
        _record_run(session, context, receipt, correlation_id, outcome="DENIED")
        await session.commit()
        raise
    except Exception:
        # The gateway has already committed the execution's own failure; the session may be
        # mid-rollback, so the receipt is closed in a clean transaction.
        await session.rollback()
        receipt = await session.get(SqlDraftReceipt, receipt_id) or receipt
        receipt.status = STATUS_FAILED
        receipt.failure_reason = "SOURCE_EXECUTION_FAILED"
        _record_run(session, context, receipt, correlation_id, outcome="FAILURE")
        await session.commit()
        raise
    receipt.status = STATUS_EXECUTED
    receipt.executed_at = clock
    receipt.query_execution_id = result.execution.id
    _record_run(session, context, receipt, correlation_id, outcome="SUCCESS")
    await session.commit()
    return result, receipt


async def _refuse(
    session: AsyncSession,
    context: SecurityContext,
    receipt: SqlDraftReceipt,
    correlation_id: str,
    code: str,
    status_code: int,
) -> NoReturn:
    """Write the refusal down, then raise it. Nothing was claimed and nothing executed."""
    record_audit(
        session,
        context,
        action="sql_draft.run",
        resource_type="sql_draft_receipt",
        resource_id=str(receipt.id),
        outcome="DENIED",
        correlation_id=correlation_id,
        details={"reason": code, "executed": False},
    )
    await session.commit()
    raise SqlWorkspaceRefused(
        code,
        status_code,
        execution_id=receipt.query_execution_id if code == RECEIPT_ALREADY_USED else None,
    )


def _record_run(
    session: AsyncSession,
    context: SecurityContext,
    receipt: SqlDraftReceipt,
    correlation_id: str,
    *,
    outcome: str,
) -> None:
    record_audit(
        session,
        context,
        action="sql_draft.run",
        resource_type="sql_draft_receipt",
        resource_id=str(receipt.id),
        outcome=outcome,
        correlation_id=correlation_id,
        details={
            "status": receipt.status,
            "statement_digest": receipt.statement_digest,
            "query_execution_id": (
                str(receipt.query_execution_id) if receipt.query_execution_id else None
            ),
            "failure_reason": receipt.failure_reason,
        },
    )
