"""R11-FP01: triggers and sequences as their own kinds, and the two axes beside them.

Four things are proven here, each of which fails against the tree before this
change:

1. **A trigger is modelled as a trigger.** It has a firing table, an event set,
   a timing, an orientation and either a body or a named action routine, and the
   envelope carries all of them. Nothing here squeezes one into
   `DiscoveredRoutine`, and the assertions would not survive an attempt to.
2. **A sequence is modelled as a sequence, and never carries its position.** The
   declaration round-trips; the current value has nowhere to land, on the
   envelope or in storage, and that is asserted structurally rather than left to
   review.
3. **Each engine gets its own honest answer.** Snowflake has sequences and no
   triggers, BigQuery and Databricks have neither, and those are
   `NOT_APPLICABLE` rather than `UNSUPPORTED` -- a distinction the capability
   vocabulary exists for and that a blanket verdict would destroy.
4. **The two axes beside them.** PostgreSQL aggregate and window functions are
   discovered with their definition refused rather than left out, and Databricks
   reads the view and routine axes it used to skip.

Real-engine evidence for PostgreSQL and SQL Server is in
`tests/test_triggers_sequences_live.py`; this file covers the four engines whose
servers this repository cannot reach, plus the row-shape and selection logic
that no live run exercises (a PostgreSQL window function cannot be created
without a C-language function, so it is only reachable here).
"""

from __future__ import annotations

import inspect
from dataclasses import fields as dataclass_fields
from unittest.mock import MagicMock, patch

import pytest

from aida.connectors import bigquery, databricks, oracle, postgres, snowflake, sqlserver
from aida.connectors.base import (
    TRIGGER_EVENTS,
    DiscoveredCatalog,
    DiscoveredSchema,
    DiscoveredSequence,
    DiscoveredTable,
    DiscoveredTrigger,
    attach_native_objects,
    build_sequences,
    build_triggers,
)
from aida.connectors.databricks import DatabricksConnector
from aida.connectors.discovery import build_routines, facet_read_scope
from aida.connectors.oracle import _assemble_catalog as _oracle_assemble
from aida.connectors.oracle import _OracleEnvelopeRows
from aida.connectors.registry import connector_registry
from aida.connectors.sqlserver import _assemble_catalog as _sqlserver_assemble
from aida.discovery_selection import (
    OBJECT_KINDS,
    DiscoverySelection,
    apply_selection,
    kind_capabilities,
)
from aida.envelope_models import MetadataSequence, MetadataTrigger

_DATABRICKS_DSN = (
    '{"server_hostname": "dbc-1.cloud.databricks.com", '
    '"http_path": "/sql/1.0/warehouses/abc", "access_token": "dapi0", '
    '"catalog": "main", "schema": "analytics"}'
)


# ---------------------------------------------------------------------------
# Row shapes: the builders, and the honesty rules they carry.
# ---------------------------------------------------------------------------


def test_a_trigger_carries_its_firing_table_events_timing_and_action() -> None:
    triggers = build_triggers(
        [
            {
                "trigger_schema": "sales",
                "trigger_name": "audit_orders",
                "table_name": "orders",
                "timing": "AFTER",
                "orientation": "ROW",
                "on_insert": True,
                "on_update": True,
                "on_delete": False,
                "on_truncate": False,
                "is_enabled": True,
                "action_routine": "sales.note_order",
                "body": None,
            }
        ]
    )

    trigger = triggers["sales"][0]
    assert trigger.table_name == "orders"
    assert trigger.timing == "AFTER"
    assert trigger.events == ("INSERT", "UPDATE")
    assert trigger.orientation == "ROW"
    assert trigger.action_routine == "sales.note_order"


def test_trigger_events_keep_one_order_across_engines() -> None:
    """Three of the four engines with triggers report the events as separate
    flags or bits and one reports a phrase. A set would make the two shapes
    indistinguishable and the order arbitrary; `TRIGGER_EVENTS` fixes it, so a
    reader comparing two engines' triggers is comparing like with like.
    """
    from_flags = build_triggers(
        [
            {
                "trigger_schema": "s",
                "trigger_name": "t",
                "table_name": "x",
                "on_delete": True,
                "on_insert": True,
            }
        ]
    )["s"][0]
    from_phrase = build_triggers(
        [
            {
                "trigger_schema": "s",
                "trigger_name": "t",
                "table_name": "x",
                "events": "DELETE OR INSERT",
            }
        ]
    )["s"][0]

    assert from_flags.events == from_phrase.events == ("INSERT", "DELETE")
    assert TRIGGER_EVENTS == ("INSERT", "UPDATE", "DELETE", "TRUNCATE")


def test_an_unrecognised_event_phrase_contributes_no_event_rather_than_prose() -> None:
    """INV-6's reasoning applied to a vocabulary rather than to a value: an
    engine phrase this code does not recognise is dropped, so no source prose
    reaches platform state under the guise of an event name. The trigger is
    still discovered -- losing the events is not losing the object.
    """
    trigger = build_triggers(
        [
            {
                "trigger_schema": "s",
                "trigger_name": "t",
                "table_name": "x",
                "events": "SOMETHING THE ENGINE INVENTED",
            }
        ]
    )["s"][0]

    assert trigger.events == ()
    assert trigger.name == "t"


