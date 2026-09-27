"""`POST /graphql` -- the metadata GraphQL endpoint (R11-GQL01, design section 13A),
with one explicit governed-execution mutation (R11-GQL02, design section 13B).

Authenticated and role-gated exactly like the REST catalog reads: the route
admits the union of the roles those reads declare (`GRAPHQL_ENDPOINT_ROLES`),
and every field then requires its own REST route's role set, tenant boundary
and workspace gate (`aida.graphql_reads`). The caller must carry an
organization: this facade is tenant-scoped, and a caller with no tenant is
refused before its document is read.

What happens to a request, in order:

0. The caller's per-minute request budget is counted (`aida.request_budget`, off by
   default like MCP's); a spent budget answers 429 `RATE_LIMITED` before the body is
   read. An admitted mutation is also counted against the per-day execution budget.
1. The body is read with a byte ceiling, before JSON parsing. A JSON array is
   HTTP batching, which is refused, as are unknown keys.
2. `aida.graphql_limits.admit_document` bounds the document -- operation name,
   single operation, token count, fragment cycles, depth, aliases, page sizes,
   argument lengths, an upper bound on returned objects, introspection -- and
   validates it. The budget is the deployment's `graphql_*` settings, with the
   introspection decision for this caller (`limits_from_settings`).
   A refusal here answers 400 (413 for the byte ceiling) with a stable code, and
   no statement has reached the database.
3. The admitted document executes against `metadata_schema` under a deadline,
   with a request-scoped `ReadScope` as its context. A query is executed with only
   the query operation type allowed; an admitted mutation -- exactly one execution
   field -- with only the mutation type allowed, under a deadline sized to the
   gateway's own statement timeout rather than the read deadline. A mutation the
   deadline interrupts leaves its receipt PENDING: the outcome is unknown, and a
   retry with the same idempotency key reads the receipt rather than executing.
4. The response is measured: over the byte budget, the data is withheld and a
   stable code is returned instead.

Errors carry a stable `extensions.code` (and, for a refusal, the value-free
reason code the REST route would have put in its 403), a correlation id, and no
message of their own: never SQL, a credential or an object name. Query
telemetry is one structured log line per request, separate from any execution
audit: a query executes nothing against a source, so there is nothing to audit (a
read of metadata is not audited over REST either), and the execution mutation is
audited once, by the governed tool path it calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.types.graphql import OperationType

from aida.governed_execution import (
    RECEIPT_OVERSIGHT_ROLES,
    TOOL_EXECUTION_ROLES,
    execution_deadline,
)
from aida.graphql_limits import (
    DocumentCost,
    DocumentRefused,
    admit_document,
    limits_from_settings,
)
from aida.graphql_reads import GRAPHQL_ENDPOINT_ROLES, open_read_scope
from aida.graphql_schema import error_code, metadata_schema
from aida.request_budget import (
    BudgetDecision,
    budget_headers,
    consume_window_budget,
    principal_hash,
)
from aida.security import SecurityContext, require_roles
from atlas.platform.config import Settings, get_settings
from atlas.platform.context import get_correlation_id
from atlas.platform.db import get_session

router = APIRouter(tags=["graphql"])

_log = structlog.get_logger(__name__)

# `extensions` is part of the GraphQL-over-HTTP request shape and some clients always
# send it; it is accepted and ignored (no persisted queries, no client-set behaviour).
_ALLOWED_KEYS = frozenset({"query", "operationName", "variables", "extensions"})

#: Who may reach the endpoint: the metadata read roles, plus those who may execute a
#: governed tool or read an execution receipt (R11-GQL02). Every field still requires
#: its own role set -- reaching the endpoint grants nothing a field does not.
GRAPHQL_ROUTE_ROLES: tuple[str, ...] = tuple(
    sorted(
        set(GRAPHQL_ENDPOINT_ROLES) | set(TOOL_EXECUTION_ROLES) | set(RECEIPT_OVERSIGHT_ROLES)
    )
)


class GraphQLErrorRead(BaseModel):
    """One error. `message` is the code; nothing object-specific is ever in it."""

    model_config = ConfigDict(extra="forbid")

    message: str
    path: list[str | int] | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


class GraphQLResponseRead(BaseModel):
    """The response envelope. `data` is absent when the document was refused."""

    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any] | None = None
    errors: list[GraphQLErrorRead] | None = None
    extensions: dict[str, Any] = Field(default_factory=dict)


_REQUEST_BODY_SCHEMA: dict[str, Any] = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["query", "operationName"],
                "properties": {
                    "query": {"type": "string", "description": "One named query operation."},
                    "operationName": {"type": "string"},
                    "variables": {"type": "object", "nullable": True},
                    "extensions": {"type": "object", "nullable": True},
                },
            }
        }
    },
}


@dataclass(frozen=True, slots=True)
class _Request:
    query: str
    operation_name: str | None
    variables: dict[str, Any] | None


async def _read_body(request: Request, limit: int) -> bytes:
    """The body, refused as soon as it passes `limit` bytes -- never buffered past it."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise DocumentRefused("REQUEST_TOO_LARGE", f"the request body exceeds {limit} bytes")
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > limit:
            raise DocumentRefused("REQUEST_TOO_LARGE", f"the request body exceeds {limit} bytes")
    return bytes(received)


