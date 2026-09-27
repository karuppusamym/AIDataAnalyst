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
the caller's. Audit records carry the receipt id, the digest and counts, never SQL. The digest is
*keyed* (`statement_digest`): it sits beside the redacted shape, so an unkeyed hash of a pasted
statement would let a reader of the row confirm a guessed literal. Receipts issued before the
digest was keyed carry an unkeyed one that no Run can match, so one still unexpired when the change
deployed answers REVALIDATION_REQUIRED and is validated again; they live minutes, not days.

**Once.** A receipt moves VALIDATED -> EXECUTING by conditional update, so a duplicate or retried
Run executes nothing and is told which execution the receipt produced. Result rows are not
retained anywhere, as for every other execution: running again means validating again.

**Parameters.** A statement may name `:placeholders` and send typed values beside the text
(`DraftParameter`). They are bound by the governed-tool renderer
(`aida.tool_rendering.render_tool_sql`) -- the binding every approved tool already runs on,
reused rather than written twice: the template is parsed first and each placeholder node is
replaced by one typed literal node, so a value can never change the statement's structure and
an injection attempt inside one is just a string. The gateway receives only the bound statement,
validates it as it validates any other, and stores it redacted. The values themselves are never
stored: the receipt's digest covers the declared types and a *keyed* digest of the normalized
values (`aida.signing.sign_value`, the same fingerprint a tool execution records), because an
unkeyed hash of a short, guessable value is a dictionary lookup away from the value. Changing a
value therefore changes the digest, and Run answers REVALIDATION_REQUIRED exactly as for an
edited statement. A value that does not bind is reported as a finding with a stable
`PARAMETER_*` code naming the parameter, never the value.

The existing `POST /v1/datasources/{id}/query-executions` route is untouched: it is the API an
agent or a tool calls, and this is the reviewed path a person takes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final, NoReturn
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot.errors import ParseError, TokenError

from aida.context_product_execution_scope import ContextProductExecutionScope
from aida.events import record_audit
from aida.models import DataSource
from aida.query_gateway import (
    AuthorizationRejected,
    GatewayResult,
    QueryExecutionGateway,
    QueryRejected,
)
from aida.schemas import ToolParameterDefinition
from aida.security import enforce_organization
from aida.security_types import SecurityContext
from aida.signing import sign_value
from aida.sql_redaction import redact_for_storage
from aida.sql_validation import (
    FINDING_SQL_PARSE_ERROR,
    SEVERITY_ERROR,
    SqlFinding,
    SqlValidationReport,
)
from aida.sql_workspace_models import SqlDraftReceipt
from aida.tool_rendering import (
    ToolParameterCode,
    ToolParameterError,
    render_tool_sql,
    template_placeholders,
)
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

#: Why a statement's parameters did not bind. Findings, not HTTP errors: a statement whose values
#: do not bind is an invalid statement, answered like any other -- findings and no receipt. Each
#: names the parameter in `ref` and, where known, its declared type in `detail`; never a value.
PARAMETER_UNDECLARED: Final = "PARAMETER_UNDECLARED"
PARAMETER_UNUSED: Final = "PARAMETER_UNUSED"
PARAMETER_VALUE_MISSING: Final = "PARAMETER_VALUE_MISSING"
PARAMETER_TYPE_MISMATCH: Final = "PARAMETER_TYPE_MISMATCH"
PARAMETER_TOO_LONG: Final = "PARAMETER_TOO_LONG"
PARAMETER_INVALID: Final = "PARAMETER_INVALID"

#: The longest text value a draft binds. A filter value, not a document.
PARAMETER_VALUE_MAX_LENGTH: Final = 4_000

#: The renderer refuses with a `ToolParameterError` whose issues carry a `ToolParameterCode` and
#: the parameter names concerned. These are the codes the draft path names to a person; any other
#: -- a code the workspace has no word for, or a refusal raised with a message and no issue -- is
#: still refused, as PARAMETER_INVALID with no name, and nothing of the message is echoed. The
#: renderer never quotes a value, and this never reads its wording.
#:
#: Not named, and so PARAMETER_INVALID: UNKNOWN_PARAMETER and REQUIRED_MISSING (the draft always
#: sends exactly its declared names, each with a value, null included), UNSUPPORTED_TYPE (the
#: request contract admits only the five types), and the allow-list and range codes (a draft
#: declares none). A test holds this table to every code the renderer has, so a new one is a
#: decision, not an accident.
_PARAMETER_CODES: Final[dict[ToolParameterCode, str]] = {
    ToolParameterCode.UNDECLARED_PLACEHOLDER: PARAMETER_UNDECLARED,
    ToolParameterCode.UNUSED_DEFINITION: PARAMETER_UNUSED,
    ToolParameterCode.REQUIRED_NULL: PARAMETER_VALUE_MISSING,
    ToolParameterCode.NOT_A_STRING: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.NOT_AN_INTEGER: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.NOT_NUMERIC: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.NOT_FINITE: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.NOT_A_BOOLEAN: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.NOT_AN_ISO_DATE: PARAMETER_TYPE_MISMATCH,
    ToolParameterCode.TOO_LONG: PARAMETER_TOO_LONG,
}

