import asyncio
import contextlib

import structlog
from temporalio.client import Client
from temporalio.worker import Worker

from aida.batch_ingestion import process_metadata_ingestion_batch
from aida.config import get_settings
from aida.logging import configure_logging

# ING-4 / P0-01: imported here (rather than only run as its own __main__
# module) so `tests/test_reachability_gate.py` sees the drafter reachable
# through the existing `aida.workflows.worker` ENTRY_POINTS row -- and so
# the drafter starts on the same process the ingest activities do, which
# is where its input events are produced.
from aida.newly_created_table_drafter import (
    run_newly_created_table_drafter_consumer,
)
from aida.workflows.activities import (
    discover_datasource,
    finalize_profile_tasks,
    plan_profile_tasks,
    profile_datasource,
    profile_table_task,
)
from aida.workflows.discovery import DatasourceDiscoveryWorkflow
from aida.workflows.ingestion import MetadataBatchIngestionWorkflow


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger(__name__)
    client = await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
    )
    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[DatasourceDiscoveryWorkflow, MetadataBatchIngestionWorkflow],
        activities=[
            discover_datasource,
            profile_datasource,
            plan_profile_tasks,
            profile_table_task,
            finalize_profile_tasks,
            process_metadata_ingestion_batch,
        ],
    )
    logger.info("temporal_worker_started", task_queue=settings.temporal_task_queue)
    drafter_task: asyncio.Task[None] | None = None
    if settings.auto_enqueue_on_ingest:
        # ING-4 / P0-01: side-car background task -- consumes
        # `catalog.table.newly_created.v1` from the shared
        # `aida.platform.events.v1` Kafka topic and auto-enqueues an
        # asset-description draft (and, once profiling completes,
        # unblocks a semantic-inference proposal) per newly-created
        # table, so a fresh table no longer sits empty until a steward
        # manually POSTs each drafter endpoint. Runs alongside the
        # Temporal worker rather than as its own deployable so the
        # module stays reachable from the same ENTRY_POINTS row.
        drafter_task = asyncio.create_task(
            run_newly_created_table_drafter_consumer(),
            name="newly_created_table_drafter",
        )
    try:
        await worker.run()
    finally:
        if drafter_task is not None:
            drafter_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drafter_task


if __name__ == "__main__":
    asyncio.run(run_worker())
