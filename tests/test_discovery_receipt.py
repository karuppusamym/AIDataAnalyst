"""R11-FP02: a discovery run's receipt says what kind of thing it took in, and how completely.

"We scanned the source" and "we scanned it, but every routine body was withheld from our
principal" read the same in a run's counters. These tests drive the real `discover_datasource`
activity against in-memory SQLite and pin the receipt that keeps them apart -- including after a
failure part-way through the stream, when the receipt must say INTERRUPTED rather than go on
claiming a stream in progress.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
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
    DiscoveredRoutine,
    DiscoveredSchema,
    DiscoveredTable,
    DiscoveredViewDefinition,
    TableProfileSnapshot,
)
from aida.db import Base
from aida.discovery_receipt import DiscoveryReceipt
from aida.models import AnalysisRun, DataDomain, DataSource, LineOfBusiness, Organization, Project


def _columns() -> tuple[DiscoveredColumn, ...]:
    return (
        DiscoveredColumn(name="id", ordinal_position=1, physical_type="bigint", nullable=False),
    )


def _view(name: str, sql: str | None, *, truncated: bool = False) -> DiscoveredTable:
    return DiscoveredTable(
        name=name,
        object_type="VIEW",
        columns=_columns(),
        view_definition=DiscoveredViewDefinition(
            definition_sql=sql,
            truncated=truncated,
            unavailable_reason=None if sql else "module is encrypted or not visible",
        ),
    )


def _batch(schema: str) -> tuple[DiscoveredCatalog, ...]:
    return (
        DiscoveredCatalog(
            name="bank",
            schemas=(
                DiscoveredSchema(
                    name=schema,
                    tables=(
                        DiscoveredTable(
                            name="orders", object_type="BASE_TABLE", columns=_columns()
                        ),
                        _view("v_open", "SELECT id FROM orders"),
                        _view("v_secret", None),
                        _view("v_long", "SELECT id FROM orders", truncated=True),
                    ),
                    routines=(
                        DiscoveredRoutine(name="refresh", routine_type="PROCEDURE", body_sql=None),
                        DiscoveredRoutine(
                            name="net", routine_type="FUNCTION", body_sql="SELECT 1"
                        ),
                    ),
                ),
            ),
        ),
    )


def test_the_receipt_counts_kinds_and_keeps_withheld_code_apart_from_captured() -> None:
    receipt = DiscoveryReceipt(
        mode="INCREMENTAL",
        selection_fingerprint=None,
        capabilities={"views": True, "routines": True, "grants": False},
    )

    receipt.observe_batch(_batch("retail"), {"VIEW": 2})
    body = receipt.as_json("COMPLETE")

    assert body["kinds"]["TABLE"] == {"discovered": 1, "excluded": 0}
    assert body["kinds"]["VIEW"] == {"discovered": 3, "excluded": 2}
    assert body["kinds"]["PROCEDURE"] == {"discovered": 1, "excluded": 0}
    assert body["facets"]["view_definitions"] == {
        "support": "SUPPORTED",
        "captured": 2,
        "withheld": 1,
        "truncated": 1,
    }
    assert body["facets"]["routine_bodies"]["withheld"] == 1
    assert body["facets"]["grants"] == {"support": "UNSUPPORTED"}
    assert body["reconciliation"] == {"performed": False, "reason": "INCREMENTAL_MODE"}


class _Connector(Connector):
    connector_type = "postgres"
    dialect = "postgres"

    def __init__(
        self, batches: list[tuple[DiscoveredCatalog, ...]], fail_after: int | None
    ) -> None:
        self._batches = batches
        self._fail_after = fail_after

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(views=True, routines=False)

    async def test_connection(self) -> None:
        return None

    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        raise NotImplementedError

    async def discover_streaming(
        self, *, batch_size: int = 500
    ) -> AsyncIterator[tuple[DiscoveredCatalog, ...]]:
        for index, batch in enumerate(self._batches, start=1):
            if self._fail_after is not None and index > self._fail_after:
                raise RuntimeError("source connection dropped mid-stream")
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


class _StubSecretResolver:
    def resolve(self, reference: str) -> str:
        return "postgresql://irrelevant/irrelevant"


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


async def _run(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    fail_after: int | None = None,
) -> tuple[UUID, AnalysisRun]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(id=uuid4(), organization_id=org.id, name="R", code=f"R{uuid4().hex[:6]}")
    domain = DataDomain(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id, name="D",
        code=f"D{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id, data_domain_id=domain.id,
        name="P", slug=f"p-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(), organization_id=org.id, line_of_business_id=lob.id, data_domain_id=domain.id,
        project_id=project.id, name="primary", connector_type="postgres", dialect="postgres",
        environment="PROD", network_zone="default", credential_reference="env://TEST_DSN",
        capabilities={}, status="ACTIVE",
    )
    run = AnalysisRun(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id, mode=mode,
        trigger_type="MANUAL", status="QUEUED",
    )
    session.add_all([org, lob, domain, project, datasource, run])
    await session.commit()
    monkeypatch.setattr(activities, "session_factory", lambda: session)
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    monkeypatch.setattr(activities, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(activities, "SecretResolver", _StubSecretResolver)
    connector = _Connector([_batch("retail"), _batch("finance")], fail_after)
    monkeypatch.setattr(activities.connector_registry, "create", lambda kind, dsn: connector)
    run_id = run.id
    try:
        await ActivityEnvironment().run(activities.discover_datasource, str(run_id))
    except RuntimeError:
        pass
    refreshed = await session.get(AnalysisRun, run_id)
    assert refreshed is not None
    return run_id, refreshed


@pytest.mark.asyncio
async def test_a_finished_full_run_records_a_complete_receipt_with_its_reconciliation(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = await _run(session, monkeypatch, mode="FULL")

    receipt = run.discovery_receipt
    assert receipt is not None
    assert receipt["stream"] == {"state": "COMPLETE", "batches": 2}
    assert receipt["kinds"]["VIEW"]["discovered"] == 6
    assert receipt["kinds"]["SCHEMA"]["discovered"] == 2
    # The connector reports it collects no routines: the receipt says so rather than
    # presenting two withheld bodies as a capture problem of the source.
    assert receipt["facets"]["routine_bodies"]["support"] == "UNSUPPORTED"
    assert receipt["facets"]["view_definitions"]["withheld"] == 2
    assert receipt["reconciliation"]["performed"] is True


@pytest.mark.asyncio
async def test_a_run_that_fails_mid_stream_keeps_an_interrupted_receipt(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = await _run(session, monkeypatch, mode="FULL", fail_after=1)

    assert run.status == "FAILED"
    receipt = run.discovery_receipt
    assert receipt is not None
    assert receipt["stream"] == {"state": "INTERRUPTED", "batches": 1}
    assert receipt["kinds"]["VIEW"]["discovered"] == 3
    # A FULL run that never saw its whole stream reconciles nothing.
    assert receipt["reconciliation"] == {"performed": False, "reason": "STREAM_NOT_FINISHED"}