def test_a_missing_trigger_body_is_unavailable_with_a_reason_not_an_empty_body() -> None:
    """The same three-state rule `build_routines` carries. A parser has to tell
    "there is no text" from "we were not given the text", and an empty string is
    a third, different state: a trigger whose body really is empty.
    """
    withheld = build_triggers(
        [{"trigger_schema": "s", "trigger_name": "t", "table_name": "x", "body": None}]
    )["s"][0]
    assert withheld.body_sql is None
    assert withheld.unavailable_reason == "source returned no trigger body"

    empty = build_triggers(
        [{"trigger_schema": "s", "trigger_name": "t", "table_name": "x", "body": ""}]
    )["s"][0]
    assert empty.body_sql is None, "a blank body is still absence, and says so"

    present = build_triggers(
        [
            {
                "trigger_schema": "s",
                "trigger_name": "t",
                "table_name": "x",
                "body": "BEGIN INSERT INTO audit SELECT 1; END",
            }
        ]
    )["s"][0]
    assert present.unavailable_reason is None


def test_a_sequence_carries_its_declaration_and_the_column_it_generates() -> None:
    sequence = build_sequences(
        [
            {
                "sequence_schema": "sales",
                "sequence_name": "order_id_seq",
                "data_type": "bigint",
                "start_with": "5",
                "increment_by": "10",
                "minimum_bound": "1",
                "maximum_bound": "9999",
                "cache_size": "3",
                "cycles": True,
                "owned_by_table": "orders",
                "owned_by_column": "order_id",
            }
        ]
    )["sales"][0]

    assert sequence.increment_by == "10"
    assert sequence.minimum_bound == "1"
    assert sequence.maximum_bound == "9999"
    assert sequence.cache_size == "3"
    assert sequence.cycles is True
    assert (sequence.owned_by_table, sequence.owned_by_column) == ("orders", "order_id")


def test_a_sequence_builder_ignores_a_current_position_offered_to_it() -> None:
    """INV-6, enforced where it would actually be broken. Every engine's own
    sequence view offers the current position beside the declaration, so the
    likely way it enters is a connector that selects the whole row. The builder
    reads named keys only, so an offered position is dropped rather than
    smuggled through into a dataclass field or an attributes bag.
    """
    sequence = build_sequences(
        [
            {
                "sequence_schema": "s",
                "sequence_name": "q",
                "increment_by": "1",
                "last_value": "8814",
                "last_number": "8814",
                "current_value": "8814",
                "next_value": "8815",
            }
        ]
    )["s"][0]

    rendered = repr(sequence)
    assert "8814" not in rendered and "8815" not in rendered
    assert sequence.attributes == {}


@pytest.mark.parametrize(
    "subject", [DiscoveredTrigger, DiscoveredSequence], ids=lambda t: t.__name__
)
def test_neither_new_envelope_type_has_a_field_for_a_live_source_value(subject: type) -> None:
    """A naming ratchet in the spirit of `tests/test_inv6_value_freedom.py`'s,
    aimed at the one value these two kinds would plausibly acquire. A sequence's
    position is the obvious one; a trigger's is subtler -- `old_value` /
    `new_value` are the names an implementer reaching for "what did the trigger
    see" would use, and each would be a customer's row.
    """
    banned = (
        "last_value",
        "last_number",
        "current_value",
        "next_value",
        "last_used",
        "old_value",
        "new_value",
        "sample",
        "row_value",
    )
    offending = [
        field.name
        for field in dataclass_fields(subject)
        if any(fragment in field.name.lower() for fragment in banned)
    ]
    assert offending == []


@pytest.mark.parametrize(
    "model", [MetadataTrigger, MetadataSequence], ids=lambda m: m.__tablename__
)
def test_neither_new_table_has_a_column_for_a_live_source_value(model: type) -> None:
    """The same ratchet against the persisted schema, because storage is where a
    value would survive a backup and a log shipper. `tests/test_inv6_value_freedom.py`
    reflects over every mapped column already; this is the targeted version, and
    it names the columns the two engines would hand over if asked.
    """
    banned = ("last_value", "last_number", "current_value", "next_value", "last_used")
    columns = {column.name for column in model.__table__.columns}
    assert not (columns & set(banned))
    assert "fingerprint" in columns, "the tripwire: reflection must see real columns"


