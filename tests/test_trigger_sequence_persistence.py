"""R11-FP01: discovered triggers and sequences are persisted, reconciled and counted.

The two axes were discovered but nothing wrote them: `metadata_trigger` and
`metadata_sequence` existed with their migration, all six adapters queried the source,
and `ingestion.persist_envelope_extensions` had no writer for either -- so a scan built
the rows and dropped them. These tests cover the writer, the four ways a run must *not*
retire what it did not look at, and the counts a receipt publishes.

**Why the reconciliation half is most of this file.** Persisting is one upsert per axis
and is hard to get wrong quietly. Retirement is the opposite: every wrong answer is
silent, arrives one run later, and looks like the source changed. There are four
separate ways a FULL pass can read silence as deletion here, and each gets its own test
with a control that shows the pass retiring when it legitimately should:

1. the object is out of the selection's scope (`out_of_scope_existing`);
2. the source refused the facet's read (`refused_facet_existing`);
3. the run was INCREMENTAL and enumerated nothing;
4. the delivery has no field for the axis at all -- every push batch
   (`ingestion.NATIVE_OBJECT_AXES`). This is the one with no precedent to copy, and
   the one that would have tombstoned a whole estate's triggers on the first nightly
   push after this feature shipped.

SQLite in memory, as `tests/test_envelope_v11.py` established for these axes; the CHECK
constraints that make an unavailable body unstorable as an empty one are real there. The
live half -- create a trigger on a real engine, scan, drop it, scan again, watch the row
tombstone -- is `tests/test_triggers_sequences_persist_live.py`, and nothing in this file
substitutes for it.
"""

from __future__ import annotations

import json
from dataclasses import fields as dataclass_fields
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.testing import ActivityEnvironment

import aida.workflows.activities as activities
from aida.connectors.base import (
    ConnectorCapabilities,
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredSchema,
    DiscoveredSequence,
    DiscoveredTable,
    DiscoveredTrigger,
)
from aida.discovery_receipt import FACET_SEQUENCES, FACET_TRIGGERS, DiscoveryReceipt
from aida.discovery_selection import DiscoverySelection
from aida.envelope_models import AVAILABLE, UNAVAILABLE, MetadataSequence, MetadataTrigger
from aida.ingestion import (
    NATIVE_OBJECT_AXES,
    EnvelopeScope,
    deprecate_missing_envelope_extensions,
    persist_envelope_extensions,
)
from aida.models import AnalysisRun, DataSource, MetadataSchema
from aida.workflows.activities import (
    out_of_scope_existing,
    persist_discovery_snapshot,
    refused_facet_existing,
)

# The 1.1 axis harness: one in-memory database per test, and a datasource under a real
# organization / line of business / domain / project, exactly as the sibling axes use it.
from tests.test_envelope_v11 import (
    _datasource,
    _run,
    session,  # noqa: F401 -- used by fixture name
)
from tests.test_inv6_value_freedom import (
    SENTINEL_CUSTOMER,
    SENTINEL_LITERAL,
    SENTINEL_ROW_VALUE,
)

# The chunked push harness `test_push_ingestion_receipt` and `test_push_selection` share.
from tests.test_push_ingestion_receipt import factory  # noqa: F401 -- used by fixture name

_SENTINELS = (SENTINEL_LITERAL, SENTINEL_ROW_VALUE, SENTINEL_CUSTOMER)

#: A SQL Server / Oracle shaped trigger: the code lives in the trigger.
_BODY = (
    "BEGIN INSERT INTO customer.account_audit (account_id) "  # noqa: S608 -- a fixture string
    "SELECT account_id FROM inserted; END;"
)


def _trigger(
    name: str = "account_audit_trg",
    *,
    table_name: str = "account",
    body_sql: str | None = _BODY,
    unavailable_reason: str | None = None,
    **overrides: Any,
) -> DiscoveredTrigger:
    return DiscoveredTrigger(
        name=name,
        table_name=table_name,
        timing="AFTER",
        events=("INSERT", "UPDATE"),
        orientation="STATEMENT",
        is_enabled=True,
        body_sql=body_sql,
        unavailable_reason=unavailable_reason,
        attributes={"native_subtype": "DML"},
        **overrides,
    )


def _postgres_trigger(name: str = "note_order_trg") -> DiscoveredTrigger:
    """PostgreSQL's shape: no body of its own, and a function that has one.

    The fourth availability state `MetadataTrigger`'s docstring names, and the only one
    of the four that no other axis has.
    """
    return DiscoveredTrigger(
        name=name,
        table_name="orders",
        timing="AFTER",
        events=("INSERT",),
        orientation="ROW",
        is_enabled=True,
        action_routine="customer.note_order",
        body_sql=None,
        unavailable_reason=(
            "a PostgreSQL trigger has no body of its own: it executes the function named "
            "by action_routine"
        ),
    )


def _sequence(name: str = "account_seq", **overrides: Any) -> DiscoveredSequence:
    declaration: dict[str, Any] = {
        "data_type": "bigint",
        "start_with": "5",
        "increment_by": "10",
        "minimum_bound": "1",
        "maximum_bound": "9999",
        "cache_size": "3",
        "cycles": True,
        "owned_by_table": "account",
        "owned_by_column": "account_id",
        "source_description": "surrogate keys for deposit accounts",
    }
    declaration.update(overrides)
    return DiscoveredSequence(name=name, **declaration)


