"""R11-FP01: what a trigger body reads and writes.

The trigger axis already knew a trigger existed and which table fires it. What it
did not know was what the trigger *does to the data*, because nothing handed a
body to the procedure parser. These tests pin the four things that were not
mechanical about closing that:

* **the implicit subject.** A trigger's body never names the table it fires on --
  `NEW`/`OLD` on PostgreSQL, `INSERTED`/`DELETED` on SQL Server are that table's
  row -- so an edge out of one of them must come out of the firing table or the
  lineage points the wrong way. Where the reference cannot be bound (Oracle spells
  it `:NEW`, which sqlglot resolves to a bind placeholder) it is recorded
  unresolved, never guessed, and never silently absent;
* **PostgreSQL's action routine.** A PostgreSQL trigger has no body at all, so
  trigger lineage there means joining to the function `action_routine` names. The
  join is on this axis because only a trigger knows the firing table the function's
  `NEW` refers to -- and one function may be attached to several tables, so the
  routine axis cannot resolve it even in principle;
* **the unparsed vocabulary.** A body the parser cannot fully read reports it the
  way a routine body does, as a marker edge with a named reason, so "the object was
  inventoried" is never mistaken for "every path is understood";
* **review state.** Only ACTIVE edges steer retrieval and tool generation, so an
  agent's undecided proposal must influence nothing.

Plus the two invariants: no body text reaches a stored row, a log or an error
(INV-6), and every query restates the organization and the datasource (INV-5),
proven by a second datasource whose rows never appear.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import MetadataRoutine, MetadataTrigger
from aida.footprint_gaps import footprint_gaps
from aida.lineage_agent import CAPABILITY_TRIGGER_LINEAGE, run_lineage_agent
from aida.models import DataSource, MetadataSchema, Organization
from aida.procedure_lineage import (
    TRIGGER_SUBJECT_RELATIONS,
    UNPARSED_TRANSFORMATION_TYPE,
    UnparsedReason,
    parse_procedure_lineage,
    parse_trigger_lineage,
    trigger_subject_aliases,
    unbound_subject_aliases,
    unbound_trigger_subject,
)
from aida.procedure_lineage_models import TriggerLineageEdge
from aida.routine_call_descent import (
    CALLEE_AMBIGUOUS,
    CALLEE_BODY_WITHHELD,
    CALLEE_NOT_CAPTURED,
)
from aida.routine_lineage_edges import (
    TRIGGER_ROUTINE_AMBIGUOUS,
    TRIGGER_ROUTINE_BODY_WITHHELD,
    TRIGGER_ROUTINE_NOT_CAPTURED,
    TriggerNotEligibleError,
    persist_trigger_edges,
    require_eligible_trigger_body,
    trigger_body,
    unreachable_body_marker,
)
from aida.task_agent import ACTION_PROPOSED, TaskAgentRunRequest
from tests.support.task_agents import (
    agent_settings,
    human,
    register_agent,
    seed_estate,
    seed_table,
    task_agent_session,
)

AGENT = "agent:lineage"

#: A PostgreSQL trigger function: the body lives on the routine axis, writes a
#: second table, and names the firing table nowhere at all. `VALUES (NEW....)` is
#: the shape a row trigger's write nearly always has.
PG_FUNCTION_BODY = """CREATE OR REPLACE FUNCTION public.note_order()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    INSERT INTO public.audit (customer_id) VALUES (NEW.customer_id);
    RETURN NEW;
END;
$function$
"""

#: A SQL Server trigger: the body is the trigger's own, and `inserted` is the
#: firing table's row arriving as a pseudo-relation in the FROM clause.
TSQL_TRIGGER_BODY = """CREATE TRIGGER note_order ON public.orders
AFTER INSERT, UPDATE AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO public.audit (customer_id)
    SELECT i.customer_id FROM inserted i;
