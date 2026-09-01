"""OB-6: cost/showback aggregation, per line of business.

`aida.cost_showback.build_cost_showback_report` aggregates real
`QueryExecution` rows (written by the live `QueryExecutionGateway.execute`
path) by the `LineOfBusiness` their `DataSource` belongs to. These tests seed
a real in-memory database with real rows across two LOBs and assert the
report reflects them -- not zeros -- the exact failure mode OB-1/OB-2/OB-3
had before their 2026-08-31 fixes (`Docs/60-delivery/04-end-to-end-audit-
2026-08-30.md` Sec.2), and exercise the real `GET /observability/cost/
showback` endpoint function against the same seeded state.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401  -- registers every table on the metadata
from aida.cost_showback import build_cost_showback_report, totals_for
from aida.db import Base
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    Organization,
    Project,
    QueryExecution,
)
from aida.observability_api import get_cost_showback
from aida.security_types import SecurityContext

_WINDOW_START = datetime(2026, 8, 1, tzinfo=UTC)
_WINDOW_END = datetime(2026, 8, 31, 23, 59, 59, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _organization(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


async def _lob_datasource(
    session: AsyncSession, org: Organization, *, code: str, name: str
) -> tuple[LineOfBusiness, DataSource]:
    lob = LineOfBusiness(organization_id=org.id, name=name, code=code)
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=org.id, line_of_business_id=lob.id, name="Ungoverned", code="UNGOVERNED"
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name=name,
        slug=f"{code.lower()}-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    datasource = DataSource(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=f"{code}-warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://x",
    )
    session.add(datasource)
    await session.flush()
    return lob, datasource


def _execution(
    org: Organization,
    datasource: DataSource,
    *,
    status: str,
    row_count: int | None,
    elapsed_ms: int | None,
    plan_cost: float | None,
    created_at: datetime,
) -> QueryExecution:
    return QueryExecution(
        organization_id=org.id,
        datasource_id=datasource.id,
        principal_id="analyst-1",
        status=status,
        dialect="postgres",
        sql_hash=uuid4().hex,
        row_count=row_count,
        elapsed_ms=elapsed_ms,
        plan_cost=plan_cost,
        created_at=created_at,
    )


def _context(
    org: Organization, *, roles: frozenset[str] = frozenset({"Viewer"})
) -> SecurityContext:
    return SecurityContext(
        principal_id="ops-1",
        principal_type="USER",
        organization_id=org.id,
        roles=roles,
    )


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


async def test_report_aggregates_real_query_executions_by_lob(session: AsyncSession) -> None:
    org = await _organization(session)
    retail_lob, retail_ds = await _lob_datasource(session, org, code="RTL", name="Retail Banking")
    markets_lob, markets_ds = await _lob_datasource(session, org, code="MKT", name="Markets")

    in_window = datetime(2026, 8, 15, tzinfo=UTC)
    session.add_all(
        [
            _execution(
                org, retail_ds, status="COMPLETED", row_count=100, elapsed_ms=50,
                plan_cost=10.0, created_at=in_window,
            ),
            _execution(
                org, retail_ds, status="COMPLETED", row_count=200, elapsed_ms=75,
                plan_cost=20.0, created_at=in_window,
            ),
            _execution(
                org, retail_ds, status="REJECTED", row_count=None, elapsed_ms=5,
                plan_cost=None, created_at=in_window,
            ),
            _execution(
                org, markets_ds, status="COMPLETED", row_count=1000, elapsed_ms=500,
                plan_cost=None, created_at=in_window,
            ),
            _execution(
                org, markets_ds, status="FAILED", row_count=None, elapsed_ms=10,
                plan_cost=None, created_at=in_window,
            ),
            # Outside the reporting window -- must not be counted.
            _execution(
                org, retail_ds, status="COMPLETED", row_count=99999, elapsed_ms=99999,
                plan_cost=999.0, created_at=datetime(2026, 7, 1, tzinfo=UTC),
            ),
        ]
    )
    await session.flush()

    report = await build_cost_showback_report(
        session, organization_id=org.id, period_start=_WINDOW_START, period_end=_WINDOW_END
    )

    by_code = {row.line_of_business_code: row for row in report.rows}
    assert set(by_code) == {"RTL", "MKT"}

    retail = by_code["RTL"]
    assert retail.datasource_count == 1
    assert retail.query_count == 3
    assert retail.completed_count == 2
    assert retail.rejected_count == 1
    assert retail.failed_count == 0
    assert retail.total_row_count == 300
    assert retail.total_elapsed_ms == 130
    assert retail.total_plan_cost_units == 30.0

    markets = by_code["MKT"]
    assert markets.datasource_count == 1
    assert markets.query_count == 2
    assert markets.completed_count == 1
    assert markets.failed_count == 1
    assert markets.total_row_count == 1000
    assert markets.total_elapsed_ms == 510
    # No connector in this fixture reported a plan_cost for Markets -- null,
    # not zero, which is the honest answer for "unknown" versus "free".
    assert markets.total_plan_cost_units is None

    totals = totals_for(report.rows)
    assert totals["query_count"] == 5
    assert totals["total_row_count"] == 1300
    assert totals["total_plan_cost_units"] == 30.0
    assert "not a reconciled dollar cost" in report.cost_basis


async def test_report_includes_a_lob_with_zero_datasources_and_zero_activity(
    session: AsyncSession,
) -> None:
    """A quiet LOB must appear with real zeros, not be silently omitted --
    omission and "genuinely zero activity" must never look the same on a
    showback report a steward reconciles chargeback against.
    """
    org = await _organization(session)
    session.add(LineOfBusiness(organization_id=org.id, name="Wealth Management", code="WLM"))
    await session.flush()

    report = await build_cost_showback_report(
        session, organization_id=org.id, period_start=_WINDOW_START, period_end=_WINDOW_END
    )

    assert len(report.rows) == 1
    row = report.rows[0]
    assert row.line_of_business_code == "WLM"
    assert row.datasource_count == 0
    assert row.query_count == 0
    assert row.total_plan_cost_units is None


async def test_report_scopes_to_the_requested_organization(session: AsyncSession) -> None:
    org_a = await _organization(session)
    org_b = await _organization(session)
    _, ds_a = await _lob_datasource(session, org_a, code="A1", name="Org A LOB")
    await _lob_datasource(session, org_b, code="B1", name="Org B LOB")

    session.add(
        _execution(
            org_a, ds_a, status="COMPLETED", row_count=5, elapsed_ms=5, plan_cost=1.0,
            created_at=datetime(2026, 8, 10, tzinfo=UTC),
        )
    )
    await session.flush()

    report = await build_cost_showback_report(
        session, organization_id=org_a.id, period_start=_WINDOW_START, period_end=_WINDOW_END
    )
    assert {row.line_of_business_code for row in report.rows} == {"A1"}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


async def test_endpoint_reflects_real_seeded_rows(session: AsyncSession) -> None:
    org = await _organization(session)
    _, ds = await _lob_datasource(session, org, code="RTL", name="Retail Banking")
    session.add(
        _execution(
            org, ds, status="COMPLETED", row_count=42, elapsed_ms=17, plan_cost=3.5,
            created_at=datetime(2026, 8, 5, tzinfo=UTC),
        )
    )
    await session.flush()

    response = await get_cost_showback(
        period_start=_WINDOW_START,
        period_end=_WINDOW_END,
        context=_context(org),
        session=session,
    )

    assert response.organization_id == org.id
    assert len(response.rows) == 1
    row = response.rows[0]
    assert row.line_of_business_code == "RTL"
    assert row.query_count == 1
    assert row.total_row_count == 42
    assert row.total_elapsed_ms == 17
    assert row.total_plan_cost_units == 3.5
    assert response.totals.query_count == 1
    assert response.totals.total_row_count == 42


async def test_endpoint_rejects_an_inverted_period(session: AsyncSession) -> None:
    from fastapi import HTTPException

    org = await _organization(session)
    try:
        await get_cost_showback(
            period_start=_WINDOW_END,
            period_end=_WINDOW_START,
            context=_context(org),
            session=session,
        )
        raise AssertionError("expected HTTPException for an inverted period")
    except HTTPException as exc:
        assert exc.status_code == 422
