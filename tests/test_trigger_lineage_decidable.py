"""R11-FP01, second half: a trigger's lineage edge is something a person can decide,
and once decided it steers.

`tests/test_trigger_lineage.py` proves the parse: a trigger that writes a second
table produces an edge out of the table it fires on. That edge was inert. It landed
PROPOSED -- correctly, an agent's edge is decided by a person -- but the review
queue did not know the table, so nobody could decide it; the unified graph did not
read it, so an approved one would have changed no impact answer anyway; nothing
recorded whether the body had been fully understood; and on PostgreSQL a changed
trigger *function* left the trigger's lineage stale for ever, because only the
trigger row's own rewrite made it eligible again. These tests pin each of the four
closed:

1. **Decidable, by the one path.** A trigger edge is listed, approved, rejected and
   bulk-decided through `parsed_lineage_review_api` -- the endpoint every other
   parsed edge uses -- with its maker-checker, its already-decided refusal, its
   organization check and its audit/outbox trail. An UNPARSED marker is a gap, not
   a proposal, and never reaches a reviewer.
2. **In the graph, only once it is fact.** An approved edge folds into the unified
   graph as `TRIGGER_DEFINITION` and impact analysis traverses it; a PROPOSED one
   is absent by default and labelled PROPOSED on opt-in; a REJECTED one never
   appears; another datasource's rows never join (INV-5).
3. **A coverage record**, `trigger_parse_coverage`, mirroring the routine one.
4. **Re-examined when the function changes**, through the routine's own change
   signal -- and when the function it could not reach is captured later.

Plus the two gap-register consequences (a trigger that writes nothing leaves the
backlog; the drill-down names the triggers its counts count) and the rename merge
that must now carry trigger edges.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, get_args

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import (
    CHANGE_LITERAL_ONLY,
    CHANGE_STRUCTURAL,
    SIGNAL_DEFINITION_CHANGED,
)
from aida.footprint_gap_detail import footprint_gap_objects
from aida.footprint_gaps import footprint_gaps
from aida.identity_merge import merge_table_identity
from aida.lineage_agent import CAPABILITY_TRIGGER_LINEAGE, run_lineage_agent
from aida.models import (
    AuditEvent,
    DataSource,
    MetadataSchema,
    MetadataTable,
    Organization,
    OutboxEvent,
)
from aida.parsed_lineage_review_api import (
    bulk_decide_parsed_lineage_edges,
    decide_parsed_lineage_edge,
    get_parsed_lineage_review_queue,
)
from aida.parsed_lineage_review_service import (
    EDGE_TYPE_TO_MODEL,
    EDGE_TYPES,
    list_parsed_lineage_review_queue,
)
from aida.procedure_lineage import UNPARSED_TRANSFORMATION_TYPE
from aida.procedure_lineage_models import TriggerLineageEdge
from aida.schemas import (
    ParsedLineageEdgeBulkDecisionItem,
    ParsedLineageEdgeBulkDecisionRequest,
    ParsedLineageEdgeDecisionRequest,
    ParsedLineageEdgeType,
)
from aida.task_agent import ACTION_PROPOSED, TaskAgentRunRequest
from aida.unified_lineage_api import (
    build_unified_lineage_graph_payload,
    build_unified_lineage_impact_payload,
)
from tests.support.task_agents import (
    agent_settings,
    human,
    register_agent,
    seed_estate,
    seed_table,
    task_agent_session,
)
from tests.test_trigger_lineage import (
    AGENT,
    TSQL_TRIGGER_BODY,
    _pg_trigger,
    _routine,
    _trigger,
)

REVIEWER = "steward-1"

#: A PostgreSQL trigger function that reads the table it fires on into a local
#: variable before it writes: the local assignment is the body's own plumbing.
PG_FUNCTION_WITH_PLUMBING = """CREATE OR REPLACE FUNCTION public.note_order()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE v_total integer;
BEGIN
    SELECT o.amount INTO v_total FROM public.orders o WHERE o.id = NEW.id;
    INSERT INTO public.audit (customer_id) VALUES (NEW.customer_id);
    RETURN NEW;
