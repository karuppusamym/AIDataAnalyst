"""PR-5: `DatasourceDiscoveryWorkflow`'s paginated loop and continue-as-new hand-off.

Mirrors `test_high_stakes_behaviors.py::test_discovery_workflow_heartbeats_
retryable_stages_and_aggregates_profiles`'s approach of monkeypatching
`discovery.workflow.execute_activity` directly rather than needing a live
Temporal server or `WorkflowEnvironment.start_time_skipping()` (confirmed
unreachable in this sandbox by a prior attempt at this task) -- these are
still real assertions about the actual workflow code, not the pure state
machine module (that has its own dedicated tests in
`test_continue_as_new_checkpoint.py`); this file is the seam between the two.
"""

from __future__ import annotations

from typing import Any

import pytest

from aida.workflows import discovery

pytestmark = pytest.mark.asyncio


class _ContinueAsNewCalled(Exception):
    """Stand-in for temporalio's `ContinueAsNewError`, which always raises and
    never returns -- the real `workflow.continue_as_new` is a plain (non-async)
    function outside of a workflow sandbox, per `inspect.signature`.
    """

    def __init__(self, args: list[Any]) -> None:
        super().__init__("continue_as_new")
        self.args_ = args


def _fake_continue_as_new(*, args: list[Any], **_kwargs: object) -> None:
    raise _ContinueAsNewCalled(args)


async def test_multi_page_run_aggregates_across_pages_without_continuing_as_new(
    monkeypatch: Any,
) -> None:
    """Two pages, `has_more` True then False, well under the continue-as-new
    budget -- the workflow should just keep looping in the same execution.
    """
    calls: list[tuple[str, object]] = []
    pages = [
        {
            "table_ids": ["t1", "t2"],
            "max_concurrency": 2,
            "has_more": True,
            "next_cursor": "cursor-1",
            "continue_as_new_after_tables": 1_000,
        },
        {
            "table_ids": ["t3"],
            "max_concurrency": 2,
            "has_more": False,
            "next_cursor": "cursor-2",
            "continue_as_new_after_tables": 1_000,
        },
    ]

    async def execute_activity(name: str, argument: object, **kwargs: object) -> object:
        calls.append((name, argument))
        if name == "plan_profile_tasks":
            return pages.pop(0)
        if name == "profile_table_task":
            return {"profiled_tables": 1, "profiled_columns": 4}
        if name == "finalize_profile_tasks":
            return argument
        return {}

    monkeypatch.setattr(discovery.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(discovery.workflow, "continue_as_new", _fake_continue_as_new)

    result = await discovery.DatasourceDiscoveryWorkflow().run("run-multi")

    assert result == {
        "run_id": "run-multi",
        "profiled_tables": 3,
        "profiled_columns": 12,
    }
    plan_calls = [call for call in calls if call[0] == "plan_profile_tasks"]
    assert len(plan_calls) == 2
    # Second call's cursor/total must reflect the first page's results.
    assert plan_calls[1][1]["cursor"] == "cursor-1"
    assert plan_calls[1][1]["tables_planned_total"] == 2
    assert len([c for c in calls if c[0] == "profile_table_task"]) == 3
    assert len([c for c in calls if c[0] == "discover_datasource"]) == 1


async def test_execution_hands_off_via_continue_as_new_once_the_budget_is_met(
    monkeypatch: Any,
) -> None:
    """A single page that alone meets `continue_as_new_after_tables` must hand
    off immediately, carrying forward a compact state -- never a table-id list.
    """

    async def execute_activity(name: str, argument: object, **kwargs: object) -> object:
        if name == "plan_profile_tasks":
            return {
                "table_ids": ["t1", "t2"],
                "max_concurrency": 2,
                "has_more": True,
                "next_cursor": "cursor-1",
                "continue_as_new_after_tables": 2,
            }
        if name == "profile_table_task":
            return {"profiled_tables": 1, "profiled_columns": 5}
        return {}

    monkeypatch.setattr(discovery.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(discovery.workflow, "continue_as_new", _fake_continue_as_new)

    with pytest.raises(_ContinueAsNewCalled) as excinfo:
        await discovery.DatasourceDiscoveryWorkflow().run("run-scale")

    run_id, state = excinfo.value.args_
    assert run_id == "run-scale"
    # Compact state only: a cursor and counters, never a table-id list.
    assert set(state) == {"cursor", "tables_planned_total", "profiled_tables", "profiled_columns"}
    assert state["cursor"] == "cursor-1"
    assert state["tables_planned_total"] == 2
    assert state["profiled_tables"] == 2
    assert state["profiled_columns"] == 10


async def test_a_resumed_execution_skips_discovery_and_starts_from_its_cursor(
    monkeypatch: Any,
) -> None:
    """`state is not None` means this execution is resuming after a prior
    `continue_as_new` -- `discover_datasource` (already run once for this
    overall analysis run) must not run again, and the very first
    `plan_profile_tasks` call must carry the resumed cursor/total forward.
    """
    calls: list[tuple[str, object]] = []

    async def execute_activity(name: str, argument: object, **kwargs: object) -> object:
        calls.append((name, argument))
        if name == "plan_profile_tasks":
            return {
                "table_ids": ["t9"],
                "max_concurrency": 2,
                "has_more": False,
                "next_cursor": "cursor-9",
                "continue_as_new_after_tables": 1_000,
            }
        if name == "profile_table_task":
            return {"profiled_tables": 1, "profiled_columns": 3}
        if name == "finalize_profile_tasks":
            return argument
        return {}

    monkeypatch.setattr(discovery.workflow, "execute_activity", execute_activity)

    resumed_state = {
        "cursor": "cursor-8",
        "tables_planned_total": 4_000,
        "profiled_tables": 3_995,
        "profiled_columns": 48_000,
    }
    result = await discovery.DatasourceDiscoveryWorkflow().run("run-resumed", resumed_state)

    assert [c[0] for c in calls] == [
        "plan_profile_tasks",
        "profile_table_task",
        "finalize_profile_tasks",
    ]
    plan_argument = calls[0][1]
    assert plan_argument["cursor"] == "cursor-8"
    assert plan_argument["tables_planned_total"] == 4_000
    assert result == {
        "run_id": "run-resumed",
        "profiled_tables": 3_996,
        "profiled_columns": 48_003,
    }


async def test_a_missing_continue_as_new_threshold_never_continues_as_new(
    monkeypatch: Any,
) -> None:
    """Backward compatibility with a plan payload that omits
    `continue_as_new_after_tables` entirely (e.g. an older activity worker
    mid-deploy): the workflow must not crash or treat a missing/zero threshold
    as "continue immediately" -- it degrades to "never continue as new",
    which is exactly the old (pre-PR-5) single-execution behaviour.
    """

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("continue_as_new must not be called")

    async def execute_activity(name: str, argument: object, **kwargs: object) -> object:
        if name == "plan_profile_tasks":
            return {"table_ids": ["t1"], "max_concurrency": 1, "has_more": False}
        if name == "profile_table_task":
            return {"profiled_tables": 1, "profiled_columns": 1}
        if name == "finalize_profile_tasks":
            return argument
        return {}

    monkeypatch.setattr(discovery.workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(discovery.workflow, "continue_as_new", _explode)

    result = await discovery.DatasourceDiscoveryWorkflow().run("run-no-threshold")
    assert result["profiled_tables"] == 1