def _parse_request(body: bytes) -> _Request:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise DocumentRefused("REQUEST_INVALID", "the request body is not JSON") from None
    if isinstance(payload, list):
        raise DocumentRefused(
            "BATCHING_NOT_SUPPORTED", "send one operation per request; batching is not supported"
        )
    if not isinstance(payload, dict):
        raise DocumentRefused("REQUEST_INVALID", "the request body must be a JSON object")
    unknown = set(payload) - _ALLOWED_KEYS
    if unknown:
        raise DocumentRefused("REQUEST_INVALID", "the request body has unsupported keys")
    query = payload.get("query")
    operation_name = payload.get("operationName")
    variables = payload.get("variables")
    if not isinstance(query, str) or not query.strip():
        raise DocumentRefused("REQUEST_INVALID", "`query` must be a non-empty string")
    if operation_name is not None and not isinstance(operation_name, str):
        raise DocumentRefused("REQUEST_INVALID", "`operationName` must be a string")
    if operation_name is not None:
        try:
            operation_name.encode("utf-8")
        except UnicodeEncodeError:
            # JSON can decode a lone surrogate; telemetry must not crash hashing it.
            raise DocumentRefused(
                "REQUEST_INVALID", "`operationName` must contain valid Unicode"
            ) from None
    if variables is not None and not isinstance(variables, dict):
        raise DocumentRefused("REQUEST_INVALID", "`variables` must be an object")
    extensions = payload.get("extensions")
    if extensions is not None and not isinstance(extensions, dict):
        raise DocumentRefused("REQUEST_INVALID", "`extensions` must be an object")
    return _Request(query=query, operation_name=operation_name, variables=variables)


def _json(content: dict[str, Any], status_code: int) -> Response:
    return Response(
        content=json.dumps(content, separators=(",", ":"), default=str),
        status_code=status_code,
        media_type="application/json",
    )


def _refusal(refused: DocumentRefused, correlation_id: str) -> Response:
    extensions: dict[str, Any] = {"code": refused.code, "detail": refused.detail}
    if refused.messages:
        extensions["messages"] = list(refused.messages)
    return _json(
        {
            "errors": [{"message": refused.code, "extensions": extensions}],
            "extensions": {"correlationId": correlation_id},
        },
        refused.status_code,
    )


async def _budget(
    settings: Settings, context: SecurityContext, bucket: str
) -> BudgetDecision:
    """One request against the caller's GraphQL budget for `bucket`."""
    limit, window = (
        (settings.graphql_requests_per_minute, 60)
        if bucket == "REQUEST_MINUTE"
        else (settings.graphql_executions_per_day, 86_400)
    )
    return await consume_window_budget(
        settings,
        namespace="graphql-budget",
        bucket=bucket,
        key_hash=principal_hash(context),
        limit=limit,
        window_seconds=window,
        enabled=settings.graphql_budget_enabled,
    )


def _rate_limited(decision: BudgetDecision, correlation_id: str) -> Response:
    """429 with the stable code, the bucket and when to come back; no data."""
    response = _refusal(
        DocumentRefused(
            "RATE_LIMITED",
            f"the caller's {decision.bucket} budget of {decision.limit} is spent",
        ),
        correlation_id,
    )
    response.headers.update(budget_headers(decision))
    return response


def _operation_digest(name: str | None) -> str | None:
    """Operation names are caller-chosen text, so telemetry keeps a digest of one,
    never the name itself (INV-6)."""
    if not name:
        return None
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def _count_objects(data: dict[str, Any] | None, *, schema_keys: tuple[str, ...] = ()) -> int:
    """How many objects a response actually holds: every JSON object beneath `data`
    (the root itself is not a returned object, just as the estimate does not count it).
    `schema_keys` are admitted introspection fields, whose answer is the schema itself,
    bounded by the schema rather than by data; the byte ceiling still holds them."""
    count = 0
    pending: list[Any] = [
        value for key, value in (data or {}).items() if key not in schema_keys
    ]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            count += 1
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return count


