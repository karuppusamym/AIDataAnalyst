"""The last outcome of each fleet-scheduler pass, persisted for the Operations screen (R11-VAL04).

Round 12 contained a failing pass: `run_scheduler_iteration` runs each one under `_isolated`,
which logs `scheduler_pass_failed` and counts `aida_scheduler_pass_failures_total`, and the rest
of the iteration carries on. Both signals live in the scheduler process. The Operations screen
reads the API, a different process with its own metrics registry, so a pass failing on every
iteration was still invisible there. This module is the persisted half: the leading replica
writes every pass's outcome once per iteration (`save_pass_outcomes`), and the API reads them
back with a state per pass (`read_pass_status`).

Kept apart from `aida.workflows.scheduler` on purpose, so the API can import the pass names and
the read without importing the scheduler, its Temporal client or its passes.

What is stored is a pass name, three timestamps, a consecutive-failure count and an exception
*class* name. The exception's message is never stored: it can carry a table name, a value or a
URL, and the log line already holds it for whoever may read logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import SchedulerPassStatus

#: Every step of `run_scheduler_iteration` that `_isolated` guards, by the label its failures
#: carry, plus the two steps guarded inline: choosing the due scan policies
#: (`scan_policy_selection`) and admitting each of them (`scan_policy_processing`).
#: `tests/test_scheduler_pass_isolation.py` holds this tuple to the calls the iteration makes.
SCHEDULER_PASS_NAMES: Final = (
    "cancellation_reconcile",
    "priority_rebalance",
    "owner_routing",
    "custom_rule_packs",
    "graph_reconciliation",
    "rollup_rebuild",
    "vector_index_rebuild",
    "model_route_reachability",
    "classification_propagation",
    "freshness_evaluation",
    "change_signal_processing",
    "context_rebuild",
    "footprint_metrics",
    "due_playbooks",
    "task_agent_schedule",
    "value_profile_purge",
    "reaper",
    "certification_expiry_warning",
    "ownership_expiry",
    "principal_reconciliation",
    "review_notification",
    "delivery_worker",
    "entitlement_fulfilment",
    "scan_policy_selection",
    "scan_policy_processing",
)

#: Longest exception class name kept; the column is `String(200)`.
MAX_ERROR_CLASS_CHARS: Final = 200

#: A pass nobody has attempted for this long is STALE: the scheduler is down, standing by on
#: every replica, or stuck inside an earlier pass. Every pass is attempted once per iteration,
#: so the bound is thirty poll intervals, and never under five minutes.
STALE_AFTER_POLLS: Final = 30
STALE_AFTER_MIN_SECONDS: Final = 300

PassState = Literal["OK", "FAILING", "STALE", "NEVER_RUN"]

#: The order `read_pass_status` sorts by: what needs a person first.
_STATE_ORDER: Final[Mapping[str, int]] = {"FAILING": 0, "STALE": 1, "NEVER_RUN": 2, "OK": 3}


async def save_pass_outcomes(
    session: AsyncSession, outcomes: Mapping[str, str | None], now: datetime
) -> None:
    """Record one iteration's outcome per pass, and commit.

    `outcomes` maps a pass name to `None` when the pass returned, or to the class name of the
    exception it raised. One SELECT and one flush for the whole iteration, whatever the number of
    passes. A success resets the failure count but keeps the last failure's time and class, so a
    pass that recovered still shows when it last failed and why.
    """
    if not outcomes:
        return
    existing = {
        row.pass_name: row
        for row in (
            await session.scalars(
                select(SchedulerPassStatus).where(SchedulerPassStatus.pass_name.in_(list(outcomes)))
            )
        ).all()
    }
    for name, error_class in outcomes.items():
        row = existing.get(name)
        if row is None:
            row = SchedulerPassStatus(pass_name=name, last_attempt_at=now, consecutive_failures=0)
            session.add(row)
        row.last_attempt_at = now
        if error_class is None:
            row.last_success_at = now
            row.consecutive_failures = 0
        else:
            row.last_failure_at = now
            row.last_error_class = error_class[:MAX_ERROR_CLASS_CHARS]
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
    await session.commit()


@dataclass(frozen=True, slots=True)
class PassStatus:
    """One pass as the Operations screen shows it."""

    pass_name: str
    state: PassState
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error_class: str | None
    consecutive_failures: int


def stale_after(poll_seconds: int) -> timedelta:
    """How long without an attempt makes a pass STALE, for a scheduler polling this often."""
    return timedelta(seconds=max(STALE_AFTER_MIN_SECONDS, STALE_AFTER_POLLS * poll_seconds))


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for a timezone-aware column; PostgreSQL aware ones."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def read_pass_status(
    session: AsyncSession, *, now: datetime, poll_seconds: int
) -> list[PassStatus]:
    """Every known pass with its state, failing first.

    A pass with no row is NEVER_RUN (a new deployment, or a scheduler that has never led). A row
    older than `stale_after` is STALE whatever it last recorded, because an old OK is not evidence
    the pass still works. A row for a name no longer in `SCHEDULER_PASS_NAMES` (a retired pass) is
    left out rather than shown as stale forever.
    """
    stored = (await session.scalars(select(SchedulerPassStatus))).all()
    rows = {row.pass_name: row for row in stored}
    bound = stale_after(poll_seconds)
    statuses: list[PassStatus] = []
    for name in SCHEDULER_PASS_NAMES:
        row = rows.get(name)
        if row is None:
            statuses.append(PassStatus(name, "NEVER_RUN", None, None, None, None, 0))
            continue
        attempted = _aware(row.last_attempt_at)
        state: PassState
        if attempted is None or now - attempted > bound:
            state = "STALE"
        elif row.consecutive_failures > 0:
            state = "FAILING"
        else:
            state = "OK"
        statuses.append(
            PassStatus(
                pass_name=name,
                state=state,
                last_attempt_at=attempted,
                last_success_at=_aware(row.last_success_at),
                last_failure_at=_aware(row.last_failure_at),
                last_error_class=row.last_error_class,
                consecutive_failures=row.consecutive_failures,
            )
        )
    order = {name: index for index, name in enumerate(SCHEDULER_PASS_NAMES)}
    statuses.sort(key=lambda status: (_STATE_ORDER[status.state], order[status.pass_name]))
    return statuses
