"""IN-2 -- operator pause / resume / cancel / replay controls for a
`MetadataIngestionBatch` (`Docs/20-modules/03-ingestion.md` §12/§13, tracker IN-2).

Runs the real endpoint bodies in `aida.ingestion_api` and the real Temporal
activity `aida.batch_ingestion.process_metadata_ingestion_batch` against an
in-memory SQLite database -- the same harness rationale as
`test_asset_evidence.py`: PostgreSQL is unreachable in this sandbox, but SQLite
is a real SQL engine that enforces the row semantics these handlers rely on.

Sections:

1. the batch state machine: every legal transition moves the manifest to the
   expected state; every illegal transition is refused with HTTP 409 rather
   than silently ignored;
2. attributability (INV-7): each control action writes exactly one audit row
   and one outbox domain event, in the same transaction as the status change;
3. tenant isolation (INV-5): a caller from another organization is denied
   before the transition;
4. RBAC (AU-7): a principal holding none of the allowed roles is refused by the
   role gate these endpoints declare;
5. cooperative worker: the durable activity observes an operator PAUSED/CANCELLED
   and stops promptly -- at entry and between chunks -- without marking the
   batch FAILED or completing it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.batch_ingestion as batch_ingestion
from aida.batch_ingestion import (
    BatchControlSignal,
    _batch_control_status,
    _mark_batch_failed,
    process_metadata_ingestion_batch,
)
from aida.config import Settings
from aida.db import Base
from aida.ingestion_api import (
    cancel_metadata_ingestion_batch,
    pause_metadata_ingestion_batch,
    replay_metadata_ingestion_batch,
    resume_metadata_ingestion_batch,
)
from aida.models import (
    AnalysisRun,
    AuditEvent,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataIngestionBatch,
    MetadataIngestionChunk,
    Organization,
    OutboxEvent,
    Project,
)
from aida.security import require_roles
from tests.support.doubles import security_context

pytestmark = pytest.mark.asyncio

_SETTINGS = Settings(_env_file=None)
_ALLOWED_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "MetadataIngestor")


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


class _FakeWorkflowClient:
    """Captures `start_workflow` calls without a Temporal server."""

    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []

    async def start_workflow(self, _run, batch_id, *, id, task_queue):  # noqa: A002
        self.started.append((batch_id, id))


def _request(client: _FakeWorkflowClient | None) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(temporal_client=client)))


async def _seed_datasource(session: AsyncSession) -> DataSource:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ops",
        code=f"OPS{uuid4().hex[:6]}",
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
        name=f"src-{uuid4().hex[:8]}",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
    )
    session.add_all([org, lob, domain, project, datasource])
    await session.flush()
    return datasource


async def _seed_batch(
    session: AsyncSession,
    datasource: DataSource,
    *,
    status: str,
    expected_chunks: int = 1,
    with_chunks: bool = True,
    with_run: bool = True,
) -> MetadataIngestionBatch:
    run_id = None
    if with_run:
        run = AnalysisRun(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            mode="INCREMENTAL",
            trigger_type="BATCH_PUSH",
            status="RUNNING",
        )
        session.add(run)
        await session.flush()
        run_id = run.id
    batch = MetadataIngestionBatch(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        analysis_run_id=run_id,
        batch_key=f"batch-{uuid4().hex[:12]}",
        envelope_version="1.0",
        producer="acme-extractor",
        snapshot_type="INCREMENTAL",
        expected_chunks=expected_chunks,
        received_chunks=expected_chunks if with_chunks else 0,
        status=status,
        submitted_by="operator-1",
    )
    session.add(batch)
    await session.flush()
    if with_chunks:
        for number in range(1, expected_chunks + 1):
            session.add(
                MetadataIngestionChunk(
                    id=uuid4(),
                    organization_id=datasource.organization_id,
                    datasource_id=datasource.id,
                    batch_id=batch.id,
                    chunk_number=number,
                    chunk_key=f"chunk-{uuid4().hex[:12]}",
                    emitted_at=datetime.now(UTC),
                    payload_fingerprint=uuid4().hex,
                    payload={"placeholder": True},
                    object_counts={"tables": 1, "columns": 1},
                )
            )
    await session.commit()
    return batch


def _context(datasource: DataSource, **overrides):
    return security_context(organization_id=datasource.organization_id, **overrides)


async def _audit_actions(session: AsyncSession, batch_id) -> list[str]:
    rows = (
        await session.scalars(
            select(AuditEvent.action).where(AuditEvent.resource_id == str(batch_id))
        )
    ).all()
    return list(rows)


async def _outbox_events(session: AsyncSession, batch_id) -> list[str]:
    rows = (
        await session.scalars(
            select(OutboxEvent.event_type).where(OutboxEvent.aggregate_id == str(batch_id))
        )
    ).all()
    return list(rows)


# ---------------------------------------------------------------------------
# 1. Legal transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("start_status", ["QUEUED", "RUNNING", "PROCESSING"])
async def test_pause_moves_active_batch_to_paused(session, start_status) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status=start_status)

    result = await pause_metadata_ingestion_batch(
        batch.id, context=_context(datasource), session=session
    )

    assert result.status == "PAUSED"
    assert await _audit_actions(session, batch.id) == ["metadata.ingestion.batch.pause"]
    assert await _outbox_events(session, batch.id) == ["metadata.ingestion.batch.paused.v1"]


async def test_pause_is_idempotent_when_already_paused(session) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status="PAUSED")

    result = await pause_metadata_ingestion_batch(
        batch.id, context=_context(datasource), session=session
    )
    assert result.status == "PAUSED"
    # No second audit/outbox row for a no-op re-pause.
    assert await _audit_actions(session, batch.id) == []


@pytest.mark.parametrize("start_status", ["DRAFT", "QUEUED", "RUNNING", "PROCESSING", "PAUSED"])
async def test_cancel_moves_non_terminal_batch_to_cancelled(session, start_status) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status=start_status)

    result = await cancel_metadata_ingestion_batch(
        batch.id, context=_context(datasource), session=session
    )

    assert result.status == "CANCELLED"
    assert await _audit_actions(session, batch.id) == ["metadata.ingestion.batch.cancel"]
    assert await _outbox_events(session, batch.id) == ["metadata.ingestion.batch.cancelled.v1"]
    # The in-flight analysis run is cancelled alongside the batch.
    run = await session.get(AnalysisRun, batch.analysis_run_id)
    assert run is not None and run.status == "CANCELLED"


async def test_resume_requeues_a_paused_batch(session) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status="PAUSED")
    previous_run_id = batch.analysis_run_id
    client = _FakeWorkflowClient()

    result = await resume_metadata_ingestion_batch(
        batch.id,
        _request(client),
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )

    assert result.status == "QUEUED"
    assert result.analysis_run_id != previous_run_id
    assert len(client.started) == 1
    assert client.started[0][0] == str(batch.id)
    # A fresh run linked back to the paused one, leaving history intact.
    new_run = await session.get(AnalysisRun, result.analysis_run_id)
    assert new_run is not None and new_run.resumed_from_run_id == previous_run_id
    assert await _audit_actions(session, batch.id) == ["metadata.ingestion.batch.resume"]
    assert await _outbox_events(session, batch.id) == ["metadata.ingestion.batch.resumed.v1"]


@pytest.mark.parametrize("start_status", ["FAILED", "SUBMISSION_FAILED", "CANCELLED"])
async def test_replay_requeues_a_terminal_batch(session, start_status) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status=start_status)
    previous_run_id = batch.analysis_run_id
    client = _FakeWorkflowClient()

    result = await replay_metadata_ingestion_batch(
        batch.id,
        _request(client),
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )

    assert result.status == "QUEUED"
    assert result.error_class is None and result.error_message is None
    assert len(client.started) == 1
    new_run = await session.get(AnalysisRun, result.analysis_run_id)
    assert new_run is not None and new_run.resumed_from_run_id == previous_run_id
    assert new_run.trigger_type == "BATCH_REPLAY"
    assert await _audit_actions(session, batch.id) == ["metadata.ingestion.batch.replay"]
    assert await _outbox_events(session, batch.id) == ["metadata.ingestion.batch.replayed.v1"]


# ---------------------------------------------------------------------------
# 1b. Illegal transitions -> 409
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("start_status", ["DRAFT", "COMPLETED", "FAILED", "SUBMISSION_FAILED"])
async def test_pause_from_illegal_state_is_409(session, start_status) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status=start_status)

    with pytest.raises(HTTPException) as exc:
        await pause_metadata_ingestion_batch(
            batch.id, context=_context(datasource), session=session
        )
    assert exc.value.status_code == 409
    assert await _audit_actions(session, batch.id) == []


@pytest.mark.parametrize("start_status", ["COMPLETED"])
async def test_cancel_from_terminal_state_is_409(session, start_status) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status=start_status)

    with pytest.raises(HTTPException) as exc:
        await cancel_metadata_ingestion_batch(
            batch.id, context=_context(datasource), session=session
        )
    assert exc.value.status_code == 409


@pytest.mark.parametrize("start_status", ["DRAFT", "QUEUED", "RUNNING", "PROCESSING", "COMPLETED"])
async def test_resume_from_non_paused_state_is_409(session, start_status) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status=start_status)

    with pytest.raises(HTTPException) as exc:
        await resume_metadata_ingestion_batch(
            batch.id,
            _request(_FakeWorkflowClient()),
            context=_context(datasource),
            session=session,
            settings=_SETTINGS,
        )
    assert exc.value.status_code == 409


@pytest.mark.parametrize("start_status", ["DRAFT", "QUEUED", "RUNNING", "PROCESSING"])
async def test_replay_from_non_terminal_state_is_409(session, start_status) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status=start_status)

    with pytest.raises(HTTPException) as exc:
        await replay_metadata_ingestion_batch(
            batch.id,
            _request(_FakeWorkflowClient()),
            context=_context(datasource),
            session=session,
            settings=_SETTINGS,
        )
    assert exc.value.status_code == 409


async def test_replay_of_completed_batch_is_409_because_payloads_were_cleared(session) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status="COMPLETED")

    with pytest.raises(HTTPException) as exc:
        await replay_metadata_ingestion_batch(
            batch.id,
            _request(_FakeWorkflowClient()),
            context=_context(datasource),
            session=session,
            settings=_SETTINGS,
        )
    assert exc.value.status_code == 409
    assert "cleared" in exc.value.detail


async def test_resume_fails_closed_when_temporal_unavailable(session) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status="PAUSED")

    with pytest.raises(HTTPException) as exc:
        await resume_metadata_ingestion_batch(
            batch.id,
            _request(None),  # no temporal client on app state
            context=_context(datasource),
            session=session,
            settings=_SETTINGS,
        )
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# 2. Attributability -- exactly one audit + one outbox row per action
# ---------------------------------------------------------------------------


async def test_each_action_writes_exactly_one_audit_and_one_outbox(session) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status="RUNNING")

    await pause_metadata_ingestion_batch(batch.id, context=_context(datasource), session=session)
    await resume_metadata_ingestion_batch(
        batch.id,
        _request(_FakeWorkflowClient()),
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )

    audits = int(
        await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.resource_id == str(batch.id))
        )
        or 0
    )
    outbox = int(
        await session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_id == str(batch.id))
        )
        or 0
    )
    assert audits == 2  # pause + resume
    assert outbox == 2


# ---------------------------------------------------------------------------
# 3. Tenant isolation (INV-5)
# ---------------------------------------------------------------------------


async def test_cross_tenant_pause_is_denied(session) -> None:
    datasource = await _seed_datasource(session)
    batch = await _seed_batch(session, datasource, status="RUNNING")

    with pytest.raises(HTTPException) as exc:
        await pause_metadata_ingestion_batch(
            batch.id,
            context=security_context(organization_id=uuid4()),  # a different organization
            session=session,
        )
    assert exc.value.status_code == 403
    assert "cross-organization" in exc.value.detail
    # Denied before any mutation was recorded.
    assert await _audit_actions(session, batch.id) == []


async def test_missing_batch_is_404(session) -> None:
    datasource = await _seed_datasource(session)

    with pytest.raises(HTTPException) as exc:
        await cancel_metadata_ingestion_batch(
            uuid4(), context=_context(datasource), session=session
        )
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# 4. RBAC (AU-7) -- the role gate these endpoints declare
# ---------------------------------------------------------------------------


async def test_wrong_role_is_denied_by_the_control_gate() -> None:
    gate = require_roles(*_ALLOWED_ROLES)
    with pytest.raises(HTTPException) as exc:
        await gate(context=security_context(organization_id=uuid4(), roles=frozenset({"Viewer"})))
    assert exc.value.status_code == 403


async def test_allowed_role_passes_the_control_gate() -> None:
    gate = require_roles(*_ALLOWED_ROLES)
    result = await gate(
        context=security_context(organization_id=uuid4(), roles=frozenset({"MetadataIngestor"}))
    )
    assert "MetadataIngestor" in result.roles


# ---------------------------------------------------------------------------
# 5. Cooperative worker abort
# ---------------------------------------------------------------------------


@pytest.fixture
async def shared_engine_factory(monkeypatch):
    """A StaticPool in-memory engine shared across the worker's independent
    sessions, wired in as `aida.batch_ingestion.session_factory`."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    monkeypatch.setattr(batch_ingestion, "session_factory", factory)
    yield factory
    await engine.dispose()


