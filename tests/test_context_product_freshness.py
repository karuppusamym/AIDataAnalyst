"""R11-FP12: a context product says how old what it describes is, not only whether it changed.

Coverage gave a consumer digests -- enough to tell that a view's definition changed, never enough
to tell that nobody has looked at the source since June. `load_source_freshness` answers the time
side, per datasource, because a discovery run's reach is a datasource. These tests pin:

* the compiled artifact does not move when a scan finishes: freshness rides in `generated_from`,
  beside the artifact, because a clock inside the hashed content would read as deployment drift;
* the last completed read and the last completed *full* read are reported apart, since only a
  FULL run retires what it did not see;
* a run that never completed is not a read, and a source that has never been read says so;
* one source's scan never dates another's, and another organization's tables resolve to nothing;
* MCP's context-product read renders the section through the same helper.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from aida.context_compiler import (
    ResolvedSourceFreshness,
    compile_context_product,
    freshness_section,
)
from aida.context_product_coverage import load_source_freshness
from aida.mcp_server import _read_context_product_resource
from aida.models import AnalysisRun, DataSource, Organization
from aida.security import SecurityContext
from tests.support.task_agents import seed_estate, seed_table, task_agent_session
from tests.test_context_product_routines import _fixture
from tests.test_context_products import _ContextProductReadSession, _published_product

JUNE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
SEPTEMBER = datetime(2026, 9, 14, 7, 30, tzinfo=UTC)


def _freshness(datasource_id: str, table_id: str) -> ResolvedSourceFreshness:
    return ResolvedSourceFreshness(
        datasource_id=datasource_id,
        table_ids=(table_id,),
        last_scan_completed_at=SEPTEMBER.isoformat(),
        last_full_scan_completed_at=JUNE.isoformat(),
    )


async def _run(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    *,
    status: str,
    mode: str,
    at: datetime,
) -> AnalysisRun:
    run = AnalysisRun(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        mode=mode,
        status=status,
        created_at=at,
        updated_at=at,
    )
    session.add(run)
    await session.flush()
    return run


def test_a_scan_finishing_never_moves_the_compiled_artifact() -> None:
    product, version, tables = _fixture()
    sources = [_freshness(str(uuid4()), tables[0].table_id)]

    for target in ("MCP", "REST", "YAML", "OSI"):
        plain = compile_context_product(product, version, target, tables)
        dated = compile_context_product(product, version, target, tables, sources=sources)
        assert dated.content == plain.content
        assert dated.artifact_hash == plain.artifact_hash
        assert "last_scan_completed_at" not in dated.content
        assert dated.generated_from["source_freshness"] == freshness_section(sources)
        # A product compiled without it still carries the key, empty, rather than omitting it:
        # "no source has been read" and "this compile did not look" must not read the same.
        assert plain.generated_from["source_freshness"] == []


async def test_freshness_reports_the_last_read_and_the_last_read_in_full() -> None:
    async with task_agent_session() as session:
        org, datasource, schema = await seed_estate(session)
        _, unread, unread_schema = await seed_estate(session, organization=org)
        orders = await seed_table(session, org, datasource, schema, name="orders")
        totals = await seed_table(session, org, datasource, schema, name="totals")
        elsewhere = await seed_table(session, org, unread, unread_schema, name="ledger")
        await _run(session, org, datasource, status="COMPLETED", mode="FULL", at=JUNE)
        await _run(session, org, datasource, status="COMPLETED", mode="INCREMENTAL", at=SEPTEMBER)
        # Still running, and newer than either: a read that has not finished dates nothing.
        await _run(
            session,
            org,
            datasource,
            status="RUNNING",
            mode="FULL",
            at=SEPTEMBER + timedelta(days=1),
        )
        await session.commit()

        read, never = await load_source_freshness(
            session, org.id, [orders.id, totals.id, elsewhere.id]
        )

    by_source = {source.datasource_id: source for source in (read, never)}
    scanned = by_source[str(datasource.id)]
    assert scanned.table_ids == tuple(sorted((str(orders.id), str(totals.id))))
    assert scanned.last_scan_completed_at is not None
    assert scanned.last_scan_completed_at.startswith("2026-09-14")
    assert scanned.last_full_scan_completed_at is not None
    assert scanned.last_full_scan_completed_at.startswith("2026-06-01")

    # A second source in the same product is dated by its own runs, never by the first's.
    quiet = by_source[str(unread.id)]
    assert quiet.table_ids == (str(elsewhere.id),)
    assert quiet.last_scan_completed_at is None
    assert quiet.last_full_scan_completed_at is None


async def test_a_run_that_did_not_complete_is_not_a_read() -> None:
    async with task_agent_session() as session:
        org, datasource, schema = await seed_estate(session)
        orders = await seed_table(session, org, datasource, schema, name="orders")
        await _run(session, org, datasource, status="FAILED", mode="FULL", at=SEPTEMBER)
        await _run(session, org, datasource, status="QUEUED", mode="INCREMENTAL", at=SEPTEMBER)
        await session.commit()

        (source,) = await load_source_freshness(session, org.id, [orders.id])

    assert source.last_scan_completed_at is None
    assert source.last_full_scan_completed_at is None


async def test_an_incremental_read_never_dates_the_last_full_one() -> None:
    async with task_agent_session() as session:
        org, datasource, schema = await seed_estate(session)
        orders = await seed_table(session, org, datasource, schema, name="orders")
        await _run(session, org, datasource, status="COMPLETED", mode="INCREMENTAL", at=SEPTEMBER)
        await session.commit()

        (source,) = await load_source_freshness(session, org.id, [orders.id])

    # The source was read yesterday, but nothing has retired what it no longer holds: an object
    # dropped there is still ACTIVE here, and the pair of times is what says so.
    assert source.last_scan_completed_at is not None
    assert source.last_full_scan_completed_at is None


async def test_another_organizations_tables_resolve_to_nothing() -> None:
    async with task_agent_session() as session:
        org, datasource, schema = await seed_estate(session)
        orders = await seed_table(session, org, datasource, schema, name="orders")
        await _run(session, org, datasource, status="COMPLETED", mode="FULL", at=SEPTEMBER)
        await session.commit()

        stranger = await load_source_freshness(session, uuid4(), [orders.id])
        empty = await load_source_freshness(session, org.id, [])

    assert stranger == [] and empty == []


async def test_mcp_renders_freshness_through_the_shared_helper() -> None:
    version, product = _published_product(allowed_roles=["Analyst"])
    context = SecurityContext(
        principal_id="analyst-agent",
        principal_type="SERVICE",
        organization_id=product.organization_id,
        roles=frozenset({"Analyst"}),
    )
    session: Any = _ContextProductReadSession((version, product))

    result = await _read_context_product_resource(
        "atlas://context-products/revenue_context/versions/1", session, context, "corr-freshness"
    )

    payload = json.loads(result["contents"][0]["text"])
    # The key is always there, as it is on the compile door: a consumer that reads no `freshness`
    # is reading an older Atlas, not a product whose sources were never scanned.
    assert payload["freshness"] == []