END;
"""

#: An Oracle trigger. `:NEW` is bind-variable syntax, so there is no qualified
#: column reference for a binding to attach to. Deliberately no `sample` schema
#: name -- sqlglot's oracle tokenizer reads that word as TABLESAMPLE, which would
#: make this a parse-error test rather than a subject test.
ORACLE_TRIGGER_BODY = """CREATE OR REPLACE TRIGGER public.note_order
AFTER INSERT ON public.orders
FOR EACH ROW
BEGIN
    INSERT INTO public.audit (customer_id) VALUES (:NEW.customer_id);
END;
"""


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


def _trigger(
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str = "note_order",
    **overrides: Any,
) -> MetadataTrigger:
    values: dict[str, Any] = {
        "id": uuid4(),
        "organization_id": org.id,
        "datasource_id": datasource.id,
        "schema_id": schema.id,
        "name": name,
        "table_name": "orders",
        "timing": "AFTER",
        "events": ["INSERT"],
        "orientation": "ROW",
        "is_enabled": True,
        "availability": "AVAILABLE",
        "body_sql_redacted": TSQL_TRIGGER_BODY,
        "body_fingerprint": "bf" * 32,
        "redaction_status": "LEXICAL",
        "screening_status": "CLEAN",
        "status": "ACTIVE",
        "fingerprint": "fp",
    }
    values.update(overrides)
    return MetadataTrigger(**values)


def _pg_trigger(
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    action_routine: str = "public.note_order",
    **overrides: Any,
) -> MetadataTrigger:
    """A PostgreSQL trigger as the connector really stores one: UNAVAILABLE with
    the engine's own reason, and `action_routine` naming the function."""
    return _trigger(
        org,
        datasource,
        schema,
        availability="UNAVAILABLE",
        body_sql_redacted=None,
        body_fingerprint=None,
        unavailable_reason="PostgreSQL keeps no trigger body; see action_routine",
        action_routine=action_routine,
        **overrides,
    )


def _routine(
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    name: str = "note_order",
    body: str | None = PG_FUNCTION_BODY,
    **overrides: Any,
) -> MetadataRoutine:
    values: dict[str, Any] = {
        "id": uuid4(),
        "organization_id": org.id,
        "datasource_id": datasource.id,
        "schema_id": schema.id,
        "name": name,
        "signature": "()",
        "routine_type": "FUNCTION",
        "body_sql_redacted": body,
        "body_fingerprint": "rf" * 32,
        "redaction_status": "LEXICAL",
        "screening_status": "CLEAN",
        "availability": "AVAILABLE",
        "status": "ACTIVE",
        "fingerprint": "fp",
    }
    values.update(overrides)
    return MetadataRoutine(**values)


def _writes(result: Any, target: str) -> list[Any]:
    return [
        edge
        for edge in result.edges
        if edge.is_write and edge.target_table.lower() == target and not edge.is_intermediate
    ]


# ---------------------------------------------------------------------------
# 1. The implicit subject.
# ---------------------------------------------------------------------------


def test_a_postgres_trigger_body_writes_from_the_firing_table() -> None:
    """The whole point. `INSERT INTO app.audit ... VALUES (NEW.customer_id)` is a
    path from the firing table to `app.audit`, and the firing table's name is
    nowhere in the body."""
    result = parse_trigger_lineage(
        PG_FUNCTION_BODY, dialect="postgres", firing_table="public.orders"
    )

    assert result.is_fully_parsed, result.errors
    [edge] = _writes(result, "public.audit")
    assert (edge.source_table, edge.source_column) == ("public.orders", "customer_id")
    assert (edge.target_table, edge.target_column) == ("public.audit", "customer_id")
    assert edge.source_resolved is True


