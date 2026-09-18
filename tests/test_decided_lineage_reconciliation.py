"""Decided routine and trigger lineage follows the body it was read from (R11-FP01, R11-FP16).

Three writers keep parsed routine and trigger lineage -- the lineage agent's routine path, its
trigger path, and a person's parse under `require_review` -- and each used to leave a decided
edge exactly as it was on a re-parse. What this module proves:

* **A re-examination no longer fails on lineage it already has.** The agent's routine path
  inserted every edge of a redefined body afresh; an edge the new body still wrote collided with
  its decided row on the unique natural key and the item FAILED with an IntegrityError -- in the
  common case, since a redefinition usually keeps most of what a routine writes.
* **Approved lineage the body stopped writing is SUPERSEDED**, on every axis, so it stops
  steering impact analysis; **only after a complete parse**, so a statement the parser could not
  read never retires approved lineage.
* **Superseded lineage the body writes again goes back to PROPOSED**, for a person to decide:
  what was approved was an earlier body.
* A REJECTED edge stays rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import CHANGE_STRUCTURAL, SIGNAL_DEFINITION_CHANGED
from aida.lineage_agent import SKIP_LINEAGE_KNOWN
from aida.procedure_lineage import parse_procedure_lineage
from aida.procedure_lineage_models import DeepProcedureLineageEdge, TriggerLineageEdge
from aida.routine_lineage_edges import SUPERSEDED, persist_routine_edges
from aida.task_agent import ACTION_PROPOSED, ACTION_SKIPPED
from tests.support.task_agents import register_agent, task_agent_session
from tests.test_lineage_agent import AGENT, _procedure_estate, _routine, _run
from tests.test_trigger_lineage_decidable import _pg_estate
from tests.test_trigger_lineage_decidable import _run as _run_triggers

#: Every statement understood: no dynamic SQL, so the parse is complete.
BOTH_COLUMNS = (
    "CREATE PROCEDURE public.load_totals AS BEGIN "
    "INSERT INTO public.order_totals (customer_id, total) "
    "SELECT o.customer_id, o.amount FROM public.orders o; END"
)
#: Keeps `customer_id`, stops writing `total`, starts writing `region`.
CUSTOMER_AND_REGION = (
    "CREATE PROCEDURE public.load_totals AS BEGIN "
    "INSERT INTO public.order_totals (customer_id, region) "
    "SELECT o.customer_id, o.region FROM public.orders o; END"
)
#: The same change plus a statement the parser cannot read.
CUSTOMER_AND_REGION_PARTIAL = (
    "CREATE PROCEDURE public.load_totals AS BEGIN "
    "INSERT INTO public.order_totals (customer_id, region) "
    "SELECT o.customer_id, o.region FROM public.orders o; "
    "EXEC(@dynamic_sql); END"
)


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


async def _approve_all(session: AsyncSession, model: Any) -> None:
    """A person's approval of everything pending, backdated so a later redefinition is newer."""
    past = datetime.now(UTC) - timedelta(days=1)
    await session.execute(
        update(model)
        .where(model.review_status == "PROPOSED")
        .values(review_status="ACTIVE", created_at=past)
    )
    await session.flush()


