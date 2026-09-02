import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import null, select, update
from temporalio import activity
from temporalio.exceptions import ApplicationError

from aida.config import get_settings
from aida.db import session_factory
from aida.events import record_audit, record_outbox
from aida.ingestion import (
    EnvelopeScope,
    catalogs_to_discovery,
    deprecate_missing_envelope_extensions,
    persist_envelope_extensions,
)
from aida.models import (
    AnalysisRun,
    DataSource,
    MetadataIngestionBatch,
    MetadataIngestionChunk,
)
from aida.schemas import MetadataIngestionChunkCreate
from aida.security import SecurityContext
from aida.workflows.activities import (
    SnapshotScope,
    deprecate_missing_snapshot,
    detect_rename_candidates,
    persist_discovery_snapshot,
)


class BatchContractError(ValueError):
    pass


# IN-2: statuses an operator can drive a batch into that the worker must stop
# for. The activity re-reads the manifest status cooperatively between chunks
# and raises `BatchControlSignal` when it sees one of these, so a pause/cancel
# issued on the operator console takes effect without the workflow having to be
# killed mid-flight. CANCELLED is terminal; PAUSED is resumable via a fresh
# workflow (see `resume_metadata_ingestion_batch`).
_BATCH_STOP_STATUSES = frozenset({"PAUSED", "CANCELLED"})


class BatchControlSignal(Exception):
    """Cooperative stop: the operator moved the batch to PAUSED or CANCELLED
    while the activity was running. Carries the observed status so the activity
    can return it cleanly rather than failing the batch."""

    def __init__(self, status: str) -> None:
        super().__init__(f"batch control signal: {status}")
        self.status = status


async def _batch_control_status(batch_id: UUID) -> str | None:
    """Re-read the batch status in a fresh session and return it if it is a
    cooperative stop status (PAUSED/CANCELLED), else None."""
    async with session_factory() as session:
        status = await session.scalar(
            select(MetadataIngestionBatch.status).where(MetadataIngestionBatch.id == batch_id)
        )
    if status in _BATCH_STOP_STATUSES:
        return str(status)
    return None


async def _mark_batch_failed(batch_id: UUID, exc: Exception) -> None:
    async with session_factory() as session:
        batch = await session.get(MetadataIngestionBatch, batch_id)
        # A batch an operator has already paused or cancelled (or that finished)
        # must not be stamped FAILED by a late worker exception -- the operator's
        # transition wins.
        if batch is None or batch.status in {"COMPLETED", *_BATCH_STOP_STATUSES}:
            return
        batch.status = "FAILED"
        batch.error_class = type(exc).__name__
        batch.error_message = str(exc)[:1000]
        if batch.analysis_run_id:
            run = await session.get(AnalysisRun, batch.analysis_run_id)
            if run is not None:
                run.status = "FAILED"
                run.error_class = type(exc).__name__
                run.error_message = "chunked metadata ingestion failed"
        await session.commit()


