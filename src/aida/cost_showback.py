"""Cost and showback aggregation, per line of business (OB-6).

Module 20's domain model sketches a first-class `cost_record(dimension, tenancy,
quantity, period)` ledger. That ledger does not exist yet, and nothing in this
codebase meters a real dollar cost anywhere -- there is no billing integration,
and `query_gateway.gate_query_estimate`'s `plan_cost` is explicitly a
connector-shaped proxy (bytes scanned for a byte-billed engine such as BigQuery,
a heuristic planner cost score for everything else), not a reconciled spend
figure, and the two are not comparable to each other. Inventing a dollar amount
from that would be exactly the "DONE was reference-only" failure this tracker
has already corrected once in this module (OB-1/OB-2/OB-3) -- so this module
does not do it.

What genuinely exists, is genuinely persisted by the live query-execution path
(`QueryExecutionGateway.execute`, `src/aida/query_gateway.py`), and rolls up
cleanly to a line of business: `QueryExecution` rows, one per query attempt,
each carrying `datasource_id`, `status`, `row_count`, `elapsed_ms` and
(when a connector's planner returns one) `plan_cost`. Every `DataSource` in
turn carries a mandatory `line_of_business_id` (ADR-0018: still the
authoritative access-adjacent tenancy column pending the workspace-scoping
cutover). This module aggregates that real signal, grouped by LOB, as an
honest consumption proxy: query volume, execution time, row volume and --
where a connector reports it -- planner cost units. It is showback (visibility
into who is consuming what), not chargeback (a priced invoice); turning this
into a priced bill needs a real cost-metering integration this platform does
not have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import DataSource, LineOfBusiness, QueryExecution

# Documented once, surfaced on every report, rather than left to be discovered
# by whoever reads the numbers: `plan_cost` mixes units across connectors and
# must never be presented as money.
COST_BASIS = (
    "Proxy consumption metrics rolled up from real QueryExecution rows, not a "
    "reconciled dollar cost -- this platform has no billing integration. "
    "query_count/completed_count/rejected_count/failed_count and "
    "total_elapsed_ms/total_row_count are directly comparable across "
    "datasources. total_plan_cost_units sums query_gateway's planner cost "
    "estimate, which is bytes-scanned for byte-billed connectors (e.g. "
    "BigQuery) and a connector-specific heuristic cost score for everything "
    "else -- the two are not the same unit and total_plan_cost_units is "
    "therefore not comparable across LOBs whose datasources use different "
    "connectors, and is null where no datasource in the LOB reported one."
)


@dataclass(frozen=True, slots=True)
class LobCostRow:
    line_of_business_id: UUID
    line_of_business_code: str
    line_of_business_name: str
    datasource_count: int
    query_count: int
    completed_count: int
    rejected_count: int
    failed_count: int
    total_row_count: int
    total_elapsed_ms: int
    total_plan_cost_units: float | None


@dataclass(frozen=True, slots=True)
class CostShowbackReport:
    organization_id: UUID
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    cost_basis: str
    rows: list[LobCostRow] = field(default_factory=list)


def totals_for(rows: list[LobCostRow]) -> dict[str, Any]:
    """Sum a report's per-LOB rows into an organization-wide total row."""
    plan_cost_values = [
        r.total_plan_cost_units for r in rows if r.total_plan_cost_units is not None
    ]
    return {
        "datasource_count": sum(r.datasource_count for r in rows),
        "query_count": sum(r.query_count for r in rows),
        "completed_count": sum(r.completed_count for r in rows),
        "rejected_count": sum(r.rejected_count for r in rows),
        "failed_count": sum(r.failed_count for r in rows),
        "total_row_count": sum(r.total_row_count for r in rows),
        "total_elapsed_ms": sum(r.total_elapsed_ms for r in rows),
        "total_plan_cost_units": sum(plan_cost_values) if plan_cost_values else None,
    }


async def build_cost_showback_report(
    session: AsyncSession,
    *,
    organization_id: UUID,
    period_start: datetime,
    period_end: datetime,
) -> CostShowbackReport:
    """Aggregate real `QueryExecution` rows by the LOB their datasource belongs to.

    Every `LineOfBusiness` in the organization appears in the report, including
    one with zero datasources or zero query activity in the period -- a
    showback report that silently omits a quiet LOB is indistinguishable from
    one that never queried it, and a steward reconciling chargeback needs to
    see the zero, not infer it.
    """
    stmt = (
        select(
            LineOfBusiness.id,
            LineOfBusiness.code,
            LineOfBusiness.name,
            func.count(func.distinct(DataSource.id)),
            func.count(QueryExecution.id),
            func.sum(case((QueryExecution.status == "COMPLETED", 1), else_=0)),
            func.sum(case((QueryExecution.status == "REJECTED", 1), else_=0)),
            func.sum(case((QueryExecution.status == "FAILED", 1), else_=0)),
            func.coalesce(func.sum(QueryExecution.row_count), 0),
            func.coalesce(func.sum(QueryExecution.elapsed_ms), 0),
            func.sum(QueryExecution.plan_cost),
        )
        .select_from(LineOfBusiness)
        .outerjoin(DataSource, DataSource.line_of_business_id == LineOfBusiness.id)
        .outerjoin(
            QueryExecution,
            and_(
                QueryExecution.datasource_id == DataSource.id,
                QueryExecution.created_at >= period_start,
                QueryExecution.created_at <= period_end,
            ),
        )
        .where(LineOfBusiness.organization_id == organization_id)
        .group_by(LineOfBusiness.id, LineOfBusiness.code, LineOfBusiness.name)
        .order_by(LineOfBusiness.code)
    )
    result = await session.execute(stmt)

    rows = [
        LobCostRow(
            line_of_business_id=lob_id,
            line_of_business_code=code,
            line_of_business_name=name,
            datasource_count=datasource_count or 0,
            query_count=query_count or 0,
            completed_count=int(completed_count or 0),
            rejected_count=int(rejected_count or 0),
            failed_count=int(failed_count or 0),
            total_row_count=int(total_row_count or 0),
            total_elapsed_ms=int(total_elapsed_ms or 0),
            total_plan_cost_units=(
                float(total_plan_cost) if total_plan_cost is not None else None
            ),
        )
        for (
            lob_id,
            code,
            name,
            datasource_count,
            query_count,
            completed_count,
            rejected_count,
            failed_count,
            total_row_count,
            total_elapsed_ms,
            total_plan_cost,
        ) in result.all()
    ]

    return CostShowbackReport(
        organization_id=organization_id,
        period_start=period_start,
        period_end=period_end,
        generated_at=datetime.now(UTC),
        cost_basis=COST_BASIS,
        rows=rows,
    )
