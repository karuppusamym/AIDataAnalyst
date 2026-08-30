"""Per-task evidence for the analysis-run DAG (module 05, PR-4).

Temporal already tracks attempt counts, heartbeats, and retry backoff for
every activity in ``aida.workflows.discovery.DatasourceDiscoveryWorkflow`` —
but only Temporal itself can see it; there is no persisted, queryable record
an operator console can drill into for a stuck or failing run without going
through `temporal` CLI/Temporal Web against the cluster directly. This module
is what ``aida.workflows.activities`` calls at the start, on heartbeat, and at
the end of every task so that history survives in Postgres and is exposed by
the read endpoints in ``aida.api`` (``GET /analysis-runs/{id}/tasks[/…]``).

The DB-facing functions below are intentionally each a short, independent
transaction (mirroring ``_mark_run_cancelled`` in activities.py) so a
heartbeat write can never block on — or be rolled back by — the long-running
work it is reporting on. The status-transition arithmetic itself
(``next_attempt_status``, ``append_retry_entry``, ``close_retry_entry``) is
kept as plain, DB-free functions so it can be unit tested directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select

from aida.db import session_factory
from aida.models import AnalysisTask

TaskOutcome = Literal["SUCCESS", "ERROR", "CANCELLED"]

_TERMINAL_STATUS_FOR_OUTCOME: dict[TaskOutcome, str] = {
    "SUCCESS": "COMPLETED",
    "CANCELLED": "CANCELLED",
}


def task_key_for(task_type: str, table_id: UUID | None) -> str:
    """A stable identity for one task within one analysis run."""
    return f"{task_type}:{table_id}" if table_id is not None else f"{task_type}:RUN"


def next_attempt_status(attempt_count: int, max_attempts: int) -> str:
    """Whether a failed attempt still has retries left, per the same
    ``maximum_attempts`` bound configured on the Temporal ``RetryPolicy``."""
    return "RETRYING" if attempt_count < max_attempts else "FAILED"


def append_retry_entry(
    history: list[dict[str, Any]],
    *,
    attempt: int,
    started_at: datetime,
) -> list[dict[str, Any]]:
    return [
        *history,
        {
            "attempt": attempt,
            "started_at": started_at.isoformat(),
            "ended_at": None,
            "outcome": "RUNNING",
            "error_class": None,
            "error_message": None,
        },
    ]


def close_retry_entry(
    history: list[dict[str, Any]],
    *,
    attempt: int,
    ended_at: datetime,
    outcome: str,
    error_class: str | None,
    error_message: str | None,
) -> list[dict[str, Any]]:
    """Close the retry_history entry for ``attempt``, leaving earlier entries
    untouched. Defensive against a missing entry (appends one) so evidence is
    never silently dropped."""
    updated: list[dict[str, Any]] = []
    found = False
    for entry in history:
        if entry.get("attempt") == attempt:
            found = True
            updated.append(
                {
                    **entry,
                    "ended_at": ended_at.isoformat(),
                    "outcome": outcome,
                    "error_class": error_class,
                    "error_message": error_message,
                }
            )
        else:
            updated.append(entry)
    if not found:
        updated.append(
            {
                "attempt": attempt,
                "started_at": ended_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "outcome": outcome,
                "error_class": error_class,
                "error_message": error_message,
            }
        )
    return updated


async def start_task(
    *,
    analysis_run_id: UUID,
    organization_id: UUID,
    task_type: str,
    table_id: UUID | None,
    max_attempts: int,
) -> int:
    """Record the start of one attempt. Returns the attempt number."""
    task_key = task_key_for(task_type, table_id)
    now = datetime.now(UTC)
    async with session_factory() as session:
        task = await session.scalar(
            select(AnalysisTask).where(
                AnalysisTask.analysis_run_id == analysis_run_id,
                AnalysisTask.task_key == task_key,
            )
        )
        if task is None:
            attempt = 1
            task = AnalysisTask(
                organization_id=organization_id,
                analysis_run_id=analysis_run_id,
                table_id=table_id,
                task_type=task_type,
                task_key=task_key,
                status="RUNNING",
                attempt_count=attempt,
                max_attempts=max_attempts,
                started_at=now,
                last_heartbeat_at=now,
                retry_history=append_retry_entry([], attempt=attempt, started_at=now),
            )
            session.add(task)
        else:
            attempt = task.attempt_count + 1
            task.attempt_count = attempt
            task.max_attempts = max_attempts
            task.status = "RUNNING"
            task.last_heartbeat_at = now
            task.completed_at = None
            task.error_class = None
            task.error_message = None
            task.retry_history = append_retry_entry(
                task.retry_history, attempt=attempt, started_at=now
            )
        await session.commit()
        return attempt


async def heartbeat_task(
    *,
    analysis_run_id: UUID,
    task_type: str,
    table_id: UUID | None,
    detail: dict[str, Any],
) -> None:
    task_key = task_key_for(task_type, table_id)
    now = datetime.now(UTC)
    async with session_factory() as session:
        task = await session.scalar(
            select(AnalysisTask).where(
                AnalysisTask.analysis_run_id == analysis_run_id,
                AnalysisTask.task_key == task_key,
            )
        )
        if task is None:
            return
        task.last_heartbeat_at = now
        task.heartbeat_detail = detail
        await session.commit()


async def finish_task(
    *,
    analysis_run_id: UUID,
    task_type: str,
    table_id: UUID | None,
    outcome: TaskOutcome,
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    task_key = task_key_for(task_type, table_id)
    now = datetime.now(UTC)
    async with session_factory() as session:
        task = await session.scalar(
            select(AnalysisTask).where(
                AnalysisTask.analysis_run_id == analysis_run_id,
                AnalysisTask.task_key == task_key,
            )
        )
        if task is None:
            return
        if outcome == "ERROR":
            status = next_attempt_status(task.attempt_count, task.max_attempts)
            retry_outcome = "FAILED" if status == "FAILED" else "RETRYING"
        else:
            status = _TERMINAL_STATUS_FOR_OUTCOME[outcome]
            retry_outcome = status
        task.status = status
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            task.completed_at = now
        if outcome == "ERROR":
            task.error_class = error_class
            task.error_message = error_message
        task.retry_history = close_retry_entry(
            task.retry_history,
            attempt=task.attempt_count,
            ended_at=now,
            outcome=retry_outcome,
            error_class=error_class if outcome == "ERROR" else None,
            error_message=error_message if outcome == "ERROR" else None,
        )
        await session.commit()