async def _preflight_batch(batch_id: UUID) -> list[UUID]:
    settings = get_settings()
    async with session_factory() as session:
        batch = await session.get(MetadataIngestionBatch, batch_id)
        if batch is None:
            raise BatchContractError("metadata ingestion batch not found")
        chunk_rows = (
            await session.execute(
                select(MetadataIngestionChunk.id, MetadataIngestionChunk.chunk_number)
                .where(MetadataIngestionChunk.batch_id == batch.id)
                .order_by(MetadataIngestionChunk.chunk_number)
            )
        ).all()
        numbers = [number for _, number in chunk_rows]
        if numbers != list(range(1, batch.expected_chunks + 1)):
            raise BatchContractError(
                "batch chunks must be complete and numbered consecutively from one"
            )
        if batch.expected_chunks > settings.metadata_batch_max_chunks:
            raise BatchContractError("batch exceeds the configured chunk limit")
        chunk_ids = [chunk_id for chunk_id, _ in chunk_rows]

    table_keys: set[tuple[str, str, str]] = set()
    catalog_attributes: dict[str, str] = {}
    schema_attributes: dict[tuple[str, str], str] = {}
    total_tables = 0
    total_columns = 0
    for index, chunk_id in enumerate(chunk_ids, start=1):
        async with session_factory() as session:
            chunk = await session.get(MetadataIngestionChunk, chunk_id)
            if chunk is None or chunk.payload is None:
                raise BatchContractError("a required chunk payload is unavailable")
            body = MetadataIngestionChunkCreate.model_validate(chunk.payload)
        for catalog in body.catalogs:
            catalog_signature = json.dumps(
                catalog.attributes, sort_keys=True, separators=(",", ":")
            )
            previous_catalog = catalog_attributes.setdefault(catalog.name, catalog_signature)
            if previous_catalog != catalog_signature:
                raise BatchContractError(
                    f"catalog attributes conflict across chunks: {catalog.name}"
                )
            for schema in catalog.schemas:
                schema_key = (catalog.name, schema.name)
                schema_signature = json.dumps(
                    schema.attributes, sort_keys=True, separators=(",", ":")
                )
                previous_schema = schema_attributes.setdefault(schema_key, schema_signature)
                if previous_schema != schema_signature:
                    raise BatchContractError(
                        "schema attributes conflict across chunks: " + ".".join(schema_key)
                    )
                for table in schema.tables:
                    total_tables += 1
                    total_columns += len(table.columns)
                    if total_tables > settings.metadata_batch_max_tables:
                        raise BatchContractError("batch exceeds the configured table limit")
                    if total_columns > settings.metadata_batch_max_columns:
                        raise BatchContractError("batch exceeds the configured column limit")
                    table_key = (catalog.name, schema.name, table.name)
                    if table_key in table_keys:
                        raise BatchContractError(
                            "a table may appear in only one chunk: " + ".".join(table_key)
                        )
                    table_keys.add(table_key)
        if activity.in_activity():
            activity.heartbeat(
                {
                    "stage": "preflight",
                    "validated_chunks": index,
                    "total_chunks": len(chunk_ids),
                }
            )
    return chunk_ids


async def _process_chunk(
    batch_id: UUID,
    chunk_id: UUID,
    scope: SnapshotScope,
    envelope_scope: EnvelopeScope,
    *,
    record_changes: bool,
) -> None:
    async with session_factory() as session:
        batch = await session.get(MetadataIngestionBatch, batch_id)
        chunk = await session.get(MetadataIngestionChunk, chunk_id)
        if batch is None or chunk is None or chunk.payload is None:
            raise BatchContractError("batch or chunk payload became unavailable")
        datasource = await session.get(DataSource, batch.datasource_id)
        run = (
            await session.get(AnalysisRun, batch.analysis_run_id) if batch.analysis_run_id else None
        )
        if datasource is None or run is None:
            raise BatchContractError("batch datasource or analysis run is unavailable")
        body = MetadataIngestionChunkCreate.model_validate(chunk.payload)
        prior_status = chunk.status
        discovery = catalogs_to_discovery(body.catalogs)
        counts = await persist_discovery_snapshot(
            session,
            run,
            datasource,
            discovery,
            deprecate_missing=False,
            connector_capabilities={
                **(datasource.capabilities or {}),
                "canonical_push": True,
                "chunked_ingestion": True,
            },
            scope=scope,
        )
        # `deprecate_missing=False` here is the INV-11 rule, not an oversight: the
        # 1.1 axes accumulate identities into `envelope_scope` across every chunk
        # and are reconciled once, in `_complete_batch`, after all chunks have
        # succeeded. A chunk-local reconciliation would retire every view
        # definition that happened to live in a later chunk.
        extension_counts = await persist_envelope_extensions(
            session,
            datasource,
            discovery,
            scope=envelope_scope,
            deprecate_missing=False,
        )
        if record_changes and prior_status != "PROCESSED":
            chunk.change_counts = {
                key: counts[key] + extension_counts[key]
                for key in ("created_objects", "changed_objects")
            }
            chunk.status = "PROCESSED"
            chunk.processed_at = datetime.now(UTC)
            batch.processed_chunks += 1
        await session.commit()


