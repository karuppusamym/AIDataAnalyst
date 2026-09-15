"""R11-FP16: a governed tool extracted from a routine is held once that routine changes.

* The procedure-tool route records the routine a draft's SQL was extracted from.
* A version whose routine was redefined or retired after it was approved is blocked at execution,
  before any SQL is rendered, with the reason in the refusal and in the audit record.
* A change from before the version's approval holds nothing, and a version approved after the
  change runs: approving a version is the re-verification that lifts the hold, with nothing to
  resolve by hand.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.change_signal_models import MetadataChangeSignal
from aida.config import Settings
from aida.db import Base
from aida.models import AuditEvent, GovernedToolVersion, ToolExecution
from aida.procedure_tool_api import ProcedureToolBlueprintRequest, create_procedure_tool_blueprint
from aida.query_gateway import QueryExecutionGateway
from aida.routine_tool_hold import SOURCE_ROUTINE_CHANGED_MESSAGE, fetch_source_routine_holds
from aida.schemas import ToolExecutionRequest
from aida.tool_api import execute_tool
from tests.test_procedure_tool_blueprint import _Scenario as ProcedureScenario
from tests.test_quality_runtime_coupling import _fake_execute
from tests.test_quality_runtime_coupling import _Scenario as CouplingScenario

_REPORT_BODY = (
    "CREATE PROCEDURE dbo.usp_report AS BEGIN "
    "SELECT c.customer_id, SUM(o.amount) AS total_amount "
    "FROM public.orders o JOIN public.customers c ON c.customer_id = o.customer_id "
    "GROUP BY c.customer_id; END"
)


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _signal(
    db: AsyncSession,
    scenario: CouplingScenario,
    routine_id: UUID,
    *,
    detected_at: datetime,
    signal_type: str = "DEFINITION_CHANGED",
) -> None:
    db.add(
        MetadataChangeSignal(
            organization_id=scenario.organization.id,
            datasource_id=scenario.datasource.id,
            subject_kind="ROUTINE",
            subject_id=routine_id,
            signal_type=signal_type,
            change_class="STRUCTURAL" if signal_type == "DEFINITION_CHANGED" else None,
            detected_at=detected_at,
        )
    )
    await db.flush()


async def test_a_procedure_tool_draft_records_the_routine_it_was_extracted_from(
    db: AsyncSession,
) -> None:
    scenario = await ProcedureScenario(db).build()
    routine = scenario.routine(body=_REPORT_BODY)
    db.add(routine)
    await db.flush()

    created = await create_procedure_tool_blueprint(
        scenario.project.id,
        ProcedureToolBlueprintRequest(
            slug="customer_order_totals",
            name="Customer order totals",
            description="Read surface from a proven read-only procedure.",
            datasource_id=scenario.datasource.id,
            routine_id=routine.id,
            allowed_roles=["Analyst"],
        ),
        context=scenario.maker(),
        session=db,
        settings=Settings(),
    )

    assert created.source_routine_id == routine.id
    version = await db.get(GovernedToolVersion, created.id)
    assert version is not None and version.source_routine_id == routine.id


async def test_a_routine_redefined_after_approval_blocks_the_tool_before_any_sql(
    db: AsyncSession,
) -> None:
    scenario = await CouplingScenario(db).build()
    version = await scenario.tool_version()
    routine_id = uuid4()
    approved_at = datetime.now(UTC) - timedelta(days=2)
    version.approved_at = approved_at
    version.source_routine_id = routine_id
    await _signal(db, scenario, routine_id, detected_at=approved_at + timedelta(days=1))

    with pytest.raises(HTTPException) as refused:
        await execute_tool(
            version.id,
            ToolExecutionRequest(parameters={}),
            context=scenario.analyst(),
            session=db,
            settings=Settings(),
        )

    assert refused.value.status_code == 409
    assert SOURCE_ROUTINE_CHANGED_MESSAGE in refused.value.detail
    assert (await db.scalars(select(ToolExecution))).all() == []
    denied = (await db.scalars(select(AuditEvent).where(AuditEvent.outcome == "DENIED"))).one()
    assert denied.details["source_routine_changed"] is True


async def test_the_hold_counts_only_changes_after_approval_and_a_later_approval_lifts_it(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(QueryExecutionGateway, "execute", _fake_execute)
    scenario = await CouplingScenario(db).build()
    version = await scenario.tool_version()
    routine_id = uuid4()
    now = datetime.now(UTC)
    version.source_routine_id = routine_id
    version.approved_at = now - timedelta(days=3)
    await _signal(db, scenario, routine_id, detected_at=now - timedelta(days=4))

    assert await fetch_source_routine_holds(db, version) == ([str(routine_id)], [])

    await _signal(
        db, scenario, routine_id, detected_at=now - timedelta(days=2), signal_type="DEPRECATED"
    )
    assets, holds = await fetch_source_routine_holds(db, version)
    assert assets == [str(routine_id)]
    assert [(hold.severity, hold.status) for hold in holds] == [("CRITICAL", "OPEN")]

    # Re-verified: the version is approved again after the change, and it runs.
    version.approved_at = now - timedelta(days=1)
    await db.flush()
    await execute_tool(
        version.id,
        ToolExecutionRequest(parameters={}),
        context=scenario.analyst(),
        session=db,
        settings=Settings(),
    )
    assert len((await db.scalars(select(ToolExecution))).all()) == 1


async def test_a_tool_not_extracted_from_a_routine_has_no_routine_dependency(
    db: AsyncSession,
) -> None:
    scenario = await CouplingScenario(db).build()
    version = await scenario.tool_version()

    assert await fetch_source_routine_holds(db, version) == ([], [])