END;
$function$
"""

#: The same function, redefined to write a second table as well.
PG_FUNCTION_REDEFINED = """CREATE OR REPLACE FUNCTION public.note_order()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO public.audit (customer_id) VALUES (NEW.customer_id);
    INSERT INTO public.audit_history (customer_id) VALUES (NEW.customer_id);
    RETURN NEW;
END;
$function$
"""

#: A trigger function that touches no other table: fully read, and nothing to propose.
PG_FUNCTION_WRITES_NOTHING = """CREATE OR REPLACE FUNCTION public.note_order()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.customer_id := NEW.customer_id;
    RETURN NEW;
END;
$function$
"""


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


async def _run(session: AsyncSession, org: Organization, **request: Any) -> Any:
    return await run_lineage_agent(
        session,
        org.id,
        request=TaskAgentRunRequest(capabilities=(CAPABILITY_TRIGGER_LINEAGE,), **request),
        settings=agent_settings(),
        triggered_by=human(org),
    )


async def _tsql_estate(
    session: AsyncSession,
) -> tuple[Organization, DataSource, MetadataTable, MetadataTable, Any]:
    """A SQL Server trigger on `orders` that writes `audit`, parsed by the agent."""
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    orders = await seed_table(session, org, datasource, schema, name="orders")
    audit = await seed_table(session, org, datasource, schema, name="audit")
    trigger = _trigger(org, datasource, schema)
    session.add(trigger)
    await session.flush()
    await register_agent(session, org, principal=AGENT)
    return org, datasource, orders, audit, trigger


async def _pg_estate(
    session: AsyncSession, *, body: str | None = None, routine: bool = True
) -> tuple[Organization, DataSource, Any, Any, Any]:
    """A PostgreSQL trigger whose body lives in the function it names."""
    org, datasource, schema = await seed_estate(session)
    for name in ("orders", "audit", "audit_history"):
        await seed_table(session, org, datasource, schema, name=name)
    function = (
        _routine(org, datasource, schema, **({"body": body} if body else {}))
        if routine
        else None
    )
    trigger = _pg_trigger(org, datasource, schema)
    session.add_all([row for row in (function, trigger) if row is not None])
    await session.flush()
    await register_agent(session, org, principal=AGENT)
    return org, datasource, schema, function, trigger


async def _proposed(session: AsyncSession) -> list[TriggerLineageEdge]:
    return list(
        (
            await session.scalars(
                select(TriggerLineageEdge).where(TriggerLineageEdge.review_status == "PROPOSED")
            )
        ).all()
    )


def _decision(decision: str = "APPROVED") -> ParsedLineageEdgeDecisionRequest:
    return ParsedLineageEdgeDecisionRequest(
        edge_type="TRIGGER", decision=decision, reason="the trigger does write this"
    )


def _reviewer(org: Organization, principal: str = REVIEWER) -> Any:
    return human(org, principal_id=principal, roles=frozenset({"DataSteward"}))


# ---------------------------------------------------------------------------
# 1. Decidable -- by the one path every parsed edge takes.
# ---------------------------------------------------------------------------


def test_the_decision_endpoint_accepts_exactly_the_review_services_edge_types() -> None:
    """The wire vocabulary and the dispatch map are one list stated twice; a type in
    one and not the other is an edge that can be listed and not decided, or the
    reverse. `TRIGGER` was in neither."""
    assert set(get_args(ParsedLineageEdgeType)) == set(EDGE_TYPES)
    assert EDGE_TYPE_TO_MODEL["TRIGGER"] is TriggerLineageEdge


async def test_a_proposed_trigger_edge_is_in_the_review_queue(session: AsyncSession) -> None:
    org, _datasource, _orders, _audit, trigger = await _tsql_estate(session)
    await _run(session, org)

    items, total = await list_parsed_lineage_review_queue(session, org.id)

    [item] = [item for item in items if item.edge_type == "TRIGGER"]
    assert total == 1
    assert (item.source_label, item.target_label) == (
        "public.orders.customer_id",
        "public.audit.customer_id",
    )
    assert item.created_by == AGENT
    reference = item.source_sql_reference
    assert reference["kind"] == "TRIGGER_BODY"
    assert reference["trigger_id"] == str(trigger.id)
    # Which trigger claims the path, by name -- the id alone tells a reviewer nothing.
    assert reference["trigger"] == "public.note_order"
    assert reference["firing_table"] == "public.orders"
    # INV-6: names, ordinals and hashes; never the body.
    rendered = " ".join(reference.values()) + item.source_label + item.target_label
    assert "NOCOUNT" not in rendered and "INSERT" not in rendered.upper()


async def test_the_queue_route_filters_to_trigger_edges(session: AsyncSession) -> None:
    org, _datasource, _orders, _audit, _trigger_row = await _tsql_estate(session)
    await _run(session, org)

    read = await get_parsed_lineage_review_queue(
        edge_type="TRIGGER",
        min_confidence=None,
        limit=100,
        offset=0,
        context=_reviewer(org),
        session=session,
    )

    assert read.total == 1
    assert [item.edge_type for item in read.items] == ["TRIGGER"]


async def test_a_reviewer_approves_a_trigger_edge_through_the_one_decision_path(
    session: AsyncSession,
) -> None:
    org, _datasource, _orders, _audit, _trigger_row = await _tsql_estate(session)
    await _run(session, org)
    [edge] = await _proposed(session)

    decided = await decide_parsed_lineage_edge(
        edge.id, _decision(), context=_reviewer(org), session=session
    )

    assert (decided.edge_type, decided.review_status) == ("TRIGGER", "ACTIVE")
    assert decided.reviewed_by == REVIEWER
    await session.refresh(edge)
    assert (edge.review_status, edge.reviewed_by) == ("ACTIVE", REVIEWER)
    # The same trail every parsed-edge decision leaves.
    audit = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.resource_id == str(edge.id))
        )
    ).one()
    assert (audit.action, audit.resource_type) == (
        "LINEAGE_PARSED_EDGE_APPROVED",
        "parsed_lineage_edge",
    )
    assert audit.details == {"edge_type": "TRIGGER", "decision": "APPROVED"}
    outbox = (
        await session.scalars(
            select(OutboxEvent).where(OutboxEvent.aggregate_id == str(edge.id))
        )
    ).one()
    assert outbox.event_type == "lineage.parsed_edge.approved.v1"
    assert outbox.payload["edge_type"] == "TRIGGER"


async def test_the_agent_cannot_approve_its_own_trigger_edge(session: AsyncSession) -> None:
    """Maker-checker: the agent wrote the edge, so the agent is the one principal
    that may not decide it."""
    org, _datasource, _orders, _audit, _trigger_row = await _tsql_estate(session)
    await _run(session, org)
    [edge] = await _proposed(session)

    with pytest.raises(HTTPException) as refused:
        await decide_parsed_lineage_edge(
            edge.id, _decision(), context=_reviewer(org, principal=AGENT), session=session
        )

    assert refused.value.status_code == 409
    await session.refresh(edge)
    assert edge.review_status == "PROPOSED"


async def test_a_decided_trigger_edge_is_not_decided_twice(session: AsyncSession) -> None:
    org, _datasource, _orders, _audit, _trigger_row = await _tsql_estate(session)
    await _run(session, org)
    [edge] = await _proposed(session)
    await decide_parsed_lineage_edge(
        edge.id, _decision("REJECTED"), context=_reviewer(org), session=session
    )

    with pytest.raises(HTTPException) as refused:
        await decide_parsed_lineage_edge(
            edge.id, _decision(), context=_reviewer(org, "steward-2"), session=session
        )

    assert refused.value.status_code == 409
    await session.refresh(edge)
    assert edge.review_status == "REJECTED"


async def test_another_organization_cannot_decide_a_trigger_edge(session: AsyncSession) -> None:
    org, _datasource, _orders, _audit, _trigger_row = await _tsql_estate(session)
    await _run(session, org)
    [edge] = await _proposed(session)
    stranger, _other_datasource, _other_schema = await seed_estate(session)

    with pytest.raises(HTTPException) as refused:
        await decide_parsed_lineage_edge(
            edge.id, _decision(), context=_reviewer(stranger), session=session
        )

    assert refused.value.status_code == 403
    await session.refresh(edge)
    assert edge.review_status == "PROPOSED"


async def test_trigger_edges_bulk_decide_through_the_same_path(session: AsyncSession) -> None:
    org, _datasource, _schema, _function, _trigger_row = await _pg_estate(
        session, body=PG_FUNCTION_REDEFINED
    )
    await _run(session, org)
    edges = await _proposed(session)
    assert len(edges) == 2

    result = await bulk_decide_parsed_lineage_edges(
        ParsedLineageEdgeBulkDecisionRequest(
            items=[
                ParsedLineageEdgeBulkDecisionItem(edge_id=edge.id, edge_type="TRIGGER")
                for edge in edges
            ],
            decision="REJECTED",
            reason="not what the trigger does",
        ),
        context=_reviewer(org),
        session=session,
    )

    assert (result.succeeded_count, result.failed_count) == (2, 0)
    rows = (await session.scalars(select(TriggerLineageEdge))).all()
    assert {row.review_status for row in rows} == {"REJECTED"}


async def test_an_unparsed_trigger_marker_never_reaches_a_reviewer(
    session: AsyncSession,
) -> None:
    """A function nobody captured is a gap, recorded ACTIVE as a marker. It is not a
    proposal: it is not listed, and the decision endpoint will not flip it."""
    org, _datasource, schema = await seed_estate(session)
    trigger = _pg_trigger(org, _datasource, schema, action_routine="app.gone")
    session.add(trigger)
    await session.flush()
    await register_agent(session, org, principal=AGENT)
    await _run(session, org)
    [marker] = (await session.scalars(select(TriggerLineageEdge))).all()
    assert marker.transformation_type == UNPARSED_TRANSFORMATION_TYPE

    items, total = await list_parsed_lineage_review_queue(session, org.id, edge_type="TRIGGER")
    assert (items, total) == ([], 0)
    with pytest.raises(HTTPException) as refused:
        await decide_parsed_lineage_edge(
            marker.id, _decision(), context=_reviewer(org), session=session
        )
    assert refused.value.status_code == 409


async def test_the_agent_puts_only_decidable_edges_in_the_queue(session: AsyncSession) -> None:
    """A body's own plumbing -- here a read of the firing table into a local
    variable -- is not a path between two tables. Put in the per-edge queue it is a
    row a reviewer can only rubber-stamp, and approving it changes nothing the graph
    can use. It used to be proposed alongside the real write."""
    org, _datasource, _schema, _function, _trigger_row = await _pg_estate(
        session, body=PG_FUNCTION_WITH_PLUMBING
    )

    await _run(session, org)

    proposed = await _proposed(session)
    assert [(edge.source_table, edge.target_table) for edge in proposed] == [
        ("public.orders", "public.audit")
    ]
    assert all(edge.target_table_id is not None for edge in proposed)


# ---------------------------------------------------------------------------
# 2. In the unified graph -- once, and only once, it is fact.
# ---------------------------------------------------------------------------


def _trigger_edges(graph: Any) -> list[Any]:
    return [edge for edge in graph.edges if edge.edge_source == "TRIGGER_DEFINITION"]


async def test_a_trigger_edge_steers_the_graph_only_once_approved(
    session: AsyncSession,
) -> None:
    org, datasource, orders, audit, trigger = await _tsql_estate(session)
    await _run(session, org)
    [edge] = await _proposed(session)

    before = await build_unified_lineage_graph_payload(session, datasource, settings=None)
    assert _trigger_edges(before) == []
    assert before.counts_by_source["TRIGGER_DEFINITION"] == 0

    await decide_parsed_lineage_edge(edge.id, _decision(), context=_reviewer(org), session=session)
    after = await build_unified_lineage_graph_payload(session, datasource, settings=None)

    [folded] = _trigger_edges(after)
    # Dependent first, as every definition edge: the table the trigger writes
    # depends on the table it fires on.
    assert (folded.source_node_id, folded.target_node_id) == (str(audit.id), str(orders.id))
    assert folded.status == "ACTIVE"
    assert (folded.source_columns, folded.target_columns) == (["customer_id"], ["customer_id"])
    assert folded.evidence["source"] == "TRIGGER_DEFINITION"
    assert folded.evidence["trigger_ids"] == [str(trigger.id)]
    # A SQL Server trigger's own body is not a routine: no reference is invented.
    assert "transformation_reference" not in folded.evidence
    assert after.counts_by_source["TRIGGER_DEFINITION"] == 1


async def test_impact_from_the_firing_table_reaches_what_the_trigger_writes(
    session: AsyncSession,
) -> None:
    """The question the whole feature is for: what else changes when `orders` does?"""
    org, datasource, orders, audit, _trigger_row = await _tsql_estate(session)
    await _run(session, org)
    [edge] = await _proposed(session)

    undecided = await build_unified_lineage_impact_payload(session, datasource, str(orders.id))
    assert str(audit.id) not in {node.node_id for node in undecided.downstream}

    await decide_parsed_lineage_edge(edge.id, _decision(), context=_reviewer(org), session=session)
    impact = await build_unified_lineage_impact_payload(session, datasource, str(orders.id))

    [reached] = [node for node in impact.downstream if node.node_id == str(audit.id)]
    assert reached.depth == 1
    assert reached.contributing_edge_sources == ["TRIGGER_DEFINITION"]


async def test_a_pending_trigger_edge_is_labelled_proposed_on_opt_in(
    session: AsyncSession,
) -> None:
    """A caller that asks for pending edges sees the proposal -- as a proposal."""
    org, datasource, _orders, _audit, _trigger_row = await _tsql_estate(session)
    await _run(session, org)

    graph = await build_unified_lineage_graph_payload(
        session, datasource, settings=None, include_pending_edges=True
    )

    [folded] = _trigger_edges(graph)
    assert folded.status == "PROPOSED"


async def test_a_rejected_trigger_edge_never_appears(session: AsyncSession) -> None:
    org, datasource, _orders, _audit, _trigger_row = await _tsql_estate(session)
    await _run(session, org)
    [edge] = await _proposed(session)
    await decide_parsed_lineage_edge(
        edge.id, _decision("REJECTED"), context=_reviewer(org), session=session
    )

    for include_pending_edges in (False, True):
        graph = await build_unified_lineage_graph_payload(
            session, datasource, settings=None, include_pending_edges=include_pending_edges
        )
        assert _trigger_edges(graph) == []


async def test_a_postgres_trigger_edge_references_the_function_whose_body_was_read(
    session: AsyncSession,
) -> None:
    org, datasource, _schema, function, _trigger_row = await _pg_estate(session)
    await _run(session, org)
    [edge] = await _proposed(session)
    await decide_parsed_lineage_edge(edge.id, _decision(), context=_reviewer(org), session=session)

    graph = await build_unified_lineage_graph_payload(session, datasource, settings=None)

    [folded] = _trigger_edges(graph)
    assert folded.evidence["routine_ids"] == [str(function.id)]
    assert folded.evidence["transformation_reference"] == {
        "tool": "get_transformation_detail",
        "entity_id": str(function.id),
        "kind": "ROUTINE_BODY",
    }


async def test_another_datasources_trigger_edges_never_join_this_graph(
    session: AsyncSession,
) -> None:
    """INV-5. A row in another source -- even one whose table ids name this
    source's tables, as a stale or hostile row could -- is not this graph's. This
    source's own approved edge is there, so the exclusion is not vacuous."""
    org, datasource, orders, audit, trigger = await _tsql_estate(session)
    await _run(session, org)
    [own] = await _proposed(session)
    await decide_parsed_lineage_edge(own.id, _decision(), context=_reviewer(org), session=session)
    _same_org, elsewhere, elsewhere_schema = await seed_estate(
        session, organization=org, dialect="tsql"
    )
    foreign = _trigger(org, elsewhere, elsewhere_schema)
    session.add(foreign)
    await session.flush()
    session.add(
        TriggerLineageEdge(
            organization_id=org.id,
            datasource_id=elsewhere.id,
            trigger_id=foreign.id,
            statement_ordinal=0,
            source_table="public.orders",
            source_column="customer_id",
            target_table="public.audit",
            target_column="customer_id",
            source_table_id=orders.id,
            target_table_id=audit.id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="tsql",
            is_write=True,
            sql_hash="h" * 64,
            review_status="ACTIVE",
            created_by=REVIEWER,
        )
    )
    await session.flush()

    graph = await build_unified_lineage_graph_payload(session, datasource, settings=None)

    [folded] = _trigger_edges(graph)
    assert folded.evidence["trigger_ids"] == [str(trigger.id)]
    assert folded.evidence["column_edge_count"] == 1