def test_a_sqlserver_trigger_body_writes_from_the_firing_table() -> None:
    """`FROM inserted i` -- and `i`, the alias over it -- both resolve to the
    firing table, not to a table called `inserted`."""
    result = parse_trigger_lineage(TSQL_TRIGGER_BODY, dialect="tsql", firing_table="public.orders")

    assert result.is_fully_parsed, result.errors
    [edge] = _writes(result, "public.audit")
    assert (edge.source_table, edge.source_column) == ("public.orders", "customer_id")
    assert edge.source_resolved is True


def test_a_create_trigger_header_is_stripped_rather_than_reported_unreadable() -> None:
    """A SQL Server trigger's `CREATE TRIGGER ... AS BEGIN` header used to become
    one opaque `Command` chunk, so a body that reads perfectly well reported an
    UNSUPPORTED_STATEMENT_SHAPE gap it does not have."""
    result = parse_trigger_lineage(TSQL_TRIGGER_BODY, dialect="tsql", firing_table="public.orders")

    assert [edge.unparsed_reason for edge in result.edges if edge.unparsed_reason] == []


def test_an_oracle_firing_row_reference_is_recorded_unresolved_never_guessed() -> None:
    """Oracle's `:NEW` is a bind placeholder to sqlglot, so there is nothing to
    bind. The parse says so rather than reporting a body whose sources it knows."""
    assert "oracle" not in TRIGGER_SUBJECT_RELATIONS
    assert unbound_trigger_subject(ORACLE_TRIGGER_BODY, "oracle") is True

    result = parse_trigger_lineage(
        ORACLE_TRIGGER_BODY, dialect="oracle", firing_table="public.orders"
    )

    assert result.is_fully_parsed is False
    assert result.is_read_only is False
    markers = [
        edge.unparsed_reason
        for edge in result.edges
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    ]
    assert any(
        reason is not None
        and reason.startswith(UnparsedReason.UNRESOLVED_TRIGGER_SUBJECT.value)
        for reason in markers
    ), markers
    # Never a source that claims to be the firing table.
    assert not _writes(result, "public.audit")


def test_the_marker_reason_carries_the_dialect_and_no_body_text() -> None:
    """INV-6: a reason is a code, not an excerpt."""
    result = parse_trigger_lineage(
        ORACLE_TRIGGER_BODY, dialect="oracle", firing_table="public.orders"
    )

    reason = next(
        edge.unparsed_reason
        for edge in result.edges
        if (edge.unparsed_reason or "").startswith(
            UnparsedReason.UNRESOLVED_TRIGGER_SUBJECT.value
        )
    )
    assert reason == f"{UnparsedReason.UNRESOLVED_TRIGGER_SUBJECT.value}: oracle"
    assert "customer_id" not in reason


def test_the_binding_is_registered_in_every_case_a_body_may_have_written() -> None:
    """Alias resolution is an exact dictionary lookup on the identifier as the
    source spelled it, so one spelling would silently miss the others."""
    aliases = trigger_subject_aliases("postgres", "public.orders")

    assert {"NEW", "new", "New", "OLD", "old", "Old"} <= set(aliases)
    assert set(aliases.values()) == {"public.orders"}
    assert trigger_subject_aliases("oracle", "public.orders") == {}


def test_a_trigger_function_on_the_routine_axis_never_invents_a_table_called_new() -> None:
    """The same function parsed as a routine -- which is where PostgreSQL keeps it
    and where the lineage agent's PROCEDURE_LINEAGE pass finds it -- has no firing
    table, and one function may be attached to several. An invented source called
    `NEW` would be worse than an admitted gap."""
    result = parse_procedure_lineage(PG_FUNCTION_BODY, dialect="postgres")

    assert unbound_subject_aliases("postgres") == {
        "NEW": "",
        "new": "",
        "New": "",
        "OLD": "",
        "old": "",
        "Old": "",
    }
    sources = {edge.source_table for edge in result.edges}
    assert "NEW" not in sources and "new" not in sources
    assert all(not edge.source_resolved for edge in result.edges), result.edges