async def _redefine_routine(
    session: AsyncSession, org: Any, datasource: Any, routine: Any, body: str
) -> None:
    """A structural redefinition as a rescan records it: new text, a newer row, a signal."""
    routine.body_sql_redacted = body
    routine.body_fingerprint = uuid4().hex
    routine.updated_at = datetime.now(UTC) + timedelta(seconds=5)
    session.add(
        MetadataChangeSignal(
            organization_id=org.id,
            datasource_id=datasource.id,
            subject_kind="ROUTINE",
            subject_id=routine.id,
            signal_type=SIGNAL_DEFINITION_CHANGED,
            change_class=CHANGE_STRUCTURAL,
            detected_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _routine_edges(session: AsyncSession) -> dict[str, str]:
    """target column -> review status, for the one routine these tests parse."""
    rows = (await session.scalars(select(DeepProcedureLineageEdge))).all()
    return {row.target_column: row.review_status for row in rows}


# --- the agent's routine path -------------------------------------------------------------


async def test_a_redefinition_that_keeps_its_lineage_is_known_lineage_not_a_failure(
    session: AsyncSession,
) -> None:
    org, datasource, schema, _orders, _totals = await _procedure_estate(session)
    routine = await _routine(session, org, datasource, schema, body=BOTH_COLUMNS)
    await register_agent(session, org, principal=AGENT)
    first = await _run(session, org)
    assert [item.action for item in first.items] == [ACTION_PROPOSED]
    await _approve_all(session, DeepProcedureLineageEdge)

    await _redefine_routine(session, org, datasource, routine, BOTH_COLUMNS)
    second = await _run(session, org)

    [item] = second.items
    assert (item.action, item.reason) == (ACTION_SKIPPED, SKIP_LINEAGE_KNOWN), item
    assert await _routine_edges(session) == {"customer_id": "ACTIVE", "total": "ACTIVE"}


async def test_approved_lineage_the_body_stopped_writing_is_superseded(
    session: AsyncSession,
) -> None:
    org, datasource, schema, _orders, _totals = await _procedure_estate(session)
    routine = await _routine(session, org, datasource, schema, body=BOTH_COLUMNS)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org)
    await _approve_all(session, DeepProcedureLineageEdge)
    total_id = (
        await session.scalar(
            select(DeepProcedureLineageEdge.id).where(
                DeepProcedureLineageEdge.target_column == "total"
            )
        )
    )

    await _redefine_routine(session, org, datasource, routine, CUSTOMER_AND_REGION)
    second = await _run(session, org)

    [item] = second.items
    assert item.action == ACTION_PROPOSED
    assert await _routine_edges(session) == {
        "customer_id": "ACTIVE",
        "total": SUPERSEDED,
        "region": "PROPOSED",
    }
    task_evidence = await _task_evidence(session, item.task_id)
    assert task_evidence["superseded_edge_ids"] == [str(total_id)]


async def test_a_partial_reparse_never_retires_approved_lineage(session: AsyncSession) -> None:
    """A statement the parser could not read may be where `total` is still written."""
    org, datasource, schema, _orders, _totals = await _procedure_estate(session)
    routine = await _routine(session, org, datasource, schema, body=BOTH_COLUMNS)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org)
    await _approve_all(session, DeepProcedureLineageEdge)

    await _redefine_routine(session, org, datasource, routine, CUSTOMER_AND_REGION_PARTIAL)
    await _run(session, org)

    edges = await _routine_edges(session)
    assert edges["total"] == "ACTIVE"
    assert edges["region"] == "PROPOSED"


async def test_superseded_lineage_the_body_writes_again_is_proposed_again(
    session: AsyncSession,
) -> None:
    org, datasource, schema, _orders, _totals = await _procedure_estate(session)
    routine = await _routine(session, org, datasource, schema, body=BOTH_COLUMNS)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org)
    await _approve_all(session, DeepProcedureLineageEdge)
    await _redefine_routine(session, org, datasource, routine, CUSTOMER_AND_REGION)
    await _run(session, org)
    assert (await _routine_edges(session))["total"] == SUPERSEDED

    await _redefine_routine(session, org, datasource, routine, BOTH_COLUMNS)
    third = await _run(session, org)

    [item] = third.items
    assert item.action == ACTION_PROPOSED
    edges = await _routine_edges(session)
    assert edges["total"] == "PROPOSED", "the earlier approval does not silently return"
    assert edges["customer_id"] == "ACTIVE"
    # The region edge the second body proposed is no longer written and was never decided:
    # an undecided proposal is replaced by the new parse, not kept.
    assert "region" not in edges