def test_attaching_native_objects_adds_a_schema_that_holds_only_them() -> None:
    """A schema whose entire content is an audit trigger is a real schema. The
    same argument `assemble_catalog` makes for a routine-only schema: dropping it
    would make the inventory silently incomplete for exactly the estates --
    procedural, audit-heavy ones -- where this axis matters most.
    """
    base = (
        DiscoveredCatalog(
            name="bank",
            schemas=(
                DiscoveredSchema(
                    name="sales",
                    tables=(DiscoveredTable(name="orders", object_type="TABLE", columns=()),),
                ),
            ),
        ),
    )

    attached = attach_native_objects(
        base,
        triggers=build_triggers(
            [{"trigger_schema": "audit", "trigger_name": "t", "table_name": "orders"}]
        ),
        sequences=build_sequences([{"sequence_schema": "sales", "sequence_name": "q"}]),
    )

    by_name = {schema.name: schema for schema in attached[0].schemas}
    assert set(by_name) == {"sales", "audit"}
    assert by_name["audit"].triggers[0].name == "t"
    assert by_name["audit"].tables == ()
    assert by_name["sales"].sequences[0].name == "q"


def test_attaching_nothing_returns_the_tree_untouched() -> None:
    """An adapter that reads neither axis must be byte-for-byte unaffected, or
    every existing connector test would be asserting against a rebuilt tree.
    """
    base = (DiscoveredCatalog(name="bank", schemas=()),)
    assert attach_native_objects(base) is base


# ---------------------------------------------------------------------------
# Discovery selection: two more kinds, and NOT_SELECTED.
# ---------------------------------------------------------------------------


def _estate() -> tuple[DiscoveredCatalog, ...]:
    return (
        DiscoveredCatalog(
            name="bank",
            schemas=(
                DiscoveredSchema(
                    name="sales",
                    tables=(DiscoveredTable(name="orders", object_type="TABLE", columns=()),),
                    triggers=(DiscoveredTrigger(name="audit_orders", table_name="orders"),),
                    sequences=(DiscoveredSequence(name="order_id_seq"),),
                ),
                DiscoveredSchema(
                    name="staging",
                    tables=(),
                    triggers=(DiscoveredTrigger(name="stage_trg", table_name="orders"),),
                    sequences=(DiscoveredSequence(name="stage_seq"),),
                ),
            ),
        ),
    )


def test_both_new_kinds_are_selectable() -> None:
    assert "TRIGGER" in OBJECT_KINDS
    assert "SEQUENCE" in OBJECT_KINDS


def test_a_selection_without_them_keeps_neither_and_counts_both() -> None:
    outcome = apply_selection(_estate(), DiscoverySelection(object_kinds=["TABLE"]))

    kept = outcome.catalogs[0].schemas[0]
    assert kept.triggers == () and kept.sequences == ()
    assert outcome.excluded["TRIGGER"] == 2
    assert outcome.excluded["SEQUENCE"] == 2


def test_a_selection_naming_them_keeps_them() -> None:
    outcome = apply_selection(
        _estate(), DiscoverySelection(object_kinds=["TRIGGER", "SEQUENCE"])
    )

    kept = outcome.catalogs[0].schemas[0]
    assert [trigger.name for trigger in kept.triggers] == ["audit_orders"]
    assert [sequence.name for sequence in kept.sequences] == ["order_id_seq"]
    assert outcome.excluded["TABLE"] == 1


def test_a_trigger_is_scoped_by_its_own_name_not_its_firing_tables() -> None:
    """`stage_trg` lives in `staging` and fires on `sales.orders`. A selection
    that excludes `staging` must stop maintaining it: whether it fires on an
    in-scope table is a lineage fact, and using that as the scope would
    re-admit an object the operator excluded.
    """
    outcome = apply_selection(_estate(), DiscoverySelection(exclude_schemas=["staging"]))

    names = {
        trigger.name
        for schema in outcome.catalogs[0].schemas
        for trigger in schema.triggers
    }
    assert names == {"audit_orders"}
    assert outcome.excluded["TRIGGER"] == 1
    assert outcome.excluded["SEQUENCE"] == 1


def test_an_excluded_kind_reads_not_selected_rather_than_unsupported() -> None:
    """The distinction the vocabulary exists for. PostgreSQL discovers both
    kinds, so a scan that leaves them out is a choice this deployment made --
    which is neither a missing feature nor a missing concept.
    """
    reads = {
        read.kind: read
        for read in kind_capabilities(
            "postgres",
            connector_registry.definition("postgres").capabilities,
            DiscoverySelection(object_kinds=["TABLE"]),
        )
    }

    assert reads["TRIGGER"].inventory == "NOT_SELECTED"
    assert reads["TRIGGER"].definition == "NOT_SELECTED"
    assert reads["SEQUENCE"].inventory == "NOT_SELECTED"
    # Still NOT_APPLICABLE: a sequence has no defining text whether or not this
    # scan reads sequences, and masking it would lose the more specific fact.
    assert reads["SEQUENCE"].definition == "NOT_APPLICABLE"


def test_a_kind_the_engine_lacks_stays_not_applicable_when_excluded() -> None:
    """Snowflake does not gain a trigger by a selection excluding triggers."""
    reads = {
        read.kind: read
        for read in kind_capabilities(
            "snowflake",
            connector_registry.definition("snowflake").capabilities,
            DiscoverySelection(object_kinds=["TABLE"]),
        )
    }
    assert reads["TRIGGER"].inventory == "NOT_APPLICABLE"
    assert reads["SEQUENCE"].inventory == "NOT_SELECTED"