async def _complete_batch(
    batch_id: UUID, scope: SnapshotScope, envelope_scope: EnvelopeScope
) -> dict[str, Any]:
    async with session_factory() as session:
        batch = await session.scalar(
            select(MetadataIngestionBatch)
            .where(MetadataIngestionBatch.id == batch_id)
            .with_for_update()
        )
        if batch is None:
            raise BatchContractError("metadata ingestion batch not found")
        datasource = await session.get(DataSource, batch.datasource_id)
        run = (
            await session.get(AnalysisRun, batch.analysis_run_id) if batch.analysis_run_id else None
        )
        if datasource is None or run is None:
            raise BatchContractError("batch datasource or analysis run is unavailable")
        chunks = (
            await session.scalars(
                select(MetadataIngestionChunk).where(MetadataIngestionChunk.batch_id == batch.id)
            )
        ).all()
        created = sum(int(chunk.change_counts.get("created_objects", 0)) for chunk in chunks)
        changed = sum(int(chunk.change_counts.get("changed_objects", 0)) for chunk in chunks)
        deprecated = 0
        if batch.snapshot_type == "FULL":
            deprecation_result = await deprecate_missing_snapshot(session, datasource, scope)
            deprecated = deprecation_result.total
            # Gated on the declared version as well as on FULL: a 1.0 batch is
            # authoritative for the 1.0 inventory only and says nothing about the
            # 1.1 axes, so reconciling its silence would retire them.
            if batch.envelope_version != "1.0":
                deprecated += await deprecate_missing_envelope_extensions(
                    session, datasource, envelope_scope
                )
            # CT-4: same-run tombstone-plus-create pairing, exactly as in the
            # unchunked pull path (`persist_discovery_snapshot`) -- `scope` here
            # is the same SnapshotScope accumulated across every chunk via
            # `_process_chunk`, so `scope.created_table_ids` is every table this
            # batch actually created and `deprecation_result.deprecated_table_ids`
            # is exactly what this call just tombstoned.
            await detect_rename_candidates(
                session,
                run=run,
                datasource=datasource,
                created_table_ids=scope.created_table_ids,
                deprecated_table_ids=deprecation_result.deprecated_table_ids,
            )
        object_counts = {**scope.object_counts(), **envelope_scope.object_counts()}
        change_counts = {
            "created_objects": created,
            "changed_objects": changed,
            "deprecated_objects": deprecated,
        }
        run.discovered_catalogs = object_counts["catalogs"]
        run.discovered_schemas = object_counts["schemas"]
        run.discovered_tables = object_counts["tables"]
        run.discovered_columns = object_counts["columns"]
        run.discovered_constraints = object_counts["constraints"]
        run.discovered_indexes = object_counts["indexes"]
        run.discovered_partitions = object_counts["partitions"]
        run.created_objects = created
        run.changed_objects = changed
        run.deprecated_objects = deprecated
        run.status = "COMPLETED"
        batch.object_counts = object_counts
        batch.change_counts = change_counts
        batch.processed_chunks = len(chunks)
        batch.status = "COMPLETED"
        batch.completed_at = datetime.now(UTC)
        batch.error_class = None
        batch.error_message = None
        await session.execute(
            update(MetadataIngestionChunk)
            .where(MetadataIngestionChunk.batch_id == batch.id)
            .values(payload=null())
        )
        worker_context = SecurityContext(
            principal_id="metadata-batch-worker",
            principal_type="WORKER",
            organization_id=batch.organization_id,
            roles=frozenset({"MetadataWorker"}),
        )
        details = {
            "datasource_id": str(batch.datasource_id),
            "expected_chunks": batch.expected_chunks,
            "snapshot_type": batch.snapshot_type,
            **object_counts,
            **change_counts,
        }
        record_audit(
            session,
            worker_context,
            action="metadata.ingestion.batch.complete",
            resource_type="metadata_ingestion_batch",
            resource_id=str(batch.id),
            outcome="SUCCESS",
            correlation_id=str(batch.id),
            details=details,
        )
        record_outbox(
            session,
            organization_id=batch.organization_id,
            aggregate_type="metadata_ingestion_batch",
            aggregate_id=str(batch.id),
            event_type="metadata.discovery.snapshot.v1",
            payload={
                "batch_id": str(batch.id),
                "run_id": str(run.id),
                "datasource_id": str(batch.datasource_id),
                **object_counts,
                **change_counts,
            },
        )
        await session.commit()
        return {"batch_id": str(batch.id), "status": batch.status, **details}