# ---------------------------------------------------------------------------
# 3. A coverage record, mirroring the routine one.
# ---------------------------------------------------------------------------


async def _coverage(session: AsyncSession) -> list[Any]:
    from aida.procedure_lineage_models import TriggerParseCoverage

    return list((await session.scalars(select(TriggerParseCoverage))).all())


async def test_the_agent_records_how_completely_a_trigger_was_understood(
    session: AsyncSession,
) -> None:
    org, datasource, _orders, _audit, trigger = await _tsql_estate(session)

    await _run(session, org)

    [coverage] = await _coverage(session)
    [edge] = await _proposed(session)
    assert (coverage.trigger_id, coverage.datasource_id) == (trigger.id, datasource.id)
    assert coverage.organization_id == org.id
    assert coverage.parse_completed is True
    assert coverage.statement_count >= 1
    assert (coverage.unparsed_statement_count, coverage.unparsed_reason_codes) == (0, "")
    assert coverage.routine_id is None  # a SQL Server trigger carries its own body
    assert coverage.sql_hash == edge.sql_hash
    assert coverage.measured_by == AGENT
    # INV-6: a measurement, not a copy of what was measured.
    rendered = " ".join(
        str(getattr(coverage, column.name)) for column in coverage.__table__.columns
    )
    assert "NOCOUNT" not in rendered and "INSERT" not in rendered.upper()
    assert "NOCOUNT" in TSQL_TRIGGER_BODY