@pytest.mark.parametrize(
    ("engine", "trigger_inventory", "trigger_definition", "sequence_inventory"),
    [
        # PostgreSQL's trigger definition is PARTIAL and that is the engine, not
        # the adapter: a PostgreSQL trigger has no body, so the adapter records
        # the action function and that function's body arrives on the routine
        # axis. Oracle and SQL Server keep the code in the trigger.
        ("postgres", "SUPPORTED", "PARTIAL", "SUPPORTED"),
        ("oracle", "SUPPORTED", "SUPPORTED", "SUPPORTED"),
        ("sqlserver", "SUPPORTED", "SUPPORTED", "SUPPORTED"),
        ("snowflake", "NOT_APPLICABLE", "NOT_APPLICABLE", "SUPPORTED"),
        ("bigquery", "NOT_APPLICABLE", "NOT_APPLICABLE", "NOT_APPLICABLE"),
        ("databricks", "NOT_APPLICABLE", "NOT_APPLICABLE", "NOT_APPLICABLE"),
    ],
)
def test_every_engine_gets_its_own_answer_for_the_two_kinds(
    engine: str,
    trigger_inventory: str,
    trigger_definition: str,
    sequence_inventory: str,
) -> None:
    """Six engines, six answers, none of them a blanket verdict -- which is the
    whole point: NOT_APPLICABLE and UNSUPPORTED mean different things to a UI
    deciding whether to offer a native type, and a table of expected values is
    the only way to keep all six honest at once.
    """
    reads = {
        read.kind: read
        for read in kind_capabilities(
            engine, connector_registry.definition(engine).capabilities
        )
    }
    assert reads["TRIGGER"].inventory == trigger_inventory
    assert reads["TRIGGER"].definition == trigger_definition
    assert reads["SEQUENCE"].inventory == sequence_inventory
    # A sequence's declaration is its metadata, so there is no definition text
    # to retrieve anywhere.
    assert reads["SEQUENCE"].definition == "NOT_APPLICABLE"


# ---------------------------------------------------------------------------
# Per-adapter: the flag is backed by the query, and the query by the flag.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "engine", "probes"),
    [
        (postgres, "postgres", ("pg_trigger", "pg_sequence")),
        (oracle, "oracle", ("ALL_TRIGGERS", "ALL_SEQUENCES")),
        (sqlserver, "sqlserver", ("sys.triggers", "sys.sequences")),
        (snowflake, "snowflake", ("information_schema.sequences",)),
    ],
)
def test_a_declared_axis_is_backed_by_a_named_query(
    module: object, engine: str, probes: tuple[str, ...]
) -> None:
    """INV-9 the way this repository already enforces it for the 1.1 axes: a
    `True` flag is paired with an assertion on the adapter's own source, so the
    flag cannot outlive the behaviour.
    """
    source = inspect.getsource(module)  # type: ignore[arg-type]
    for probe in probes:
        assert probe.casefold() in source.casefold(), probe


@pytest.mark.parametrize(
    ("engine", "flag"),
    [
        ("snowflake", "triggers"),
        ("bigquery", "triggers"),
        ("bigquery", "sequences"),
        ("databricks", "triggers"),
        ("databricks", "sequences"),
    ],
)
def test_an_engine_without_the_concept_declares_the_flag_false(engine: str, flag: str) -> None:
    """The flag stays False on an engine that has no such object, which is what
    lets `discovery_selection` answer NOT_APPLICABLE. A `True` here would be a
    claim about the engine's own SQL, not about this adapter.
    """
    assert connector_registry.definition(engine).capabilities[flag] is False


def test_neither_engine_without_the_concept_reads_its_catalog_view() -> None:
    """The negative half, and not a tautology: an adapter could read another
    engine's catalog view by copy-paste, and the flag would then be the only
    thing keeping the row honest. Probed by *catalog object name* rather than by
    the phrase "create trigger", which both modules now mention in prose while
    explaining why they have none.
    """
    catalog_objects = (
        "pg_trigger",
        "pg_sequence",
        "all_triggers",
        "all_sequences",
        "sys.triggers",
        "sys.sequences",
        "information_schema.sequences",
    )
    for module in (bigquery, databricks):
        source = inspect.getsource(module).casefold()
        for name in catalog_objects:
            assert name not in source, (module.__name__, name)


# ---------------------------------------------------------------------------
# Oracle: one phrase for the timing, another for the events.
# ---------------------------------------------------------------------------


def _oracle_catalog(**envelope: object) -> tuple[DiscoveredCatalog, ...]:
    return _oracle_assemble("BANK", [], [], [], envelope=_OracleEnvelopeRows(**envelope))  # type: ignore[arg-type]