def _catalog(
    *,
    triggers: tuple[DiscoveredTrigger, ...] = (),
    sequences: tuple[DiscoveredSequence, ...] = (),
    tables: tuple[DiscoveredTable, ...] | None = None,
    schema_name: str = "customer",
) -> tuple[DiscoveredCatalog, ...]:
    table = DiscoveredTable(
        name="account",
        object_type="BASE_TABLE",
        columns=(
            DiscoveredColumn(
                name="account_id", ordinal_position=1, physical_type="bigint", nullable=False
            ),
        ),
    )
    schema = DiscoveredSchema(
        name=schema_name,
        tables=(table,) if tables is None else tables,
        triggers=triggers,
        sequences=sequences,
    )
    return (DiscoveredCatalog(name="bank", schemas=(schema,)),)


async def _scan(
    session: AsyncSession,  # noqa: F811 -- a local parameter, not the imported fixture
    datasource: DataSource,
    catalogs: tuple[DiscoveredCatalog, ...],
    *,
    reconcile: bool = False,
    native_axes_read: frozenset[str] = NATIVE_OBJECT_AXES,
) -> dict[str, int]:
    """Both persistence halves, driven the way the pull path drives them."""
    run = await _run(session, datasource)
    await persist_discovery_snapshot(
        session,
        run,
        datasource,
        catalogs,
        deprecate_missing=reconcile,
        connector_capabilities={"triggers": True, "sequences": True},
    )
    counts = await persist_envelope_extensions(
        session,
        datasource,
        catalogs,
        deprecate_missing=reconcile,
        analysis_run_id=run.id,
        native_axes_read=native_axes_read,
    )
    await session.commit()
    return counts


async def _triggers(session: AsyncSession, datasource: DataSource) -> dict[str, MetadataTrigger]:  # noqa: F811 -- a local parameter, not the imported fixture
    rows = await session.scalars(
        select(MetadataTrigger).where(
            MetadataTrigger.organization_id == datasource.organization_id,
            MetadataTrigger.datasource_id == datasource.id,
        )
    )
    return {row.name: row for row in rows.all()}


async def _sequences(session: AsyncSession, datasource: DataSource) -> dict[str, MetadataSequence]:  # noqa: F811 -- a local parameter, not the imported fixture
    rows = await session.scalars(
        select(MetadataSequence).where(
            MetadataSequence.organization_id == datasource.organization_id,
            MetadataSequence.datasource_id == datasource.id,
        )
    )
    return {row.name: row for row in rows.all()}


# --- the axes land ----------------------------------------------------------


async def test_a_discovered_trigger_and_sequence_are_persisted(session: AsyncSession) -> None:  # noqa: F811
    """The headline, and the whole of what was missing: a scan's rows reach storage.

    Asserted fact by fact rather than as a count, because a writer that persists a row
    with the right name and the wrong firing table is worse than one that persists
    nothing -- the firing table is the lineage fact the trigger axis exists for, and a
    wrong one is a data path pointed at the wrong table.
    """
    datasource = await _datasource(session)

    counts = await _scan(
        session, datasource, _catalog(triggers=(_trigger(),), sequences=(_sequence(),))
    )

    assert counts["triggers"] == 1
    assert counts["sequences"] == 1
    trigger = (await _triggers(session, datasource))["account_audit_trg"]
    assert trigger.table_name == "account"
    assert trigger.timing == "AFTER"
    assert trigger.events == ["INSERT", "UPDATE"]
    assert trigger.orientation == "STATEMENT"
    assert trigger.is_enabled is True
    assert trigger.attributes == {"native_subtype": "DML"}
    assert trigger.status == "ACTIVE"
    assert trigger.deprecated_at is None
    # The body went through the one write point: stored redacted, fingerprinted over the
    # original, screened at write time with the classifier version beside the verdict.
    assert trigger.availability == AVAILABLE
    assert trigger.body_sql_redacted is not None
    assert trigger.body_fingerprint is not None
    assert trigger.redaction_status in {"PARSED", "LEXICAL"}
    assert trigger.screening_status == "CLEAN"
    assert trigger.screening_version is not None
    assert trigger.unavailable_reason is None

    sequence = (await _sequences(session, datasource))["account_seq"]
    assert sequence.data_type == "bigint"
    assert (sequence.start_with, sequence.increment_by) == ("5", "10")
    assert (sequence.minimum_bound, sequence.maximum_bound) == ("1", "9999")
    assert sequence.cache_size == "3"
    assert sequence.cycles is True
    # The edge that makes a sequence part of the footprint rather than a loose object.
    assert (sequence.owned_by_table, sequence.owned_by_column) == ("account", "account_id")
    assert sequence.status == "ACTIVE"

    # INV-5: both rows restate the tenant and the source, and are not reached only
    # through their parent schema.
    for row in (trigger, sequence):
        assert row.organization_id == datasource.organization_id
        assert row.datasource_id == datasource.id


async def test_a_trigger_with_no_body_of_its_own_keeps_the_engine_s_own_reason(
    session: AsyncSession,  # noqa: F811
) -> None:
    """PostgreSQL's trigger has no body; that is not a refusal and not an empty body.

    Three states have to survive the writer, and the CHECK constraint on
    `metadata_trigger` is what makes the third structurally unstorable as the second:
    UNAVAILABLE with a NULL body, the connector's own explanation kept rather than
    replaced by the generic default, and `action_routine` naming where the code
    actually is.
    """
    datasource = await _datasource(session)

    await _scan(session, datasource, _catalog(triggers=(_postgres_trigger(),)))

    trigger = (await _triggers(session, datasource))["note_order_trg"]
    assert trigger.availability == UNAVAILABLE
    assert trigger.body_sql_redacted is None
    assert trigger.body_fingerprint is None
    assert trigger.action_routine == "customer.note_order"
    # The connector's reason, not `_availability`'s default: overwriting it would
    # collapse "this engine keeps the code elsewhere" into "the source would not give
    # the definition text", and only one of those sends an administrator hunting for a
    # grant.
    assert trigger.unavailable_reason is not None
    assert "action_routine" in trigger.unavailable_reason


