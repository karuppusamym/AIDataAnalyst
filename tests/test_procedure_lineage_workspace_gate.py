"""R11-D30: the routine and trigger lineage routes ask the datasource's workspace gate.

`POST .../procedures/{routine_id}/lineage/parse`, `GET .../procedures/{routine_id}/lineage`,
`GET .../procedures/{routine_id}/parse-coverage` and `GET .../triggers/{trigger_id}/parse-coverage`
checked the caller's roles and tenant only. Every catalog read of the same datasource asks the
workspace gate (`READ_METADATA` on the datasource), so in an organization with an enforcing
workspace a caller refused a datasource's tables could read which tables its routines and
triggers read and write -- the gap R11-D28 closed for the unified-lineage surfaces.

These run each route against the R11-D28 estate: a datasource bound to an enforcing workspace
the default caller is not a member of. The ids asked for do not exist, on purpose: a refused
caller must get the gate's 403 *before* any routine or trigger is looked up, so a refusal says
nothing about what exists; a member gets past the gate to the route's own 404; and a datasource
with no workspace reads as before.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from aida.procedure_lineage_api import (
    get_routine_parse_coverage,
    get_trigger_parse_coverage,
    list_deep_procedure_lineage,
    parse_deep_procedure_lineage_endpoint,
)
from aida.security_types import SecurityContext
from tests import test_graphql_lineage as lineage
from tests.test_graphql_lineage import Estate
from tests.test_lineage_workspace_gate import MEMBER, _admit_member, _caller

estate = lineage.estate

SURFACES = ("parse", "lineage", "routine_coverage", "trigger_coverage")


async def _call(surface: str, estate: Estate, context: SecurityContext, datasource_id: UUID) -> Any:
    subject = uuid4()
    if surface == "parse":
        return await parse_deep_procedure_lineage_endpoint(
            datasource_id, subject, context=context, session=estate.db
        )
    if surface == "lineage":
        return await list_deep_procedure_lineage(
            datasource_id, subject, limit=200, offset=0, context=context, session=estate.db
        )
    if surface == "routine_coverage":
        return await get_routine_parse_coverage(
            datasource_id, subject, context=context, session=estate.db
        )
    return await get_trigger_parse_coverage(
        datasource_id, subject, context=context, session=estate.db
    )


#: What each route answers once the gate admits the caller, for an id that does not exist: the
#: listing is an empty page, the others a 404 naming the missing routine, trigger or coverage.
_PAST_THE_GATE = {
    "parse": 404,
    "lineage": "EMPTY",
    "routine_coverage": 404,
    "trigger_coverage": 404,
}


async def _answer(
    surface: str, estate: Estate, context: SecurityContext, datasource_id: UUID
) -> int | str:
    try:
        served = await _call(surface, estate, context, datasource_id)
    except HTTPException as refused:
        return refused.status_code
    return "EMPTY" if served == [] else "SERVED"


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_route_refuses_a_datasource_the_callers_workspace_refuses(
    estate: Estate, surface: str
) -> None:
    with pytest.raises(HTTPException) as refused:
        await _call(surface, estate, _caller(estate), estate.closed_ds.id)

    # The gate's own reason, before the (nonexistent) routine or trigger was looked up.
    assert (refused.value.status_code, refused.value.detail) == (403, "NO_WORKSPACE_MEMBERSHIP")


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_member_of_that_workspace_gets_past_the_gate(
    estate: Estate, surface: str
) -> None:
    await _admit_member(estate)

    # Past the gate, to the route's own answer about an object that does not exist.
    assert await _answer(surface, estate, _caller(estate, MEMBER), estate.closed_ds.id) == (
        _PAST_THE_GATE[surface]
    )


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_datasource_with_no_workspace_reads_as_before(estate: Estate, surface: str) -> None:
    assert await _answer(surface, estate, _caller(estate), estate.open_ds.id) == (
        _PAST_THE_GATE[surface]
    )
