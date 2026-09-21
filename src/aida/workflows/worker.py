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
    supervise_newly_created_table_drafter,
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


def _report_drafter_task_exit(task: asyncio.Task[None]) -> None:
    """Say so at once if the supervised drafter task ends with an error.

    The supervisor is written never to raise, so this should not fire. It is here
    because the failure it replaces (R11-AUD03) was silent for exactly this
    reason: the task died, nothing awaited it until the worker shut down, and
    nothing logged in between. A task that ends by cancellation or by returning
    (a stop signal) is not an error.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        structlog.get_logger(__name__).error(
            "newly_created_table_drafter_supervisor_failed",
            error_type=type(exc).__name__,
            exc_info=exc,
        )


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
        #
        # R11-AUD03: it is the *supervisor* that runs here, not the bare
        # consumer. The default stack has no Redpanda, and the bare consumer's
        # `start()` then raised inside this task, which nobody awaits until
        # shutdown: automatic drafting quietly never happened and nothing
        # logged. The supervisor logs `newly_created_table_drafter_unavailable`
        # and retries with a capped backoff, so a broker that appears later is
        # picked up without restarting the worker, and a broker that never
        # appears cannot take the Temporal worker down. Set
        # `AIDA_AUTO_ENQUEUE_ON_INGEST=false` to not start it at all.
        drafter_task = asyncio.create_task(
            supervise_newly_created_table_drafter(),
            name="newly_created_table_drafter",
        )
        drafter_task.add_done_callback(_report_drafter_task_exit)
    try:
        await worker.run()
    finally:
        if drafter_task is not None:
            drafter_task.cancel()
            # It ends by this cancellation, or earlier by a stop signal, or -- a
            # bug, since the supervisor is written not to raise -- with an
            # exception the done-callback has already logged. None of those may
            # replace whatever `worker.run()` ended with.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drafter_task


if __name__ == "__main__":
    asyncio.run(run_worker())