async def test_two_triggers_of_one_name_on_different_tables_both_survive_a_rescan(
    session: AsyncSession,  # noqa: F811
) -> None:
    """PostgreSQL scopes a trigger name to its table, so `audit_trg` on two tables is
    two objects. Keyed `(schema_id, name)` they would collide, the second would
    overwrite the first, and every FULL rescan would tombstone one of them -- the
    overload defect `routine_signature` exists to prevent, in a second place. The
    rescan is part of the test because a collision that only shows up on reconciliation
    is exactly the kind that reaches production.
    """
    datasource = await _datasource(session)
    both = (
        _trigger("audit_trg", table_name="account"),
        _trigger("audit_trg", table_name="ledger"),
    )
    tables = tuple(
        DiscoveredTable(
            name=name,
            object_type="BASE_TABLE",
            columns=(
                DiscoveredColumn(
                    name="id", ordinal_position=1, physical_type="bigint", nullable=False
                ),
            ),
        )
        for name in ("account", "ledger")
    )

    await _scan(session, datasource, _catalog(triggers=both, tables=tables))
    await _scan(session, datasource, _catalog(triggers=both, tables=tables), reconcile=True)

    rows = await session.scalars(
        select(MetadataTrigger).where(
            MetadataTrigger.organization_id == datasource.organization_id,
            MetadataTrigger.datasource_id == datasource.id,
            MetadataTrigger.status == "ACTIVE",
        )
    )
    assert {row.table_name for row in rows.all()} == {"account", "ledger"}


async def test_a_schema_that_holds_only_triggers_and_sequences_is_not_skipped(
    session: AsyncSession,  # noqa: F811
) -> None:
    """An audit schema with no tables and no routines is a real shape, and
    `attach_native_objects` deliberately produces one. The extension pass skips a schema
    that "carries no extensions", and until triggers and sequences counted towards that
    test the whole schema was stepped over before its own loop ran -- a silent drop that
    no count would have shown, because the schema contributed nothing to compare.
    """
    datasource = await _datasource(session)

    counts = await _scan(
        session,
        datasource,
        _catalog(triggers=(_trigger(),), sequences=(_sequence(),), tables=()),
    )

    assert (counts["triggers"], counts["sequences"]) == (1, 1)
    assert set(await _triggers(session, datasource)) == {"account_audit_trg"}
    assert set(await _sequences(session, datasource)) == {"account_seq"}


async def test_reapplying_the_same_scan_changes_nothing(session: AsyncSession) -> None:  # noqa: F811
    """Ingestion is retried by Temporal on any transient failure, and the chunked push
    path deliberately re-persists every chunk a second time to resolve cross-chunk keys.
    A non-idempotent upsert is therefore a duplicated inventory in production, not a
    test-only wrinkle. The fingerprint is what makes the second pass a no-op.
    """
    datasource = await _datasource(session)
    catalogs = _catalog(triggers=(_trigger(),), sequences=(_sequence(),))

    first = await _scan(session, datasource, catalogs)
    before = (await _triggers(session, datasource))["account_audit_trg"].fingerprint
    second = await _scan(session, datasource, catalogs)

    assert first["created_objects"] >= 2
    assert second["created_objects"] == 0
    assert second["changed_objects"] == 0
    assert len(await _triggers(session, datasource)) == 1
    assert len(await _sequences(session, datasource)) == 1
    assert (await _triggers(session, datasource))["account_audit_trg"].fingerprint == before


# --- INV-6: no source value reaches either row ------------------------------


def _persisted_values(instance: Any) -> list[str]:
    """Every column of a mapped row, rendered as text -- `test_inv6_value_freedom`'s
    scan, applied to the two tables this task added a writer for. JSON columns go
    through `json.dumps` so a sentinel buried in `attributes` is as findable as one in
    a varchar."""
    rendered: list[str] = []
    for column in instance.__table__.columns:
        value = getattr(instance, column.name, None)
        if value is None:
            continue
        rendered.append(value if isinstance(value, str) else json.dumps(value, default=str))
    return rendered


async def _sentinel_leaks(session: AsyncSession, datasource: DataSource) -> list[str]:  # noqa: F811 -- a local parameter, not the imported fixture
    leaks: list[str] = []
    rows: list[Any] = [
        *(await _triggers(session, datasource)).values(),
        *(await _sequences(session, datasource)).values(),
    ]
    for row in rows:
        for rendered in _persisted_values(row):
            for sentinel in _SENTINELS:
                if sentinel in rendered:
                    leaks.append(f"{type(row).__name__}.{sentinel}: {rendered[:160]}")
    return leaks


async def test_no_source_values_reach_a_persisted_trigger_or_sequence(
    session: AsyncSession,  # noqa: F811
) -> None:
    """INV-6 on the axis this task added a writer for.

    A trigger body is SQL and SQL carries source values in its literals -- a trigger can
    perfectly well be written `... WHERE ssn = '<a real number>'`, and unlike a view
    definition a trigger body is usually *procedural*, so it is the more likely of the
    two to carry a literal at all. This drives the real writer with a sentinel-laden
    body and a sentinel-laden sequence comment, then searches every column of every row
    it staged. `test_the_sentinel_scan_would_notice_a_leak` below is why the result
    means something.
    """
    datasource = await _datasource(session)
    hostile_body = (
        "BEGIN UPDATE customer.account "  # noqa: S608
        f"SET note = '{SENTINEL_ROW_VALUE}' WHERE ssn = '{SENTINEL_LITERAL}' "
        f"AND ref = '{SENTINEL_CUSTOMER}'; END;"
    )

    await _scan(
        session,
        datasource,
        _catalog(triggers=(_trigger(body_sql=hostile_body),), sequences=(_sequence(),)),
    )

    assert await _sentinel_leaks(session, datasource) == []
    trigger = (await _triggers(session, datasource))["account_audit_trg"]
    # Vacuous unless the body was actually stored, and unless enough structure survived
    # redaction for the stored form to still be worth parsing.
    assert trigger.body_sql_redacted is not None
    assert "account" in trigger.body_sql_redacted.lower()
    # The fingerprint is a digest of the original, which is the point: it detects a
    # literal-only change without holding the literal.
    assert trigger.body_fingerprint is not None
    assert SENTINEL_LITERAL not in trigger.body_fingerprint


