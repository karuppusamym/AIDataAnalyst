"""R11-FP01: the discovery selection governs a pushed snapshot too.

A pull scan filters what it asks for. A push has already been sent -- and until this change
the push path applied no selection at all, so a pushed snapshot was in scope by definition:
an operator who had scoped a pull scan to two schemas of forty got none of that scoping when
the same estate arrived through the ingestion API, and the receipt's `selection_fingerprint`
was null because there was nothing to fingerprint.

The contract chosen is **ignore, never silently**: an out-of-scope object a sender delivered
is not persisted, is counted per kind on the receipt, and is reported back to the sender in
the batch's own change counts. The reasoning, and why *reject* and *keep-but-mark* were
refused, is stated in full above `_process_chunk` in `aida/batch_ingestion.py`.

The clause these tests exist to defend is the retirement one: a FULL pull run counts existing
out-of-scope objects as *seen* rather than retiring them (`workflows.activities`'s
`out_of_scope_existing`, and `discovery_selection`'s "narrowing a selection stops maintaining
an object; it never retires one"). A FULL *push* batch now does the same, through the same
function -- because a selection that ignored on the way in and deleted on the way out would
be worse than the bug being fixed.

Drives the real activity over real two-chunk batches on in-memory SQLite, the harness
`test_push_ingestion_receipt.py` and `test_in2_batch_controls.py` already use.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.batch_ingestion import process_metadata_ingestion_batch
from aida.discovery_selection import DiscoverySelection
from aida.models import (
    AnalysisRun,
    MetadataCatalog,
    MetadataIngestionBatch,
    MetadataIngestionChunk,
    MetadataSchema,
    MetadataTable,
)
from aida.schemas import MetadataIngestionChunkCreate
from tests.test_in2_batch_controls import _seed_datasource
from tests.test_ingestion import _chunk
from tests.test_push_ingestion_receipt import factory  # noqa: F401 -- used by fixture name

#: The scope the operator set: one schema of the two the sender delivers.
SCOPE = {"include_schemas": ["retail"]}


def _chunk_for(number: int, *, schema: str, table: str) -> MetadataIngestionChunkCreate:
    """One chunk carrying one table in one schema, so scope can be varied per chunk."""
    payload = _chunk(number, f"estate:chunk:{number:04d}", table_name=table).model_dump(
        mode="json"
    )
    payload["catalogs"][0]["schemas"][0]["name"] = schema
    return MetadataIngestionChunkCreate.model_validate(payload)


async def _delivery(
    session: AsyncSession,
    datasource: Any,
    *,
    snapshot_type: str = "FULL",
    chunks: tuple[tuple[str, str], ...] = (("retail", "account"), ("finance", "ledger")),
) -> tuple[MetadataIngestionBatch, AnalysisRun]:
    run = AnalysisRun(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        mode=snapshot_type,
        trigger_type="BATCH_PUSH",
        status="QUEUED",
    )
    session.add(run)
    await session.flush()
    batch = MetadataIngestionBatch(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        analysis_run_id=run.id,
        batch_key=f"batch-{uuid4().hex[:12]}",
        envelope_version="1.1",
        producer="bank-metadata-bridge",
        snapshot_type=snapshot_type,
        expected_chunks=len(chunks),
        received_chunks=len(chunks),
        status="QUEUED",
        submitted_by="operator-1",
    )
    session.add(batch)
    await session.flush()
    for number, (schema, table) in enumerate(chunks, start=1):
        chunk = _chunk_for(number, schema=schema, table=table)
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


async def _scoped_datasource(session: AsyncSession, selection: dict[str, Any] | None) -> Any:
    datasource = await _seed_datasource(session)
    datasource.discovery_selection = selection
    await session.commit()
    return datasource


async def _existing_table(
    session: AsyncSession, datasource: Any, *, schema_name: str, table_name: str
) -> MetadataTable:
    """A table an earlier, unscoped run already put in the catalog."""
    catalog = await session.scalar(
        select(MetadataCatalog).where(MetadataCatalog.datasource_id == datasource.id)
    )
    if catalog is None:
        catalog = MetadataCatalog(
            id=uuid4(),
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            name="bank",
            fingerprint="fp",
        )
        session.add(catalog)
        await session.flush()
    schema = await session.scalar(
        select(MetadataSchema).where(
            MetadataSchema.catalog_id == catalog.id, MetadataSchema.name == schema_name
        )
    )
    if schema is None:
        schema = MetadataSchema(
            id=uuid4(),
            organization_id=datasource.organization_id,
            catalog_id=catalog.id,
            name=schema_name,
            fingerprint="fp",
        )
        session.add(schema)
        await session.flush()
    table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=table_name,
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(table)
    await session.commit()
    return table


async def test_a_scoped_push_keeps_only_what_the_selection_covers(factory) -> None:  # type: ignore[no-untyped-def] # noqa: F811
    """The headline. Before this change both tables were persisted and the receipt said the
    scope was null; now the delivery is scoped exactly as a pull scan of the same source."""
    async with factory() as session:
        datasource = await _scoped_datasource(session, SCOPE)
        batch, run = await _delivery(session, datasource)

    result = await process_metadata_ingestion_batch(str(batch.id))

    assert result["status"] == "COMPLETED"
    async with factory() as session:
        completed = await session.get(AnalysisRun, run.id)
        stored = await session.get(MetadataIngestionBatch, batch.id)
        names = set((await session.scalars(select(MetadataTable.name))).all())
        schemas = set((await session.scalars(select(MetadataSchema.name))).all())
    # The in-scope table landed; the out-of-scope one is not in the catalog at all -- and
    # the reapply pass, which re-persists every chunk to resolve cross-chunk keys, did not
    # put it back either.
    assert names == {"account"}
    assert schemas == {"retail"}

    assert completed is not None and completed.discovery_receipt is not None
    receipt = completed.discovery_receipt
    # A pushed snapshot now records which selection governed it, exactly as a pull run does.
    assert receipt["selection_fingerprint"] == DiscoverySelection(**SCOPE).fingerprint()
    assert receipt["kinds"]["TABLE"] == {"discovered": 1, "excluded": 1, "invisible": None}
    assert receipt["kinds"]["SCHEMA"]["excluded"] == 1
    # Counted once, on the pass that records changes -- not twice by the reapply pass.
    assert completed.excluded_objects == 2
    assert completed.discovery_selection_fingerprint == receipt["selection_fingerprint"]
    # The sender's copy: a producer polling the batch it submitted can see that some of what
    # it delivered was not kept. Ignoring is allowed; ignoring silently is not.
    assert stored is not None
    assert stored.change_counts["excluded_objects"] == 2
    # The same three facts reach the completion audit record (`details`), which is what this
    # activity returns.
    assert result["excluded_objects"] == 2
    assert result["selection_fingerprint"] == receipt["selection_fingerprint"]
    assert result["retained_out_of_scope"] == 0


async def test_an_unscoped_push_is_exactly_what_it_was(factory) -> None:  # type: ignore[no-untyped-def] # noqa: F811
    """A source that never set a selection behaves as before, down to the null fingerprint:
    an empty selection is unrestricted and has no fingerprint (`DiscoverySelection`)."""
    async with factory() as session:
        datasource = await _scoped_datasource(session, None)
        batch, run = await _delivery(session, datasource)

    await process_metadata_ingestion_batch(str(batch.id))

    async with factory() as session:
        completed = await session.get(AnalysisRun, run.id)
        names = set((await session.scalars(select(MetadataTable.name))).all())
    assert names == {"account", "ledger"}
    assert completed is not None and completed.discovery_receipt is not None
    assert completed.discovery_receipt["selection_fingerprint"] is None
    assert completed.discovery_receipt["kinds"]["TABLE"]["excluded"] == 0
    assert completed.excluded_objects == 0


async def test_a_scoped_full_push_retires_nothing_it_was_told_not_to_look_at(factory) -> None:  # type: ignore[no-untyped-def] # noqa: F811
    """The clause that matters most.

    A FULL delivery retires what it does not carry. The catalog here holds two tables from an
    earlier unscoped run, and this delivery carries neither: one the new scope excludes, one
    it includes. The excluded one was never looked for, so it is reconciled as seen -- the
    same rule, through the same function, as a FULL pull run. The included one really is
    absent from an authoritative snapshot, so it retires. Without the guard, narrowing a
    selection and pushing a snapshot would delete rather than ignore.
    """
    async with factory() as session:
        datasource = await _scoped_datasource(session, SCOPE)
        out_of_scope = await _existing_table(
            session, datasource, schema_name="finance", table_name="ledger"
        )
        in_scope = await _existing_table(
            session, datasource, schema_name="retail", table_name="retired_table"
        )
        batch, run = await _delivery(
            session, datasource, chunks=(("retail", "account"), ("finance", "vault"))
        )

    await process_metadata_ingestion_batch(str(batch.id))

    async with factory() as session:
        kept = await session.get(MetadataTable, out_of_scope.id)
        retired = await session.get(MetadataTable, in_scope.id)
        completed = await session.get(AnalysisRun, run.id)
    assert kept is not None and kept.status == "ACTIVE"
    assert retired is not None and retired.status == "DEPRECATED"
    # The receipt says how much it held back from retiring, as a pull run's does.
    assert completed is not None and completed.discovery_receipt is not None
    reconciliation = completed.discovery_receipt["reconciliation"]
    assert reconciliation["performed"] is True
    assert reconciliation["retained_out_of_scope"] == 1


async def test_an_incremental_scoped_push_still_reconciles_nothing(factory) -> None:  # type: ignore[no-untyped-def] # noqa: F811
    """Scoping changes what a delivery persists, never what an INCREMENTAL one may retire:
    it is not authoritative about absence, and the receipt names that reason."""
    async with factory() as session:
        datasource = await _scoped_datasource(session, SCOPE)
        existing = await _existing_table(
            session, datasource, schema_name="retail", table_name="untouched"
        )
        batch, run = await _delivery(session, datasource, snapshot_type="INCREMENTAL")

    await process_metadata_ingestion_batch(str(batch.id))

    async with factory() as session:
        kept = await session.get(MetadataTable, existing.id)
        completed = await session.get(AnalysisRun, run.id)
    assert kept is not None and kept.status == "ACTIVE"
    assert completed is not None and completed.discovery_receipt is not None
    assert completed.discovery_receipt["reconciliation"] == {
        "performed": False,
        "reason": "INCREMENTAL_MODE",
    }
    # The scope still governed what landed, and the fingerprint still records which one.
    assert completed.discovery_receipt["kinds"]["TABLE"]["excluded"] == 1
    assert completed.discovery_receipt["selection_fingerprint"] is not None


@pytest.mark.parametrize("snapshot_type", ["FULL", "INCREMENTAL"])
async def test_nothing_out_of_scope_is_persisted_on_either_mode(factory, snapshot_type) -> None:  # type: ignore[no-untyped-def] # noqa: F811
    """The selection is applied at persistence, so it does not depend on the mode -- the
    mode only decides what may be retired."""
    async with factory() as session:
        datasource = await _scoped_datasource(session, SCOPE)
        batch, _ = await _delivery(session, datasource, snapshot_type=snapshot_type)

    await process_metadata_ingestion_batch(str(batch.id))

    async with factory() as session:
        names = set((await session.scalars(select(MetadataTable.name))).all())
    assert names == {"account"}