async def test_batch_control_status_reports_stop_states(shared_engine_factory) -> None:
    async with shared_engine_factory() as session:
        datasource = await _seed_datasource(session)
        cancelled = await _seed_batch(session, datasource, status="CANCELLED")
        paused = await _seed_batch(session, datasource, status="PAUSED")
        running = await _seed_batch(session, datasource, status="RUNNING")

    assert await _batch_control_status(cancelled.id) == "CANCELLED"
    assert await _batch_control_status(paused.id) == "PAUSED"
    assert await _batch_control_status(running.id) is None


async def test_activity_stops_at_entry_when_operator_cancelled(shared_engine_factory) -> None:
    """The operator cancelled the batch while it was still QUEUED; the activity
    observes the real DB status at entry and returns cleanly, never entering
    processing and never marking the batch FAILED."""
    async with shared_engine_factory() as session:
        datasource = await _seed_datasource(session)
        batch = await _seed_batch(session, datasource, status="CANCELLED")

    result = await process_metadata_ingestion_batch(str(batch.id))

    assert result == {"batch_id": str(batch.id), "status": "CANCELLED", "stopped": True}
    async with shared_engine_factory() as session:
        reloaded = await session.get(MetadataIngestionBatch, batch.id)
        assert reloaded is not None and reloaded.status == "CANCELLED"