async def test_the_sentinel_scan_would_notice_a_leak(session: AsyncSession) -> None:  # noqa: F811
    """The negative control. A scan that passes because it looks in the wrong place is
    worse than no scan, so this plants the raw body on the row the writer just wrote and
    proves the same scan fails on it.
    """
    datasource = await _datasource(session)
    await _scan(session, datasource, _catalog(triggers=(_trigger(),), sequences=(_sequence(),)))
    assert await _sentinel_leaks(session, datasource) == [], "planted before the baseline was clean"

    trigger = (await _triggers(session, datasource))["account_audit_trg"]
    trigger.body_sql_redacted = f"BEGIN SELECT '{SENTINEL_LITERAL}'; END;"
    sequence = (await _sequences(session, datasource))["account_seq"]
    sequence.attributes = {"last_value": SENTINEL_CUSTOMER}
    await session.flush()

    leaks = await _sentinel_leaks(session, datasource)
    assert len(leaks) == 2, leaks
    assert any("MetadataTrigger" in leak for leak in leaks)
    assert any("MetadataSequence" in leak for leak in leaks)


def test_a_sequence_carries_no_current_position_anywhere_on_its_path() -> None:
    """INV-6's naming ratchet for this axis, at both ends of the new writer.

    A sequence's current position is the value the next insert will write into a
    customer's row. The peer made it unreachable on `DiscoveredSequence`; this asserts
    the writer did not reintroduce it on the stored row, structurally rather than by
    inspecting a value -- the point being that no such value can arrive.
    """
    banned = {"last_value", "last_number", "current_value", "next_value", "last_used_value"}
    assert not ({field.name for field in dataclass_fields(DiscoveredSequence)} & banned)
    assert not ({column.name for column in MetadataSequence.__table__.columns} & banned)


# --- reconciliation: what a FULL run may retire -----------------------------


async def test_a_full_rescan_tombstones_a_trigger_and_a_sequence_that_disappeared(
    session: AsyncSession,  # noqa: F811
) -> None:
    """The positive case every "must not retire" test below is measured against.

    A trigger that was dropped at the source is `DEPRECATED` with a `deprecated_at`, not
    deleted and not merely absent: a consumer that held a reference to it needs to find
    out what happened to it, which is the whole reason this axis uses the same soft
    lifecycle the rest of the catalog does.
    """
    datasource = await _datasource(session)
    await _scan(
        session,
        datasource,
        _catalog(triggers=(_trigger(), _postgres_trigger()), sequences=(_sequence(),)),
    )

    # The source now has only the PostgreSQL-shaped trigger: the other trigger and the
    # sequence were dropped.
    await _scan(session, datasource, _catalog(triggers=(_postgres_trigger(),)), reconcile=True)

    triggers = await _triggers(session, datasource)
    assert triggers["account_audit_trg"].status == "DEPRECATED"
    assert triggers["account_audit_trg"].deprecated_at is not None
    assert triggers["note_order_trg"].status == "ACTIVE", "the survivor was not touched"
    sequence = (await _sequences(session, datasource))["account_seq"]
    assert sequence.status == "DEPRECATED"
    assert sequence.deprecated_at is not None


async def test_a_trigger_that_comes_back_is_reactivated(session: AsyncSession) -> None:  # noqa: F811
    """A trigger dropped and recreated is the same trigger, and the row it already has
    is the one every reference points at. Reactivating rather than inserting a second
    row is what `status`/`deprecated_at` are for, and the sibling axes do exactly this.
    """
    datasource = await _datasource(session)
    await _scan(session, datasource, _catalog(triggers=(_trigger(),)))
    await _scan(session, datasource, _catalog(), reconcile=True)
    deprecated_id = (await _triggers(session, datasource))["account_audit_trg"].id

    await _scan(session, datasource, _catalog(triggers=(_trigger(),)), reconcile=True)

    triggers = await _triggers(session, datasource)
    assert len(triggers) == 1, "a returning trigger must not become a second row"
    assert triggers["account_audit_trg"].id == deprecated_id
    assert triggers["account_audit_trg"].status == "ACTIVE"
    assert triggers["account_audit_trg"].deprecated_at is None