async def _task_evidence(session: AsyncSession, task_id: Any) -> dict[str, Any]:
    from aida.models import AgentTask

    task = await session.get(AgentTask, task_id)
    assert task is not None
    evidence = task.evidence or {}
    return dict(evidence)


# --- the agent's trigger path -------------------------------------------------------------

PG_FUNCTION_HISTORY_ONLY = """CREATE OR REPLACE FUNCTION public.note_order()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO public.audit_history (customer_id) VALUES (NEW.customer_id);
    RETURN NEW;
END;
$function$
"""


async def _redefine_function(
    session: AsyncSession, org: Any, datasource: Any, function: Any, body: str
) -> None:
    """`CREATE OR REPLACE FUNCTION` as a rescan records it; the trigger row does not move."""
    await session.execute(
        update(type(function))
        .where(type(function).id == function.id)
        .values(body_sql_redacted=body, body_fingerprint=uuid4().hex)
    )
    session.add(
        MetadataChangeSignal(
            organization_id=org.id,
            datasource_id=datasource.id,
            subject_kind="ROUTINE",
            subject_id=function.id,
            signal_type=SIGNAL_DEFINITION_CHANGED,
            change_class=CHANGE_STRUCTURAL,
            detected_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def test_a_trigger_whose_function_stopped_writing_a_table_supersedes_that_edge(
    session: AsyncSession,
) -> None:
    org, datasource, _schema, function, _trigger = await _pg_estate(session)
    await _run_triggers(session, org)
    await _approve_all(session, TriggerLineageEdge)
    original = function.body_sql_redacted

    await _redefine_function(session, org, datasource, function, PG_FUNCTION_HISTORY_ONLY)
    await _run_triggers(session, org)

    statuses = {
        row.target_table: row.review_status
        for row in (await session.scalars(select(TriggerLineageEdge))).all()
    }
    assert statuses == {"public.audit": SUPERSEDED, "public.audit_history": "PROPOSED"}

    # And back: the trigger writes `audit` again, which a person decides again.
    await _redefine_function(session, org, datasource, function, original)
    await _run_triggers(session, org)
    statuses = {
        row.target_table: row.review_status
        for row in (await session.scalars(select(TriggerLineageEdge))).all()
    }
    assert statuses["public.audit"] == "PROPOSED"


# --- a person's parse under require_review ------------------------------------------------


async def test_a_persons_reparse_supersedes_and_never_touches_a_rejection(
    session: AsyncSession,
) -> None:
    org, datasource, schema, _orders, _totals = await _procedure_estate(session)
    routine = await _routine(session, org, datasource, schema, body=BOTH_COLUMNS)
    first = parse_procedure_lineage(BOTH_COLUMNS, dialect="tsql")
    assert first.is_fully_parsed
    await persist_routine_edges(
        session,
        datasource=datasource,
        routine=routine,
        result=first,
        review_mode="require_review",
        threshold=1.0,
        created_by="steward",
    )
    await session.execute(
        update(DeepProcedureLineageEdge)
        .where(DeepProcedureLineageEdge.target_column == "total")
        .values(review_status="ACTIVE")
    )
    await session.execute(
        update(DeepProcedureLineageEdge)
        .where(DeepProcedureLineageEdge.target_column == "customer_id")
        .values(review_status="REJECTED")
    )
    await session.flush()

    await persist_routine_edges(
        session,
        datasource=datasource,
        routine=routine,
        result=parse_procedure_lineage(
            "CREATE PROCEDURE public.load_totals AS BEGIN "
            "INSERT INTO public.order_totals (region) SELECT o.region FROM public.orders o; END",
            dialect="tsql",
        ),
        review_mode="require_review",
        threshold=1.0,
        created_by="steward",
    )
    await session.flush()

    edges = await _routine_edges(session)
    assert edges["total"] == SUPERSEDED
    assert edges["customer_id"] == "REJECTED"
    assert edges["region"] in {"PROPOSED", "ACTIVE"}