_PARAMETER_HINTS: Final[dict[str, str]] = {
    PARAMETER_UNDECLARED: (
        "the statement uses this :name placeholder but no parameter declares it; declare it "
        "with a type and a value"
    ),
    PARAMETER_UNUSED: (
        "this parameter is declared but the statement has no :name placeholder for it; use it "
        "or remove it"
    ),
    PARAMETER_VALUE_MISSING: "this parameter has no value; every declared parameter needs one",
    PARAMETER_TYPE_MISMATCH: (
        "the value does not match the declared type: STRING takes text, INTEGER a whole "
        "number, NUMBER a finite number, BOOLEAN true or false, DATE an ISO date (YYYY-MM-DD)"
    ),
    PARAMETER_TOO_LONG: (f"a text value is limited to {PARAMETER_VALUE_MAX_LENGTH:,} characters"),
    PARAMETER_INVALID: "the parameters could not be bound to this statement",
}

#: A parameter name as the tool contract spells it (`ToolParameterDefinition.name`). Only a name
#: of this shape is echoed as a finding's `ref`.
_PARAMETER_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: What a statement digest is taken over, named inside the signed text (see `statement_digest`).
_DIGEST_PURPOSE: Final = "sql_draft.statement.v2"

#: What sqlglot raises for text it cannot read -- the set `aida.sql_redaction` catches.
_UNPARSEABLE: Final = (ParseError, TokenError, ValueError, RecursionError)