async def test_a_postgres_triggers_coverage_names_the_function_it_read(
    session: AsyncSession,
) -> None:
    org, _datasource, _schema, function, _trigger_row = await _pg_estate(session)

    await _run(session, org)

    [coverage] = await _coverage(session)
    assert coverage.routine_id == function.id
    assert coverage.parse_completed is True


async def test_an_unreachable_function_is_measured_rather_than_absent(
    session: AsyncSession,
) -> None:
    org, _datasource, _schema, _function, _trigger_row = await _pg_estate(
        session, routine=False
    )

    await _run(session, org)

    [coverage] = await _coverage(session)
    assert coverage.parse_completed is False
    assert coverage.routine_id is None
    assert coverage.unparsed_statement_count == 1
    assert coverage.unparsed_reason_codes == "NESTED_PROCEDURE_CALL"


async def test_a_dry_run_measures_nothing(session: AsyncSession) -> None:
    org, _datasource, _orders, _audit, _trigger_row = await _tsql_estate(session)

    await _run(session, org, dry_run=True)

    assert await _coverage(session) == []


async def test_a_trigger_that_writes_nothing_leaves_the_agents_backlog(
    session: AsyncSession,
) -> None:
    """Read in full, it writes no other table, so no edge is ever stored for it. With
    only edges to go on, the gap register counted it as waiting on the agent for
    ever; the measurement is what says it was done."""
    org, _datasource, _schema, _function, _trigger_row = await _pg_estate(
        session, body=PG_FUNCTION_WRITES_NOTHING
    )

    async def awaiting() -> int | None:
        read = await footprint_gaps(
            session,
            context=human(org, roles=frozenset({"MetadataAdmin", "DataSteward"})),
            settings=agent_settings(),
            organization_id=org.id,
        )
        return read.totals.get("LINEAGE_AWAITING_PARSE")

    # The function itself is a routine awaiting its own parse; the trigger is the
    # second object in the backlog.
    assert await awaiting() == 2
    await _run(session, org)

    assert (await session.scalars(select(TriggerLineageEdge))).all() == []
    assert await awaiting() == 1


