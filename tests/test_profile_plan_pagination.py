"""PR-5: DB-backed correctness for `plan_profile_tasks`'s keyset pagination.

Runs the real activity body against an in-memory SQLite database (the same
`Base.metadata.create_all` approach `test_catalog_pagination.py` uses for
CT-2) -- a real SQL engine enforcing the same row-value comparison semantics
`apply_keyset` relies on, not a mock. This is the actual proof for the
exit-condition-adjacent claim in the task: the exact-fit-final-page off-by-one
this module's docstring calls out is a case here, not just in the pure state
machine's tests.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.workflows.activities as activities
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AnalysisRun,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def _seed(session: AsyncSession, *, table_count: int) -> tuple[DataSource, AnalysisRun]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="primary",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    tables = [
        MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=f"table_{i:04d}",
            object_type="BASE_TABLE",
            fingerprint="fp",
        )
        for i in range(table_count)
    ]
    session.add_all(tables)
    run = AnalysisRun(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        status="RUNNING",
    )
    session.add(run)
    await session.commit()
    return datasource, run


async def test_a_single_page_covering_every_table_reports_no_more(session, monkeypatch) -> None:
    _datasource, run = await _seed(session, table_count=5)
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))

    plan = await activities.plan_profile_tasks({"run_id": str(run.id)})

    assert len(plan["table_ids"]) == 5
    assert plan["has_more"] is False


async def test_exact_fit_final_page_is_not_misread_as_more_remains(session, monkeypatch) -> None:
    """The off-by-one this PR's exit condition explicitly calls out: when a page
    boundary lands exactly on the last row (page size == remaining rows), the
    activity must report `has_more=False`, not misread the boundary as "more
    remains" from a naive `len(page) == page_size` check.
    """
    settings = Settings(_env_file=None, profile_plan_page_size=5)
    _datasource, run = await _seed(session, table_count=10)  # exactly two pages of 5
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: settings)

    first = await activities.plan_profile_tasks({"run_id": str(run.id)})
    assert len(first["table_ids"]) == 5
    assert first["has_more"] is True

    second = await activities.plan_profile_tasks(
        {
            "run_id": str(run.id),
            "cursor": first["next_cursor"],
            "tables_planned_total": 5,
        }
    )
    assert len(second["table_ids"]) == 5
    # This is the exact assertion a naive `len(rows) == limit` implementation
    # gets wrong: exactly 5 rows remained and were returned, so `len(page) ==
    # page_size` is True even though nothing is left.
    assert second["has_more"] is False

    third = await activities.plan_profile_tasks(
        {
            "run_id": str(run.id),
            "cursor": second["next_cursor"],
            "tables_planned_total": 10,
        }
    )
    assert third["table_ids"] == []
    assert third["has_more"] is False


async def test_pagination_walks_every_table_exactly_once_across_uneven_pages(
    session, monkeypatch
) -> None:
    settings = Settings(_env_file=None, profile_plan_page_size=7)
    _datasource, run = await _seed(session, table_count=23)
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: settings)

    seen: list[str] = []
    cursor = None
    tables_planned_total = 0
    pages = 0
    while True:
        plan = await activities.plan_profile_tasks(
            {
                "run_id": str(run.id),
                "cursor": cursor,
                "tables_planned_total": tables_planned_total,
            }
        )
        pages += 1
        seen.extend(plan["table_ids"])
        tables_planned_total += len(plan["table_ids"])
        cursor = plan["next_cursor"]
        if not plan["has_more"]:
            break
        assert pages < 100, "pagination did not terminate"

    assert len(seen) == 23
    assert len(set(seen)) == 23, "a table id was returned more than once"
    assert pages == 4  # 7, 7, 7, 2


async def test_the_overall_run_cap_is_enforced_across_pages(session, monkeypatch) -> None:
    """`profile_max_tables_per_run` still bounds the whole run, just spread
    across many bounded page calls instead of one `.limit(...)` -- mirroring
    the old one-shot activity's cap, not relaxing it.
    """
    settings = Settings(_env_file=None, profile_plan_page_size=10, profile_max_tables_per_run=15)
    _datasource, run = await _seed(session, table_count=50)
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: settings)

    seen: list[str] = []
    cursor = None
    tables_planned_total = 0
    for _ in range(10):
        plan = await activities.plan_profile_tasks(
            {
                "run_id": str(run.id),
                "cursor": cursor,
                "tables_planned_total": tables_planned_total,
            }
        )
        seen.extend(plan["table_ids"])
        tables_planned_total += len(plan["table_ids"])
        cursor = plan["next_cursor"]
        if not plan["has_more"]:
            break

    assert len(seen) == 15
    assert len(set(seen)) == 15


async def test_an_invalid_cursor_is_rejected_as_non_retryable(session, monkeypatch) -> None:
    from temporalio.exceptions import ApplicationError

    _datasource, run = await _seed(session, table_count=3)
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))

    with pytest.raises(ApplicationError) as excinfo:
        await activities.plan_profile_tasks(
            {"run_id": str(run.id), "cursor": "not-a-valid-cursor"}
        )
    assert excinfo.value.non_retryable is True