def test_an_oracle_trigger_splits_its_type_phrase_into_timing_and_orientation() -> None:
    """`ALL_TRIGGERS.TRIGGER_TYPE` packs both into one string, and every other
    engine reports them separately. Matched against a closed table rather than
    split on whitespace, so a phrase this code does not know contributes no
    timing instead of publishing Oracle prose as one.
    """
    catalogs = _oracle_catalog(
        triggers=(
            {
                "OWNER": "SALES",
                "TRIGGER_NAME": "AUDIT_ORDERS",
                "TABLE_OWNER": "SALES",
                "TABLE_NAME": "ORDERS",
                "TRIGGER_TYPE": "BEFORE EACH ROW",
                "TRIGGERING_EVENT": "INSERT OR UPDATE",
                "STATUS": "ENABLED",
                "BASE_OBJECT_TYPE": "TABLE",
                "TRIGGER_BODY": "BEGIN NULL; END;",
            },
        )
    )

    trigger = catalogs[0].schemas[0].triggers[0]
    assert trigger.timing == "BEFORE"
    assert trigger.orientation == "ROW"
    assert trigger.events == ("INSERT", "UPDATE")
    assert trigger.is_enabled is True
    assert trigger.body_sql == "BEGIN NULL; END;"
    assert trigger.unavailable_reason is None
    # Same owner as the trigger, so no redundant schema name is carried.
    assert trigger.table_schema is None


def test_an_oracle_trigger_on_another_schemas_table_records_that_schema() -> None:
    """Oracle is the one engine where a trigger's owner may differ from its
    table's, which is the case `DiscoveredTrigger.table_schema` exists for.
    """
    catalogs = _oracle_catalog(
        triggers=(
            {
                "OWNER": "AUDIT",
                "TRIGGER_NAME": "WATCH_ORDERS",
                "TABLE_OWNER": "SALES",
                "TABLE_NAME": "ORDERS",
                "TRIGGER_TYPE": "AFTER STATEMENT",
                "TRIGGERING_EVENT": "DELETE",
                "STATUS": "DISABLED",
                "BASE_OBJECT_TYPE": "TABLE",
                "TRIGGER_BODY": "BEGIN NULL; END;",
            },
        )
    )

    trigger = next(
        trigger
        for schema in catalogs[0].schemas
        for trigger in schema.triggers
    )
    assert trigger.table_schema == "SALES"
    assert trigger.orientation == "STATEMENT"
    assert trigger.events == ("DELETE",)
    assert trigger.is_enabled is False


def test_a_refused_all_triggers_query_lands_on_the_catalog_not_as_no_triggers() -> None:
    """A dictionary view a least-privilege reader may not hold must not read as
    "this estate has no triggers". `_OracleEnvelopeRows.unavailable` is where
    that lands, exactly as it does for `ALL_SOURCE` and `ALL_TAB_PRIVS`.
    """
    catalogs = _oracle_catalog(unavailable=(("triggers", "DatabaseError: ORA-00942"),))

    assert catalogs[0].attributes["envelope_v11_unavailable"]["triggers"].startswith(
        "DatabaseError"
    )


def test_an_oracle_sequence_has_no_start_with_and_says_so_by_absence() -> None:
    """`ALL_SEQUENCES` has no `START WITH` column: the start is consumed by the
    first `NEXTVAL` and only `LAST_NUMBER` remains, which is the one thing this
    axis must never read. `start_with` is honestly absent rather than
    back-computed from the position it would have had to read.
    """
    catalogs = _oracle_catalog(
        sequences=(
            {
                "SEQUENCE_OWNER": "SALES",
                "SEQUENCE_NAME": "ORDER_SEQ",
                "MIN_VALUE": "1",
                "MAX_VALUE": "999999999999999999999999999",
                "INCREMENT_BY": "1",
                "CYCLE_FLAG": "N",
                "CACHE_SIZE": "20",
                # Offered, never read. Oracle's own view carries it.
                "LAST_NUMBER": "8814",
            },
        )
    )

    sequence = catalogs[0].schemas[0].sequences[0]
    assert sequence.start_with is None
    assert sequence.maximum_bound == "999999999999999999999999999"
    assert sequence.cycles is False
    assert sequence.cache_size == "20"
    assert "8814" not in repr(sequence)


# ---------------------------------------------------------------------------
# SQL Server: one row per trigger, from three system views.
# ---------------------------------------------------------------------------


def test_a_sqlserver_trigger_arrives_with_its_events_and_body() -> None:
    catalogs = _sqlserver_assemble(
        "bank",
        [],
        [],
        [],
        trigger_rows=[
            {
                "trigger_schema": "sales",
                "trigger_name": "audit_orders",
                "table_name": "orders",
                "table_schema": "sales",
                "timing": "AFTER",
                "orientation": "STATEMENT",
                "on_insert": 1,
                "on_update": 1,
                "on_delete": 0,
                "on_truncate": 0,
                "is_enabled": 1,
                "body": "CREATE TRIGGER audit_orders ON sales.orders AFTER INSERT AS SELECT 1",
                "unavailable_reason": None,
            }
        ],
        sequence_rows=[
            {
                "sequence_schema": "sales",
                "sequence_name": "order_seq",
                "data_type": "bigint",
                "start_with": "5",
                "increment_by": "10",
                "minimum_bound": "1",
                "maximum_bound": "9999",
                "cache_size": "3",
                "cycles": 1,
            }
        ],
    )

    schema = catalogs[0].schemas[0]
    trigger = schema.triggers[0]
    assert trigger.events == ("INSERT", "UPDATE")
    assert trigger.timing == "AFTER"
    assert trigger.body_sql is not None
    assert trigger.action_routine is None, "SQL Server keeps the code in the trigger"
    assert schema.sequences[0].cycles is True


