import asyncio
from datetime import timedelta
from typing import Any, cast

from temporalio import workflow
from temporalio.common import RetryPolicy

from aida.workflows.continuation import ProfilingProgress, advance, should_continue_as_new


@workflow.defn(name="DatasourceDiscoveryWorkflow")
class DatasourceDiscoveryWorkflow:
    @workflow.run
    async def run(self, run_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
        """PR-5: paginated plan, looped, with continue-as-new at scale.

        ``state`` is ``None`` on the run's very first execution and a
        ``ProfilingProgress.to_state()`` payload on every execution after a
        ``continue_as_new`` -- see that module for why the carried state is
        deliberately just a cursor and four counters, never a table-id list
        or per-table results. ``discover_datasource`` only ever runs once per
        overall run (``state is None``); every later execution in the same
        continue-as-new chain skips straight to resuming the profiling loop,
        since re-running discovery on every hand-off would be both wasteful
        and semantically wrong for a run that already has a scope.
        """
        progress = ProfilingProgress.from_state(run_id, state)
        if state is None:
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
        while True:
            plan = await workflow.execute_activity(
                "plan_profile_tasks",
                {
                    "run_id": run_id,
                    "cursor": progress.cursor,
                    "tables_planned_total": progress.tables_planned_total,
                },
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
            page_profiled_tables = 0
            page_profiled_columns = 0
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
                    page_profiled_tables += counts["profiled_tables"]
                    page_profiled_columns += counts["profiled_columns"]
            progress = advance(
                progress,
                next_cursor=cast("str | None", typed_plan.get("next_cursor")),
                tables_in_page=len(table_ids),
                profiled_tables=page_profiled_tables,
                profiled_columns=page_profiled_columns,
            )
            if not typed_plan.get("has_more"):
                break
            max_tables_per_execution = int(
                typed_plan.get("continue_as_new_after_tables", 0) or 0
            )
            if max_tables_per_execution > 0 and should_continue_as_new(
                progress, max_tables_per_execution=max_tables_per_execution
            ):
                # `continue_as_new` never returns -- it always raises
                # `ContinueAsNewError`, which must propagate out of `run`
                # uncaught so the SDK can start the fresh execution.
                workflow.continue_as_new(args=[run_id, progress.to_state()])
        result = await workflow.execute_activity(
            "finalize_profile_tasks",
            {
                "run_id": run_id,
                "profiled_tables": progress.profiled_tables,
                "profiled_columns": progress.profiled_columns,
            },
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )
        return cast(dict[str, Any], result)
