"""R11-FP07: against real PostgreSQL and SQL Server, a nested call is read through.

The footprint sample pack defines `nested_revenue`, which only calls another routine: on PostgreSQL
`CALL refresh_totals()`, on SQL Server `EXEC read_revenue`. Its own body carries no lineage, so
until the call was read through it was a gap. This discovers the pack's routines through the real
ingestion, then parses `nested_revenue`'s stored body and reads the call through the routine the
scan captured. Each engine is skipped when its server is not reachable.
"""

from __future__ import annotations

from sqlalchemy import select

from aida.envelope_models import MetadataRoutine
from aida.lineage_agent import CAPABILITY_PROCEDURE_LINEAGE, run_lineage_agent
from aida.procedure_lineage import UNPARSED_TRANSFORMATION_TYPE, parse_procedure_lineage
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.routine_call_descent import descend_routine_calls
from aida.routine_lineage_edges import require_eligible_routine_body
from aida.task_agent import ACTION_PROPOSED, TaskAgentRunRequest
from tests.support.task_agents import (
    agent_settings,
    human,
    register_agent,
    seed_estate,
    task_agent_session,
)
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    FIXTURES,
    JourneySource,
    _postgres,
    _scan,
    _sqlserver,
    source,
)

#: The routine `nested_revenue` calls, by engine.
CALLED = {"postgres": "refresh_totals", "sqlserver": "read_revenue"}


async def test_the_sample_nested_procedure_is_read_through_the_routine_it_calls(
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    engine = source.connector_type
    for fixture in ("read.sql", "refresh.sql", "nested.sql"):
        await source.execute((FIXTURES / engine / fixture).read_text("utf-8"))

    async with task_agent_session() as session:
        _org, datasource, _ = await seed_estate(session, dialect=source.dialect)
        datasource.connector_type = engine
        await session.flush()
        await _scan(session, datasource, source)
        nested = await session.scalar(
            select(MetadataRoutine).where(
                MetadataRoutine.datasource_id == datasource.id,
                MetadataRoutine.name == "nested_revenue",
                MetadataRoutine.status == "ACTIVE",
            )
        )
        assert nested is not None
        parsed = parse_procedure_lineage(
            require_eligible_routine_body(nested), dialect=datasource.dialect
        )
        result = await descend_routine_calls(session, datasource, nested, parsed)

    assert any(edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE for edge in parsed.edges), (
        "the caller's own body is only a call"
    )
    gaps = [
        edge.unparsed_reason
        for edge in result.edges
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]
    assert gaps == [], gaps
    assert result.is_fully_parsed
    via = {(edge.via_routine or "").lower() for edge in result.edges}
    assert via == {f"footprint_context_sample.{CALLED[engine]}"}, via
    if engine == "postgres":
        assert (
            "orders",
            "discount",
            "footprint_context_sample.customer_totals",
            "net_revenue",
        ) in {
            (
                edge.source_table.rsplit(".", 1)[-1],
                edge.source_column,
                edge.target_table,
                edge.target_column,
            )
            for edge in result.edges
        }


#: A procedure of the test's own, reading the sample pack's table-valued function.
READS_FUNCTION = {
    "postgres": (
        "CREATE PROCEDURE footprint_context_sample.load_from_function() AS $$ BEGIN "
        "INSERT INTO footprint_context_sample.customer_totals (customer_id, net_revenue) "
        "SELECT n.customer_id, n.net_revenue "
        "FROM footprint_context_sample.customer_net(1) n; END; $$ LANGUAGE plpgsql;"
    ),
    "sqlserver": (
        "CREATE PROCEDURE footprint_context_sample.load_from_function AS BEGIN "
        "INSERT INTO footprint_context_sample.customer_totals (customer_id, net_revenue) "
        "SELECT n.customer_id, n.net_revenue "
        "FROM footprint_context_sample.customer_net(1) n; END"
    ),
}


async def test_a_table_function_read_is_named_and_read_through_where_it_resolves(
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """PostgreSQL's sample function is overloaded, so it stays an honest gap; SQL Server's one
    inline function resolves and its own source reaches what the caller writes."""
    engine = source.connector_type
    await source.execute((FIXTURES / engine / "function.sql").read_text("utf-8"))
    await source.execute(READS_FUNCTION[engine])

    async with task_agent_session() as session:
        org, datasource, _ = await seed_estate(session, dialect=source.dialect)
        datasource.connector_type = engine
        await session.flush()
        await _scan(session, datasource, source)
        caller = await session.scalar(
            select(MetadataRoutine).where(
                MetadataRoutine.datasource_id == datasource.id,
                MetadataRoutine.name == "load_from_function",
                MetadataRoutine.status == "ACTIVE",
            )
        )
        assert caller is not None
        result = await descend_routine_calls(
            session,
            datasource,
            caller,
            parse_procedure_lineage(
                require_eligible_routine_body(caller), dialect=datasource.dialect
            ),
        )

        # R11-FP07: the lineage agent, run against this live estate, proposes what it read.
        await register_agent(session, org, principal="agent:lineage")
        outcome = await run_lineage_agent(
            session,
            org.id,
            request=TaskAgentRunRequest(
                capabilities=(CAPABILITY_PROCEDURE_LINEAGE,), datasource_id=datasource.id
            ),
            settings=agent_settings(),
            triggered_by=human(org),
        )
        proposed = [item for item in outcome.items if item.action == ACTION_PROPOSED]
        edge_count = len((await session.scalars(select(DeepProcedureLineageEdge))).all())

    assert proposed and edge_count > 0, outcome.items
    assert all(edge.source_table != "footprint_context_sample" for edge in result.edges), (
        "a function's schema is never stated as the table it reads"
    )
    gaps = [
        edge.unparsed_reason
        for edge in result.edges
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]
    if engine == "postgres":
        # Two overloads of `customer_net` are captured, so the call cannot be resolved to one.
        assert gaps == ["TABLE_FUNCTION_READ: footprint_context_sample.customer_net (AMBIGUOUS)"]
        return
    assert gaps == []
    assert ("footprint_context_sample.customer_revenue", "net_revenue") in {
        (edge.source_table, edge.source_column)
        for edge in result.edges
        if edge.target_table == "footprint_context_sample.customer_totals"
    }
