"""R11-B19, on real engines: a PII label crosses a real captured stored procedure.

`tests/test_procedure_classification_propagation.py` proves each rule against rows the
real parser and writer produce. This is the same claim with nothing seeded but two
labels: the footprint sample pack's own `refresh_totals`, which goes through a temp
table on both engines (`CREATE TEMP TABLE footprint_totals` on PostgreSQL,
`SELECT ... INTO #footprint_totals` on SQL Server), created on live PostgreSQL and SQL
Server, scanned by the real connector, and then, per engine:

* **the agent's proposal moves nothing.** The lineage agent reads the body and
  proposes the procedure's end-to-end edges; while they are proposals, a PII label on
  `orders.customer_id` reaches nothing;
* **a person's approval moves exactly what the body moves.** Approved through the one
  decision endpoint, the transitive edge across the temp table carries PII to
  `customer_totals.customer_id` -- derived, never asserted -- and to nothing else;
* **a person's parse stores the plumbing ACTIVE, and it is still not an endpoint.**
  The parse endpoint (default review mode, `auto_active`) writes the fill into the temp
  and the hop out of it as ACTIVE rows. A real table named `footprint_totals` sits in
  the same schema, so those rows are bound to it -- the temp's stored name is bare on
  both engines. Neither the fill nor the hop tags that table or carries its PHI label
  into `customer_totals`, and no carried hop is reported as a gap.

Reuses the footprint journey's private source and `test_trigger_lineage_live`'s
platform, scan and datasource helpers, so it creates no server of its own and skips
with the journey when one is unreachable. Everything it creates on the source is
dropped in a `finally`; the journey fixture then drops the whole database; the
platform side is an in-memory database that ends with the test.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.classification_propagation import collect_propagation_inputs, propagate_for_datasource
from aida.envelope_models import MetadataRoutine
from aida.lineage_agent import CAPABILITY_PROCEDURE_LINEAGE, run_lineage_agent
from aida.models import DataSource, MetadataColumn, Organization
from aida.parsed_lineage_review_api import decide_parsed_lineage_edge
from aida.procedure_lineage_api import parse_deep_procedure_lineage_endpoint
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.schemas import ParsedLineageEdgeDecisionRequest
from aida.task_agent import ACTION_PROPOSED, TaskAgentRunRequest
from tests.support.task_agents import agent_settings, human, register_agent
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    FIXTURES,
    SCHEMA,
    JourneySource,
    _postgres,
    _sqlserver,
    source,
)
from tests.test_trigger_lineage_live import (  # noqa: F401 -- `platform` is a fixture
    AGENT,
    _datasource,
    _scan,
    _table_id,
    platform,
)

PROCEDURE = "refresh_totals"
#: The temp table's stored name on both engines, and the name of the real table this
#: test puts beside it.
NAMESAKE = "footprint_totals"

_NAMESAKE_DDL = {
    "postgres": (
        f"CREATE TABLE {SCHEMA}.{NAMESAKE} (customer_id integer, net_revenue numeric(12,2));"
    ),
    "sqlserver": (
        f"CREATE TABLE {SCHEMA}.{NAMESAKE} (customer_id int, net_revenue numeric(12,2));"
    ),
}

_TEARDOWN = {
    "postgres": (
        f"DROP PROCEDURE IF EXISTS {SCHEMA}.{PROCEDURE}();\n"
        f"DROP TABLE IF EXISTS {SCHEMA}.{NAMESAKE};"
    ),
    "sqlserver": (
        f"IF OBJECT_ID(N'{SCHEMA}.{PROCEDURE}') IS NOT NULL DROP PROCEDURE {SCHEMA}.{PROCEDURE};\n"
        f"IF OBJECT_ID(N'{SCHEMA}.{NAMESAKE}') IS NOT NULL DROP TABLE {SCHEMA}.{NAMESAKE};"
    ),
}


async def _column(
    session: AsyncSession, organization_id: UUID, table_id: object, name: str
) -> MetadataColumn:
    column = await session.scalar(
        select(MetadataColumn).where(
            MetadataColumn.organization_id == organization_id,
            MetadataColumn.table_id == table_id,
            MetadataColumn.name == name,
        )
    )
    assert column is not None, f"the scan created no {name} column"
    return column


async def _label(session: AsyncSession, column: MetadataColumn, classification: str) -> None:
    await session.execute(
        update(MetadataColumn)
        .where(MetadataColumn.id == column.id)
        .values(classification=classification)
    )
    await session.flush()


async def _propagate(session: AsyncSession, datasource: DataSource) -> list[tuple[object, str]]:
    written = await propagate_for_datasource(
        session,
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        created_by="scheduler",
    )
    await session.flush()
    return [(row.column_id, row.classification) for row in written]


async def _routine_rows(
    session: AsyncSession, routine: MetadataRoutine
) -> list[DeepProcedureLineageEdge]:
    return list(
        (
            await session.scalars(
                select(DeepProcedureLineageEdge).where(
                    DeepProcedureLineageEdge.organization_id == routine.organization_id,
                    DeepProcedureLineageEdge.routine_id == routine.id,
                )
            )
        ).all()
    )


async def test_a_real_procedures_reviewed_lineage_carries_pii_to_what_it_writes(
    platform: AsyncSession,  # noqa: F811 -- the imported fixture, used by name
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    engine = source.connector_type
    datasource = await _datasource(platform, source)
    organization = await platform.get(Organization, datasource.organization_id)
    assert organization is not None
    try:
        await source.execute((FIXTURES / engine / "refresh.sql").read_text("utf-8"))
        await source.execute(_NAMESAKE_DDL[engine])
        await _scan(platform, datasource, source)
        await register_agent(platform, organization, principal=AGENT)
        routine = await platform.scalar(
            select(MetadataRoutine).where(
                MetadataRoutine.organization_id == organization.id,
                MetadataRoutine.datasource_id == datasource.id,
                MetadataRoutine.name == PROCEDURE,
                MetadataRoutine.status == "ACTIVE",
            )
        )
        assert routine is not None, "the scan captured no procedure"
        orders_id = await _table_id(platform, datasource, "orders")
        totals_id = await _table_id(platform, datasource, "customer_totals")
        namesake_id = await _table_id(platform, datasource, NAMESAKE)
        assert None not in (orders_id, totals_id, namesake_id)

        # The only two things written into the catalog by hand: a steward's PII label
        # on the source column, and a PHI label on the real table that shares the
        # temp's name -- so a hop read as that table would visibly carry PHI.
        origin = await _column(platform, organization.id, orders_id, "customer_id")
        await _label(platform, origin, "PII")
        namesake_revenue = await _column(platform, organization.id, namesake_id, "net_revenue")
        await _label(platform, namesake_revenue, "PHI")
        namesake_customer = await _column(platform, organization.id, namesake_id, "customer_id")
        target = await _column(platform, organization.id, totals_id, "customer_id")
        target_revenue = await _column(platform, organization.id, totals_id, "net_revenue")

        # --- PROPOSED: the agent's edges move nothing ------------------------
        outcome = await run_lineage_agent(
            platform,
            organization.id,
            request=TaskAgentRunRequest(
                capabilities=(CAPABILITY_PROCEDURE_LINEAGE,), datasource_id=datasource.id
            ),
            settings=agent_settings(),
            triggered_by=human(organization),
        )
        await platform.commit()
        assert (ACTION_PROPOSED, routine.id) in [
            (item.action, item.subject_id) for item in outcome.items
        ], outcome.items
        proposed = await _routine_rows(platform, routine)
        assert proposed and {row.review_status for row in proposed} == {"PROPOSED"}
        [carried] = [
            row
            for row in proposed
            if (row.source_table_id, row.target_table_id) == (orders_id, totals_id)
            and row.target_column.lower() == "customer_id"
        ]
        assert (carried.via_temp_table or "").lower() == NAMESAKE, (
            "the end-to-end edge is the one synthesised across the temp table"
        )
        assert await _propagate(platform, datasource) == []

        # --- a person approves through the one decision endpoint -------------
        decision = ParsedLineageEdgeDecisionRequest(
            edge_type="ROUTINE", decision="APPROVED", reason="the procedure writes totals"
        )
        for row in proposed:
            decided = await decide_parsed_lineage_edge(
                row.id, decision, context=human(organization), session=platform
            )
            assert decided.review_status == "ACTIVE"

        written = await _propagate(platform, datasource)
        assert written == [(target.id, "PII")]
        derived = await platform.scalar(
            select(MetadataColumn.classification).where(MetadataColumn.id == target.id)
        )
        assert derived != "PII", "a derived value must never become asserted"

        # --- a person's parse: the plumbing lands ACTIVE, bound to the namesake --
        # The deployment's review mode, whatever it is here: the assertion below
        # says it landed the plumbing ACTIVE, which is the case under test.
        await parse_deep_procedure_lineage_endpoint(
            datasource.id,
            routine.id,
            context=human(organization, roles=frozenset({"DataSteward"})),
            session=platform,
        )
        await platform.flush()
        parsed = await _routine_rows(platform, routine)
        assert {row.review_status for row in parsed if row.transformation_type != "UNPARSED"} == {
            "ACTIVE"
        }
        # The collision the collector must not fall for, on this engine's real names.
        assert any(
            row.is_intermediate and row.target_table_id == namesake_id for row in parsed
        ), [(r.source_table, r.target_table, r.is_intermediate) for r in parsed]
        assert any(
            not row.is_intermediate
            and row.via_temp_table is None
            and row.source_table_id == namesake_id
            and row.target_table_id == totals_id
            for row in parsed
        ), [(r.source_table, r.target_table, r.via_temp_table) for r in parsed]

        inputs = await collect_propagation_inputs(
            platform, organization_id=organization.id, datasource_id=datasource.id
        )
        assert inputs.gaps == (), inputs.gaps
        written = await _propagate(platform, datasource)
        assert written == [(target.id, "PII")]
        untouched = {namesake_customer.id, namesake_revenue.id, target_revenue.id}
        assert not untouched & {column_id for column_id, _ in written}
    finally:
        await source.execute(_TEARDOWN[engine])
