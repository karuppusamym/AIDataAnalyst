"""R11-FP01: triggers, sequences and refused definitions, against real servers.

A trigger discovered against a real trigger is worth more than any number of
hand-built rows, because every interesting fact about one is an engine detail a
fake cannot get wrong for you: PostgreSQL packs the timing, the orientation and
the event set into a single `tgtype` bitmask and hides an FK's own enforcement
triggers behind `tgisinternal`; SQL Server splits the events across
`sys.trigger_events` and has no BEFORE at all. Both files' unit tests assert the
shape this module asserts the *engine* produces.

Each engine's private source is the footprint journey's, reused by importing its
fixtures, so this file creates no server of its own and skips with the journey
when a server is unreachable. It adds a sequence, an identity-owned sequence, an
audit table, a trigger that writes it, and an aggregate function -- then reads
the source through the adapter and checks every fact the two new envelope axes
claim.

**What this file deliberately does not prove.** A PostgreSQL window function
(`prokind = 'w'`) cannot be created without a C-language function, so no DDL
this file could execute would produce one; the aggregate below covers
`prokind = 'a'`, and the window case stays unit-tested in
`tests/test_connectors_triggers_and_sequences.py`. And nothing here asserts
persistence, because there is none yet -- see the engine capability matrix's
declared gaps.
"""

from __future__ import annotations

from collections.abc import Iterable

from aida.connectors.base import (
    DiscoveredCatalog,
    DiscoveredRoutine,
    DiscoveredSequence,
    DiscoveredTrigger,
)
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    CONNECTORS,
    JourneySource,
    _postgres,
    _sqlserver,
    source,
)

SCHEMA = "footprint_context_sample"

#: An audit table, a sequence declared with every parameter set to a value that
#: is not the engine's default, a sequence owned by a column, a trigger that
#: writes the audit table, and an aggregate function.
#:
#: Every declaration parameter is deliberately non-default (`START WITH 5`, not
#: 1; `CACHE 3`, not 20 or 50): a test against defaults passes just as well when
#: the adapter reads the wrong column or reads nothing and the engine fills in a
#: default, and that is the failure this fixture exists to catch.
_OBJECTS = {
    "postgres": f"""
        CREATE SEQUENCE {SCHEMA}.order_audit_seq
            START WITH 5 INCREMENT BY 10 MINVALUE 1 MAXVALUE 9999 CACHE 3 CYCLE;
        CREATE TABLE {SCHEMA}.order_audit (
            audit_id bigserial PRIMARY KEY,
            customer_id integer
        );
        CREATE FUNCTION {SCHEMA}.note_order() RETURNS trigger LANGUAGE plpgsql AS $body$
        BEGIN
            INSERT INTO {SCHEMA}.order_audit (customer_id) VALUES (NEW.customer_id);
            RETURN NEW;
        END;
        $body$;
        CREATE TRIGGER note_order_trg
            AFTER INSERT OR UPDATE ON {SCHEMA}.orders
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.note_order();
        CREATE AGGREGATE {SCHEMA}.sum_amount(integer) (
            SFUNC = int4pl, STYPE = integer, INITCOND = '0'
        );
    """,  # noqa: S608 -- DDL for a private scratch database built by this test; the only interpolation is the module constant `SCHEMA`
    "sqlserver": f"""
        CREATE SEQUENCE {SCHEMA}.order_audit_seq
            AS bigint START WITH 5 INCREMENT BY 10
            MINVALUE 1 MAXVALUE 9999 CACHE 3 CYCLE;
        GO
        CREATE TABLE {SCHEMA}.order_audit (
            audit_id bigint IDENTITY(1, 1) PRIMARY KEY,
            customer_id int
        );
        GO
        CREATE TRIGGER note_order_trg ON {SCHEMA}.orders
        AFTER INSERT, UPDATE AS
        BEGIN
            SET NOCOUNT ON;
            INSERT INTO {SCHEMA}.order_audit (customer_id)
            SELECT i.customer_id FROM inserted i;
        END;
    """,  # noqa: S608 -- DDL for a private scratch database built by this test; the only interpolation is the module constant `SCHEMA`
}


