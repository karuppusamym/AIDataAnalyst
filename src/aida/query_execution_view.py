"""The one projection from a gateway result to its API response.

**Invariant this module exists to hold:** every route that returns an
executed query returns the same fields, computed the same way, from the same
gateway result. There is exactly one place that decides what a caller is
told about an execution.

`aida.api._query_execution_response` and `aida.tool_api._query_response` were
byte-identical (`Docs/review-2026-09-05/REVIEW.md` R07's exact-AST-duplicate
scan named this pair). Duplicated projections are how two endpoints start
disclosing different things about the same execution: `masked_columns` or
`rows` gets added to one copy and not the other, and the endpoint that
forgot it looks like it simply returned nothing sensitive.

That is the reason this consolidation is a policy fix and not tidying.
`masked_columns` and `rows` are the fields the review's F20 work put on
screen; they must be projected identically no matter which route produced
the result.

Deliberately not a router import in either direction: both `api.py` and
`tool_api.py` import this, and it imports neither. `GatewayResult` is
referenced under `TYPE_CHECKING` only, so this module adds no runtime edge to
the execution gateway either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aida.schemas import QueryExecutionResponse

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aida.query_gateway import GatewayResult

__all__ = ["query_execution_response"]


def query_execution_response(result: GatewayResult) -> QueryExecutionResponse:
    """Project a gateway result into its API response.

    `plan_cost`, `row_count` and `elapsed_ms` fall back to zero rather than
    None: the response type declares them non-optional, and a refused or
    not-yet-executed statement has no measurement to report. Zero here means
    "not measured", which is why callers must read `status` to know whether
    the execution happened at all.
    """
    execution = result.execution
    return QueryExecutionResponse(
        execution_id=execution.id,
        status=execution.status,
        normalized_sql=execution.normalized_sql or "",
        referenced_tables=execution.referenced_tables,
        referenced_columns=execution.referenced_columns,
        column_lineage=execution.column_lineage,
        plan_cost=execution.plan_cost or 0.0,
        warehouse_query_id=execution.warehouse_query_id,
        row_count=execution.row_count or 0,
        elapsed_ms=execution.elapsed_ms or 0,
        masked_columns=list(result.masked_columns),
        rows=list(result.rows),
    )
