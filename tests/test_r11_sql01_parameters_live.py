"""R11-SQL01 parameters against real PostgreSQL and SQL Server: bound values reach the engine.

The footprint journey's private sample source, discovered through the real ingestion halves, then
reviewed SQL with typed parameters on each engine: an INTEGER and a NUMBER select customer 1's
known revenue; a changed value at Run is refused without an execution; and a STRING value
carrying an injection attempt -- one that would widen the filter to every customer if it were
spliced into the text -- matches nothing, because the engine compared it as one string. Nothing
of any value is left in the platform database. Each engine is skipped when its server is not
reachable. No model is called.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from aida.models import QueryExecution
from aida.security import SecurityContext
from aida.sql_workspace_api import (
    SqlDraftParameter,
    SqlDraftRequest,
    SqlDraftRunRequest,
    SqlDraftRunResponse,
    create_sql_draft,
    run_sql_draft,
)
from atlas.platform.config import Settings
from atlas.platform.db import Base
from tests.support.task_agents import agent_settings, human, seed_estate, task_agent_session
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    JourneySource,
    _postgres,
    _scan,
    _sqlserver,
    source,
)

REVENUE_SQL = (
    "SELECT r.customer_id, r.net_revenue FROM footprint_context_sample.customer_revenue AS r "
    "WHERE r.customer_id = :customer_id AND r.net_revenue > :floor"
)
REGION_SQL = (
    "SELECT c.customer_id, c.region FROM footprint_context_sample.customers AS c "
    "WHERE c.region = :region AND c.customer_id >= :min_id"
)
#: Spliced into the text this reads every customer (`region = 'East' OR '1'='1'`); bound, it is
#: a region no customer has.
ATTACK = "East' OR '1'='1"
#: Values distinctive enough that finding one in the platform database means it was stored.
FLOOR = 100.25


def _parameters(**values: tuple[str, Any]) -> list[SqlDraftParameter]:
    return [
        SqlDraftParameter(name=name, parameter_type=kind, value=value)
        for name, (kind, value) in values.items()
    ]


async def _validate_and_run(
    session: Any,
    datasource_id: Any,
    analyst: SecurityContext,
    settings: Settings,
    sql: str,
    parameters: list[SqlDraftParameter],
) -> SqlDraftRunResponse:
    before = set(await session.scalars(select(QueryExecution.id)))
    draft = await create_sql_draft(
        datasource_id,
        SqlDraftRequest(sql=sql, parameters=parameters),
        context=analyst,
        session=session,
        settings=settings,
    )
    assert draft.validation is not None and draft.validation.valid, draft.validation
    assert draft.receipt is not None
    assert set(await session.scalars(select(QueryExecution.id))) == before, "validating ran it"
    return await run_sql_draft(
        draft.receipt.id,
        SqlDraftRunRequest(sql=sql, parameters=parameters),
        context=analyst,
        session=session,
        settings=settings,
    )


async def test_typed_parameters_bind_on_the_real_engine(
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DSN", source.dsn)
    settings = agent_settings()
    async with task_agent_session() as session:
        org, datasource, _ = await seed_estate(session, dialect=source.dialect)
        datasource.connector_type = source.connector_type
        await session.flush()
        analyst = human(org, "analyst-1", frozenset({"Analyst"}))
        await _scan(session, datasource, source)

        # INTEGER and NUMBER: customer 1's orders are 100 - 10 and 60 - 0.
        revenue_parameters = _parameters(customer_id=("INTEGER", 1), floor=("NUMBER", FLOOR))
        draft = await create_sql_draft(
            datasource.id,
            SqlDraftRequest(sql=REVENUE_SQL, parameters=revenue_parameters),
            context=analyst,
            session=session,
            settings=settings,
        )
        assert draft.validation is not None and draft.validation.valid, draft.validation
        assert draft.receipt is not None
        executions = set(await session.scalars(select(QueryExecution.id)))

        # Another value is another statement: refused, and nothing reaches the source.
        with pytest.raises(HTTPException) as changed:
            await run_sql_draft(
                draft.receipt.id,
                SqlDraftRunRequest(
                    sql=REVENUE_SQL,
                    parameters=_parameters(customer_id=("INTEGER", 2), floor=("NUMBER", FLOOR)),
                ),
                context=analyst,
                session=session,
                settings=settings,
            )
        assert changed.value.status_code == 409
        assert changed.value.detail["code"] == "REVALIDATION_REQUIRED"
        assert set(await session.scalars(select(QueryExecution.id))) == executions

        ran = await run_sql_draft(
            draft.receipt.id,
            SqlDraftRunRequest(sql=REVENUE_SQL, parameters=revenue_parameters),
            context=analyst,
            session=session,
            settings=settings,
        )
        assert ran.execution.row_count == 1
        assert int(ran.execution.rows[0]["customer_id"]) == 1
        assert float(ran.execution.rows[0]["net_revenue"]) == 150.0

        # STRING: the real region selects its customer; the injection attempt selects nothing.
        east = await _validate_and_run(
            session,
            datasource.id,
            analyst,
            settings,
            REGION_SQL,
            _parameters(region=("STRING", "East"), min_id=("INTEGER", 1)),
        )
        assert [int(row["customer_id"]) for row in east.execution.rows] == [1]
        attacked = await _validate_and_run(
            session,
            datasource.id,
            analyst,
            settings,
            REGION_SQL,
            _parameters(region=("STRING", ATTACK), min_id=("INTEGER", 1)),
        )
        assert attacked.execution.row_count == 0, attacked.execution.rows

        # INV-6: the platform kept none of the values it bound.
        stored: list[str] = []
        for table in Base.metadata.sorted_tables:
            stored.extend(repr(tuple(row)) for row in (await session.execute(select(table))).all())
        everything = "\n".join(stored)
        assert "OR '1'='1" not in everything and "OR ''1''=''1" not in everything
        assert str(FLOOR) not in everything
        for response in (ran, east, attacked):
            assert "OR '1'" not in response.model_dump_json()
