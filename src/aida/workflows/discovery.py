import asyncio
from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn(name="DatasourceDiscoveryWorkflow")
class DatasourceDiscoveryWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict[str, Any]:
        await workflow.execute_activity(
            "discover_datasource",
            run_id,
            start_to_close_timeout=timedelta(minutes=20),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=1),
                maximum_attempts=5,
                non_retryable_error_types=[
                    "SecretResolutionError",
                    "UnsupportedConnectorError",
                ],
            ),
        )
        plan = await workflow.execute_activity(
            "plan_profile_tasks",
            run_id,
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                backoff_coefficient=2.0,
                maximum_interval=timedelta(minutes=1),
                maximum_attempts=5,
                non_retryable_error_types=[
                    "SecretResolutionError",
                    "UnsupportedConnectorError",
                    "DataSourceDisabledError",
                ],
            ),
        )
        typed_plan = cast(dict[str, Any], plan)
        table_ids = cast(list[str], typed_plan["table_ids"])
        max_concurrency = max(1, int(typed_plan["max_concurrency"]))
        profiled_tables = 0
        profiled_columns = 0
        for batch_start in range(0, len(table_ids), max_concurrency):
            batch = table_ids[batch_start : batch_start + max_concurrency]
            results = await asyncio.gather(
                *(
                    workflow.execute_activity(
                        "profile_table_task",
                        {"run_id": run_id, "table_id": table_id},
                        start_to_close_timeout=timedelta(minutes=30),
                        heartbeat_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(
                            initial_interval=timedelta(seconds=5),
                            backoff_coefficient=2.0,
                            maximum_interval=timedelta(minutes=2),
                            maximum_attempts=4,
                            non_retryable_error_types=[
                                "SecretResolutionError",
                                "UnsupportedConnectorError",
                                "DataSourceDisabledError",
                            ],
                        ),
                    )
                    for table_id in batch
                )
            )
            for task_result in results:
                counts = cast(dict[str, int], task_result)
                profiled_tables += counts["profiled_tables"]
                profiled_columns += counts["profiled_columns"]
        result = await workflow.execute_activity(
            "finalize_profile_tasks",
            {
                "run_id": run_id,
                "profiled_tables": profiled_tables,
                "profiled_columns": profiled_columns,
            },
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        return cast(dict[str, Any], result)