def test_a_real_table_named_inserted_outside_a_trigger_is_left_alone() -> None:
    """The empty binding must not overwrite a table the walk actually found: on an
    engine whose firing-row name is an ordinary identifier, `FROM inserted` outside
    a trigger is a real table."""
    body = """CREATE PROCEDURE app.p AS
BEGIN
    INSERT INTO app.audit (customer_id) SELECT i.customer_id FROM app.inserted i;
END;
"""
    result = parse_procedure_lineage(body, dialect="tsql")

    [edge] = _writes(result, "app.audit")
    assert edge.source_table == "app.inserted"
    assert edge.source_resolved is True


# ---------------------------------------------------------------------------
# 2. The gate, and PostgreSQL's action routine.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"status": "DEPRECATED"}, "TRIGGER_INACTIVE"),
        (
            {"availability": "UNAVAILABLE", "body_sql_redacted": None},
            "TRIGGER_BODY_UNAVAILABLE",
        ),
        ({"redaction_status": "UNPARSED"}, "TRIGGER_BODY_NOT_STORED"),
        ({"screening_status": "QUARANTINED"}, "TRIGGER_BODY_QUARANTINED"),
    ],
    ids=["retired", "withheld", "not-stored", "quarantined"],
)
def test_a_trigger_body_passes_the_same_gate_a_routine_body_does(
    overrides: dict[str, Any], code: str
) -> None:
    org = Organization(id=uuid4(), name="Bank", slug="bank")
    datasource = DataSource(id=uuid4(), organization_id=org.id)
    schema = MetadataSchema(id=uuid4(), organization_id=org.id)
    trigger = _trigger(org, datasource, schema, **overrides)

    with pytest.raises(TriggerNotEligibleError) as raised:
        require_eligible_trigger_body(trigger)

    assert raised.value.code == code
    # The refusal says which axis refused, and never quotes the body.
    assert "trigger" in str(raised.value)
    assert "customer_id" not in str(raised.value)


def test_a_missing_trigger_is_refused_rather_than_treated_as_empty() -> None:
    with pytest.raises(TriggerNotEligibleError) as raised:
        require_eligible_trigger_body(None)
    assert raised.value.code == "TRIGGER_MISSING"


async def test_a_postgres_trigger_is_joined_to_the_routine_that_holds_its_body(
    session: AsyncSession,
) -> None:
    """The decision this task had to make: the join lives on the trigger axis,
    because only the trigger knows the firing table that the function's `NEW`
    refers to."""
    org, datasource, schema = await seed_estate(session)
    routine = _routine(org, datasource, schema)
    trigger = _pg_trigger(org, datasource, schema)
    session.add_all([routine, trigger])
    await session.flush()

    body = await trigger_body(session, datasource, trigger)

    assert body.firing_table == "public.orders"
    assert body.sql == PG_FUNCTION_BODY
    assert body.routine_id == routine.id
    assert body.via_routine == "public.note_order"
    assert body.unresolved_reason is None


async def test_the_action_routine_join_is_scoped_to_the_trigger_own_datasource(
    session: AsyncSession,
) -> None:
    """INV-5. A function of the same name in another source of the same
    organization is not this trigger's body."""
    org, datasource, schema = await seed_estate(session)
    _other_org, other_datasource, other_schema = await seed_estate(session, organization=org)
    session.add(_routine(org, other_datasource, other_schema))
    trigger = _pg_trigger(org, datasource, schema)
    session.add(trigger)
    await session.flush()

    body = await trigger_body(session, datasource, trigger)

    assert body.sql is None
    assert body.unresolved_reason == TRIGGER_ROUTINE_NOT_CAPTURED


