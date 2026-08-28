import asyncio

import structlog
from temporalio.client import Client
from temporalio.worker import Worker

from aida.batch_ingestion import process_metadata_ingestion_batch
from aida.config import get_settings
from aida.logging import configure_logging
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
    await worker.run()


if __name__ == "__main__":
    asyncio.run(run_worker())