def test_an_encrypted_sqlserver_trigger_is_unavailable_with_its_reason() -> None:
    """`WITH ENCRYPTION`, or a principal without `VIEW DEFINITION`, yields a NULL
    definition. That is not a trigger with an empty body, and the difference has
    to survive into the envelope.
    """
    catalogs = _sqlserver_assemble(
        "bank",
        [],
        [],
        [],
        trigger_rows=[
            {
                "trigger_schema": "sales",
                "trigger_name": "hidden",
                "table_name": "orders",
                "on_insert": 1,
                "body": None,
                "unavailable_reason": "sys.sql_modules returned no definition: encrypted",
            }
        ],
    )

    trigger = catalogs[0].schemas[0].triggers[0]
    assert trigger.body_sql is None
    assert trigger.unavailable_reason is not None
    assert "encrypted" in trigger.unavailable_reason


def test_the_sqlserver_trigger_query_never_aggregates_the_definition() -> None:
    """A regression guard with a specific failure behind it: `sys.sql_modules
    .definition` is `nvarchar(max)` and SQL Server refuses `MAX()` on that type,
    so a `GROUP BY` over the joined event rows would fail at run time on every
    instance. The events come through an `OUTER APPLY` instead, and this is what
    keeps them there.
    """
    assert "OUTER APPLY" in sqlserver._TRIGGER_SQL
    assert "GROUP BY" not in sqlserver._TRIGGER_SQL
    assert "MAX(m.definition)" not in sqlserver._TRIGGER_SQL
    # A literal '%' would make the schema-scope pushdown refuse the query.
    assert "%" not in sqlserver._TRIGGER_SQL
    assert "%" not in sqlserver._SEQUENCE_SQL


def test_neither_sqlserver_query_reads_a_sequences_current_position() -> None:
    assert "current_value" not in sqlserver._SEQUENCE_SQL
    assert "last_used_value" not in sqlserver._SEQUENCE_SQL


# ---------------------------------------------------------------------------
# PostgreSQL: the aggregate and window functions, and what is not read.
# ---------------------------------------------------------------------------


def test_the_aggregate_query_asks_for_no_definition_at_all() -> None:
    """The reason this is a second query rather than a widened `IN` list:
    `pg_get_functiondef` raises on either prokind, so a single query calling it
    would fail outright against any database holding one aggregate. A `CASE`
    guard is not equivalent -- PostgreSQL does not guarantee a branch's function
    call goes unevaluated -- so the guarantee has to be that the call is absent.
    """
    # The *call* is what must be absent. The function's name still appears in
    # the query, as the refusal reason the row carries -- which is the point of
    # the row.
    assert "pg_get_functiondef(" not in postgres._AGGREGATE_ROUTINE_SQL
    assert "pg_get_functiondef refuses" in postgres._AGGREGATE_ROUTINE_SQL
    assert "prokind IN ('a', 'w')" in postgres._AGGREGATE_ROUTINE_SQL
    # And the original query is untouched, so an ordinary function's definition
    # is still fetched exactly as before.
    assert "pg_get_functiondef(p.oid)" in postgres._ROUTINE_SQL
    assert "prokind IN ('f', 'p')" in postgres._ROUTINE_SQL
    # The parameter query covers all four, or an aggregate would arrive with no
    # signature and collide with a same-named function.
    assert "prokind IN ('f', 'p', 'a', 'w')" in postgres._ROUTINE_PARAMETER_SQL


def test_a_window_function_is_a_function_with_its_definition_refused() -> None:
    """Only reachable here: a PostgreSQL window function cannot be created
    without a C-language function, so no DDL a live test could run would produce
    one. The row shape is the engine's, and the assertion is that "the definition
    cannot be fetched" lands as an unavailable definition rather than as a
    missing object.
    """
    routines = build_routines(
        [
            {
                "routine_schema": "public",
                "routine_name": "rank_within_region",
                "specific_name": "41001",
                "routine_type": "FUNCTION",
                "native_subtype": "WINDOW",
                "language": "internal",
                "body": None,
                "return_type": "integer",
                "unavailable_reason": (
                    "pg_get_functiondef refuses prokind w: PostgreSQL exposes no "
                    "CREATE statement for an aggregate or window function"
                ),
            }
        ]
    )

    routine = routines["public"][0]
    assert routine.routine_type == "FUNCTION"
    assert routine.attributes["native_subtype"] == "WINDOW"
    assert routine.body_sql is None
    assert routine.unavailable_reason is not None
    assert "prokind w" in routine.unavailable_reason
    assert routine.return_type == "integer"