class SqlWorkspaceRefused(Exception):
    """A Run refused before anything executed. `code` is stable; `status_code` is the HTTP one."""

    def __init__(self, code: str, status_code: int, *, execution_id: UUID | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.execution_id = execution_id


async def statement_digest(
    settings: Settings,
    *,
    sql: str,
    max_rows: int | None,
    context_product_version_id: UUID | None,
    workspace_id: UUID | None,
    parameter_types: Mapping[str, str] | None = None,
    parameter_fingerprint: str | None = None,
) -> str:
    """The exact statement and every binding a Run must repeat, as one *keyed* digest.

    The text is digested as sent, byte for byte: a changed literal, a reformatted line or a new
    limit is a different statement, and a different statement needs its own validation. For a
    parameterized statement the text is the template; the declared types and the keyed digest of
    the values are bound beside it, so a changed type or value is a different statement too.

    Keyed (`aida.signing.sign_value`, the deployment's signer) because the receipt stores this
    digest beside the statement's redacted shape, and a pasted statement's text includes its
    literals: with an unkeyed hash, anyone who could read the row knew everything about the
    statement but those literals and could confirm a short, guessable one by trying candidates.
    Under a key they cannot. The result is the width the signer's output has -- 64 hex characters
    for the local provider, `vault:v<n>:` and 44 base64 characters for Vault Transit -- which is
    what `query_execution.sql_hash` already keeps in the same `String(64)`.

    A digest is only ever recomputed and compared here, never trusted from the caller, so the
    key is never needed to *verify* anything the caller sends.
    """
    payload: dict[str, Any] = {
        # Names what is signed, so the signature cannot equal one the same key made for another
        # kind of value: `sign_value` also keys a question, a comment and tool parameters.
        "purpose": _DIGEST_PURPOSE,
        "sql": sql,
        "max_rows": max_rows,
        "context_product_version_id": (
            str(context_product_version_id) if context_product_version_id else None
        ),
        "workspace_id": str(workspace_id) if workspace_id else None,
    }
    if parameter_types:
        payload["parameter_types"] = dict(sorted(parameter_types.items()))
        payload["parameter_fingerprint"] = parameter_fingerprint
    return await sign_value(settings, json.dumps(payload, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True, slots=True)
class DraftParameter:
    """One named parameter of a draft: its declared type and the value bound to it.

    `parameter_type` is the governed-tool vocabulary (`ToolParameterDefinition.parameter_type`):
    STRING, INTEGER, NUMBER, BOOLEAN or DATE. The value is the caller's and is never stored.
    """

    name: str
    parameter_type: str
    value: Any


@dataclass(frozen=True, slots=True)
class DraftBinding:
    """A statement with its parameters bound, or the findings that stopped the binding.

    `executable_sql` is what the gateway receives: the caller's text unchanged when there is
    nothing to bind, otherwise the template with every placeholder replaced by a typed literal.
    It holds the values, so it is handed to the gateway and never stored here.
    """

    executable_sql: str | None
    parameter_types: dict[str, str] = field(default_factory=dict)
    normalized_values: dict[str, Any] = field(default_factory=dict)
    findings: tuple[SqlFinding, ...] = ()


def _parameter_findings(
    exc: ToolParameterError, declared_types: Mapping[str, str]
) -> tuple[SqlFinding, ...]:
    """The renderer's refusal as findings: one per parameter it names, by the codes it carries."""
    findings: list[SqlFinding] = []
    for issue in exc.issues:
        code = _PARAMETER_CODES.get(issue.code, PARAMETER_INVALID)
        refs: list[str | None] = (
            list(issue.names) if code != PARAMETER_INVALID and issue.names else [None]
        )
        for ref in refs:
            name = ref if ref is not None and _PARAMETER_NAME.fullmatch(ref) else None
            findings.append(
                SqlFinding(
                    code=code,
                    severity=SEVERITY_ERROR,
                    ref=name,
                    hint=_PARAMETER_HINTS[code],
                    detail=(
                        {"parameter_type": declared_types[name]}
                        if name is not None and name in declared_types
                        else {}
                    ),
                )
            )
    return tuple(findings)


def _binding_refused(code: str, *, ref: str | None = None, hint: str | None = None) -> DraftBinding:
    return DraftBinding(
        executable_sql=None,
        findings=(
            SqlFinding(
                code=code,
                severity=SEVERITY_ERROR,
                ref=ref,
                hint=hint or _PARAMETER_HINTS[code],
            ),
        ),
    )


def bind_parameters(
    sql: str, *, dialect: str, parameters: Sequence[DraftParameter]
) -> DraftBinding:
    """Bind typed values into a statement's `:name` placeholders, with the governed-tool renderer.

    Nothing is bound, and the text passes through untouched, when no parameter is declared and
    the statement names no placeholder -- the raw-SQL path, byte for byte as before. A statement
    that names a placeholder without declaring it is refused here rather than handed to the
    gateway: an unbound placeholder would pass the guard as syntax and fail only at the source.
    A statement that does not parse at all is also left to the gateway, whose finding for it
    already exists -- unless it declares parameters, which then cannot be bound.
    """
    if not parameters:
        try:
            placeholders = template_placeholders(sql, dialect=dialect)
        except _UNPARSEABLE:
            placeholders = set()
        if not placeholders:
            return DraftBinding(executable_sql=sql)
    declared_types = {parameter.name: parameter.parameter_type for parameter in parameters}
    if len(declared_types) != len(parameters):
        # The request contract refuses a repeated name; this keeps a direct caller from binding
        # whichever of two values happened to come last.
        return _binding_refused(PARAMETER_INVALID)
    try:
        definitions = [
            ToolParameterDefinition(
                name=parameter.name,
                parameter_type=parameter.parameter_type,
                required=True,
                max_length=(
                    PARAMETER_VALUE_MAX_LENGTH if parameter.parameter_type == "STRING" else None
                ),
            )
            for parameter in parameters
        ]
    except ValidationError:
        return _binding_refused(PARAMETER_INVALID)
    try:
        rendered = render_tool_sql(
            sql,
            dialect=dialect,
            definitions=definitions,
            values={parameter.name: parameter.value for parameter in parameters},
        )
    except ToolParameterError as exc:
        # Caught before `_UNPARSEABLE`, which names its base class, `ValueError`.
        return DraftBinding(executable_sql=None, findings=_parameter_findings(exc, declared_types))
    except _UNPARSEABLE:
        # The parser's own message is withheld, as the gateway withholds it: it quotes the
        # fragment it choked on, values included.
        return _binding_refused(
            FINDING_SQL_PARSE_ERROR,
            hint="the statement does not parse, so its parameters cannot be bound",
        )
    return DraftBinding(
        executable_sql=rendered.sql,
        parameter_types=declared_types,
        normalized_values=dict(rendered.normalized_parameters),
    )


async def parameter_fingerprint(settings: Settings, binding: DraftBinding) -> str | None:
    """The keyed digest of the bound values -- a tool execution's `parameter_fingerprint`.

    The same canonical form (`json.dumps(..., sort_keys=True, separators=(",", ":"))` of the
    renderer's normalized values) under the same signer, so the two are comparable. None when
    nothing was bound.
    """
    if not binding.parameter_types:
        return None
    return await sign_value(
        settings,
        json.dumps(binding.normalized_values, sort_keys=True, separators=(",", ":")),
    )


def _binding_report(binding: DraftBinding, *, dialect: str) -> SqlValidationReport:
    """A statement refused before the gateway saw it: its findings, and nothing else."""
    return SqlValidationReport(
        valid=False,
        findings=binding.findings,
        dialect=dialect,
        normalized_sql=None,
        referenced_tables=(),
        referenced_columns=(),
        applied_row_limit=None,
        column_lineage=(),
    )


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
    parameters: Sequence[DraftParameter] = (),
    now: datetime | None = None,
) -> DraftValidation:
    """Validate a draft through the gateway without executing it; receipt a valid one.

    With parameters, the gateway validates the bound statement -- the one Run will execute --
    and the receipt records the template's redacted shape, whose placeholders name the
    parameters and hold no value.
    """
    binding = bind_parameters(sql, dialect=datasource.dialect, parameters=parameters)
    if binding.executable_sql is None:
        report = _binding_report(binding, dialect=datasource.dialect)
    else:
        report = await gateway.validate(
            session,
            datasource=datasource,
            context=context,
            correlation_id=correlation_id,
            sql=binding.executable_sql,
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
    fingerprint = await parameter_fingerprint(settings, binding)
    # The caller's text, not the bound statement: for a template that is the shape with its
    # placeholders, and redaction removes any literal written into it directly. Comments go too:
    # they are the caller's, and one can hold a name, a literal or a secret.
    redacted = redact_for_storage(sql, dialect=datasource.dialect, strip_comments=True)
    receipt = SqlDraftReceipt(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        principal_id=context.principal_id,
        principal_type=context.principal_type,
        origin=origin,
        status=STATUS_VALIDATED,
        statement_digest=await statement_digest(
            settings,
            sql=sql,
            max_rows=max_rows,
            context_product_version_id=scope.version_id if scope else None,
            workspace_id=workspace_id,
            parameter_types=binding.parameter_types,
            parameter_fingerprint=fingerprint,
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
            "parameter_count": len(binding.parameter_types),
            "parameter_fingerprint": fingerprint,
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
    parameters: Sequence[DraftParameter] = (),
    now: datetime | None = None,
) -> tuple[GatewayResult, SqlDraftReceipt]:
    """Run a validated draft once, through the gateway, if its receipt still stands.

    Every refusal is raised before any execution session opens. The order is the order a person
    can act on: someone else's receipt (403), then a spent one (409, naming the execution it
    produced), an expired one, and finally a statement that is not the one validated -- which
    includes the same text with another parameter value, type or name.
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
    datasource = await session.get(DataSource, receipt.datasource_id)
    if datasource is None:
        raise SqlWorkspaceRefused(RECEIPT_NOT_FOUND, 404)
    binding = bind_parameters(sql, dialect=datasource.dialect, parameters=parameters)
    if binding.executable_sql is None:
        # Values that do not bind cannot be the values that validated.
        await _refuse(session, context, receipt, correlation_id, REVALIDATION_REQUIRED, 409)
    fingerprint = await parameter_fingerprint(gateway.settings, binding)
    presented = await statement_digest(
        gateway.settings,
        sql=sql,
        max_rows=max_rows,
        context_product_version_id=scope.version_id if scope else None,
        workspace_id=workspace_id,
        parameter_types=binding.parameter_types,
        parameter_fingerprint=fingerprint,
    )
    if presented != receipt.statement_digest:
        # An edited statement, another parameter value, limit or workspace, or a product that
        # now resolves to a different published version: not what was validated.
        await _refuse(session, context, receipt, correlation_id, REVALIDATION_REQUIRED, 409)
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
            sql=binding.executable_sql,
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
        _record_run(
            session, context, receipt, correlation_id, outcome="DENIED", parameters=fingerprint
        )
        await session.commit()
        raise
    except Exception:
        # The gateway has already committed the execution's own failure; the session may be
        # mid-rollback, so the receipt is closed in a clean transaction.
        await session.rollback()
        receipt = await session.get(SqlDraftReceipt, receipt_id) or receipt
        receipt.status = STATUS_FAILED
        receipt.failure_reason = "SOURCE_EXECUTION_FAILED"
        _record_run(
            session, context, receipt, correlation_id, outcome="FAILURE", parameters=fingerprint
        )
        await session.commit()
        raise
    receipt.status = STATUS_EXECUTED
    receipt.executed_at = clock
    receipt.query_execution_id = result.execution.id
    _record_run(
        session, context, receipt, correlation_id, outcome="SUCCESS", parameters=fingerprint
    )
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
    parameters: str | None,
) -> None:
    """`parameters` is the keyed digest of the bound values, never the values."""
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
            "parameter_fingerprint": parameters,
            "query_execution_id": (
                str(receipt.query_execution_id) if receipt.query_execution_id else None
            ),
            "failure_reason": receipt.failure_reason,
        },
    )
