"""R11-FP01: `trigger_parse_coverage` has a read route, and it answers only for its own scope.

The coverage row existed from 2026-09-17 with nothing to read it but the gap register. This
proves the route: a trigger the lineage agent parsed reports how completely its body was
understood -- and, on PostgreSQL, which function that body was read from -- while a trigger no
parse has measured is "not measured" (404), not a zeroed answer, and a trigger reached through
another datasource is not found. The reader-role gate is the router's `require_roles`
dependency, which these direct calls bypass; `tests/test_inv4_authorization_wiring.py` is what
holds the route to it.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.procedure_lineage_api import get_trigger_parse_coverage
from tests.support.task_agents import human, seed_estate, task_agent_session
from tests.test_trigger_lineage import _pg_trigger
from tests.test_trigger_lineage_decidable import _pg_estate
from tests.test_trigger_lineage_decidable import _run as _run_triggers

READER = frozenset({"Analyst"})


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


async def test_a_parsed_trigger_reports_its_coverage_and_the_function_it_was_read_from(
    session: AsyncSession,
) -> None:
    org, datasource, _schema, function, trigger = await _pg_estate(session)
    await _run_triggers(session, org)

    read = await get_trigger_parse_coverage(
        datasource.id, trigger.id, human(org, roles=READER), session
    )

    assert read.trigger_id == trigger.id
    assert read.routine_id == function.id
    assert read.statement_count >= 1
    assert read.parse_completed is True
    assert read.state


async def test_a_trigger_no_parse_has_measured_is_not_measured(session: AsyncSession) -> None:
    org, datasource, _schema, _function, trigger = await _pg_estate(session)
    with pytest.raises(HTTPException) as refused:
        await get_trigger_parse_coverage(
            datasource.id, trigger.id, human(org, roles=READER), session
        )
    assert refused.value.status_code == 404
    assert "measured" in str(refused.value.detail)


async def test_a_trigger_of_another_datasource_is_not_found_through_this_one(
    session: AsyncSession,
) -> None:
    org, datasource, _schema, _function, trigger = await _pg_estate(session)
    await _run_triggers(session, org)
    _other_org, other, other_schema = await seed_estate(session, organization=org)
    stranger = _pg_trigger(org, other, other_schema)
    session.add(stranger)
    await session.flush()

    with pytest.raises(HTTPException) as refused:
        await get_trigger_parse_coverage(
            other.id, trigger.id, human(org, roles=READER), session
        )
    assert refused.value.status_code == 404
    with pytest.raises(HTTPException):
        await get_trigger_parse_coverage(
            datasource.id, uuid4(), human(org, roles=READER), session
        )