def test_the_postgres_sequence_query_reads_the_catalog_not_the_view() -> None:
    """`pg_sequences` is a view that carries `last_value`; `pg_sequence` is the
    catalog and has only the declaration. Reading the catalog is what makes it
    impossible to pick up the position by accident, which is a stronger
    guarantee than remembering not to select a column.
    """
    assert "FROM pg_sequence " in postgres._SEQUENCE_SQL
    assert "pg_sequences" not in postgres._SEQUENCE_SQL
    assert "last_value" not in postgres._SEQUENCE_SQL


def test_the_postgres_trigger_query_excludes_constraint_enforcement_triggers() -> None:
    """Those are already discovered as constraints. Reporting them again would
    double every referential rule in the estate and publish an implementation
    detail as user code.
    """
    assert "NOT t.tgisinternal" in postgres._TRIGGER_SQL
    assert "NOT t.tgisinternal" in postgres._TRIGGER_BATCH_SQL


def test_the_streaming_path_scopes_triggers_to_each_batch() -> None:
    """A trigger is keyed by the table it fires on, so it pages with the table
    roster; a sequence belongs to a schema and is fetched once. An audit estate
    can hold more triggers than tables, which is the shape `discover_streaming`
    exists for -- so fetching them all up front would put the 100K-table
    timeout back where it was.
    """
    assert "_batch(schema_name, table_name)" in postgres._TRIGGER_BATCH_SQL
    streaming = inspect.getsource(postgres.PostgresConnector.discover_streaming)
    assert "_TRIGGER_BATCH_SQL" in streaming
    assert "_SEQUENCE_SQL" in streaming


# ---------------------------------------------------------------------------
# Databricks: the two axes it used to skip.
# ---------------------------------------------------------------------------

_DATABRICKS_COLUMNS = [
    {
        "table_schema": "analytics",
        "table_name": "active_customers",
        "table_type": "VIEW",
        "column_name": "id",
        "ordinal_position": 1,
        "data_type": "bigint",
        "is_nullable": "NO",
        "column_default": None,
        "table_comment": None,
        "column_comment": None,
    }
]
_DATABRICKS_VIEW_SQL = "SELECT id FROM main.analytics.customers WHERE active"


def _databricks_fetch_sequence(
    *,
    views: object = None,
    routines: object = None,
    parameters: object = None,
) -> list[object]:
    """The exact `fetchall()` sequence `discover()` drives, in call order.

    Written out rather than matched on SQL, the way the Snowflake harness is, so
    that adding a query to `discover()` fails loudly here instead of silently
    shifting a response onto the wrong statement.
    """
    return [
        _DATABRICKS_COLUMNS,
        [],
        [],
        [{"schema_name": "analytics", "comment": None}],
        [{"catalog_name": "main", "comment": None}],
        (
            views
            if views is not None
            else [
                {
                    "table_schema": "analytics",
                    "table_name": "active_customers",
                    "view_definition": _DATABRICKS_VIEW_SQL,
                    "is_updatable": "NO",
                    "check_option": "NONE",
                }
            ]
        ),
        (
            routines
            if routines is not None
            else [
                {
                    "routine_schema": "analytics",
                    "routine_name": "normalize_name",
                    "specific_name": "normalize_name",
                    "routine_type": "FUNCTION",
                    "data_type": "STRING",
                    "routine_body": "SQL",
                    "routine_definition": "UPPER(TRIM(raw))",
                    "external_language": None,
                    "is_deterministic": "YES",
                    "security_type": "DEFINER",
                    "comment": "Canonical name",
                }
            ]
        ),
        (
            parameters
            if parameters is not None
            else [
                {
                    "specific_schema": "analytics",
                    "specific_name": "normalize_name",
                    "ordinal_position": 0,
                    "parameter_mode": None,
                    "is_result": "YES",
                    "parameter_name": None,
                    "data_type": "STRING",
                    "parameter_default": None,
                },
                {
                    "specific_schema": "analytics",
                    "specific_name": "normalize_name",
                    "ordinal_position": 1,
                    "parameter_mode": "IN",
                    "is_result": "NO",
                    "parameter_name": "raw",
                    "data_type": "STRING",
                    "parameter_default": None,
                },
            ]
        ),
    ]


async def _databricks_discover(
    sequence: list[object], *, execute: object = None
) -> tuple[DiscoveredCatalog, ...]:
    connector = DatabricksConnector(_DATABRICKS_DSN)
    connection = MagicMock()
    cursor = MagicMock()
    connection.cursor.return_value = cursor
    cursor.fetchall.side_effect = sequence
    if execute is not None:
        cursor.execute.side_effect = execute
    with patch.object(connector, "_get_connection", return_value=connection):
        return await connector.discover()


def test_databricks_declares_the_axes_it_now_reads() -> None:
    capabilities = DatabricksConnector(_DATABRICKS_DSN).capabilities
    assert capabilities.views is True
    assert capabilities.routines is True
    # Still not claimed: Unity Catalog's privilege model is not the SQL grant
    # model that axis records, and it has neither of the two new kinds.
    assert capabilities.grants is False
    assert capabilities.triggers is False
    assert capabilities.sequences is False
    advertised = connector_registry.definition("databricks").capabilities
    assert advertised["views"] is True
    assert advertised["routines"] is True
    assert advertised["grants"] is False


