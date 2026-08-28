from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn(name="MetadataBatchIngestionWorkflow")
class MetadataBatchIngestionWorkflow:
    @workflow.run
    async def run(self, batch_id: str) -> dict[str, Any]:
        result = await workflow.execute_activity(
            "process_metadata_ingestion_batch",
            batch_id,
            start_to_close_timeout=timedelta(hours=12),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=5),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=5),
                maximum_attempts=5,
                non_retryable_error_types=["BatchContractError"],
            ),
        )
        return cast(dict[str, Any], result)
