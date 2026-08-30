"""PR-5: unit tests for the pure continue-as-new checkpoint state machine.

`aida.workflows.continuation` is deliberately free of any `temporalio` import
so these run with no workflow sandbox and no live Temporal server -- they are
the actual proof of correctness for the checkpoint/resume logic; see that
module's docstring and the PR-5 tracker note for what remains unverified
(a real 1M-table run against live infrastructure).
"""

from __future__ import annotations

from aida.workflows.continuation import (
    ProfilingProgress,
    advance,
    clamp_page_size,
    should_continue_as_new,
)


def test_initial_progress_starts_at_zero_with_no_cursor() -> None:
    progress = ProfilingProgress.initial("run-1")
    assert progress.run_id == "run-1"
    assert progress.cursor is None
    assert progress.tables_planned_total == 0
    assert progress.tables_processed_this_execution == 0
    assert progress.profiled_tables == 0
    assert progress.profiled_columns == 0


def test_from_state_with_none_state_is_the_same_as_initial() -> None:
    assert ProfilingProgress.from_state("run-1", None) == ProfilingProgress.initial("run-1")


def test_from_state_with_empty_dict_is_also_treated_as_initial() -> None:
    # `not state` in `from_state` deliberately treats `{}` the same as `None` --
    # both mean "nothing to rehydrate".
    assert ProfilingProgress.from_state("run-1", {}) == ProfilingProgress.initial("run-1")


def test_from_state_rehydrates_cumulative_counters_and_resets_the_per_execution_one() -> None:
    prior = ProfilingProgress(
        run_id="run-1",
        cursor="cursor-abc",
        tables_planned_total=4_500,
        tables_processed_this_execution=2_000,  # must NOT carry forward
        profiled_tables=4_480,
        profiled_columns=55_000,
    )
    resumed = ProfilingProgress.from_state("run-1", prior.to_state())
    assert resumed.cursor == "cursor-abc"
    assert resumed.tables_planned_total == 4_500
    assert resumed.profiled_tables == 4_480
    assert resumed.profiled_columns == 55_000
    # The new execution earns its own fresh budget.
    assert resumed.tables_processed_this_execution == 0


def test_to_state_excludes_run_id_and_the_execution_local_counter() -> None:
    progress = ProfilingProgress(
        run_id="run-1", cursor="c1", tables_planned_total=10, tables_processed_this_execution=10
    )
    state = progress.to_state()
    assert "run_id" not in state
    assert "tables_processed_this_execution" not in state


def test_advance_accumulates_across_multiple_pages() -> None:
    progress = ProfilingProgress.initial("run-1")
    progress = advance(
        progress, next_cursor="c1", tables_in_page=500, profiled_tables=500, profiled_columns=6_000
    )
    progress = advance(
        progress, next_cursor="c2", tables_in_page=500, profiled_tables=498, profiled_columns=5_950
    )
    assert progress.cursor == "c2"
    assert progress.tables_planned_total == 1_000
    assert progress.tables_processed_this_execution == 1_000
    assert progress.profiled_tables == 998
    assert progress.profiled_columns == 11_950


def test_advance_never_mutates_run_id() -> None:
    progress = ProfilingProgress.initial("run-1")
    updated = advance(
        progress, next_cursor="c1", tables_in_page=1, profiled_tables=1, profiled_columns=1
    )
    assert updated.run_id == "run-1"
    # the original is untouched -- `advance` is pure
    assert progress.tables_planned_total == 0


def test_should_continue_as_new_is_false_below_the_execution_budget() -> None:
    progress = ProfilingProgress(run_id="run-1", tables_processed_this_execution=1_999)
    assert should_continue_as_new(progress, max_tables_per_execution=2_000) is False


def test_should_continue_as_new_boundary_exact_fit_triggers_immediately() -> None:
    """The off-by-one this PR's exit condition explicitly calls out: an
    execution that has processed *exactly* the budget's worth of tables must
    roll over now, via `>=` -- not be misread as "not yet at the limit" (which
    a `>` comparison would do) nor be allowed one extra page past the budget.
    """
    progress = ProfilingProgress(run_id="run-1", tables_processed_this_execution=2_000)
    assert should_continue_as_new(progress, max_tables_per_execution=2_000) is True


def test_should_continue_as_new_true_once_past_the_budget() -> None:
    progress = ProfilingProgress(run_id="run-1", tables_processed_this_execution=2_001)
    assert should_continue_as_new(progress, max_tables_per_execution=2_000) is True


def test_clamp_page_size_passes_through_an_in_range_value() -> None:
    assert clamp_page_size(500, maximum=10_000) == 500


def test_clamp_page_size_caps_an_oversized_request() -> None:
    assert clamp_page_size(50_000, maximum=10_000) == 10_000


def test_clamp_page_size_floors_zero_and_negative_requests_to_one() -> None:
    assert clamp_page_size(0, maximum=10_000) == 1
    assert clamp_page_size(-5, maximum=10_000) == 1


def test_clamp_page_size_never_returns_zero_even_with_a_zero_maximum() -> None:
    # A misconfigured `maximum=0` must not produce the infinite-loop hazard
    # described in the function's docstring.
    assert clamp_page_size(100, maximum=0) == 1
