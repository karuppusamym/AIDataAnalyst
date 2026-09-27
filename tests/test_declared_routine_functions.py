"""R11-FP14: a call naming one of the source's own routines is refused.

The guard refuses a call sqlglot does not recognise. sqlglot models about six hundred function
names, for every dialect, so a user-defined `nvl` parsed as COALESCE and was trusted as a built-in
on PostgreSQL. Discovery already read this source's routines, so the gateway hands the guard their
names and the guard refuses a call that uses one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.config import Settings
from aida.db import Base
from aida.query_gateway import QueryExecutionGateway
from aida.schemas import GovernedToolVersionCreate
from aida.tool_api import create_tool_version
from tests.test_procedure_tool_blueprint import _Scenario as ProcedureScenario

_CALLS_A_ROUTINE = "SELECT nvl(o.amount, 0) AS amount FROM public.orders AS o"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _draft(db: AsyncSession, scenario: ProcedureScenario, slug: str) -> object:
    return await create_tool_version(
        scenario.project.id,
        GovernedToolVersionCreate(
            slug=slug,
            name="Order amounts",
            description="Reads order amounts, defaulting the missing ones.",
            datasource_id=scenario.datasource.id,
            sql_template=_CALLS_A_ROUTINE,
            parameters=[],
            allowed_roles=["Analyst"],
        ),
        context=scenario.maker(),
        session=db,
        settings=Settings(),
    )


async def test_the_gateway_reports_only_this_source_s_active_routines(db: AsyncSession) -> None:
    scenario = await ProcedureScenario(db).build()
    live = scenario.routine(body="SELECT 1", name="nvl")
    retired = scenario.routine(body="SELECT 1", name="fn_retired")
    retired.status = "DEPRECATED"
    db.add_all([live, retired])
    await db.flush()

    names = await QueryExecutionGateway(Settings()).declared_routine_names(db, scenario.datasource)

    assert names == {"nvl"}


async def test_a_tool_draft_calling_a_declared_routine_is_refused(db: AsyncSession) -> None:
    scenario = await ProcedureScenario(db).build()

    # Before discovery sees the routine, `nvl` reads as the built-in sqlglot models.
    allowed = await _draft(db, scenario, slug="order_amounts_before")
    assert allowed is not None

    routine = scenario.routine(body="SELECT 1", name="nvl")
    db.add(routine)
    await db.flush()

    with pytest.raises(HTTPException) as refused:
        await _draft(db, scenario, slug="order_amounts_after")

    assert refused.value.status_code == 422
    assert "UNAUTHORIZED_FUNCTION:nvl" in str(refused.value.detail)


async def test_an_operator_may_authorize_a_routine_by_name(db: AsyncSession) -> None:
    scenario = await ProcedureScenario(db).build()
    routine = scenario.routine(body="SELECT 1", name="nvl")
    db.add(routine)
    await db.flush()

    created = await create_tool_version(
        scenario.project.id,
        GovernedToolVersionCreate(
            slug="order_amounts_authorized",
            name="Order amounts",
            description="Reads order amounts, defaulting the missing ones.",
            datasource_id=scenario.datasource.id,
            sql_template=_CALLS_A_ROUTINE,
            parameters=[],
            allowed_roles=["Analyst"],
        ),
        context=scenario.maker(),
        session=db,
        settings=Settings(sql_guard_allowed_functions=["nvl"]),
    )

    assert created is not None