def test_trigger_coverage_stores_booleans_not_states() -> None:
    from aida.procedure_lineage_models import RoutineParseCoverage, TriggerParseCoverage

    routine_columns = {column.name for column in RoutineParseCoverage.__table__.columns}
    trigger_columns = {column.name for column in TriggerParseCoverage.__table__.columns}
    # A mirror, with the identity swapped the way the edge table swaps it.
    assert trigger_columns == routine_columns | {"trigger_id"}
    assert "state" not in trigger_columns


# ---------------------------------------------------------------------------
# 4. Re-examined when the body changes and the trigger row does not.
# ---------------------------------------------------------------------------


def _routine_signal(
    org: Organization, datasource: DataSource, routine_id: Any, change_class: str
) -> MetadataChangeSignal:
    """What `ingestion._upsert_routine` records when a function's text moves."""
    return MetadataChangeSignal(
        organization_id=org.id,
        datasource_id=datasource.id,
        subject_kind="ROUTINE",
        subject_id=routine_id,
        signal_type=SIGNAL_DEFINITION_CHANGED,
        change_class=change_class,
        detected_at=datetime.now(UTC),
    )


async def _redefine(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    function: Any,
    *,
    change_class: str = CHANGE_STRUCTURAL,
) -> None:
    """`CREATE OR REPLACE FUNCTION`, as a rescan records it: the routine row and a
    ROUTINE signal. The trigger row is not touched -- on PostgreSQL it does not
    change."""
    await session.execute(
        update(type(function))
        .where(type(function).id == function.id)
        .values(body_sql_redacted=PG_FUNCTION_REDEFINED, body_fingerprint="r2" * 32)
    )
    session.add(_routine_signal(org, datasource, function.id, change_class))
    await session.flush()


