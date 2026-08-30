"""PR-5: pure, Temporal-independent checkpoint state machine for
``DatasourceDiscoveryWorkflow``'s continue-as-new loop.

Everything in this module is deliberately free of any ``temporalio`` import,
so it is fully unit-testable without a workflow sandbox or a live Temporal
server -- ``workflows/discovery.py`` is a thin shell that calls these
functions and hands the result to ``workflow.continue_as_new``.

The whole point of continue-as-new at 1M-table scale is that a workflow
execution's history must not grow with the number of tables it has scanned.
``ProfilingProgress`` therefore carries only a keyset cursor (the last
``MetadataTable.id`` seen) and four running counters -- never a table-id
list, never per-table results -- so its serialized size is O(1) regardless
of how many tables the overall analysis run has processed so far.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class ProfilingProgress:
    """Checkpoint carried across one ``continue_as_new`` boundary.

    ``tables_planned_total`` / ``profiled_tables`` / ``profiled_columns`` are
    cumulative across the *entire* run (every execution in the continue-as-new
    chain); ``tables_processed_this_execution`` counts only the current
    execution and is always reset to zero by :meth:`from_state`, since that is
    the number continue-as-new decisions are made against -- each fresh
    execution earns its own budget.
    """

    run_id: str
    cursor: str | None = None
    tables_planned_total: int = 0
    tables_processed_this_execution: int = 0
    profiled_tables: int = 0
    profiled_columns: int = 0

    def to_state(self) -> dict[str, Any]:
        """The compact payload handed to ``workflow.continue_as_new``.

        Deliberately excludes ``run_id`` (carried as its own positional
        argument, not duplicated into the state blob) and
        ``tables_processed_this_execution`` (an execution-local counter that
        the next execution must start fresh, never inherit).
        """
        return {
            "cursor": self.cursor,
            "tables_planned_total": self.tables_planned_total,
            "profiled_tables": self.profiled_tables,
            "profiled_columns": self.profiled_columns,
        }

    @classmethod
    def initial(cls, run_id: str) -> ProfilingProgress:
        return cls(run_id=run_id)

    @classmethod
    def from_state(cls, run_id: str, state: dict[str, Any] | None) -> ProfilingProgress:
        """Rehydrate progress at the start of a workflow execution.

        ``state is None`` means this is the run's very first execution (no
        continue-as-new has happened yet); any other value is what a prior
        execution's :meth:`to_state` produced.
        """
        if not state:
            return cls.initial(run_id)
        return cls(
            run_id=run_id,
            cursor=state.get("cursor"),
            tables_planned_total=int(state.get("tables_planned_total", 0)),
            tables_processed_this_execution=0,
            profiled_tables=int(state.get("profiled_tables", 0)),
            profiled_columns=int(state.get("profiled_columns", 0)),
        )


def clamp_page_size(requested: int, *, maximum: int) -> int:
    """Bound a page size to ``[1, max(1, maximum)]``.

    Never returns zero or a negative number: a page size of 0 would make the
    keyset scan loop forever without ever advancing the cursor, silently
    building an unbounded chain of continue-as-new executions instead of
    failing loudly or simply making progress.
    """
    return max(1, min(requested, max(1, maximum)))


def advance(
    progress: ProfilingProgress,
    *,
    next_cursor: str | None,
    tables_in_page: int,
    profiled_tables: int,
    profiled_columns: int,
) -> ProfilingProgress:
    """Fold one page's worth of activity results into the checkpoint."""
    return replace(
        progress,
        cursor=next_cursor,
        tables_planned_total=progress.tables_planned_total + tables_in_page,
        tables_processed_this_execution=(
            progress.tables_processed_this_execution + tables_in_page
        ),
        profiled_tables=progress.profiled_tables + profiled_tables,
        profiled_columns=progress.profiled_columns + profiled_columns,
    )


def should_continue_as_new(progress: ProfilingProgress, *, max_tables_per_execution: int) -> bool:
    """Whether this execution has processed enough tables that it should hand
    off to a fresh execution via ``continue_as_new`` rather than keep growing
    its own event history.

    ``>=`` (not ``>``): an execution that has processed exactly
    ``max_tables_per_execution`` tables has met its budget and should roll
    over immediately, rather than being allowed one more page first.
    """
    return progress.tables_processed_this_execution >= max_tables_per_execution