async def test_an_action_routine_nobody_captured_is_a_marker_not_silence(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session)
    trigger = _pg_trigger(org, datasource, schema, action_routine="app.gone")
    session.add(trigger)
    await session.flush()

    body = await trigger_body(session, datasource, trigger)
    assert (body.sql, body.unresolved_reason) == (None, TRIGGER_ROUTINE_NOT_CAPTURED)

    result = unreachable_body_marker(body, dialect="postgres", sql_hash="h" * 64)

    assert result.is_fully_parsed is False
    assert result.edges, "a trigger whose code cannot be read reported nothing at all"
    [edge] = result.edges
    assert edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    assert edge.unparsed_reason is not None
    assert edge.unparsed_reason.endswith(f"({TRIGGER_ROUTINE_NOT_CAPTURED})")


async def test_an_overloaded_action_routine_is_ambiguous_never_guessed(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session)
    session.add_all(
        [
            _routine(org, datasource, schema),
            _routine(org, datasource, schema, signature="(integer)"),
        ]
    )
    trigger = _pg_trigger(org, datasource, schema)
    session.add(trigger)
    await session.flush()

    body = await trigger_body(session, datasource, trigger)

    assert (body.sql, body.unresolved_reason) == (None, TRIGGER_ROUTINE_AMBIGUOUS)


async def test_an_action_routine_whose_body_is_withheld_says_so(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session)
    session.add(_routine(org, datasource, schema, screening_status="QUARANTINED"))
    trigger = _pg_trigger(org, datasource, schema)
    session.add(trigger)
    await session.flush()

    body = await trigger_body(session, datasource, trigger)

    assert body.unresolved_reason == TRIGGER_ROUTINE_BODY_WITHHELD
    assert body.via_routine == "public.note_order"


def test_the_trigger_axis_reports_an_unreachable_routine_in_the_routine_axis_words() -> None:
    """`footprint_gaps` matches one pattern against both tables, so the two
    vocabularies must be the same three words. Restated rather than imported,
    because `routine_call_descent` imports `routine_lineage_edges`."""
    assert TRIGGER_ROUTINE_NOT_CAPTURED == CALLEE_NOT_CAPTURED
    assert TRIGGER_ROUTINE_AMBIGUOUS == CALLEE_AMBIGUOUS
    assert TRIGGER_ROUTINE_BODY_WITHHELD == CALLEE_BODY_WITHHELD