async def test_a_redefined_function_re_examines_the_trigger_that_runs_it(
    session: AsyncSession,
) -> None:
    org, datasource, _schema, function, trigger = await _pg_estate(session)
    first = await _run(session, org)
    assert [item.action for item in first.items] == [ACTION_PROPOSED]
    trigger_updated_at = trigger.updated_at

    await _redefine(session, org, datasource, function)
    second = await _run(session, org)

    assert [(item.action, item.subject_id) for item in second.items] == [
        (ACTION_PROPOSED, trigger.id)
    ]
    targets = {edge.target_table for edge in await _proposed(session)}
    assert targets == {"public.audit", "public.audit_history"}
    await session.refresh(trigger)
    # SQLite hands the timestamp back naive; the instant is what is compared.
    assert trigger.updated_at.replace(tzinfo=None) == trigger_updated_at.replace(tzinfo=None), (
        "the trigger row itself never moved"
    )


async def test_once_re_examined_the_trigger_is_left_alone_again(session: AsyncSession) -> None:
    org, datasource, _schema, function, _trigger_row = await _pg_estate(session)
    await _run(session, org)
    await _redefine(session, org, datasource, function)
    await _run(session, org)

    third = await _run(session, org)

    assert third.items == []


async def test_a_literal_only_change_to_the_function_does_not(session: AsyncSession) -> None:
    """R11-FP16's rule, unchanged for this axis: lineage does not depend on literals."""
    org, datasource, _schema, function, _trigger_row = await _pg_estate(session)
    await _run(session, org)

    await _redefine(session, org, datasource, function, change_class=CHANGE_LITERAL_ONLY)
    again = await _run(session, org)

    assert again.items == []