@router.post(
    "/graphql",
    response_model=GraphQLResponseRead,
    summary=(
        "Metadata GraphQL: one named operation per request -- a read-only query, or the "
        "single governed-execution mutation"
    ),
    openapi_extra={"requestBody": _REQUEST_BODY_SCHEMA},
)
async def graphql_query(
    request: Request,
    context: SecurityContext = Depends(require_roles(*GRAPHQL_ROUTE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Serve one metadata query, or one governed execution mutation.

    A query executes nothing against a source. A mutation runs an approved tool
    version through `aida.governed_execution`, once per caller-scoped key.
    """
    organization_id = context.require_organization()
    limits = limits_from_settings(settings, roles=context.roles)
    correlation_id = get_correlation_id()
    started = perf_counter()
    operation_digest: str | None = None
    requests = await _budget(settings, context, "REQUEST_MINUTE")
    if not requests.allowed:
        _telemetry(
            outcome="RATE_LIMITED",
            operation_digest=None,
            cost=None,
            started=started,
            error_codes=["RATE_LIMITED"],
        )
        return _rate_limited(requests, correlation_id)
    try:
        parsed = _parse_request(await _read_body(request, limits.max_request_bytes))
        operation_digest = _operation_digest(parsed.operation_name)
        _, cost = admit_document(
            query=parsed.query,
            operation_name=parsed.operation_name,
            variables=parsed.variables,
            schema=metadata_schema._schema,
            limits=limits,
        )
    except DocumentRefused as refused:
        _telemetry(
            outcome=refused.code,
            operation_digest=operation_digest,
            cost=None,
            started=started,
            error_codes=[refused.code],
        )
        return _refusal(refused, correlation_id)

    scope = open_read_scope(
        session=session,
        context=context,
        settings=settings,
        organization_id=organization_id,
        limits=limits,
    )
    cost_extension = {
        "depth": cost.depth,
        "aliases": cost.aliases,
        "estimatedNodes": cost.estimated_nodes,
    }
    mutation = cost.operation_type == "mutation"
    if mutation:
        executions = await _budget(settings, context, "EXECUTION_DAY")
        if not executions.allowed:
            _telemetry(
                outcome="RATE_LIMITED",
                operation_digest=operation_digest,
                cost=cost,
                started=started,
                error_codes=["RATE_LIMITED"],
            )
            return _rate_limited(executions, correlation_id)
    deadline = (
        execution_deadline(settings).total_seconds()
        if mutation
        else limits.deadline_seconds
    )
    try:
        async with asyncio.timeout(deadline):
            result = await metadata_schema.execute(
                parsed.query,
                variable_values=parsed.variables,
                context_value=scope,
                operation_name=parsed.operation_name,
                allowed_operation_types=(
                    (OperationType.MUTATION,) if mutation else (OperationType.QUERY,)
                ),
            )
    except TimeoutError:
        _telemetry(
            outcome="DEADLINE_EXCEEDED",
            operation_digest=operation_digest,
            cost=cost,
            started=started,
            error_codes=["DEADLINE_EXCEEDED"],
        )
        return _json(
            {
                "data": None,
                "errors": [
                    {"message": "DEADLINE_EXCEEDED", "extensions": {"code": "DEADLINE_EXCEEDED"}}
                ],
                "extensions": {"correlationId": correlation_id, "cost": cost_extension},
            },
            200,
        )

    errors: list[dict[str, Any]] = []
    for error in result.errors or ():
        code, reason = error_code(error)
        extensions: dict[str, Any] = {"code": code}
        if reason is not None:
            extensions["reason"] = reason
        errors.append({"message": code, "path": error.path, "extensions": extensions})
    returned = _count_objects(result.data, schema_keys=cost.introspection_keys)
    content: dict[str, Any] = {"data": result.data}
    if errors:
        content["errors"] = errors
    content["extensions"] = {
        "correlationId": correlation_id,
        "cost": {**cost_extension, "returnedObjects": returned},
    }
    encoded = json.dumps(content, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > limits.max_response_bytes or returned > limits.max_nodes:
        _telemetry(
            outcome="RESPONSE_TOO_LARGE",
            operation_digest=operation_digest,
            cost=cost,
            started=started,
            error_codes=["RESPONSE_TOO_LARGE"],
        )
        return _json(
            {
                "data": None,
                "errors": [
                    {
                        "message": "RESPONSE_TOO_LARGE",
                        "extensions": {"code": "RESPONSE_TOO_LARGE"},
                    }
                ],
                "extensions": {"correlationId": correlation_id, "cost": cost_extension},
            },
            200,
        )
    _telemetry(
        outcome="OK" if not errors else "PARTIAL",
        operation_digest=operation_digest,
        cost=cost,
        started=started,
        error_codes=sorted({error["extensions"]["code"] for error in errors}),
        returned=returned,
    )
    return Response(content=encoded, status_code=200, media_type="application/json")


def _telemetry(
    *,
    outcome: str,
    operation_digest: str | None,
    cost: DocumentCost | None,
    started: float,
    error_codes: list[str],
    returned: int | None = None,
) -> None:
    """One value-free line per request: codes and counts, never names or values."""
    _log.info(
        "graphql.request",
        outcome=outcome,
        operation_digest=operation_digest,
        depth=cost.depth if cost else None,
        aliases=cost.aliases if cost else None,
        estimated_nodes=cost.estimated_nodes if cost else None,
        returned_objects=returned,
        error_codes=error_codes,
        duration_ms=round((perf_counter() - started) * 1000, 1),
    )