def _triggers(catalogs: Iterable[DiscoveredCatalog]) -> dict[str, DiscoveredTrigger]:
    return {
        trigger.name.lower(): trigger
        for catalog in catalogs
        for schema in catalog.schemas
        for trigger in schema.triggers
    }


def _sequences(catalogs: Iterable[DiscoveredCatalog]) -> dict[str, DiscoveredSequence]:
    return {
        sequence.name.lower(): sequence
        for catalog in catalogs
        for schema in catalog.schemas
        for sequence in schema.sequences
    }


def _routines(catalogs: Iterable[DiscoveredCatalog]) -> dict[str, DiscoveredRoutine]:
    return {
        routine.name.lower(): routine
        for catalog in catalogs
        for schema in catalog.schemas
        for routine in schema.routines
    }


async def test_a_real_trigger_arrives_with_its_firing_table_event_and_timing(
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """The fact the axis exists for: a trigger that writes another table.

    `order_audit` is written by nothing a view definition, a call site or a dbt
    model could reveal -- the only record of the path is this trigger, and the
    firing table is the half of it discovery captures.
    """
    await source.execute(_OBJECTS[source.connector_type])
    catalogs = await CONNECTORS[source.connector_type](source.dsn).discover()

    trigger = _triggers(catalogs)["note_order_trg"]
    assert trigger.table_name.lower() == "orders"
    assert trigger.timing == "AFTER"
    assert set(trigger.events) == {"INSERT", "UPDATE"}
    assert trigger.is_enabled is True
    # PostgreSQL fires per row and says so; SQL Server has no `FOR EACH ROW` at
    # all, so its only honest answer is STATEMENT. Asserted per engine rather
    # than as one value, because a single expected value would mean one of the
    # two engines was being described wrongly.
    expected_orientation = "ROW" if source.connector_type == "postgres" else "STATEMENT"
    assert trigger.orientation == expected_orientation


async def test_a_trigger_body_is_available_or_unavailable_with_its_reason(
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """The three-state honesty rule, on a real engine, per engine.

    SQL Server keeps the trigger's code in the trigger, so the body arrives and
    `unavailable_reason` is None. PostgreSQL keeps it in a function, so there is
    no body to give and the reason says which function has it -- which is not
    the same state as a refusal and not the same state as an empty body.

    The body is checked for an *identifier* it must mention, never a literal: a
    trigger body is the same injection and value-bearing surface a routine body
    is, and INV-6 is about the values in it.
    """
    await source.execute(_OBJECTS[source.connector_type])
    catalogs = await CONNECTORS[source.connector_type](source.dsn).discover()
    trigger = _triggers(catalogs)["note_order_trg"]

    if source.connector_type == "postgres":
        assert trigger.body_sql is None
        assert trigger.unavailable_reason is not None
        assert "action_routine" in trigger.unavailable_reason
        assert trigger.action_routine is not None
        assert trigger.action_routine.lower() == f"{SCHEMA}.note_order"
        # And the function it names is itself discovered, with its own body --
        # which is what makes the PARTIAL definition state honest rather than a
        # dead end.
        assert _routines(catalogs)["note_order"].body_sql is not None
    else:
        assert trigger.unavailable_reason is None
        assert trigger.body_sql is not None
        assert "order_audit" in trigger.body_sql
        assert trigger.action_routine is None


async def test_an_enforcement_trigger_is_not_reported_as_user_code(
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """`orders` has a foreign key, so PostgreSQL has created two internal
    triggers to enforce it. Reporting those would double every referential rule
    in the estate and publish an implementation detail as user code; they are
    already discovered as constraints. `NOT tgisinternal` is what excludes them,
    and this is the assertion that keeps it there.

    SQL Server creates no such trigger for a foreign key, so the same assertion
    is simply "only the trigger we created is there" -- which is still worth
    making, because a query that dropped `is_ms_shipped = 0` would fail it.
    """
    await source.execute(_OBJECTS[source.connector_type])
    catalogs = await CONNECTORS[source.connector_type](source.dsn).discover()

    assert set(_triggers(catalogs)) == {"note_order_trg"}
    constraints = {
        constraint.constraint_type
        for catalog in catalogs
        for schema in catalog.schemas
        for table in schema.tables
        if table.name.lower() == "orders"
        for constraint in table.constraints
    }
    assert "FOREIGN_KEY" in constraints, "the FK is discovered as a constraint, as before"


async def test_a_real_sequence_arrives_as_its_declaration_and_never_its_position(
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """Every declaration parameter, read back from the engine that stored it.

    The second half is the one that matters most: `DiscoveredSequence` has no
    field for the sequence's current position, so no amount of engine cooperation
    can put one on the envelope. That is asserted structurally rather than by
    inspecting a value, because the point is that the value never arrives.
    """
    await source.execute(_OBJECTS[source.connector_type])
    catalogs = await CONNECTORS[source.connector_type](source.dsn).discover()

    sequence = _sequences(catalogs)["order_audit_seq"]
    assert sequence.increment_by == "10"
    assert sequence.minimum_bound == "1"
    assert sequence.maximum_bound == "9999"
    assert sequence.cache_size == "3"
    assert sequence.cycles is True
    # Oracle is the engine with no START WITH column; both engines here have one.
    assert sequence.start_with == "5"

    fields = set(vars(sequence).keys()) if hasattr(sequence, "__dict__") else set()
    banned = {"last_value", "last_number", "current_value", "next_value", "last_used_value"}
    assert not (fields & banned)


async def test_a_column_owned_sequence_names_the_column_it_generates(
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """The edge that makes a sequence part of the footprint rather than a loose
    object: which column's values this generator produces.

    PostgreSQL records it in `pg_depend` and the adapter reads it. SQL Server
    records nothing comparable -- an `IDENTITY` column is a column property, not
    a sequence object, so there is no second sequence to find and no ownership to
    report. Asserted as each engine's own answer, so neither is described by the
    other's.
    """
    await source.execute(_OBJECTS[source.connector_type])
    catalogs = await CONNECTORS[source.connector_type](source.dsn).discover()
    sequences = _sequences(catalogs)

    if source.connector_type == "postgres":
        owned = sequences["order_audit_audit_id_seq"]
        assert owned.owned_by_table == "order_audit"
        assert owned.owned_by_column == "audit_id"
        # The standalone sequence has no owner, and says so rather than
        # inheriting the other one's.
        assert sequences["order_audit_seq"].owned_by_table is None
    else:
        assert set(sequences) == {"order_audit_seq"}
        assert sequences["order_audit_seq"].owned_by_table is None


async def test_an_aggregate_is_discovered_with_its_definition_refused(
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """R11-FP01's second half, against the engine that refuses the definition.

    `pg_get_functiondef` raises on `prokind = 'a'`. That is why the aggregate was
    missing and it is not the same fact as "the object does not exist": the
    identity, the signature, the parameter list and the return type are all real
    and all discovered here, and only the definition is UNAVAILABLE with the
    engine's refusal as its reason.

    A real engine is the only place this can be proved, because the failure mode
    being ruled out is that the widened query *raises* -- which a fake row set
    can never do.
    """
    if source.connector_type != "postgres":
        return  # only PostgreSQL has the prokind distinction this covers
    await source.execute(_OBJECTS[source.connector_type])
    catalogs = await CONNECTORS[source.connector_type](source.dsn).discover()

    aggregate = _routines(catalogs)["sum_amount"]
    assert aggregate.routine_type == "FUNCTION"
    assert aggregate.attributes["native_subtype"] == "AGGREGATE"
    assert aggregate.body_sql is None
    assert aggregate.unavailable_reason is not None
    assert "pg_get_functiondef" in aggregate.unavailable_reason
    assert aggregate.return_type == "integer"
    assert [parameter.physical_type for parameter in aggregate.parameters] == ["integer"]
    # And the run as a whole survived: an ordinary function is still discovered
    # with its body, which is the regression a raising definition call would
    # have produced.
    assert _routines(catalogs)["note_order"].body_sql is not None