async def test_a_change_to_a_routine_the_trigger_does_not_run_does_not(
    session: AsyncSession,
) -> None:
    org, datasource, schema, _function, _trigger_row = await _pg_estate(session)
    await _run(session, org)
    unrelated = _routine(org, datasource, schema, name="refresh_totals")
    session.add(unrelated)
    await session.flush()

    session.add(_routine_signal(org, datasource, unrelated.id, CHANGE_STRUCTURAL))
    await session.flush()
    again = await _run(session, org)

    assert again.items == []


async def test_a_function_captured_after_the_trigger_was_examined_is_read_then(
    session: AsyncSession,
) -> None:
    """The gap register routes "action routine not captured" to the source
    administrator with "widen the selection and rescan". That only closes the gap
    if the rescan's new routine brings the trigger back: a new object is not a
    change signal, and the trigger row does not move."""
    org, datasource, schema, _function, trigger = await _pg_estate(session, routine=False)
    first = await _run(session, org)
    assert [item.action for item in first.items] != [ACTION_PROPOSED]

    session.add(_routine(org, datasource, schema))
    await session.flush()
    second = await _run(session, org)

    assert [(item.action, item.subject_id) for item in second.items] == [
        (ACTION_PROPOSED, trigger.id)
    ]
    rows = (await session.scalars(select(TriggerLineageEdge))).all()
    assert [row.transformation_type for row in rows] == ["DIRECT"], "the marker was replaced"
    assert (await _run(session, org)).items == []