async def test_neither_axis_retires_unless_the_caller_says_it_read_it(
    session: AsyncSession,  # noqa: F811
) -> None:
    """The gate that protects every delivery with no field for these axes.

    `native_axes_read` defaults to neither, and the default is the protection: a FULL
    push batch cannot carry a trigger at all, so its silence is a fact about the
    transport rather than about the source. Asserted per axis, because a Snowflake scan
    reads sequences and has no trigger object at all and must reconcile one while
    leaving the other alone (INV-9).
    """
    datasource = await _datasource(session)
    await _scan(session, datasource, _catalog(triggers=(_trigger(),), sequences=(_sequence(),)))

    # A FULL reconciliation over a tree holding neither, by a caller claiming neither.
    await _scan(session, datasource, _catalog(), reconcile=True, native_axes_read=frozenset())

    assert (await _triggers(session, datasource))["account_audit_trg"].status == "ACTIVE"
    assert (await _sequences(session, datasource))["account_seq"].status == "ACTIVE"

    # Now a caller that read sequences only -- Snowflake's shape.
    await _scan(
        session,
        datasource,
        _catalog(),
        reconcile=True,
        native_axes_read=frozenset({FACET_SEQUENCES}),
    )

    assert (await _triggers(session, datasource))["account_audit_trg"].status == "ACTIVE"
    assert (await _sequences(session, datasource))["account_seq"].status == "DEPRECATED"


async def test_an_out_of_scope_trigger_and_sequence_are_counted_as_seen(
    session: AsyncSession,  # noqa: F811
) -> None:
    """`discovery_selection`'s rule, on the two new kinds: "narrowing a selection stops
    maintaining an object; it never retires one."

    A trigger is scoped by its *own* qualified name and kind, exactly as
    `apply_selection` scopes it on the way in -- never by its firing table's name. The
    two halves have to agree, or an object is dropped from the scan by one rule and
    tombstoned by the other, which is the worst of the available outcomes: the operator
    asked for less scanning and got deletion.
    """
    datasource = await _datasource(session)
    await _scan(
        session,
        datasource,
        _catalog(triggers=(_trigger(),), sequences=(_sequence(),), schema_name="audit"),
    )
    selection = DiscoverySelection(exclude_schemas=["audit"])

    _snapshot, kept = await out_of_scope_existing(session, datasource, selection)

    trigger_id = (await _triggers(session, datasource))["account_audit_trg"].id
    sequence_id = (await _sequences(session, datasource))["account_seq"].id
    assert trigger_id in kept.trigger_ids
    assert sequence_id in kept.sequence_ids

    # And feeding that scope to the reconciliation pass is what makes them survive it.
    deprecated = await deprecate_missing_envelope_extensions(
        session, datasource, kept, native_axes_read=NATIVE_OBJECT_AXES
    )
    await session.commit()
    assert deprecated == 0
    assert (await _triggers(session, datasource))["account_audit_trg"].status == "ACTIVE"
    assert (await _sequences(session, datasource))["account_seq"].status == "ACTIVE"


async def test_a_selection_that_does_not_name_the_kind_retires_none_of_it(
    session: AsyncSession,  # noqa: F811
) -> None:
    """The other half of scope: `object_kinds`. An operator who scoped a source to
    TABLE and VIEW has stopped asking for triggers, which is not the same as saying
    there are none -- and `apply_selection` drops every trigger from the scan, so a FULL
    pass with no protection would tombstone all of them on the next run.
    """
    datasource = await _datasource(session)
    await _scan(session, datasource, _catalog(triggers=(_trigger(),), sequences=(_sequence(),)))

    _snapshot, kept = await out_of_scope_existing(
        session, datasource, DiscoverySelection(object_kinds=["TABLE", "VIEW"])
    )

    assert len(kept.trigger_ids) == 1
    assert len(kept.sequence_ids) == 1


async def test_a_refused_facet_keeps_its_existing_rows_out_of_the_deprecate_pass(
    session: AsyncSession,  # noqa: F811
) -> None:
    """R11-FP02's rule, extended to these two axes: an object whose facet the source
    refused was not looked at, so it is not missing.

    `pg_trigger` and `sys.triggers` are ordinary relations a login can be denied, so
    this axis is refusable in exactly the way the grants axis is -- and the negative
    control below is the same one that proved the defect real for grants: without the
    protection, one missing privilege tombstones every row a better-privileged run
    captured, and the refusal costs the estate rather than the facet.
    """
    datasource = await _datasource(session)
    await _scan(session, datasource, _catalog(triggers=(_trigger(),), sequences=(_sequence(),)))

    _snapshot, kept = await refused_facet_existing(
        session, datasource, {FACET_TRIGGERS, FACET_SEQUENCES}
    )
    assert len(kept.trigger_ids) == 1
    assert len(kept.sequence_ids) == 1
    deprecated = await deprecate_missing_envelope_extensions(
        session, datasource, kept, native_axes_read=NATIVE_OBJECT_AXES
    )
    await session.commit()

    assert deprecated == 0
    assert (await _triggers(session, datasource))["account_audit_trg"].status == "ACTIVE"
    assert (await _sequences(session, datasource))["account_seq"].status == "ACTIVE"

    # The control: the same reconciliation with nothing refused does retire them, so the
    # assertion above is about the protection and not about a pass that never fires.
    unprotected = await deprecate_missing_envelope_extensions(
        session, datasource, EnvelopeScope(), native_axes_read=NATIVE_OBJECT_AXES
    )
    await session.commit()
    assert unprotected == 2
    assert (await _triggers(session, datasource))["account_audit_trg"].status == "DEPRECATED"
    assert (await _sequences(session, datasource))["account_seq"].status == "DEPRECATED"


def test_every_native_axis_has_a_reconciliation_rule_for_a_refused_read() -> None:
    """The ratchet that keeps the three vocabularies aligned.

    The facet name, the `ConnectorCapabilities` flag and the reconciliation axis are one
    string with one meaning, and they live in three modules. A third axis added tomorrow
    with no `_FACET_AXES` entry would look finished and would tombstone its own rows on
    the first refused read -- which is precisely how this defect arrived for grants.
    """
    capability_flags = {field.name for field in dataclass_fields(ConnectorCapabilities)}
    for facet in NATIVE_OBJECT_AXES:
        assert facet in capability_flags, facet
        assert facet in activities._FACET_AXES, facet
    assert NATIVE_OBJECT_AXES == {FACET_TRIGGERS, FACET_SEQUENCES}


