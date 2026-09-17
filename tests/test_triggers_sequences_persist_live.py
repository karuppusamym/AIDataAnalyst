"""R11-FP01: triggers and sequences are *persisted and reconciled*, against real servers.

`tests/test_triggers_sequences_live.py` proves the two axes are discovered correctly from
real engines, and says in its own docstring that it asserts no persistence "because there
is none yet". This is the other half, and it is the only evidence that matters for the
claim this task makes: **create a trigger on a live engine, scan, find the row; drop the
trigger, scan again, find the row tombstoned.**

Why a unit test cannot stand in for it. Everything between the source and the stored row
is engine detail a fake gets to choose: PostgreSQL packs a trigger's timing, orientation
and event set into one `tgtype` bitmask and hides an FK's enforcement triggers behind
`tgisinternal`; PostgreSQL has no trigger body at all while SQL Server keeps the code in
the trigger; identifier case folds in opposite directions on the two engines, which is
precisely what decides whether a rescan matches the row it wrote last time or writes a
second one and tombstones the first. A fake cannot get any of that wrong for you, so a
green unit suite is compatible with a writer that inserts a duplicate on every scan.

The drop half is the part with no unit-test equivalent at all. "Deprecated" and "merely
absent" look identical from the outside unless the row is still there with a
`deprecated_at` on it, and the only honest way to produce the absence is to ask a real
engine to stop having the object.

Each engine's private source is the footprint journey's, reused by importing its
fixtures, so this file creates no server of its own and skips with the journey when one
is unreachable. Every object it creates is dropped in a `finally` -- and the journey
fixture drops the whole database behind it, so the rollback is belt and braces, as
`scripts/verify_database_footprint_live.py` does it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.envelope_models  # noqa: F401 -- registers the 1.1 tables on the metadata
import aida.models  # noqa: F401 -- registers every 1.0 table on the metadata
from aida.db import Base
from aida.discovery_receipt import FACET_SEQUENCES, FACET_TRIGGERS, DiscoveryReceipt
from aida.envelope_models import AVAILABLE, UNAVAILABLE, MetadataSequence, MetadataTrigger
from aida.ingestion import persist_envelope_extensions
from aida.models import (
    AnalysisRun,
    DataDomain,
    DataSource,
    LineOfBusiness,
    Organization,
    Project,
)
from aida.workflows.activities import persist_discovery_snapshot
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    CONNECTORS,
    SCHEMA,
    JourneySource,
    _postgres,
    _sqlserver,
    source,
)

#: The objects this file adds to the sample schema, and drops again. Named with a
#: `persist_probe_` prefix so nothing here can be confused with the sample pack's own
#: objects or with the discovery-side file's.
#:
#: Every declaration parameter is non-default on purpose (`START WITH 7`, not 1;
#: `CACHE 2`, not the engine's own default): a test against defaults passes just as well
#: when the writer stores the wrong column, or stores nothing and the engine fills a
#: default in, which is the failure this fixture exists to catch.
_CREATE = {
    "postgres": f"""
        CREATE SEQUENCE {SCHEMA}.persist_probe_seq
            START WITH 7 INCREMENT BY 3 MINVALUE 1 MAXVALUE 999 CACHE 2 CYCLE;
        CREATE TABLE {SCHEMA}.persist_probe_audit (
            audit_id bigserial PRIMARY KEY,
            customer_id integer
        );
        CREATE FUNCTION {SCHEMA}.persist_probe_note() RETURNS trigger
            LANGUAGE plpgsql AS $body$
        BEGIN
            INSERT INTO {SCHEMA}.persist_probe_audit (customer_id) VALUES (NEW.customer_id);
            RETURN NEW;
        END;
        $body$;
        CREATE TRIGGER persist_probe_trg
            AFTER INSERT OR UPDATE ON {SCHEMA}.orders
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.persist_probe_note();
    """,  # noqa: S608 -- DDL for a private scratch database built by this test; the only interpolation is the module constant `SCHEMA`
    "sqlserver": f"""
        CREATE SEQUENCE {SCHEMA}.persist_probe_seq
            AS bigint START WITH 7 INCREMENT BY 3
            MINVALUE 1 MAXVALUE 999 CACHE 2 CYCLE;
        GO
        CREATE TABLE {SCHEMA}.persist_probe_audit (
            audit_id bigint IDENTITY(1, 1) PRIMARY KEY,
            customer_id int
        );
        GO
        CREATE TRIGGER persist_probe_trg ON {SCHEMA}.orders
        AFTER INSERT, UPDATE AS
        BEGIN
            SET NOCOUNT ON;
            INSERT INTO {SCHEMA}.persist_probe_audit (customer_id)
            SELECT i.customer_id FROM inserted i;
        END;
    """,  # noqa: S608 -- DDL for a private scratch database built by this test; the only interpolation is the module constant `SCHEMA`
}

#: The trigger and the standalone sequence only. The audit table and, on PostgreSQL, the
#: sequence its `bigserial` column owns are deliberately left in place: the rescan then
#: has a surviving sequence to leave alone, which is what tells "reconciled the axis"
#: apart from "tombstoned everything on the axis".
_DROP = {
    "postgres": (
        f"DROP TRIGGER IF EXISTS persist_probe_trg ON {SCHEMA}.orders;\n"
        f"DROP SEQUENCE IF EXISTS {SCHEMA}.persist_probe_seq;"
    ),
    "sqlserver": (
        f"IF OBJECT_ID(N'{SCHEMA}.persist_probe_trg') IS NOT NULL "
        f"DROP TRIGGER {SCHEMA}.persist_probe_trg;\n"
        f"IF OBJECT_ID(N'{SCHEMA}.persist_probe_seq') IS NOT NULL "
        f"DROP SEQUENCE {SCHEMA}.persist_probe_seq;"
    ),
}

#: Everything, for the rollback. Run after the assertions whatever happened, and before
#: the journey fixture drops the database underneath it.
_TEARDOWN = {
    "postgres": (
        f"{_DROP['postgres']}\n"
        f"DROP FUNCTION IF EXISTS {SCHEMA}.persist_probe_note();\n"
        f"DROP TABLE IF EXISTS {SCHEMA}.persist_probe_audit;"
    ),
    "sqlserver": (
        f"{_DROP['sqlserver']}\n"
        f"IF OBJECT_ID(N'{SCHEMA}.persist_probe_audit') IS NOT NULL "
        f"DROP TABLE {SCHEMA}.persist_probe_audit;"
    ),
}


@pytest_asyncio.fixture
async def platform() -> AsyncIterator[AsyncSession]:
    """The control plane the rows land in -- in-memory SQLite, as the 1.1 axis tests use.

    The source under test is a real server; the platform side of a discovery run is
    engine-agnostic and needs no second one.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


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
        # The real dialect, because it selects the redaction parser for the trigger body.
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
    *,
    reconcile: bool,
) -> DiscoveryReceipt:
    """One real discovery of the sample schema, persisted through the real halves.

    `native_axes_read` is derived from the adapter's own capability flags exactly as
    `workflows.activities.discover_datasource` derives it, so this also asserts the real
    PostgreSQL and SQL Server adapters declare the two axes -- a flag silently left
    False would make every reconciliation below a no-op and every "still ACTIVE"
    assertion vacuous.
    """
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
    assert native_axes_read == {FACET_TRIGGERS, FACET_SEQUENCES}, (
        f"{source.connector_type} does not declare both axes, so nothing below is proved"
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
        session,
        run,
        datasource,
        selected,
        deprecate_missing=reconcile,
        connector_capabilities={},
    )
    await persist_envelope_extensions(
        session,
        datasource,
        selected,
        deprecate_missing=reconcile,
        analysis_run_id=run.id,
        native_axes_read=native_axes_read,
    )
    run.status = "COMPLETED"
    await session.commit()
    receipt = DiscoveryReceipt(
        mode="FULL",
        selection_fingerprint=None,
        capabilities={facet: True for facet in native_axes_read},
    )
    receipt.observe_batch(selected, {})
    return receipt