async def test_activity_stops_at_chunk_boundary_when_cancelled(
    shared_engine_factory, monkeypatch
) -> None:
    """A cancel that lands after the activity has started is observed at the
    next cooperative checkpoint (between chunks), before the chunk is
    processed -- so the batch never completes and is not marked FAILED."""
    async with shared_engine_factory() as session:
        datasource = await _seed_datasource(session)
        batch = await _seed_batch(session, datasource, status="QUEUED")

    # Preflight is exercised elsewhere; here we drive straight to the loop and
    # prove the checkpoint aborts before _process_chunk runs.
    async def fake_preflight(_batch_id):
        return [uuid4()]

    processed: list = []

    async def fail_if_processed(*_args, **_kwargs):
        processed.append(_args)

    async def always_cancelled(_batch_id):
        return "CANCELLED"

    monkeypatch.setattr(batch_ingestion, "_preflight_batch", fake_preflight)
    monkeypatch.setattr(batch_ingestion, "_process_chunk", fail_if_processed)
    monkeypatch.setattr(batch_ingestion, "_batch_control_status", always_cancelled)

    result = await process_metadata_ingestion_batch(str(batch.id))

    assert result["stopped"] is True
    assert result["status"] == "CANCELLED"
    assert processed == []  # aborted before any chunk was processed
    async with shared_engine_factory() as session:
        reloaded = await session.get(MetadataIngestionBatch, batch.id)
        # Not COMPLETED, not FAILED -- the operator's control decision stands.
        assert reloaded is not None and reloaded.status not in {"COMPLETED", "FAILED"}


async def test_mark_batch_failed_never_overrides_an_operator_stop(shared_engine_factory) -> None:
    async with shared_engine_factory() as session:
        datasource = await _seed_datasource(session)
        cancelled = await _seed_batch(session, datasource, status="CANCELLED")
        paused = await _seed_batch(session, datasource, status="PAUSED")

    await _mark_batch_failed(cancelled.id, RuntimeError("late worker error"))
    await _mark_batch_failed(paused.id, RuntimeError("late worker error"))

    async with shared_engine_factory() as session:
        assert (await session.get(MetadataIngestionBatch, cancelled.id)).status == "CANCELLED"
        assert (await session.get(MetadataIngestionBatch, paused.id)).status == "PAUSED"


async def test_bare_batch_control_signal_carries_status() -> None:
    signal = BatchControlSignal("PAUSED")
    assert signal.status == "PAUSED"