# --- the receipt ------------------------------------------------------------


def test_the_receipt_counts_both_kinds_it_discovered() -> None:
    """The defect stated in the task: `apply_selection` already counted an excluded
    TRIGGER, `observe_batch` counted no discovered one, so a scoped run published
    `{"discovered": 0, "excluded": 1}` for a kind it had read one of -- which reads as
    "the source has none" when the truth is "nobody counted".
    """
    receipt = DiscoveryReceipt(
        mode="FULL",
        selection_fingerprint=None,
        capabilities={"triggers": True, "sequences": True},
    )

    receipt.observe_batch(
        _catalog(triggers=(_trigger(), _postgres_trigger()), sequences=(_sequence(),)),
        {"TRIGGER": 1},
    )
    body = receipt.as_json("COMPLETE")

    assert body["kinds"]["TRIGGER"] == {"discovered": 2, "excluded": 1, "invisible": None}
    assert body["kinds"]["SEQUENCE"] == {"discovered": 1, "excluded": 0, "invisible": None}


def test_a_kind_the_adapter_does_not_collect_gets_no_zero_row() -> None:
    """INV-9's absent-rather-than-empty rule, in the one place a `Counter` would have
    broken it quietly.

    Snowflake has sequences and no trigger object at all; BigQuery and Databricks have
    neither. `self.discovered["TRIGGER"] += 0` would materialise a `TRIGGER` row reading
    `discovered: 0` on every one of those runs. The facet's `support` is what says "this
    adapter does not collect them", and an absent kind row is how this receipt already
    says "nothing to report".
    """
    snowflake = DiscoveryReceipt(
        mode="FULL",
        selection_fingerprint=None,
        capabilities={"triggers": False, "sequences": True},
    )

    snowflake.observe_batch(_catalog(sequences=(_sequence(),)), {})
    body = snowflake.as_json("COMPLETE")

    assert "TRIGGER" not in body["kinds"]
    assert body["kinds"]["SEQUENCE"]["discovered"] == 1
    assert body["facets"]["triggers"] == {
        "support": "UNSUPPORTED",
        "state": "UNSUPPORTED",
        "reason": None,
    }
    assert body["facets"]["sequences"]["support"] == "SUPPORTED"
    assert body["facets"]["sequences"]["state"] == "SUPPORTED"


def test_a_refused_native_facet_is_publishable_on_the_receipt() -> None:
    """The receipt half of the protection lands ahead of the connector half.

    No adapter attributes a trigger read to a facet yet -- `DISCOVERY_FACETS` lives in
    `connectors.discovery` and has no entry for either name, so `FacetReadScope.record`
    would reject one. The receipt accepts and publishes the outcome, and
    `_FACET_AXES` honours it, so the day that one vocabulary entry lands there is
    nothing else to remember. A facet the receipt would silently drop is the one failure
    mode `record_facet_outcome` refuses outright, which is why this is worth pinning.
    """
    from aida.capability_states import REASON_SOURCE_DENIED_READ, CapabilityState

    receipt = DiscoveryReceipt(
        mode="FULL", selection_fingerprint=None, capabilities={"triggers": True}
    )
    receipt.record_facet_outcome(
        FACET_TRIGGERS,
        state=CapabilityState.PERMISSION_DENIED,
        reason=REASON_SOURCE_DENIED_READ,
    )

    facet = receipt.as_json("COMPLETE")["facets"]["triggers"]
    assert facet["state"] == "PERMISSION_DENIED"
    assert facet["reason"] == REASON_SOURCE_DENIED_READ


# --- the pull activity, end to end over SQLite ------------------------------


class _NativeObjectConnector:
    """A connector that yields one batch of triggers and sequences, as a real adapter
    does. Not a `Connector` subclass on purpose -- the activity only calls
    `test_connection`, `capabilities`, `scope_discovery`, `count_invisible_objects` and
    `discover_streaming`, and a narrow double keeps what is being proved visible.
    """

    connector_type = "postgres"
    dialect = "postgres"

    def __init__(
        self,
        batches: list[tuple[DiscoveredCatalog, ...]],
        *,
        collects: ConnectorCapabilities | None = None,
    ) -> None:
        self._batches = batches
        self._capabilities = collects or ConnectorCapabilities(
            views=True, routines=True, triggers=True, sequences=True
        )

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self._capabilities

    async def test_connection(self) -> None:
        return None

    def scope_discovery(self, **_: Any) -> bool:
        return False

    async def count_invisible_objects(self) -> None:
        return None

    async def discover_streaming(self, *, batch_size: int = 500) -> Any:
        for batch in self._batches:
            yield batch


async def _discover(
    session: AsyncSession,  # noqa: F811 -- a local parameter, not the imported fixture
    monkeypatch: pytest.MonkeyPatch,
    datasource: DataSource,
    connector: _NativeObjectConnector,
    *,
    mode: str = "FULL",
) -> AnalysisRun:
    """Drive the real `discover_datasource` activity against the in-memory database."""
    from tests.test_discover_datasource_streaming import (
        _patch_activity_plumbing,
        _StubSecretResolver,
    )

    _patch_activity_plumbing(monkeypatch, session)
    monkeypatch.setattr(activities, "SecretResolver", _StubSecretResolver)
    monkeypatch.setattr(
        activities.connector_registry, "create", lambda connector_type, dsn: connector
    )
    run = AnalysisRun(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode=mode,
        trigger_type="MANUAL",
        status="QUEUED",
    )
    session.add(run)
    await session.commit()
    await ActivityEnvironment().run(activities.discover_datasource, str(run.id))
    return run