async def test_a_databricks_view_definition_round_trips() -> None:
    catalogs = await _databricks_discover(_databricks_fetch_sequence())

    view = catalogs[0].schemas[0].tables[0]
    assert view.view_definition is not None
    assert view.view_definition.definition_sql == _DATABRICKS_VIEW_SQL
    assert view.view_definition.unavailable_reason is None
    assert view.view_definition.is_updatable is False


async def test_a_databricks_routine_round_trips_with_its_parameters() -> None:
    catalogs = await _databricks_discover(_databricks_fetch_sequence())

    routine = catalogs[0].schemas[0].routines[0]
    assert routine.name == "normalize_name"
    assert routine.routine_type == "FUNCTION"
    assert routine.body_sql == "UPPER(TRIM(raw))"
    assert routine.return_type == "STRING"
    assert routine.language == "SQL"
    assert routine.source_description == "Canonical name"
    # The `is_result` row is the function's return, not a parameter.
    assert [(p.name, p.physical_type) for p in routine.parameters] == [("raw", "STRING")]


async def test_a_view_the_principal_cannot_read_is_unavailable_not_empty() -> None:
    """A Unity Catalog principal without `USE SCHEMA` on the view's own schema
    gets a NULL definition. Recorded as unavailable with a reason, so a lineage
    parser does not report the view as having no sources.
    """
    catalogs = await _databricks_discover(
        _databricks_fetch_sequence(
            views=[
                {
                    "table_schema": "analytics",
                    "table_name": "active_customers",
                    "view_definition": None,
                    "is_updatable": None,
                    "check_option": None,
                }
            ]
        )
    )

    definition = catalogs[0].schemas[0].tables[0].view_definition
    assert definition is not None
    assert definition.definition_sql is None
    assert definition.unavailable_reason is not None
    assert "not readable by this principal" in definition.unavailable_reason


async def test_a_metastore_without_the_optional_view_columns_is_retried_narrow() -> None:
    """`is_updatable` and `check_option` are the two columns an older metastore
    is most likely not to have. Losing them must cost the columns, not the axis:
    the query is retried without them before anything is recorded as
    unavailable, which is the difference between a degraded read and a silent
    "this catalog has no views".
    """
    narrow_rows = [
        {
            "table_schema": "analytics",
            "table_name": "active_customers",
            "view_definition": _DATABRICKS_VIEW_SQL,
        }
    ]

    def _refuse_the_wide_view_query(sql: str, *arguments: object) -> None:
        if "is_updatable" in sql:
            raise RuntimeError("[UNRESOLVED_COLUMN] is_updatable")

    catalogs = await _databricks_discover(
        [*_databricks_fetch_sequence()[:5], narrow_rows, [], []],
        execute=_refuse_the_wide_view_query,
    )

    definition = catalogs[0].schemas[0].tables[0].view_definition
    assert definition is not None
    assert definition.definition_sql == _DATABRICKS_VIEW_SQL
    assert definition.is_updatable is None, "the column was not read, so no claim"


async def test_a_refused_routine_query_lands_on_the_catalog_as_a_reason() -> None:
    """The whole basis for claiming the axis without a live workspace: a refusal
    shrinks the envelope and says why, rather than failing the run or reading as
    "this catalog has no routines".

    R11-FP02 follow-through: refused the way Unity Catalog refuses -- SQLSTATE 42501 in
    the error's `context` -- and inside the `facet_read_scope` the discovery activity
    binds, because only a refusal is absorbed now. It used to be a plain
    `RuntimeError`, which the adapter absorbed like any other failure.
    """
    from databricks.sql import exc as databricks_exc

    def _refuse_routines(sql: str, *arguments: object) -> None:
        if "information_schema.routines" in sql:
            raise databricks_exc.ServerOperationError(
                "[INSUFFICIENT_PERMISSIONS] USE SCHEMA", {"sqlState": "42501"}
            )

    with facet_read_scope() as scope:
        catalogs = await _databricks_discover(
            [*_databricks_fetch_sequence()[:6], []],
            execute=_refuse_routines,
        )

    assert catalogs[0].schemas[0].routines == ()
    assert "routines" in catalogs[0].attributes["envelope_v11_unavailable"]
    assert scope.outcomes["routine_bodies"][0].value == "PERMISSION_DENIED"


async def test_databricks_reports_neither_a_trigger_nor_a_sequence() -> None:
    """Unity Catalog has neither object, so both tuples are empty and the flags
    are False -- which is how `discovery_selection` reaches NOT_APPLICABLE
    rather than UNSUPPORTED.
    """
    catalogs = await _databricks_discover(_databricks_fetch_sequence())

    for schema in catalogs[0].schemas:
        assert schema.triggers == ()
        assert schema.sequences == ()
