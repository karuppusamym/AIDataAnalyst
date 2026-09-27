"""R11-FP02: a snapshot that was pushed keeps the same scope receipt a pulled one does.

Every pull run records what it took in, facet by facet, and a run with no receipt is a run that
cannot be read honestly at all: `discovered_tables` alone says how much, never how completely.
The chunked push path wrote none, so a bank delivering its estate through the ingestion API got
counters and nothing else -- the very path where Atlas has least idea what it was not sent.

Drives the real activity over a real two-chunk batch on in-memory SQLite (the same harness
`test_in2_batch_controls.py` uses, and for the same reason) and pins:

* the completed run carries a receipt whose kinds and code facets match what arrived;
* the reapply pass that resolves cross-chunk keys does not count the estate twice;
* what the sender did not say is UNKNOWN, not zero: `invisible` stays null, and an INCREMENTAL
  batch records that it reconciled nothing rather than that nothing was missing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.batch_ingestion as batch_ingestion
from aida.batch_ingestion import process_metadata_ingestion_batch
from aida.db import Base
from aida.models import (
    AnalysisRun,
    MetadataIngestionBatch,
    MetadataIngestionChunk,
)
from tests.test_in2_batch_controls import _seed_datasource
from tests.test_ingestion import _chunk


@pytest.fixture
async def factory(monkeypatch):  # type: ignore[no-untyped-def]
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    monkeypatch.setattr(batch_ingestion, "session_factory", sessions)
    yield sessions
    await engine.dispose()


async def _batch(session: AsyncSession, datasource: object, *, snapshot_type: str) -> tuple:
    run = AnalysisRun(
        id=uuid4(),
        organization_id=datasource.organization_id,  # type: ignore[attr-defined]
        datasource_id=datasource.id,  # type: ignore[attr-defined]
        mode=snapshot_type,
        trigger_type="BATCH_PUSH",
        status="QUEUED",
    )
    session.add(run)
    await session.flush()
    batch = MetadataIngestionBatch(
        id=uuid4(),
        organization_id=datasource.organization_id,  # type: ignore[attr-defined]
        datasource_id=datasource.id,  # type: ignore[attr-defined]
        analysis_run_id=run.id,
        batch_key=f"batch-{uuid4().hex[:12]}",
        envelope_version="1.1",
        producer="bank-metadata-bridge",
        snapshot_type=snapshot_type,
        expected_chunks=2,
        received_chunks=2,
        status="QUEUED",
        submitted_by="operator-1",
    )
    session.add(batch)
    await session.flush()
    for number, table_name in ((1, "account"), (2, "customer")):
        chunk = _chunk(number, f"estate:chunk:{number:04d}", table_name=table_name)
        session.add(
            MetadataIngestionChunk(
                id=uuid4(),
                organization_id=batch.organization_id,
                datasource_id=batch.datasource_id,
                batch_id=batch.id,
                chunk_number=number,
                chunk_key=chunk.chunk_key,
                emitted_at=datetime.now(UTC),
                payload_fingerprint=uuid4().hex,
                payload=chunk.model_dump(mode="json"),
                object_counts={"tables": 1, "columns": 2},
            )
        )
    await session.commit()
    return batch, run


async def test_a_pushed_snapshot_records_the_same_receipt_a_pulled_one_does(factory) -> None:  # type: ignore[no-untyped-def]
    async with factory() as session:
        datasource = await _seed_datasource(session)
        batch, run = await _batch(session, datasource, snapshot_type="FULL")

    result = await process_metadata_ingestion_batch(str(batch.id))

    assert result["status"] == "COMPLETED"
    async with factory() as session:
        completed = await session.get(AnalysisRun, run.id)
    assert completed is not None
    receipt = completed.discovery_receipt
    assert receipt is not None
    assert receipt["mode"] == "FULL"
    assert receipt["stream"] == {"state": "COMPLETE", "batches": 2}
    # Two chunks, one table each -- counted once, not twice by the reapply pass that runs
    # after them to resolve cross-chunk foreign keys.
    assert receipt["kinds"]["TABLE"]["discovered"] == 2
    # Nothing was excluded, because this path applies no discovery selection: it persists what
    # the sender sent, and the receipt says so with a null selection rather than an empty one.
    assert receipt["kinds"]["TABLE"]["excluded"] == 0
    assert receipt["selection_fingerprint"] is None
    # R11-FP02: a sender cannot be asked what it left out. UNKNOWN, never a claim of none.
    assert receipt["kinds"]["TABLE"]["invisible"] is None
    assert receipt["reconciliation"]["performed"] is True


async def test_an_incremental_push_says_it_reconciled_nothing(factory) -> None:  # type: ignore[no-untyped-def]
    async with factory() as session:
        datasource = await _seed_datasource(session)
        batch, run = await _batch(session, datasource, snapshot_type="INCREMENTAL")

    await process_metadata_ingestion_batch(str(batch.id))

    async with factory() as session:
        completed = await session.get(AnalysisRun, run.id)
    assert completed is not None and completed.discovery_receipt is not None
    # An incremental delivery is not authoritative about absence, so nothing it did not carry
    # is retired -- and the receipt names that reason rather than leaving it to be inferred.
    assert completed.discovery_receipt["reconciliation"] == {
        "performed": False,
        "reason": "INCREMENTAL_MODE",
    }