async def test_a_full_pull_run_persists_reconciles_and_counts_both_kinds(
    session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real activity, end to end: the rows land, the receipt counts them, and a
    second run over a source that lost the trigger tombstones exactly that one.

    This is the wiring test -- `persist_envelope_extensions` can be right while the
    activity never passes the axes to the reconciliation pass, which is a defect no
    unit test of either function can see.
    """
    datasource = await _datasource(session)
    connector = _NativeObjectConnector(
        [_catalog(triggers=(_trigger(),), sequences=(_sequence(),))]
    )

    first = await _discover(session, monkeypatch, datasource, connector)

    stored = await session.get(AnalysisRun, first.id)
    assert stored is not None and stored.discovery_receipt is not None
    assert stored.discovery_receipt["kinds"]["TRIGGER"]["discovered"] == 1
    assert stored.discovery_receipt["kinds"]["SEQUENCE"]["discovered"] == 1
    assert stored.discovery_receipt["facets"]["triggers"]["support"] == "SUPPORTED"
    assert (await _triggers(session, datasource))["account_audit_trg"].status == "ACTIVE"
    assert (await _sequences(session, datasource))["account_seq"].status == "ACTIVE"

    # The source drops the trigger and keeps the sequence.
    await _discover(
        session,
        monkeypatch,
        datasource,
        _NativeObjectConnector([_catalog(sequences=(_sequence(),))]),
    )

    assert (await _triggers(session, datasource))["account_audit_trg"].status == "DEPRECATED"
    assert (await _sequences(session, datasource))["account_seq"].status == "ACTIVE"


async def test_a_full_pull_run_retires_no_axis_its_connector_does_not_collect(
    session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source re-pointed at an adapter that does not read triggers must not lose the
    triggers the previous adapter found. The capability flag, not an empty tuple, is
    what licenses retirement: "read the axis and found none" is the only state that
    does, and an empty tuple cannot be told apart from "never looked" (INV-9).
    """
    datasource = await _datasource(session)
    await _discover(
        session,
        monkeypatch,
        datasource,
        _NativeObjectConnector([_catalog(triggers=(_trigger(),), sequences=(_sequence(),))]),
    )

    await _discover(
        session,
        monkeypatch,
        datasource,
        _NativeObjectConnector(
            [_catalog()], collects=ConnectorCapabilities(triggers=False, sequences=True)
        ),
    )

    assert (await _triggers(session, datasource))["account_audit_trg"].status == "ACTIVE"
    # The axis it does collect is reconciled, so this is a per-axis gate and not an
    # across-the-board opt-out.
    assert (await _sequences(session, datasource))["account_seq"].status == "DEPRECATED"


async def test_an_incremental_run_retires_neither_axis(
    session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An INCREMENTAL run enumerates a slice of the source and is authoritative for
    nothing it did not enumerate. The existing `run.mode == "FULL"` gate covers the new
    axes because they reconcile inside the same pass -- pinned here because a future
    per-axis reconciliation placed outside that gate would look harmless and would
    tombstone the estate on every incremental pass.
    """
    datasource = await _datasource(session)
    await _discover(
        session,
        monkeypatch,
        datasource,
        _NativeObjectConnector([_catalog(triggers=(_trigger(),), sequences=(_sequence(),))]),
    )

    await _discover(
        session, monkeypatch, datasource, _NativeObjectConnector([_catalog()]), mode="INCREMENTAL"
    )

    assert (await _triggers(session, datasource))["account_audit_trg"].status == "ACTIVE"
    assert (await _sequences(session, datasource))["account_seq"].status == "ACTIVE"


async def test_a_scoped_pull_run_counts_what_it_excluded_and_retires_none_of_it(
    session: AsyncSession,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two halves of scope, together, through the activity: the excluded trigger is
    counted on the receipt and its existing row survives the FULL reconciliation.

    `retained_out_of_scope` has to include the two kinds, or an operator who narrowed a
    scan away from an audit schema full of triggers reads `retained_out_of_scope: 0`
    while dozens were retained -- the number is what tells them narrowing stopped
    maintaining rather than deleted.
    """
    datasource = await _datasource(session)
    await _discover(
        session,
        monkeypatch,
        datasource,
        _NativeObjectConnector(
            [_catalog(triggers=(_trigger(),), sequences=(_sequence(),), schema_name="audit")]
        ),
    )
    # Re-fetched rather than mutated in place: the activity closes the session it was
    # handed, which detaches every instance, so assigning to the original object would
    # commit nothing and the "scoped" run below would silently be an unscoped one.
    stored_source = await session.get(DataSource, datasource.id)
    assert stored_source is not None
    stored_source.discovery_selection = {"exclude_schemas": ["audit"]}
    await session.commit()

    scoped = await _discover(
        session,
        monkeypatch,
        datasource,
        _NativeObjectConnector(
            [_catalog(triggers=(_trigger(),), sequences=(_sequence(),), schema_name="audit")]
        ),
    )

    stored = await session.get(AnalysisRun, scoped.id)
    assert stored is not None and stored.discovery_receipt is not None
    receipt = stored.discovery_receipt
    assert receipt["kinds"]["TRIGGER"]["excluded"] == 1
    assert receipt["kinds"]["SEQUENCE"]["excluded"] == 1
    # Both kinds are in the retained count, beside the tables and routines.
    assert receipt["reconciliation"]["retained_out_of_scope"] >= 2
    assert (await _triggers(session, datasource))["account_audit_trg"].status == "ACTIVE"
    assert (await _sequences(session, datasource))["account_seq"].status == "ACTIVE"


def _schema_id_of(rows: dict[str, Any]) -> UUID:
    return next(iter(rows.values())).schema_id


async def test_the_two_axes_hang_off_the_schema_that_holds_them(
    session: AsyncSession,  # noqa: F811
) -> None:
    """A trigger belongs to a schema, not to its firing table.

    Oracle lets a trigger's owner differ from its table's, and discovery can be
    schema-scoped so the firing table may not be in the tree at all. Looking the table
    up and skipping the trigger when it is missing -- the rule the view-definition loop
    rightly applies -- would drop exactly those two cases, so the firing table is stored
    as a name and this pins that it is not resolved.
    """
    datasource = await _datasource(session)

    await _scan(
        session,
        datasource,
        _catalog(triggers=(_trigger(table_name="nowhere_to_be_found"),), tables=()),
    )

    triggers = await _triggers(session, datasource)
    assert triggers["account_audit_trg"].table_name == "nowhere_to_be_found"
    schema = await session.get(MetadataSchema, _schema_id_of(triggers))
    assert schema is not None and schema.name == "customer"


# --- the push path, which carries neither axis ------------------------------


async def test_a_full_push_batch_retires_nothing_a_pull_scan_discovered(
    session: AsyncSession,  # noqa: F811
) -> None:
    """The trap with no precedent to copy, and the one that would have hurt most.

    No envelope version has a field for a trigger or a sequence, so
    `catalogs_to_discovery` builds every pushed schema with empty tuples for both. A
    FULL 1.1 batch is therefore exactly as silent about triggers as a 1.0 batch is about
    views -- and reconciling that silence would have tombstoned every trigger and
    sequence a pull scan discovered, on the first nightly push after this feature
    shipped. `native_axes_read` defaults to neither for precisely this call.
    """
    from aida.ingestion import catalogs_to_discovery
    from aida.schemas import MetadataIngestionCreate
    from tests.test_envelope_v11 import _envelope

    datasource = await _datasource(session)
    await _scan(session, datasource, _catalog(triggers=(_trigger(),), sequences=(_sequence(),)))

    envelope: MetadataIngestionCreate = _envelope(snapshot_type="FULL")
    pushed = catalogs_to_discovery(envelope.catalogs)
    # The transport itself cannot carry them; that is the fact the gate rests on.
    assert all(
        not schema.triggers and not schema.sequences
        for catalog in pushed
        for schema in catalog.schemas
    )
    run = await _run(session, datasource)
    await persist_discovery_snapshot(
        session, run, datasource, pushed, deprecate_missing=True, connector_capabilities={}
    )
    await persist_envelope_extensions(
        session, datasource, pushed, deprecate_missing=True, analysis_run_id=run.id
    )
    await session.commit()

    assert (await _triggers(session, datasource))["account_audit_trg"].status == "ACTIVE"
    assert (await _sequences(session, datasource))["account_seq"].status == "ACTIVE"


def test_a_pushed_snapshot_reports_both_facets_as_unsupported() -> None:
    """The honest half of not persisting them on that path.

    `datasource.capabilities` is a copy of the *connector's* capability dict, so a
    PostgreSQL source pushing a snapshot arrives carrying `triggers: true`. Left alone,
    the receipt would publish `triggers: SUPPORTED` with no TRIGGER kind row at all --
    which reads as "this source has no triggers" when the truth is "this transport
    cannot carry one". This is the same read the kind counts fix, one level up.
    """
    from aida.batch_ingestion import FACET_SEQUENCES as pushed_sequences
    from aida.batch_ingestion import FACET_TRIGGERS as pushed_triggers

    receipt = DiscoveryReceipt(
        mode="FULL",
        selection_fingerprint=None,
        capabilities={
            "triggers": True,
            "sequences": True,
            "canonical_push": True,
            pushed_triggers: False,
            pushed_sequences: False,
        },
    )

    facets = receipt.as_json("COMPLETE")["facets"]
    assert facets["triggers"]["support"] == "UNSUPPORTED"
    assert facets["sequences"]["support"] == "UNSUPPORTED"


async def test_the_chunked_push_activity_publishes_the_unsupported_facets(factory) -> None:  # type: ignore[no-untyped-def] # noqa: F811
    """The wiring for the assertion above, through the real batch activity.

    Driven over the harness `test_push_ingestion_receipt` and `test_push_selection`
    already use, with the datasource carrying the capability dict a real PostgreSQL
    source carries -- which is the only way the override being tested can be observed
    to matter.
    """
    from aida.models import MetadataIngestionBatch
    from tests.test_in2_batch_controls import _seed_datasource
    from tests.test_push_selection import _delivery

    async with factory() as push_session:
        datasource = await _seed_datasource(push_session)
        datasource.capabilities = {"views": True, "routines": True, "triggers": True,
                                   "sequences": True}
        batch, run = await _delivery(push_session, datasource)

    from aida.batch_ingestion import process_metadata_ingestion_batch

    result = await process_metadata_ingestion_batch(str(batch.id))

    assert result["status"] == "COMPLETED"
    async with factory() as push_session:
        completed = await push_session.get(AnalysisRun, run.id)
        stored_batch = await push_session.get(MetadataIngestionBatch, batch.id)
    assert completed is not None and completed.discovery_receipt is not None
    facets = completed.discovery_receipt["facets"]
    assert facets["triggers"]["support"] == "UNSUPPORTED"
    assert facets["sequences"]["support"] == "UNSUPPORTED"
    # The axes the envelope does carry are unaffected by the override.
    assert facets["view_definitions"]["support"] == "SUPPORTED"
    assert stored_batch is not None
    assert stored_batch.object_counts["triggers"] == 0
