"""R11-FP07: a nested call to a captured routine is read through, within bounds.

These tests pin:

* a call to a routine that resolves contributes that routine's edges at the call site, marked
  `via_routine` and at most PARTIAL, and the gap goes when the callee was fully parsed;
* a call that cannot be read through keeps its gap and says why: not captured, a cycle, the depth
  limit, or a callee that was not fully parsed itself;
* on PostgreSQL a callee's result set stays local to the caller; on SQL Server `EXEC` streams it;
* a parse with no nested call is returned untouched;
* the lineage agent reads a captured procedure's call through and proposes the callee's edges.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida import routine_call_descent
from aida.procedure_lineage import (
    PROCEDURE_LOCAL_TARGET,
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureParseResult,
    parse_procedure_lineage,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_call_descent import (
    CALLEE_CYCLE,
    CALLEE_DEPTH_LIMIT,
    CALLEE_NOT_CAPTURED,
    CALLEE_NOT_FULLY_PARSED,
    Callee,
    Resolver,
    callee_name,
    descend_nested_calls,
)
from aida.sql_lineage_parser import PROCEDURE_RESULT_TARGET
from tests.support.task_agents import register_agent, task_agent_session
from tests.test_lineage_agent import AGENT, _procedure_estate, _routine, _run


def _plpgsql(name: str, body: str, *, function: bool = False) -> str:
    kind = "FUNCTION" if function else "PROCEDURE"
    returns = " RETURNS TABLE (customer_id integer)" if function else ""
    return f"CREATE {kind} s.{name}(){returns} AS $$ BEGIN {body} END; $$ LANGUAGE plpgsql;"


REFRESH = _plpgsql(
    "refresh_totals",
    "INSERT INTO s.customer_totals (customer_id, net_revenue) "
    "SELECT o.customer_id, o.amount FROM s.orders o;",
)


def _resolver(bodies: dict[str, str]) -> Resolver:
    def resolve(name: str) -> Callee:
        key = name.lower()
        if key not in bodies:
            return Callee(None, None, None, CALLEE_NOT_CAPTURED)
        return Callee(key, key, bodies[key])

    return resolve


def _descend(sql: str, bodies: dict[str, str], dialect: str = "postgres") -> ProcedureParseResult:
    return descend_nested_calls(
        parse_procedure_lineage(sql, dialect=dialect),
        dialect=dialect,
        resolve=_resolver(bodies),
        root_key="root",
    )


def _gaps(result: ProcedureParseResult) -> list[str | None]:
    return [
        edge.unparsed_reason
        for edge in result.edges
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]


def test_a_call_to_a_resolved_routine_is_read_through_at_the_call_site() -> None:
    caller = _plpgsql("nightly", "CALL s.refresh_totals();")
    before = parse_procedure_lineage(caller, dialect="postgres")
    assert [callee_name(edge) for edge in before.edges] == ["s.refresh_totals"]

    result = _descend(caller, {"s.refresh_totals": REFRESH})

    assert _gaps(result) == []
    assert (result.is_fully_parsed, result.is_read_only, result.confidence) == (
        True,
        False,
        "PARTIAL",
    )
    read_through = {
        (edge.source_column, edge.target_table, edge.target_column, edge.via_routine)
        for edge in result.edges
    }
    assert ("amount", "s.customer_totals", "net_revenue", "s.refresh_totals") in read_through
    assert {edge.statement_ordinal for edge in result.edges} == {before.edges[0].statement_ordinal}


def test_a_call_that_cannot_be_read_through_keeps_its_gap_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = _descend(_plpgsql("nightly", "CALL s.not_here();"), {})
    assert _gaps(missing) == [f"NESTED_PROCEDURE_CALL: s.not_here ({CALLEE_NOT_CAPTURED})"]
    assert not missing.is_fully_parsed

    self_calling = _plpgsql("a", "CALL s.a();")
    cycle = descend_nested_calls(
        parse_procedure_lineage(self_calling, dialect="postgres"),
        dialect="postgres",
        resolve=_resolver({"s.a": self_calling}),
        root_key="s.a",
    )
    assert _gaps(cycle) == [f"NESTED_PROCEDURE_CALL: s.a ({CALLEE_CYCLE})"]

    # p0 -> p1 -> p2 -> p3 -> p4, and p4 writes. From p0 the chain is deeper than MAX_DEPTH, so
    # nothing from p4 arrives and the gap stays, naming the callee that could not be accounted for.
    chain = {f"s.p{index}": _plpgsql(f"p{index}", f"CALL s.p{index + 1}();") for index in range(4)}
    chain["s.p4"] = REFRESH
    deep = _descend(_plpgsql("root", "CALL s.p0();"), chain)
    assert _gaps(deep) == [f"NESTED_PROCEDURE_CALL: s.p0 ({CALLEE_NOT_FULLY_PARSED})"]
    assert not any(edge.via_routine for edge in deep.edges)
    shallow = _descend(_plpgsql("root", "CALL s.p3();"), chain)
    assert shallow.is_fully_parsed and {edge.via_routine for edge in shallow.edges} == {"s.p3"}

    monkeypatch.setattr(routine_call_descent, "MAX_DEPTH", 0)
    limited = _descend(_plpgsql("root", "CALL s.p3();"), chain)
    assert _gaps(limited) == [f"NESTED_PROCEDURE_CALL: s.p3 ({CALLEE_DEPTH_LIMIT})"]


def test_a_result_set_stays_local_on_postgres_and_streams_to_the_caller_on_sqlserver() -> None:
    revenue = _plpgsql(
        "revenue", "RETURN QUERY SELECT o.customer_id FROM s.orders o;", function=True
    )
    local = _descend(_plpgsql("nightly", "PERFORM s.revenue();"), {"s.revenue": revenue})
    assert _gaps(local) == []
    assert {(edge.target_table, edge.is_intermediate) for edge in local.edges} == {
        (PROCEDURE_LOCAL_TARGET, True)
    }

    streamed = _descend(
        "CREATE PROCEDURE s.nightly AS BEGIN EXEC s.read_revenue; END",
        {
            "s.read_revenue": (
                "CREATE PROCEDURE s.read_revenue AS BEGIN "
                "SELECT r.customer_id FROM s.customer_revenue r; END"
            )
        },
        dialect="tsql",
    )
    assert _gaps(streamed) == [] and streamed.is_read_only
    assert {(edge.target_table, edge.via_routine) for edge in streamed.edges} == {
        (PROCEDURE_RESULT_TARGET, "s.read_revenue")
    }


def test_a_parse_without_a_nested_call_is_returned_untouched() -> None:
    parsed = parse_procedure_lineage(REFRESH, dialect="postgres")
    assert (
        descend_nested_calls(parsed, dialect="postgres", resolve=_resolver({}), root_key="r")
        is parsed
    )


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


async def test_the_lineage_agent_reads_a_call_through_and_proposes_the_callee_edges(
    session: AsyncSession,
) -> None:
    org, datasource, schema, _orders, _totals = await _procedure_estate(session)
    await _routine(
        session,
        org,
        datasource,
        schema,
        name="load_totals",
        body=(
            "CREATE PROCEDURE public.load_totals AS BEGIN "
            "INSERT INTO public.order_totals (customer_id, total) "
            "SELECT o.customer_id, o.amount FROM public.orders o; END"
        ),
    )
    nightly = await _routine(
        session,
        org,
        datasource,
        schema,
        name="nightly",
        body="CREATE PROCEDURE public.nightly AS BEGIN EXEC public.load_totals; END",
    )
    await register_agent(session, org, principal=AGENT)

    await _run(session, org)

    edges = (
        await session.scalars(
            select(DeepProcedureLineageEdge).where(
                DeepProcedureLineageEdge.routine_id == nightly.id
            )
        )
    ).all()
    assert {
        (edge.source_column, edge.target_column, edge.via_routine, edge.review_status)
        for edge in edges
    } == {
        ("customer_id", "customer_id", "public.load_totals", "PROPOSED"),
        ("amount", "total", "public.load_totals", "PROPOSED"),
    }