async def test_a_trigger_with_its_own_body_never_consults_the_routine_axis(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    trigger = _trigger(org, datasource, schema, action_routine=None)
    session.add(trigger)
    await session.flush()

    body = await trigger_body(session, datasource, trigger)

    assert body.sql == TSQL_TRIGGER_BODY
    assert (body.routine_id, body.via_routine) == (None, None)


async def test_the_firing_table_takes_its_own_schema_when_the_engine_gives_one(
    session: AsyncSession,
) -> None:
    """Oracle lets a trigger's owner differ from its table's, which is exactly why
    `table_schema_name` exists."""
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    trigger = _trigger(org, datasource, schema, table_schema_name="sales")
    session.add(trigger)
    await session.flush()

    body = await trigger_body(session, datasource, trigger)

    assert body.firing_table == "sales.orders"


# ---------------------------------------------------------------------------
# 3. Persistence: review state, replacement, and value-freedom.
# ---------------------------------------------------------------------------


async def _persisted(
    session: AsyncSession, *, agent_proposal: bool, review_mode: str = "auto_active"
) -> tuple[DataSource, MetadataTrigger, list[TriggerLineageEdge]]:
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    await seed_table(session, org, datasource, schema, name="orders")
    await seed_table(session, org, datasource, schema, name="audit")
    trigger = _trigger(org, datasource, schema)
    session.add(trigger)
    await session.flush()
    result = parse_trigger_lineage(
        TSQL_TRIGGER_BODY, dialect="tsql", firing_table="public.orders"
    )
    written = await persist_trigger_edges(
        session,
        datasource=datasource,
        trigger=trigger,
        result=result,
        review_mode=review_mode,
        threshold=0.0,
        created_by=AGENT,
        agent_proposal=agent_proposal,
    )
    await session.flush()
    return datasource, trigger, written


async def test_an_agents_trigger_edge_is_never_active_on_arrival(
    session: AsyncSession,
) -> None:
    """A threshold of 0.0 under `auto_active` would activate every edge a *person*
    parsed. Only ACTIVE edges steer retrieval and tool generation, so an agent's
    undecided proposal must influence nothing."""
    _datasource, _trigger_row, written = await _persisted(session, agent_proposal=True)

    assert written, "nothing was written, so the review rule is not being tested"
    assert {row.review_status for row in written} == {"PROPOSED"}


async def test_a_persons_parse_still_obeys_the_organizations_review_mode(
    session: AsyncSession,
) -> None:
    _datasource, _trigger_row, written = await _persisted(session, agent_proposal=False)

    assert {row.review_status for row in written} == {"ACTIVE"}


async def test_an_unparsed_marker_is_active_because_it_is_a_gap_not_a_proposal(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session)
    trigger = _pg_trigger(org, datasource, schema, action_routine="app.gone")
    session.add(trigger)
    await session.flush()
    body = await trigger_body(session, datasource, trigger)
    result = unreachable_body_marker(body, dialect="postgres", sql_hash="h" * 64)

    written = await persist_trigger_edges(
        session,
        datasource=datasource,
        trigger=trigger,
        result=result,
        review_mode="require_review",
        threshold=1.0,
        created_by=AGENT,
        agent_proposal=True,
    )
    await session.flush()

    [marker] = written
    assert marker.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    assert marker.review_status == "ACTIVE"


async def test_the_edge_resolves_the_firing_table_and_the_written_table_to_ids(
    session: AsyncSession,
) -> None:
    datasource, _trigger_row, written = await _persisted(session, agent_proposal=True)

    [edge] = [row for row in written if row.is_write]
    assert edge.source_table_id is not None, "the firing table did not resolve to a catalog id"
    assert edge.target_table_id is not None
    assert edge.source_table_id != edge.target_table_id


async def test_a_reparse_replaces_an_undecided_edge_and_leaves_a_decided_one(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    trigger = _trigger(org, datasource, schema)
    session.add(trigger)
    await session.flush()
    result = parse_trigger_lineage(
        TSQL_TRIGGER_BODY, dialect="tsql", firing_table="public.orders"
    )
    first = await persist_trigger_edges(
        session,
        datasource=datasource,
        trigger=trigger,
        result=result,
        review_mode="require_review",
        threshold=1.0,
        created_by=AGENT,
        agent_proposal=True,
    )
    await session.flush()
    [edge] = first
    edge.review_status = "REJECTED"
    edge.reviewed_by = "steward-1"
    await session.flush()

    await persist_trigger_edges(
        session,
        datasource=datasource,
        trigger=trigger,
        result=result,
        review_mode="require_review",
        threshold=1.0,
        created_by=AGENT,
        agent_proposal=True,
    )
    await session.flush()

    rows = (await session.scalars(select(TriggerLineageEdge))).all()
    assert [row.review_status for row in rows] == ["REJECTED"]
    assert [row.reviewed_by for row in rows] == ["steward-1"]


async def test_a_reparse_never_writes_a_second_row_for_the_same_fact(
    session: AsyncSession,
) -> None:
    """The natural key is enforced, so a writer that appended instead of replacing
    would fail here rather than in production."""
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    trigger = _trigger(org, datasource, schema)
    session.add(trigger)
    await session.flush()
    result = parse_trigger_lineage(
        TSQL_TRIGGER_BODY, dialect="tsql", firing_table="public.orders"
    )
    for _ in range(2):
        await persist_trigger_edges(
            session,
            datasource=datasource,
            trigger=trigger,
            result=result,
            review_mode="auto_active",
            threshold=1.0,
            created_by=AGENT,
            agent_proposal=False,
        )
        await session.flush()

    rows = (await session.scalars(select(TriggerLineageEdge))).all()
    assert len(rows) == 1


async def test_no_trigger_body_text_reaches_a_stored_row(session: AsyncSession) -> None:
    """INV-6. The body is a value-bearing, injection-carrying artifact; a stored
    edge carries names, ordinals and reason codes."""
    _datasource, _trigger_row, written = await _persisted(session, agent_proposal=True)

    # A word that is in the body and in nothing an edge legitimately carries.
    assert "NOCOUNT" in TSQL_TRIGGER_BODY
    for row in written:
        rendered = " ".join(
            str(getattr(row, column.name, None)) for column in row.__table__.columns
        )
        assert "NOCOUNT" not in rendered, rendered
        assert "SELECT" not in rendered.upper().replace("SELECTED", ""), rendered


# ---------------------------------------------------------------------------
# 4. The agent, and the gap register.
# ---------------------------------------------------------------------------


async def _run(session: AsyncSession, org: Organization, **request: Any) -> Any:
    return await run_lineage_agent(
        session,
        org.id,
        request=TaskAgentRunRequest(capabilities=(CAPABILITY_TRIGGER_LINEAGE,), **request),
        settings=agent_settings(),
        triggered_by=human(org),
    )


async def test_the_agent_proposes_a_postgres_triggers_write_through_its_function(
    session: AsyncSession,
) -> None:
    """End to end on the engine that needed the join: the trigger has no body, the
    function has one, and the edge that lands says the write comes out of the
    table the trigger fires on."""
    org, datasource, schema = await seed_estate(session)
    orders = await seed_table(session, org, datasource, schema, name="orders")
    audit = await seed_table(session, org, datasource, schema, name="audit")
    routine = _routine(org, datasource, schema)
    trigger = _pg_trigger(org, datasource, schema)
    session.add_all([routine, trigger])
    await session.flush()
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    [item] = outcome.items
    assert (item.action, item.subject_name) == (ACTION_PROPOSED, "public.note_order")
    rows = list((await session.scalars(select(TriggerLineageEdge))).all())
    [edge] = [row for row in rows if row.is_write]
    assert (edge.source_table, edge.target_table) == ("public.orders", "public.audit")
    assert (edge.source_table_id, edge.target_table_id) == (orders.id, audit.id)
    assert edge.review_status == "PROPOSED"
    assert edge.created_by == AGENT
    assert edge.trigger_id == trigger.id
    # Which body was actually read is recorded, both ways.
    assert edge.routine_id == routine.id
    assert edge.via_routine is None or edge.via_routine == "public.note_order"


async def test_the_agent_leaves_the_action_routine_free_to_be_parsed_on_its_own_axis(
    session: AsyncSession,
) -> None:
    """The reason trigger edges are a table of their own: filing them under the
    function's id would make the function look already parsed, and the routine
    axis would stop looking at it."""
    from aida.procedure_lineage_models import DeepProcedureLineageEdge

    org, datasource, schema = await seed_estate(session)
    await seed_table(session, org, datasource, schema, name="orders")
    await seed_table(session, org, datasource, schema, name="audit")
    session.add_all([_routine(org, datasource, schema), _pg_trigger(org, datasource, schema)])
    await session.flush()
    await register_agent(session, org, principal=AGENT)

    await _run(session, org)

    assert (await session.scalars(select(DeepProcedureLineageEdge))).all() == []


async def test_a_trigger_the_agent_already_examined_is_left_alone(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    await seed_table(session, org, datasource, schema, name="orders")
    await seed_table(session, org, datasource, schema, name="audit")
    session.add(_trigger(org, datasource, schema))
    await session.flush()
    await register_agent(session, org, principal=AGENT)

    first = await _run(session, org)
    second = await _run(session, org)

    assert [item.action for item in first.items] == [ACTION_PROPOSED]
    assert second.items == []
    assert len((await session.scalars(select(TriggerLineageEdge))).all()) == 1


async def test_a_trigger_with_neither_a_body_nor_a_named_routine_is_not_a_candidate(
    session: AsyncSession,
) -> None:
    """There is nothing to hand the parser, so this is the source withholding the
    body -- not work an agent can do."""
    org, datasource, schema = await seed_estate(session)
    session.add(
        _pg_trigger(org, datasource, schema, action_routine=None),
    )
    await session.flush()
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    assert outcome.items == []
    assert (await session.scalars(select(TriggerLineageEdge))).all() == []


async def test_a_dry_run_reports_and_writes_nothing(session: AsyncSession) -> None:
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    await seed_table(session, org, datasource, schema, name="orders")
    session.add(_trigger(org, datasource, schema))
    await session.flush()
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org, dry_run=True)

    assert [item.action for item in outcome.items] == ["WOULD_PROPOSE"]
    assert (await session.scalars(select(TriggerLineageEdge))).all() == []


async def _gaps(session: AsyncSession, org: Organization) -> dict[str, int]:
    read = await footprint_gaps(
        session,
        context=human(org, roles=frozenset({"MetadataAdmin", "DataSteward"})),
        settings=agent_settings(),
        organization_id=org.id,
    )
    return read.totals


async def test_a_trigger_awaiting_a_parse_is_counted_as_the_agents_backlog(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    session.add(_trigger(org, datasource, schema))
    await session.flush()

    assert (await _gaps(session, org)).get("LINEAGE_AWAITING_PARSE") == 1


async def test_a_parsed_trigger_stops_being_reported_as_a_gap(
    session: AsyncSession,
) -> None:
    """The thing this task set out to change: the gap closes when the body has
    actually been parsed, not when the trigger was inventoried."""
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    await seed_table(session, org, datasource, schema, name="orders")
    await seed_table(session, org, datasource, schema, name="audit")
    session.add(_trigger(org, datasource, schema))
    await session.flush()
    before = await _gaps(session, org)
    await register_agent(session, org, principal=AGENT)

    await _run(session, org)

    after = await _gaps(session, org)
    assert before.get("LINEAGE_AWAITING_PARSE") == 1
    assert after.get("LINEAGE_AWAITING_PARSE") is None
    # And the edge it produced is waiting for a person, counted as such.
    assert after.get("LINEAGE_AWAITING_REVIEW") == 1


async def test_a_trigger_the_parser_could_not_fully_read_is_counted_as_unparsed(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session, dialect="oracle")
    trigger = _trigger(org, datasource, schema, body_sql_redacted=ORACLE_TRIGGER_BODY)
    session.add(trigger)
    await session.flush()
    result = parse_trigger_lineage(
        ORACLE_TRIGGER_BODY, dialect="oracle", firing_table="public.orders"
    )
    await persist_trigger_edges(
        session,
        datasource=datasource,
        trigger=trigger,
        result=result,
        review_mode="require_review",
        threshold=1.0,
        created_by=AGENT,
        agent_proposal=True,
    )
    await session.flush()

    assert (await _gaps(session, org)).get("LINEAGE_UNPARSED_STATEMENTS") == 1


async def test_an_unreachable_action_routine_is_routed_to_the_source_administrator(
    session: AsyncSession,
) -> None:
    org, datasource, schema = await seed_estate(session)
    trigger = _pg_trigger(org, datasource, schema, action_routine="app.gone")
    session.add(trigger)
    await session.flush()
    await register_agent(session, org, principal=AGENT)

    await _run(session, org)

    totals = await _gaps(session, org)
    assert totals.get("LINEAGE_UNRESOLVED_CALLEE") == 1
    assert totals.get("LINEAGE_UNPARSED_STATEMENTS") == 1
    assert totals.get("LINEAGE_AWAITING_REVIEW") is None