# ---------------------------------------------------------------------------
# 5. The gap register's drill-down names the triggers its counts count.
# ---------------------------------------------------------------------------


async def _objects(session: AsyncSession, org: Organization, datasource: DataSource, kind: str):
    detail = await footprint_gap_objects(
        session, organization_id=org.id, datasource_id=datasource.id, kind=kind
    )
    return [(item.object_type, item.qualified_name, item.detail) for item in detail.objects]


async def test_the_gap_detail_names_the_triggers_behind_each_count(
    session: AsyncSession,
) -> None:
    org, datasource, _orders, _audit, _trigger_row = await _tsql_estate(session)
    name = "bank.public.note_order on orders"

    assert await _objects(session, org, datasource, "LINEAGE_AWAITING_PARSE") == [
        ("TRIGGER", name, None)
    ]
    await _run(session, org)
    assert await _objects(session, org, datasource, "LINEAGE_AWAITING_REVIEW") == [
        ("TRIGGER", name, "PROPOSED")
    ]
    assert await _objects(session, org, datasource, "LINEAGE_AWAITING_PARSE") == []


async def test_the_gap_detail_names_a_trigger_whose_function_is_missing(
    session: AsyncSession,
) -> None:
    org, datasource, _schema, _function, _trigger_row = await _pg_estate(
        session, routine=False
    )
    await _run(session, org)
    name = "bank.public.note_order on orders"

    assert await _objects(session, org, datasource, "LINEAGE_UNRESOLVED_CALLEE") == [
        ("TRIGGER", name, "NOT_CAPTURED")
    ]
    assert await _objects(session, org, datasource, "LINEAGE_UNPARSED_STATEMENTS") == [
        ("TRIGGER", name, "NOT_CAPTURED")
    ]


# ---------------------------------------------------------------------------
# 6. A merged rename carries trigger edges with it.
# ---------------------------------------------------------------------------


async def test_a_merged_rename_carries_trigger_edges_with_it(session: AsyncSession) -> None:
    """Approved trigger edges fold into the graph now, so an edge left on a
    tombstoned table is a path the renamed table silently lost."""
    org, datasource, orders, _audit, _trigger_row = await _tsql_estate(session)
    await _run(session, org)
    [edge] = await _proposed(session)
    renamed = await seed_table(
        session, org, datasource, await _schema_of(session, orders), name="orders_v2"
    )

    moved = await merge_table_identity(session, old_table_id=orders.id, new_table_id=renamed.id)

    assert moved.get("trigger_lineage_edge.source_table_id") == 1
    await session.refresh(edge)
    assert edge.source_table_id == renamed.id


async def _schema_of(session: AsyncSession, table: MetadataTable) -> Any:
    return await session.get(MetadataSchema, table.schema_id)
