"""CN-3/PR-5: proves the actual fix, not just the diagnosis.

`PostgresConnector.discover()` ran ~14 unbounded, sequential full-source-scan
queries into one in-memory tree before `discover_datasource`
(`workflows/activities.py`) persisted anything at all -- a source too large to
finish inside the activity's `start_to_close_timeout` (`workflows/discovery.py`)
was retried from scratch on every attempt with zero snapshot rows ever
committed. `discover_datasource` now drives `connector.discover_streaming()`
and persists/commits each yielded batch as it arrives.

This file does not touch real Postgres or `PostgresConnector`'s actual SQL --
that already has no unit-test coverage of its query bodies (see
`test_connectors.py`'s own docstrings: those hand-build rows and drive the
`connectors.discovery` assembly helpers directly, the established convention
for this codebase's non-DB connector tests, per its "PostgreSQL is not
reachable in this sandbox" rationale in `test_index_partition_inventory.py`).
Instead this exercises the real `discover_datasource` activity body, the real
`persist_discovery_snapshot`/`persist_envelope_extensions`/
`deprecate_missing_snapshot` functions, and a real in-memory SQLite database,
through a fake `Connector` that implements the new `discover_streaming`
interface directly (`Connector.discover_streaming` in `connectors/base.py`) --
proving the *persistence and reconciliation* half of the fix, which is
connector-agnostic and is where the correctness hazard (INV-11) actually
lives.

Two things are proved, matching the task's own verification bar:

1. `test_mid_stream_failure_leaves_earlier_batches_committed_and_does_not_deprecate`:
   a batch that already landed stays committed after a later batch fails --
   the actual point of switching to streaming persistence -- and, critically,
   the FULL-mode deprecate-missing pass never runs at all when the stream
   never finishes, so a pre-existing table absent from the (incomplete) new
   snapshot is *not* wrongly tombstoned by a run that never got to see the
   rest of the source.

2. `test_full_stream_deprecates_missing_once_without_touching_earlier_batches_tables`:
   once every batch has landed, deprecate-missing runs -- exactly once,
   confirmed by counting calls to `deprecate_missing_snapshot` -- and a table
   from the *first* batch is untouched by it despite having been committed
   long before the run's last batch arrived. This is the direct proof that
   `discover_datasource` avoids the trap of reconciling "missing" against a
   partial, chunk-scoped view.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from temporalio.testing import ActivityEnvironment

import aida.task_tracking as task_tracking
import aida.workflows.activities as activities
from aida.config import Settings
from aida.connectors.base import (
    Connector,
    ConnectorCapabilities,
    DiscoveredCatalog,
    DiscoveredColumn,
    DiscoveredSchema,
    DiscoveredTable,
    TableProfileSnapshot,
)
from aida.db import Base
from aida.models import (
    AnalysisRun,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)

pytestmark = pytest.mark.asyncio


class _SimulatedMidStreamFailure(RuntimeError):
    """Stands in for a dropped connection, a source-side timeout, or the
    activity being killed partway through -- whatever actually happened to
    the real 100K-table run this fix targets. What matters for the test is
    only that `discover_streaming` stops producing batches after some have
    already been yielded (and, downstream, already committed).
    """


class _FakeStreamingConnector(Connector):
    """Implements `discover_streaming` directly, per `Connector.discover_streaming`
    in `connectors/base.py` -- the interface `PostgresConnector` implements for
    real. `discover()` itself is deliberately unimplemented: this fake exists to
    drive `discover_datasource` through the streaming path only.
    """

    connector_type = "postgres"
    dialect = "postgres"

    def __init__(
        self,
        batches: list[tuple[DiscoveredCatalog, ...]],
        *,
        fail_after: int | None = None,
    ) -> None:
        self._batches = batches
        self._fail_after = fail_after
        self.batches_yielded = 0

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities()

    async def test_connection(self) -> None:
        return None

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        raise NotImplementedError("this fake only exercises discover_streaming")

    async def discover_streaming(
        self, *, batch_size: int = 500
    ) -> AsyncIterator[tuple[DiscoveredCatalog, ...]]:
        for index, batch in enumerate(self._batches, start=1):
            if self._fail_after is not None and index > self._fail_after:
                raise _SimulatedMidStreamFailure("source connection dropped mid-stream")
            self.batches_yielded += 1
            yield batch

    async def profile_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        *,
        sample_rows: int,
        column_batch_size: int,
        timeout_seconds: int,
    ) -> TableProfileSnapshot:
        return TableProfileSnapshot(None, 0, ())


def _batch(table_name: str) -> tuple[DiscoveredCatalog, ...]:
    table = DiscoveredTable(
        name=table_name,
        object_type="BASE_TABLE",
        columns=(
            DiscoveredColumn(
                name="id", ordinal_position=1, physical_type="bigint", nullable=False
            ),
        ),
    )
    schema = DiscoveredSchema(name="retail", tables=(table,))
    return (DiscoveredCatalog(name="bank", schemas=(schema,)),)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def _seed_datasource_with_legacy_table(
    session: AsyncSession,
) -> tuple[DataSource, MetadataTable]:
    """A datasource with one prior-run table already ACTIVE (`legacy_table`),
    absent from every batch the fakes below yield -- the FULL-mode
    deprecate-missing target every test in this file watches.
    """
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="primary",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="retail", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    legacy_table = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="legacy_table",
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(legacy_table)
    await session.commit()
    return datasource, legacy_table


class _StubSecretResolver:
    """Stands in for `aida.secrets.SecretResolver`: this fake connector never
    actually dials a DSN, so the resolved value only needs to be a string.
    """

    def resolve(self, reference: str) -> str:
        return "postgresql://irrelevant/irrelevant"


def _patch_activity_plumbing(monkeypatch: pytest.MonkeyPatch, session: AsyncSession) -> None:
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(activities, "SecretResolver", _StubSecretResolver)


def _spy_on_deprecate_missing_snapshot(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"count": 0}
    real = activities.deprecate_missing_snapshot

    async def _counting(*args: object, **kwargs: object) -> object:
        calls["count"] += 1
        return await real(*args, **kwargs)

    monkeypatch.setattr(activities, "deprecate_missing_snapshot", _counting)
    return calls


async def test_mid_stream_failure_leaves_earlier_batches_committed_and_does_not_deprecate(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasource, legacy_table = await _seed_datasource_with_legacy_table(session)
    run = AnalysisRun(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
        status="RUNNING",
    )
    session.add(run)
    await session.commit()

    _patch_activity_plumbing(monkeypatch, session)
    deprecate_calls = _spy_on_deprecate_missing_snapshot(monkeypatch)
    connector = _FakeStreamingConnector(
        [_batch("table_a"), _batch("table_b"), _batch("table_c")], fail_after=1
    )
    monkeypatch.setattr(
        activities.connector_registry, "create", lambda connector_type, dsn: connector
    )

    with pytest.raises(_SimulatedMidStreamFailure):
        await ActivityEnvironment().run(activities.discover_datasource, str(run.id))

    # Batch one landed and stayed committed even though the run as a whole failed --
    # this is the actual point of the fix: a mid-run failure now leaves genuine
    # partial progress instead of the zero rows the unbatched `discover()` call left.
    assert connector.batches_yielded == 1
    persisted_names = set(
        (await session.scalars(select(MetadataTable.name))).all()
    )
    assert "table_a" in persisted_names
    assert "table_b" not in persisted_names
    assert "table_c" not in persisted_names

    # The correctness trap: deprecate-missing must never run against a partial
    # scope. It didn't run at all here (the stream never finished), so the
    # pre-existing `legacy_table` -- absent from every batch this connector
    # yielded -- is untouched, not wrongly tombstoned by an incomplete view.
    assert deprecate_calls["count"] == 0
    # `discover_datasource` opens (and closes) its own `session_factory()` blocks
    # internally; since that factory is patched to hand back this same shared
    # session, each of its `close()` calls expunges the identity map -- so the
    # `legacy_table`/`run` instances from setup are no longer attached, and a
    # fresh `session.get` (not `session.refresh`, which requires attachment) is
    # what actually proves the committed row's current state.
    refreshed_legacy_table = await session.get(MetadataTable, legacy_table.id)
    assert refreshed_legacy_table is not None
    assert refreshed_legacy_table.status == "ACTIVE"

    refreshed_run = await session.get(AnalysisRun, run.id)
    assert refreshed_run is not None
    assert refreshed_run.status == "FAILED"


async def test_full_stream_deprecates_missing_once_without_touching_earlier_batches_tables(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasource, legacy_table = await _seed_datasource_with_legacy_table(session)
    run = AnalysisRun(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
        status="RUNNING",
    )
    session.add(run)
    await session.commit()

    _patch_activity_plumbing(monkeypatch, session)
    deprecate_calls = _spy_on_deprecate_missing_snapshot(monkeypatch)
    connector = _FakeStreamingConnector(
        [_batch("table_a"), _batch("table_b"), _batch("table_c")], fail_after=None
    )
    monkeypatch.setattr(
        activities.connector_registry, "create", lambda connector_type, dsn: connector
    )

    result = await ActivityEnvironment().run(activities.discover_datasource, str(run.id))

    assert result["status"] == "COMPLETED"
    assert connector.batches_yielded == 3
    # table_a, table_b, table_c -- legacy_table is deprecated, not counted here
    assert result["tables"] == 3

    # Exactly one deprecate-missing pass for the whole run, made only after every
    # batch had landed -- not one per batch, which would have tombstoned every
    # table outside whichever batch was current.
    assert deprecate_calls["count"] == 1

    tables = {
        row.name: row.status
        for row in (await session.scalars(select(MetadataTable))).all()
    }
    assert tables["table_a"] == "ACTIVE"
    assert tables["table_b"] == "ACTIVE"
    assert tables["table_c"] == "ACTIVE"
    # The actual hazard this whole design exists to avoid: table_a was committed
    # in the *first* batch, long before the stream (and the deprecate-missing
    # pass that only runs after it) finished -- it must not have been swept up
    # as "missing" by a chunk-scoped reconciliation.
    assert tables["legacy_table"] == "DEPRECATED"

    refreshed_run = await session.get(AnalysisRun, run.id)
    assert refreshed_run is not None
    assert refreshed_run.status == "PROFILING"
    assert refreshed_run.discovered_tables == 3
    assert refreshed_run.deprecated_objects == 1
