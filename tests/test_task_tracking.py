"""Task-level retry/heartbeat drill-down (module 05, PR-4).

Temporal already tracks attempt counts, heartbeats, and retry backoff inside
the cluster; `aida.task_tracking` is the operator-facing mirror of that state
in Postgres. Three things are worth proving:

* the plain status-transition/history arithmetic (`task_key_for`,
  `next_attempt_status`, `append_retry_entry`, `close_retry_entry`) is
  DB-free and independently correct;
* `TASK_TYPE_MAX_ATTEMPTS` -- the single source of truth this module's
  `next_attempt_status` uses -- has not silently drifted from the actual
  `RetryPolicy.maximum_attempts` configured per activity in
  `aida.workflows.discovery`;
* the DB-facing `start_task`/`heartbeat_task`/`finish_task` round-trip
  produces the row the drill-down API reads, against a real (in-memory
  SQLite) database rather than a hand-rolled fake.
"""

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.task_tracking as task_tracking
from aida.analysis_tasks import (
    TASK_TYPE_DISCOVER_DATASOURCE,
    TASK_TYPE_FINALIZE_PROFILE_TASKS,
    TASK_TYPE_MAX_ATTEMPTS,
    TASK_TYPE_PLAN_PROFILE_TASKS,
    TASK_TYPE_PROFILE_TABLE,
)
from aida.db import Base
from aida.main import app
from aida.models import AnalysisRun, AnalysisTask, DataSource, Organization
from aida.task_tracking import (
    append_retry_entry,
    close_retry_entry,
    finish_task,
    heartbeat_task,
    next_attempt_status,
    start_task,
    task_key_for,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_SOURCE = (
    REPO_ROOT / "src" / "aida" / "workflows" / "discovery.py"
).read_text(encoding="utf-8")

# ---------------------------------------------------------------------------
# Pure functions: identity, status transition, retry-history bookkeeping
# ---------------------------------------------------------------------------


def test_task_key_distinguishes_table_scoped_and_run_scoped_tasks() -> None:
    table_id = uuid4()

    table_scoped = task_key_for(TASK_TYPE_PROFILE_TABLE, table_id)
    run_scoped = task_key_for(TASK_TYPE_DISCOVER_DATASOURCE, None)

    assert table_scoped == f"{TASK_TYPE_PROFILE_TABLE}:{table_id}"
    assert run_scoped == f"{TASK_TYPE_DISCOVER_DATASOURCE}:RUN"
    assert table_scoped != run_scoped


def test_next_attempt_status_retries_until_max_attempts_exhausted() -> None:
    assert next_attempt_status(1, max_attempts=4) == "RETRYING"
    assert next_attempt_status(3, max_attempts=4) == "RETRYING"
    assert next_attempt_status(4, max_attempts=4) == "FAILED"
    # A single-attempt task type has no retries at all.
    assert next_attempt_status(1, max_attempts=1) == "FAILED"


def test_append_retry_entry_adds_a_running_entry() -> None:
    started = datetime(2026, 8, 30, 12, tzinfo=UTC)

    history = append_retry_entry([], attempt=1, started_at=started)

    assert history == [
        {
            "attempt": 1,
            "started_at": started.isoformat(),
            "ended_at": None,
            "outcome": "RUNNING",
            "error_class": None,
            "error_message": None,
        }
    ]


def test_close_retry_entry_updates_only_the_matching_attempt() -> None:
    started = datetime(2026, 8, 30, 12, tzinfo=UTC)
    ended = datetime(2026, 8, 30, 12, 5, tzinfo=UTC)
    history = append_retry_entry([], attempt=1, started_at=started)
    history = append_retry_entry(history, attempt=2, started_at=ended)

    closed = close_retry_entry(
        history,
        attempt=1,
        ended_at=ended,
        outcome="FAILED",
        error_class="TimeoutError",
        error_message="connector timed out",
    )

    assert closed[0]["outcome"] == "FAILED"
    assert closed[0]["error_class"] == "TimeoutError"
    assert closed[0]["ended_at"] == ended.isoformat()
    # The second (still-open) attempt is untouched.
    assert closed[1]["outcome"] == "RUNNING"
    assert closed[1]["ended_at"] is None


def test_close_retry_entry_is_defensive_against_a_missing_attempt() -> None:
    ended = datetime(2026, 8, 30, 12, 5, tzinfo=UTC)

    closed = close_retry_entry(
        [], attempt=1, ended_at=ended, outcome="COMPLETED", error_class=None, error_message=None
    )

    # Evidence is never silently dropped: a close with no matching open entry
    # still produces a record of what happened.
    assert len(closed) == 1
    assert closed[0]["attempt"] == 1
    assert closed[0]["outcome"] == "COMPLETED"


# ---------------------------------------------------------------------------
# TASK_TYPE_MAX_ATTEMPTS must not drift from the Temporal RetryPolicy it mirrors
# ---------------------------------------------------------------------------


def test_analysis_task_max_attempts_matches_the_temporal_retry_policy() -> None:
    """`next_attempt_status` decides RETRYING vs. FAILED from
    `TASK_TYPE_MAX_ATTEMPTS`; if that constant silently drifted from the
    `maximum_attempts=` actually configured on each activity's `RetryPolicy`
    in `aida.workflows.discovery`, the drill-down API would report a task as
    still retrying (or already exhausted) when Temporal disagrees.
    """
    max_attempts_in_source = [
        int(value) for value in re.findall(r"maximum_attempts=(\d+)", DISCOVERY_SOURCE)
    ]

    # discover_datasource, plan_profile_tasks, profile_table_task, finalize_profile_tasks
    assert max_attempts_in_source == [
        TASK_TYPE_MAX_ATTEMPTS[TASK_TYPE_DISCOVER_DATASOURCE],
        TASK_TYPE_MAX_ATTEMPTS[TASK_TYPE_PLAN_PROFILE_TASKS],
        TASK_TYPE_MAX_ATTEMPTS[TASK_TYPE_PROFILE_TABLE],
        TASK_TYPE_MAX_ATTEMPTS[TASK_TYPE_FINALIZE_PROFILE_TASKS],
    ]


# ---------------------------------------------------------------------------
# DB-facing round trip: start -> heartbeat -> finish
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seeded_run(session: AsyncSession) -> AnalysisRun:
    organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(organization)
    await session.flush()
    datasource = DataSource(
        organization_id=organization.id,
        line_of_business_id=uuid4(),
        data_domain_id=uuid4(),
        project_id=uuid4(),
        name="Warehouse",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        credential_reference="vault://x",
    )
    # FK constraints on lob/domain/project are not enforced by this fixture
    # (task_tracking never joins through them); AnalysisRun/AnalysisTask only
    # need a real DataSource/organization row to satisfy their own FKs.
    session.add(datasource)
    await session.flush()
    run = AnalysisRun(
        organization_id=organization.id,
        datasource_id=datasource.id,
        mode="FULL",
        trigger_type="MANUAL",
        status="RUNNING",
    )
    session.add(run)
    await session.flush()
    return run


async def test_start_task_creates_a_running_row_with_attempt_one(
    session: AsyncSession, monkeypatch: object
) -> None:
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    run = await _seeded_run(session)

    attempt = await start_task(
        analysis_run_id=run.id,
        organization_id=run.organization_id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=None,
        max_attempts=4,
    )

    assert attempt == 1
    task = await session.scalar(
        select(AnalysisTask).where(AnalysisTask.analysis_run_id == run.id)
    )
    assert task is not None
    assert task.status == "RUNNING"
    assert task.attempt_count == 1
    assert task.max_attempts == 4
    assert task.started_at is not None
    assert task.retry_history[0]["attempt"] == 1
    assert task.retry_history[0]["outcome"] == "RUNNING"


async def test_heartbeat_task_updates_last_heartbeat_and_detail(
    session: AsyncSession, monkeypatch: object
) -> None:
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    run = await _seeded_run(session)
    await start_task(
        analysis_run_id=run.id,
        organization_id=run.organization_id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=None,
        max_attempts=4,
    )

    await heartbeat_task(
        analysis_run_id=run.id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=None,
        detail={"stage": "profiling", "rows_seen": 5000},
    )

    task = await session.scalar(
        select(AnalysisTask).where(AnalysisTask.analysis_run_id == run.id)
    )
    assert task.heartbeat_detail == {"stage": "profiling", "rows_seen": 5000}
    assert task.last_heartbeat_at is not None


async def test_finish_task_success_marks_completed(
    session: AsyncSession, monkeypatch: object
) -> None:
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    run = await _seeded_run(session)
    await start_task(
        analysis_run_id=run.id,
        organization_id=run.organization_id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=None,
        max_attempts=4,
    )

    await finish_task(
        analysis_run_id=run.id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=None,
        outcome="SUCCESS",
    )

    task = await session.scalar(
        select(AnalysisTask).where(AnalysisTask.analysis_run_id == run.id)
    )
    assert task.status == "COMPLETED"
    assert task.completed_at is not None
    assert task.retry_history[0]["outcome"] == "COMPLETED"


async def test_finish_task_error_retries_then_fails_once_attempts_are_exhausted(
    session: AsyncSession, monkeypatch: object
) -> None:
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    run = await _seeded_run(session)

    # Attempt 1 fails with retries remaining.
    await start_task(
        analysis_run_id=run.id,
        organization_id=run.organization_id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=None,
        max_attempts=2,
    )
    await finish_task(
        analysis_run_id=run.id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=None,
        outcome="ERROR",
        error_class="TimeoutError",
        error_message="connector timed out",
    )
    task = await session.scalar(
        select(AnalysisTask).where(AnalysisTask.analysis_run_id == run.id)
    )
    assert task.status == "RETRYING"
    assert task.completed_at is None
    assert task.error_class == "TimeoutError"

    # Attempt 2 (Temporal's retry) fails too, and this task type has no more
    # attempts left.
    attempt = await start_task(
        analysis_run_id=run.id,
        organization_id=run.organization_id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=None,
        max_attempts=2,
    )
    assert attempt == 2
    await finish_task(
        analysis_run_id=run.id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=None,
        outcome="ERROR",
        error_class="TimeoutError",
        error_message="connector timed out again",
    )

    task = await session.scalar(
        select(AnalysisTask).where(AnalysisTask.analysis_run_id == run.id)
    )
    assert task.status == "FAILED"
    assert task.completed_at is not None
    assert task.attempt_count == 2
    assert len(task.retry_history) == 2
    # Attempt 1 closed as RETRYING (it had another attempt coming); only
    # attempt 2 -- the one that exhausted max_attempts -- closes as FAILED.
    assert task.retry_history[0]["outcome"] == "RETRYING"
    assert task.retry_history[1]["outcome"] == "FAILED"


async def test_table_scoped_tasks_in_the_same_run_are_independent_rows(
    session: AsyncSession, monkeypatch: object
) -> None:
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    run = await _seeded_run(session)
    first_table, second_table = uuid4(), uuid4()

    await start_task(
        analysis_run_id=run.id,
        organization_id=run.organization_id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=first_table,
        max_attempts=4,
    )
    await start_task(
        analysis_run_id=run.id,
        organization_id=run.organization_id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=second_table,
        max_attempts=4,
    )

    rows = (
        await session.scalars(select(AnalysisTask).where(AnalysisTask.analysis_run_id == run.id))
    ).all()
    assert {row.table_id for row in rows} == {first_table, second_table}


async def test_heartbeat_and_finish_are_no_ops_for_an_unknown_task(
    session: AsyncSession, monkeypatch: object
) -> None:
    """A defensive no-op, not a raise: a heartbeat/finish racing a cancelled
    or already-cleared task must never itself crash the activity it is only
    trying to report on."""
    monkeypatch.setattr(task_tracking, "session_factory", lambda: session)
    run = await _seeded_run(session)

    await heartbeat_task(
        analysis_run_id=run.id,
        task_type=TASK_TYPE_PROFILE_TABLE,
        table_id=None,
        detail={"stage": "ghost"},
    )
    await finish_task(
        analysis_run_id=run.id, task_type=TASK_TYPE_PROFILE_TABLE, table_id=None, outcome="SUCCESS"
    )

    rows = (
        await session.scalars(select(AnalysisTask).where(AnalysisTask.analysis_run_id == run.id))
    ).all()
    assert rows == []


# ---------------------------------------------------------------------------
# API contract: the drill-down endpoints are registered
# ---------------------------------------------------------------------------


def test_analysis_task_drill_down_endpoints_are_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/analysis-runs/{run_id}/tasks" in paths
    assert "get" in paths["/v1/analysis-runs/{run_id}/tasks"]
    assert "/v1/analysis-runs/{run_id}/tasks/{task_id}" in paths
    assert "get" in paths["/v1/analysis-runs/{run_id}/tasks/{task_id}"]
