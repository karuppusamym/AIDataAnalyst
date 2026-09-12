"""atlas.modules.observability_audit -- HTTP routes.

Moved verbatim from `aida.observability_api` on 2026-09-03 under ST-07
Commit C for observability_audit (analog of the catalog module's Commit C).
Every endpoint keeps its path, method, response model, `tags=["observability"]`,
required roles and status code, so `openapi.json` is byte-identical after the
move. Only the source module changes.

The old path `aida.observability_api` remains as a re-export shim so
`main.py` and the two tests that import specific handler functions
(`get_cost_showback`, `get_archive_status`) keep working unchanged.

Original module docstring follows.

---
Observability API (OB-1 through OB-4, OB-6).

Audit archive status and cost/showback aggregation endpoints.

The SLO definitions CRUD and error-budget endpoints were retired on
2026-09-12 (R11-D10). `slo_measurement` never had a writer, and the reason it
never got one is that there was nothing to write: an SLO was bound to no
measurable signal (`slo_key` was a free-text slug with no registry), no SLI
concept existed in `src/`, and nothing scrapes the Prometheus exposition on
`/metrics` -- no compose file or `infra/` manifest deploys a Prometheus at
all. A budget endpoint that can only ever answer NO_DATA, and a create form
that writes an audit and an outbox event for a control nobody measures, are
worse than no feature: they are governance evidence for supervision that does
not exist. Removing them alters the OpenAPI surface (three routes) and
therefore `Docs/90-reference/openapi-baseline.json` and
`Docs/50-security/surface-control-matrix.md`, which are regenerated centrally.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.cost_showback import build_cost_showback_report, totals_for
from aida.db import get_session
from aida.models import AuditArchiveRecord
from aida.schemas import (
    ArchiveStatusRead,
    CostShowbackRead,
    CostShowbackTotalsRead,
    LobCostRowRead,
)
from aida.security import SecurityContext, require_roles
from aida.worm_archive import STATE_VERIFIED

router = APIRouter(prefix="/v1", tags=["observability"])


@router.get("/observability/archive/status", response_model=ArchiveStatusRead)
async def get_archive_status(
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> ArchiveStatusRead:
    org_id = context.require_organization()
    # VERIFIED only (review F01). A PREPARED, UPLOADED or FAILED row is an
    # attempt, not an archive; counting them here is exactly how this
    # endpoint came to report archive evidence for events nothing had
    # stored. LEGACY_UNVERIFIED rows -- written when the archiver returned a
    # success object without writing anything -- are excluded for the same
    # reason.
    filters = [
        AuditArchiveRecord.organization_id == org_id,
        AuditArchiveRecord.state == STATE_VERIFIED,
    ]

    stats = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(AuditArchiveRecord.event_count), 0),
                func.count().filter(AuditArchiveRecord.legal_hold.is_(True)),
            ).where(*filters)
        )
    ).one()

    latest = await session.scalar(
        select(AuditArchiveRecord)
        .where(*filters)
        .order_by(AuditArchiveRecord.created_at.desc())
        .limit(1)
    )

    total_archives = stats[0] or 0
    total_events_archived = int(stats[1])
    legal_hold_count = stats[2] or 0

    if total_archives == 0:
        status = "NO_ARCHIVES"
    elif legal_hold_count > 0:
        status = "LEGAL_HOLD_ACTIVE"
    else:
        status = "HEALTHY"

    return ArchiveStatusRead(
        total_archives=total_archives,
        total_events_archived=total_events_archived,
        latest_archive_id=latest.archive_id if latest else None,
        latest_checksum=latest.checksum if latest else None,
        legal_hold_count=legal_hold_count,
        status=status,
    )


@router.get(
    "/observability/cost/showback",
    response_model=CostShowbackRead,
    summary="Cost/showback aggregation, per line of business",
)
async def get_cost_showback(
    period_start: datetime = Query(...),
    period_end: datetime = Query(...),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "Operations", "ComplianceOfficer", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> CostShowbackRead:
    """Real-time showback report: `QueryExecution` rows aggregated by the LOB
    their `DataSource` belongs to. See `aida.cost_showback` module docstring
    for exactly what `total_plan_cost_units` is (a per-connector proxy) and is
    not (a reconciled dollar cost) -- this platform has no billing
    integration, and `cost_basis` on every response says so explicitly rather
    than let a proxy metric be mistaken for one.
    """
    org_id = context.require_organization()
    if period_end <= period_start:
        raise HTTPException(status_code=422, detail="period_end must be after period_start")

    report = await build_cost_showback_report(
        session,
        organization_id=org_id,
        period_start=period_start,
        period_end=period_end,
    )

    rows = [
        LobCostRowRead(
            line_of_business_id=row.line_of_business_id,
            line_of_business_code=row.line_of_business_code,
            line_of_business_name=row.line_of_business_name,
            datasource_count=row.datasource_count,
            query_count=row.query_count,
            completed_count=row.completed_count,
            rejected_count=row.rejected_count,
            failed_count=row.failed_count,
            total_row_count=row.total_row_count,
            total_elapsed_ms=row.total_elapsed_ms,
            total_plan_cost_units=row.total_plan_cost_units,
        )
        for row in report.rows
    ]
    return CostShowbackRead(
        organization_id=report.organization_id,
        period_start=report.period_start,
        period_end=report.period_end,
        generated_at=report.generated_at,
        cost_basis=report.cost_basis,
        rows=rows,
        totals=CostShowbackTotalsRead(**totals_for(report.rows)),
    )