async def _trigger(
    session: AsyncSession, datasource: DataSource, name: str
) -> MetadataTrigger | None:
    return await session.scalar(
        select(MetadataTrigger).where(
            MetadataTrigger.organization_id == datasource.organization_id,
            MetadataTrigger.datasource_id == datasource.id,
            MetadataTrigger.name == name,
        )
    )


async def _sequence(
    session: AsyncSession, datasource: DataSource, name: str
) -> MetadataSequence | None:
    return await session.scalar(
        select(MetadataSequence).where(
            MetadataSequence.organization_id == datasource.organization_id,
            MetadataSequence.datasource_id == datasource.id,
            MetadataSequence.name == name,
        )
    )


async def test_a_real_trigger_and_sequence_are_persisted_then_tombstoned_when_dropped(
    platform: AsyncSession,
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """The whole claim, in one sequence of real events per engine.

    create -> scan -> the row is there and says what the engine says; drop -> scan ->
    the same row is DEPRECATED with a `deprecated_at`, and the sequence that was not
    dropped is untouched. Run as one test rather than four because the interesting
    assertions are about the *transition*, and a per-step test would have to recreate
    the state each time on a real server for no extra proof.
    """
    datasource = await _datasource(platform, source)
    try:
        await source.execute(_CREATE[source.connector_type])

        receipt = await _scan(platform, datasource, source, reconcile=True)

        # --- the rows landed, carrying what this engine actually said -------------
        trigger = await _trigger(platform, datasource, "persist_probe_trg")
        assert trigger is not None, "the discovered trigger reached no row"
        assert trigger.status == "ACTIVE"
        assert trigger.deprecated_at is None
        assert trigger.table_name.lower() == "orders"
        assert trigger.timing == "AFTER"
        assert set(trigger.events) == {"INSERT", "UPDATE"}
        assert trigger.is_enabled is True
        assert trigger.organization_id == datasource.organization_id
        assert trigger.datasource_id == datasource.id
        # PostgreSQL fires per row and says so; SQL Server has no `FOR EACH ROW`, so
        # STATEMENT is its only honest answer. Asserted per engine, because one expected
        # value would mean one of the two was being described wrongly.
        expected_orientation = "ROW" if source.connector_type == "postgres" else "STATEMENT"
        assert trigger.orientation == expected_orientation

        sequence = await _sequence(platform, datasource, "persist_probe_seq")
        assert sequence is not None, "the discovered sequence reached no row"
        assert sequence.status == "ACTIVE"
        assert (sequence.start_with, sequence.increment_by) == ("7", "3")
        assert (sequence.minimum_bound, sequence.maximum_bound) == ("1", "999")
        assert sequence.cache_size == "2"
        assert sequence.cycles is True

        # --- the body, stored the way a routine body is ---------------------------
        if source.connector_type == "postgres":
            # A PostgreSQL trigger has no body of its own: the engine's own reason
            # survives into storage rather than being replaced by the generic default,
            # and `action_routine` names the function that does have one.
            assert trigger.availability == UNAVAILABLE
            assert trigger.body_sql_redacted is None
            assert trigger.unavailable_reason is not None
            assert "action_routine" in trigger.unavailable_reason
            assert trigger.action_routine is not None
            assert trigger.action_routine.lower() == f"{SCHEMA}.persist_probe_note"
        else:
            assert trigger.availability == AVAILABLE
            assert trigger.body_sql_redacted is not None
            assert trigger.redaction_status in {"PARSED", "LEXICAL"}
            assert trigger.screening_status == "CLEAN"
            assert trigger.screening_version is not None
            assert trigger.body_fingerprint is not None
            # An identifier the body must mention, never a literal: the stored form is
            # the redacted one and INV-6 is about the values in it.
            assert "persist_probe_audit" in trigger.body_sql_redacted.lower()

        # --- the receipt counted them, from a real scan ---------------------------
        body = receipt.as_json("COMPLETE")
        assert body["kinds"]["TRIGGER"]["discovered"] >= 1
        assert body["kinds"]["SEQUENCE"]["discovered"] >= 1

        # PostgreSQL records the column a `bigserial`'s sequence generates; SQL Server's
        # IDENTITY is a column property with no sequence object behind it, so there is
        # nothing to find. Each engine's own answer, so neither is described by the
        # other's -- and this is the edge that makes a sequence part of the footprint.
        if source.connector_type == "postgres":
            owned = await _sequence(platform, datasource, "persist_probe_audit_audit_id_seq")
            assert owned is not None
            assert owned.owned_by_table == "persist_probe_audit"
            assert owned.owned_by_column == "audit_id"
            assert sequence.owned_by_table is None, "the standalone sequence has no owner"

        trigger_id, sequence_id = trigger.id, sequence.id

        # --- the source loses the trigger and the standalone sequence -------------
        await source.execute(_DROP[source.connector_type])

        await _scan(platform, datasource, source, reconcile=True)

        gone_trigger = await _trigger(platform, datasource, "persist_probe_trg")
        assert gone_trigger is not None, (
            "the row was deleted rather than tombstoned; a consumer holding a reference "
            "to it can no longer find out what happened to it"
        )
        assert gone_trigger.id == trigger_id, "a new row, not the reconciled one"
        assert gone_trigger.status == "DEPRECATED"
        assert gone_trigger.deprecated_at is not None
        gone_sequence = await _sequence(platform, datasource, "persist_probe_seq")
        assert gone_sequence is not None and gone_sequence.id == sequence_id
        assert gone_sequence.status == "DEPRECATED"
        assert gone_sequence.deprecated_at is not None

        if source.connector_type == "postgres":
            # The sequence that was not dropped is still ACTIVE, which is what tells a
            # reconciled axis apart from an axis tombstoned wholesale.
            survivor = await _sequence(platform, datasource, "persist_probe_audit_audit_id_seq")
            assert survivor is not None and survivor.status == "ACTIVE"
    finally:
        # Roll back every object this test created, whatever happened above. The journey
        # fixture then drops the database itself, so a failure here cannot leave the
        # sample estate modified.
        await source.execute(_TEARDOWN[source.connector_type])


async def test_a_rescan_of_an_unchanged_source_writes_no_second_row(
    platform: AsyncSession,
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """Identity, proved where identifier case actually folds.

    `metadata_trigger` is keyed `(schema_id, table_name, name)` and the engines disagree
    about identifier case -- PostgreSQL folds to lower, and a writer that compared
    against the wrong case would insert a second row on every scan and then tombstone
    the first. That is invisible to a fake, whose rows come back exactly as they were
    written, and it is the defect a real rescan catches immediately.
    """
    datasource = await _datasource(platform, source)
    try:
        await source.execute(_CREATE[source.connector_type])

        await _scan(platform, datasource, source, reconcile=True)
        await _scan(platform, datasource, source, reconcile=True)

        triggers = list(
            (
                await platform.scalars(
                    select(MetadataTrigger).where(
                        MetadataTrigger.organization_id == datasource.organization_id,
                        MetadataTrigger.datasource_id == datasource.id,
                        MetadataTrigger.name == "persist_probe_trg",
                    )
                )
            ).all()
        )
        assert len(triggers) == 1, "the rescan wrote a second row for the same trigger"
        assert triggers[0].status == "ACTIVE", "the rescan tombstoned the row it rewrote"
        sequences = list(
            (
                await platform.scalars(
                    select(MetadataSequence).where(
                        MetadataSequence.organization_id == datasource.organization_id,
                        MetadataSequence.datasource_id == datasource.id,
                        MetadataSequence.name == "persist_probe_seq",
                    )
                )
            ).all()
        )
        assert len(sequences) == 1
        assert sequences[0].status == "ACTIVE"
    finally:
        await source.execute(_TEARDOWN[source.connector_type])


async def test_an_enforcement_trigger_never_reaches_a_row(
    platform: AsyncSession,
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    """`orders` has a foreign key, so PostgreSQL created internal triggers to enforce
    it. They are already discovered as constraints, and persisting them again as
    triggers would double every referential rule in the estate and publish an
    implementation detail as user code. The discovery-side file pins that they are not
    *returned*; this pins that nothing downstream puts them back.
    """
    datasource = await _datasource(platform, source)
    try:
        await source.execute(_CREATE[source.connector_type])

        await _scan(platform, datasource, source, reconcile=True)

        names = {
            row.name.lower()
            for row in (
                await platform.scalars(
                    select(MetadataTrigger).where(
                        MetadataTrigger.organization_id == datasource.organization_id,
                        MetadataTrigger.datasource_id == datasource.id,
                    )
                )
            ).all()
        }
        assert names == {"persist_probe_trg"}, names
    finally:
        await source.execute(_TEARDOWN[source.connector_type])
