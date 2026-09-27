"""R11-FP01: a real trigger's write edge, read off a real engine.

`tests/test_triggers_sequences_persist_live.py` proves a trigger reaches a row and
is reconciled. This is the half after it: **create a trigger that genuinely writes
a second table, scan, and assert the stored lineage edge says that write comes out
of the table the trigger fires on.**

Why a unit test cannot stand in for it. Everything between the source and the
stored edge is engine detail a fake gets to choose, and two of those choices are
the whole feature:

* PostgreSQL has **no trigger body at all**. The code is in the function
  `pg_trigger` points at, `pg_get_functiondef` returns it with whatever
  dollar-quote tag it likes, and the two objects arrive on two different discovery
  axes. Whether the join finds the function depends on how the real engine spells
  the name it puts in `action_routine` and how identifier case folds -- a fake
  hands back whatever the test wrote.
* The firing table is named **nowhere in either body**. PostgreSQL says
  `NEW.customer_id`, SQL Server says `FROM inserted i`, and only the catalog knows
  that both mean `orders`. If the binding is wrong the parse still succeeds and
  still produces an edge; it just points at the wrong table, or at nothing. That is
  a failure a green unit suite is entirely compatible with, and a real scan is
  what distinguishes them.

The second test is the half after *that* (2026-09-17): the edge is only worth
anything once a person can decide it and a decided one steers. **Real trigger ->
scan -> the lineage agent proposes -> the edge is in the review queue and absent
from the graph -> the agent is refused as its own reviewer -> a person approves it
through the one decision endpoint -> it is ACTIVE, in the unified graph, and
impact from the firing table reaches the table the trigger writes.** On
PostgreSQL it goes one step further, into the defect a fake could never show:
`CREATE OR REPLACE FUNCTION` on the real engine, rescan, and the trigger row is
byte-for-byte unchanged -- only the routine's change signal moved -- yet the agent
re-examines the trigger and proposes what the new body writes.

Each engine's private source is the footprint journey's, reused by importing its
fixtures, so this file creates no server of its own and skips with the journey when
one is unreachable. Every object it creates is dropped in a `finally`, and the
journey fixture drops the whole database behind it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import aida.envelope_models  # noqa: F401 -- registers the 1.1 tables on the metadata
import aida.models  # noqa: F401 -- registers every 1.0 table on the metadata
from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import CHANGE_STRUCTURAL, SIGNAL_DEFINITION_CHANGED
from aida.discovery_receipt import FACET_SEQUENCES, FACET_TRIGGERS
from aida.envelope_models import MetadataRoutine, MetadataTrigger
from aida.ingestion import persist_envelope_extensions
from aida.lineage_agent import CAPABILITY_TRIGGER_LINEAGE, run_lineage_agent
from aida.models import (
    AnalysisRun,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataTable,
    Organization,
    Project,
)
from aida.parsed_lineage_review_api import decide_parsed_lineage_edge
from aida.parsed_lineage_review_service import list_parsed_lineage_review_queue
from aida.procedure_lineage import UNPARSED_TRANSFORMATION_TYPE, parse_trigger_lineage
from aida.procedure_lineage_models import TriggerLineageEdge, TriggerParseCoverage
from aida.routine_lineage_edges import persist_trigger_edges, trigger_body
from aida.schemas import ParsedLineageEdgeDecisionRequest
from aida.task_agent import ACTION_PROPOSED, TaskAgentRunRequest
from aida.unified_lineage_api import (
    build_unified_lineage_graph_payload,
    build_unified_lineage_impact_payload,
)
from aida.workflows.activities import persist_discovery_snapshot
from tests.support.task_agents import (
    agent_settings,
    human,
    register_agent,
    task_agent_session,
)
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    CONNECTORS,
    SCHEMA,
    JourneySource,
    _postgres,
    _sqlserver,
    source,
)

AGENT = "agent:lineage"
#: The table the trigger writes. Named with its own prefix so nothing here can be
#: confused with the sample pack's objects or with the other live files'.
AUDIT = "trigger_lineage_probe_audit"
TRIGGER = "trigger_lineage_probe_trg"
FUNCTION = "trigger_lineage_probe_note"

#: A trigger that writes a second table from the firing row, per engine. The
#: written column is `customer_id` and the *only* place it can have come from is
#: `orders.customer_id`, which is the edge under test.
_CREATE = {
    "postgres": f"""
        CREATE TABLE {SCHEMA}.{AUDIT} (
            audit_id bigserial PRIMARY KEY,
            customer_id integer
        );
        CREATE FUNCTION {SCHEMA}.{FUNCTION}() RETURNS trigger
            LANGUAGE plpgsql AS $body$
        BEGIN
            INSERT INTO {SCHEMA}.{AUDIT} (customer_id) VALUES (NEW.customer_id);
            RETURN NEW;
        END;
        $body$;
        CREATE TRIGGER {TRIGGER}
            AFTER INSERT OR UPDATE ON {SCHEMA}.orders
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{FUNCTION}();
    """,  # noqa: S608 -- DDL for a private scratch database built by this test; the only interpolation is this module's own constants
    "sqlserver": f"""
        CREATE TABLE {SCHEMA}.{AUDIT} (
            audit_id bigint IDENTITY(1, 1) PRIMARY KEY,
            customer_id int
        );
        GO
        CREATE TRIGGER {TRIGGER} ON {SCHEMA}.orders
        AFTER INSERT, UPDATE AS
        BEGIN
            SET NOCOUNT ON;
            INSERT INTO {SCHEMA}.{AUDIT} (customer_id)
            SELECT i.customer_id FROM inserted i;
        END;
    """,  # noqa: S608 -- DDL for a private scratch database built by this test; the only interpolation is this module's own constants
}

#: A second table the redefined PostgreSQL function writes as well (second test).
HISTORY = "trigger_lineage_probe_history"

#: `CREATE OR REPLACE FUNCTION` on the real engine: the trigger is not touched, and
#: on PostgreSQL that leaves `pg_trigger` -- and so the trigger's row -- as it was.
_REDEFINE_FUNCTION = f"""
    CREATE TABLE {SCHEMA}.{HISTORY} (
        history_id bigserial PRIMARY KEY,
        customer_id integer
    );
    CREATE OR REPLACE FUNCTION {SCHEMA}.{FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $body$
    BEGIN
        INSERT INTO {SCHEMA}.{AUDIT} (customer_id) VALUES (NEW.customer_id);
        INSERT INTO {SCHEMA}.{HISTORY} (customer_id) VALUES (NEW.customer_id);
        RETURN NEW;
    END;
    $body$;
"""  # noqa: S608 -- DDL for a private scratch database built by this test; the only interpolation is this module's own constants

_TEARDOWN = {
    "postgres": (
        f"DROP TRIGGER IF EXISTS {TRIGGER} ON {SCHEMA}.orders;\n"
        f"DROP FUNCTION IF EXISTS {SCHEMA}.{FUNCTION}();\n"
        f"DROP TABLE IF EXISTS {SCHEMA}.{AUDIT};\n"
        f"DROP TABLE IF EXISTS {SCHEMA}.{HISTORY};"
    ),
    "sqlserver": (
        f"IF OBJECT_ID(N'{SCHEMA}.{TRIGGER}') IS NOT NULL DROP TRIGGER {SCHEMA}.{TRIGGER};\n"
        f"IF OBJECT_ID(N'{SCHEMA}.{AUDIT}') IS NOT NULL DROP TABLE {SCHEMA}.{AUDIT};"
    ),
}


@pytest_asyncio.fixture
async def platform() -> AsyncIterator[AsyncSession]:
    """The control plane the rows land in -- in-memory SQLite, as the 1.1 axis
    tests use. The source under test is a real server; the platform side of a
    discovery run is engine-agnostic and needs no second one.

    The task-agent harness's database, because the second test runs the real
    lineage agent, whose per-item savepoints need SQLite to BEGIN the way
    PostgreSQL does (`tests.support.task_agents.task_agent_maker`)."""
    async with task_agent_session() as active:
        yield active


async def _datasource(session: AsyncSession, source: JourneySource) -> DataSource:  # noqa: F811 -- a local parameter, not the imported fixture
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    lob = LineOfBusiness(
        organization_id=organization.id, name="Retail", code=f"RTL{uuid4().hex[:4]}"
    )
    session.add(lob)
    await session.flush()
    domain = DataDomain(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        name="Deposits",
        code=f"DEP{uuid4().hex[:4]}",
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    datasource = DataSource(
        organization_id=organization.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="Footprint sample",
        connector_type=source.connector_type,
        # The real dialect: it selects the redaction parser for the trigger body
        # and, here, the firing-row vocabulary the parse binds.
        dialect=source.dialect,
        environment="TEST",
        credential_reference="env://TEST_DSN",
        status="ACTIVE",
    )
    session.add(datasource)
    await session.flush()
    return datasource


async def _scan(
    session: AsyncSession,
    datasource: DataSource,
    source: JourneySource,  # noqa: F811 -- a local parameter, not the imported fixture
) -> None:
    """One real discovery of the sample schema, persisted through the real halves."""
    connector = CONNECTORS[source.connector_type](source.dsn)
    catalogs = await connector.discover()
    selected = tuple(
        replace(catalog, schemas=tuple(s for s in catalog.schemas if s.name == SCHEMA))
        for catalog in catalogs
    )
    native_axes_read = frozenset(
        facet
        for facet in (FACET_TRIGGERS, FACET_SEQUENCES)
        if getattr(connector.capabilities, facet, False)
    )
    run = AnalysisRun(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
        status="RUNNING",
    )
    session.add(run)
    await session.flush()
    await persist_discovery_snapshot(
        session, run, datasource, selected, deprecate_missing=True, connector_capabilities={}
    )
    await persist_envelope_extensions(
        session,
        datasource,
        selected,
        deprecate_missing=True,
        analysis_run_id=run.id,
        native_axes_read=native_axes_read,
    )
    run.status = "COMPLETED"
    await session.commit()


async def _table_id(session: AsyncSession, datasource: DataSource, name: str) -> object:
    return await session.scalar(
        select(MetadataTable.id).where(
            MetadataTable.organization_id == datasource.organization_id,
            MetadataTable.datasource_id == datasource.id,
            MetadataTable.name == name,
        )
    )


async def test_a_real_triggers_write_is_a_path_out_of_the_table_it_fires_on(
    platform: AsyncSession,
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """The claim, in one sequence of real events per engine.

    create a trigger that writes a second table -> scan -> parse the body the
    engine really gave us -> the stored edge says `orders` wrote the audit table,
    with both sides resolved to real catalog ids.
    """
    datasource = await _datasource(platform, source)
    try:
        await source.execute(_CREATE[source.connector_type])

        await _scan(platform, datasource, source)

        trigger = await platform.scalar(
            select(MetadataTrigger).where(
                MetadataTrigger.organization_id == datasource.organization_id,
                MetadataTrigger.datasource_id == datasource.id,
                MetadataTrigger.name == TRIGGER,
            )
        )
        assert trigger is not None, "the discovered trigger reached no row"

        # --- the body, wherever this engine keeps it ------------------------
        body = await trigger_body(platform, datasource, trigger)
        assert body.unresolved_reason is None, body.unresolved_reason
        assert body.sql is not None
        assert body.firing_table.lower().endswith("orders")
        if source.connector_type == "postgres":
            # PostgreSQL keeps no trigger body: the join to `action_routine` is the
            # only way there is any lineage here at all, and it must land on the
            # function the real engine named.
            assert trigger.body_sql_redacted is None
            assert body.routine_id is not None
            assert body.via_routine is not None
            assert body.via_routine.lower() == f"{SCHEMA}.{FUNCTION}"
            routine = await platform.get(MetadataRoutine, body.routine_id)
            assert routine is not None and routine.name.lower() == FUNCTION
        else:
            # SQL Server carries its own body, so no routine is involved.
            assert (body.routine_id, body.via_routine) == (None, None)

        # --- the parse, with the firing table bound -------------------------
        result = parse_trigger_lineage(
            body.sql, dialect=datasource.dialect, firing_table=body.firing_table
        )
        written = await persist_trigger_edges(
            platform,
            datasource=datasource,
            trigger=trigger,
            result=result,
            review_mode="require_review",
            threshold=1.0,
            created_by=AGENT,
            routine_id=body.routine_id,
            agent_proposal=True,
        )
        await platform.flush()

        # --- the write edge, out of the firing table ------------------------
        writes = [
            row
            for row in written
            if row.is_write and row.target_table.lower().endswith(AUDIT.lower())
        ]
        assert writes, (
            "the trigger writes a second table on a real engine and no write edge "
            f"was stored; rows={[(r.source_table, r.target_table) for r in written]}"
        )
        [edge] = writes
        assert edge.source_table.lower().endswith("orders"), (
            "the write's source is not the firing table, so the lineage points the "
            f"wrong way: {edge.source_table}"
        )
        assert edge.source_column.lower() == "customer_id"
        assert edge.target_column.lower() == "customer_id"
        assert edge.source_resolved is True
        assert edge.trigger_id == trigger.id
        # Both ends resolved to the catalog rows the same scan created, which is
        # what makes the edge traversable rather than a pair of strings.
        assert edge.source_table_id == await _table_id(platform, datasource, "orders")
        assert edge.target_table_id == await _table_id(platform, datasource, AUDIT)
        # An agent's edge is decided by a person; it steers nothing on arrival.
        assert edge.review_status == "PROPOSED"

        # --- nothing was claimed that was not parsed ------------------------
        markers = [
            row.unparsed_reason
            for row in written
            if row.transformation_type == UNPARSED_TRANSFORMATION_TYPE
        ]
        assert markers == [], f"the body was not fully read: {markers}"

        # --- INV-6: no body text in any stored row --------------------------
        for row in written:
            rendered = " ".join(
                str(getattr(row, column.name, None)) for column in row.__table__.columns
            )
            assert FUNCTION not in rendered or row.via_routine is not None
            assert "INSERT" not in rendered.upper()
            assert "$body$" not in rendered
    finally:
        # Roll back every object this test created, whatever happened above. The
        # journey fixture then drops the database itself, so a failure here cannot
        # leave the sample estate modified.
        await source.execute(_TEARDOWN[source.connector_type])


async def _run_agent(session: AsyncSession, datasource: DataSource) -> list[object]:
    """One proposing run of the real lineage agent, trigger capability only."""
    organization = await session.get(Organization, datasource.organization_id)
    assert organization is not None
    outcome = await run_lineage_agent(
        session,
        organization.id,
        request=TaskAgentRunRequest(capabilities=(CAPABILITY_TRIGGER_LINEAGE,)),
        settings=agent_settings(),
        triggered_by=human(organization),
    )
    await session.commit()
    return [(item.action, item.subject_id) for item in outcome.items]


async def test_a_real_triggers_edge_is_decided_and_then_steers_the_graph(
    platform: AsyncSession,
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """PROPOSED -> one decision -> ACTIVE -> in the graph, per engine; and on
    PostgreSQL, a redefined function re-examines a trigger whose row never moved."""
    datasource = await _datasource(platform, source)
    organization = await platform.get(Organization, datasource.organization_id)
    assert organization is not None
    try:
        await source.execute(_CREATE[source.connector_type])
        await _scan(platform, datasource, source)
        await register_agent(platform, organization, principal=AGENT)
        trigger = await platform.scalar(
            select(MetadataTrigger).where(
                MetadataTrigger.organization_id == datasource.organization_id,
                MetadataTrigger.datasource_id == datasource.id,
                MetadataTrigger.name == TRIGGER,
            )
        )
        assert trigger is not None
        orders_id = await _table_id(platform, datasource, "orders")
        audit_id = await _table_id(platform, datasource, AUDIT)

        # --- PROPOSED: the agent's edge, in the queue and nowhere else --------
        assert await _run_agent(platform, datasource) == [(ACTION_PROPOSED, trigger.id)]
        [edge] = (
            await platform.scalars(
                select(TriggerLineageEdge).where(
                    TriggerLineageEdge.trigger_id == trigger.id,
                    TriggerLineageEdge.target_table_id == audit_id,
                )
            )
        ).all()
        assert (edge.review_status, edge.source_table_id) == ("PROPOSED", orders_id)
        items, _total = await list_parsed_lineage_review_queue(
            platform, datasource.organization_id, edge_type="TRIGGER"
        )
        assert [item.edge_id for item in items] == [edge.id]
        assert items[0].source_sql_reference["firing_table"].lower().endswith(".orders")

        graph = await build_unified_lineage_graph_payload(platform, datasource, settings=None)
        assert [e for e in graph.edges if e.edge_source == "TRIGGER_DEFINITION"] == []

        # --- the maker is refused -------------------------------------------
        decision = ParsedLineageEdgeDecisionRequest(
            edge_type="TRIGGER", decision="APPROVED", reason="the trigger writes audit"
        )
        with pytest.raises(HTTPException) as refused:
            await decide_parsed_lineage_edge(
                edge.id,
                decision,
                context=human(organization, principal_id=AGENT),
                session=platform,
            )
        assert refused.value.status_code == 409

        # --- ACTIVE: one decision, by a person, through the one endpoint -----
        decided = await decide_parsed_lineage_edge(
            edge.id, decision, context=human(organization), session=platform
        )
        assert decided.review_status == "ACTIVE"

        # --- in the graph, and on the impact path ---------------------------
        graph = await build_unified_lineage_graph_payload(platform, datasource, settings=None)
        [folded] = [e for e in graph.edges if e.edge_source == "TRIGGER_DEFINITION"]
        assert (folded.source_node_id, folded.target_node_id) == (str(audit_id), str(orders_id))
        assert folded.status == "ACTIVE"
        assert folded.evidence["trigger_ids"] == [str(trigger.id)]
        impact = await build_unified_lineage_impact_payload(
            platform, datasource, str(orders_id)
        )
        assert str(audit_id) in {node.node_id for node in impact.downstream}

        # --- the measurement ------------------------------------------------
        coverage = await platform.scalar(
            select(TriggerParseCoverage).where(TriggerParseCoverage.trigger_id == trigger.id)
        )
        assert coverage is not None
        assert coverage.parse_completed is True
        assert coverage.unparsed_statement_count == 0
        if source.connector_type != "postgres":
            assert coverage.routine_id is None
            return

        # --- PostgreSQL: the function changes, the trigger row does not ------
        assert coverage.routine_id is not None
        function_id = coverage.routine_id
        trigger_fingerprint, trigger_updated = trigger.fingerprint, trigger.updated_at
        await source.execute(_REDEFINE_FUNCTION)
        await _scan(platform, datasource, source)
        await platform.refresh(trigger)
        assert trigger.fingerprint == trigger_fingerprint
        assert trigger.updated_at.replace(tzinfo=None) == trigger_updated.replace(tzinfo=None), (
            "the real engine rewrote nothing about the trigger itself"
        )
        signal = await platform.scalar(
            select(MetadataChangeSignal).where(
                MetadataChangeSignal.organization_id == datasource.organization_id,
                MetadataChangeSignal.subject_kind == "ROUTINE",
                MetadataChangeSignal.subject_id == function_id,
            )
        )
        assert signal is not None, "the rescan recorded no change to the function"
        assert (signal.signal_type, signal.change_class) == (
            SIGNAL_DEFINITION_CHANGED,
            CHANGE_STRUCTURAL,
        )

        assert await _run_agent(platform, datasource) == [(ACTION_PROPOSED, trigger.id)]
        history_id = await _table_id(platform, datasource, HISTORY)
        rows = (
            await platform.scalars(
                select(TriggerLineageEdge).where(TriggerLineageEdge.trigger_id == trigger.id)
            )
        ).all()
        by_target = {row.target_table_id: row.review_status for row in rows}
        # The decided edge stands; what the new body also writes waits for a person.
        assert by_target == {audit_id: "ACTIVE", history_id: "PROPOSED"}
        # And having read the new body, the agent leaves the trigger alone.
        assert await _run_agent(platform, datasource) == []
    finally:
        await source.execute(_TEARDOWN[source.connector_type])