@activity.defn(name="process_metadata_ingestion_batch")
async def process_metadata_ingestion_batch(batch_id: str) -> dict[str, Any]:
    batch_uuid = UUID(batch_id)
    try:
        async with session_factory() as session:
            batch = await session.get(MetadataIngestionBatch, batch_uuid)
            if batch is None:
                raise BatchContractError("metadata ingestion batch not found")
            if batch.status == "COMPLETED":
                return {
                    "batch_id": str(batch.id),
                    "status": batch.status,
                    **batch.object_counts,
                    **batch.change_counts,
                }
            # IN-2: honor an operator pause/cancel that landed before this
            # activity picked the batch up (it may have been PAUSED/CANCELLED
            # while still QUEUED). Stop cleanly rather than overwriting the
            # operator's status with PROCESSING.
            if batch.status in _BATCH_STOP_STATUSES:
                return {"batch_id": str(batch.id), "status": batch.status, "stopped": True}
            datasource = await session.get(DataSource, batch.datasource_id)
            if datasource is None or datasource.status == "DISABLED":
                raise BatchContractError("batch datasource is unavailable or disabled")
            batch.status = "PROCESSING"
            batch.error_class = None
            batch.error_message = None
            if batch.analysis_run_id:
                run = await session.get(AnalysisRun, batch.analysis_run_id)
                if run is not None:
                    run.status = "RUNNING"
            await session.commit()

        chunk_ids = await _preflight_batch(batch_uuid)
        scope = SnapshotScope()
        envelope_scope = EnvelopeScope()
        for index, chunk_id in enumerate(chunk_ids, start=1):
            # IN-2: cooperative checkpoint. An operator pause/cancel issued via
            # the console flips the manifest status; the worker observes it here,
            # between chunks, and stops promptly without a partial reconciliation
            # (a FULL batch reconciles only in `_complete_batch`, which this stop
            # never reaches, so no metadata is retired from a stopped delivery).
            control = await _batch_control_status(batch_uuid)
            if control is not None:
                raise BatchControlSignal(control)
            await _process_chunk(
                batch_uuid, chunk_id, scope, envelope_scope, record_changes=True
            )
            if activity.in_activity():
                activity.heartbeat(
                    {
                        "stage": "persisting",
                        "processed_chunks": index,
                        "total_chunks": len(chunk_ids),
                    }
                )
        # Reapply metadata without recording changes so cross-chunk foreign keys resolve
        # regardless of chunk order. Fingerprints make this pass idempotent.
        for chunk_id in chunk_ids:
            control = await _batch_control_status(batch_uuid)
            if control is not None:
                raise BatchControlSignal(control)
            await _process_chunk(
                batch_uuid, chunk_id, scope, envelope_scope, record_changes=False
            )
        return await _complete_batch(batch_uuid, scope, envelope_scope)
    except BatchControlSignal as signal:
        # The operator already owns the batch's status (PAUSED/CANCELLED); return
        # cleanly so the workflow completes without retrying and without marking
        # the batch FAILED. A PAUSED batch is later re-driven by a fresh workflow
        # on resume.
        return {"batch_id": str(batch_uuid), "status": signal.status, "stopped": True}
    except BatchContractError as exc:
        await _mark_batch_failed(batch_uuid, exc)
        raise ApplicationError(str(exc), type="BatchContractError", non_retryable=True) from exc
    except Exception as exc:
        await _mark_batch_failed(batch_uuid, exc)
        raise
