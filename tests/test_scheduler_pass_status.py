"""A failing scheduler pass is visible where an operator looks (R11-VAL04).

Round 12 stopped one failing pass from ending the scheduler: `_isolated` logs it and counts it in
`aida_scheduler_pass_failures_total`. Both live in the scheduler process, and the Operations
screen reads the API, so a pass that failed on every iteration was still visible only to someone
reading logs. The leading replica now persists each pass's outcome once per iteration
(`aida.scheduler_pass_status`), and `GET /v1/operations/scheduler-passes` reads it back.

`tests/test_scheduler_pass_isolation.py` covers what the iteration hands on; this file covers
what is stored, how it is read, and who may read it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from structlog.testing import capture_logs

from aida.config import Settings, get_settings
from aida.db import Base, get_session
from aida.main import app
from aida.models import SchedulerPassStatus
from aida.scheduler_pass_status import (
    MAX_ERROR_CLASS_CHARS,
    SCHEDULER_PASS_NAMES,
    read_pass_status,
    save_pass_outcomes,
    stale_after,
)
from aida.workflows import scheduler

NOW = datetime(2026, 9, 21, 22, 0, tzinfo=UTC)
POLL = 10
REAPER = "reaper"
DELIVERY = "delivery_worker"


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _all_ok() -> dict[str, str | None]:
    return {name: None for name in SCHEDULER_PASS_NAMES}


async def _state(session: AsyncSession, name: str, now: datetime = NOW) -> str:
    statuses = await read_pass_status(session, now=now, poll_seconds=POLL)
    return next(status.state for status in statuses if status.pass_name == name)


# --- what is stored -----------------------------------------------------------------------


async def test_a_new_deployment_shows_every_pass_as_never_run(session: AsyncSession) -> None:
    statuses = await read_pass_status(session, now=NOW, poll_seconds=POLL)

    assert [status.pass_name for status in statuses] == list(SCHEDULER_PASS_NAMES)
    assert {status.state for status in statuses} == {"NEVER_RUN"}


async def test_a_pass_that_raises_is_failing_and_counts_its_consecutive_failures(
    session: AsyncSession,
) -> None:
    await save_pass_outcomes(session, _all_ok() | {"reaper": "OperationalError"}, NOW)
    later = NOW + timedelta(seconds=POLL)
    await save_pass_outcomes(session, _all_ok() | {"reaper": "OperationalError"}, later)

    (reaper,) = [s for s in await read_pass_status(session, now=later, poll_seconds=POLL)
                 if s.pass_name == REAPER]
    assert reaper.state == "FAILING"
    assert reaper.consecutive_failures == 2
    assert reaper.last_error_class == "OperationalError"
    assert reaper.last_failure_at == later
    assert reaper.last_success_at is None
    assert await _state(session, "delivery_worker", later) == "OK"


async def test_a_recovered_pass_is_ok_and_still_shows_when_and_why_it_last_failed(
    session: AsyncSession,
) -> None:
    await save_pass_outcomes(session, {"reaper": "TimeoutError"}, NOW)
    later = NOW + timedelta(seconds=POLL)
    await save_pass_outcomes(session, {"reaper": None}, later)

    (reaper,) = [s for s in await read_pass_status(session, now=later, poll_seconds=POLL)
                 if s.pass_name == REAPER]
    assert (reaper.state, reaper.consecutive_failures) == ("OK", 0)
    assert (reaper.last_failure_at, reaper.last_error_class) == (NOW, "TimeoutError")
    assert reaper.last_success_at == later


async def test_a_pass_nobody_has_attempted_lately_is_stale_whatever_it_last_recorded(
    session: AsyncSession,
) -> None:
    await save_pass_outcomes(session, _all_ok(), NOW)
    bound = stale_after(POLL)

    assert await _state(session, "reaper", NOW + bound) == "OK"
    assert await _state(session, "reaper", NOW + bound + timedelta(seconds=1)) == "STALE"


def test_the_stale_bound_is_thirty_polls_and_never_under_five_minutes() -> None:
    assert stale_after(10) == timedelta(minutes=5)
    assert stale_after(60) == timedelta(minutes=30)


async def test_failing_passes_come_first(session: AsyncSession) -> None:
    await save_pass_outcomes(session, _all_ok() | {"delivery_worker": "ConnectError"}, NOW)

    statuses = await read_pass_status(session, now=NOW, poll_seconds=POLL)
    assert statuses[0].pass_name == DELIVERY
    assert [s.state for s in statuses[1:]] == ["OK"] * (len(SCHEDULER_PASS_NAMES) - 1)


async def test_a_retired_pass_name_is_not_shown(session: AsyncSession) -> None:
    await save_pass_outcomes(session, {"a_pass_since_removed": "RuntimeError"}, NOW)

    names = {s.pass_name for s in await read_pass_status(session, now=NOW, poll_seconds=POLL)}
    assert "a_pass_since_removed" not in names


async def test_only_the_exception_class_is_stored_and_it_is_bounded(session: AsyncSession) -> None:
    await save_pass_outcomes(session, {"reaper": "E" * (MAX_ERROR_CLASS_CHARS + 50)}, NOW)

    row = await session.scalar(select(SchedulerPassStatus))
    assert row is not None
    assert row.last_error_class == "E" * MAX_ERROR_CLASS_CHARS


async def test_one_iteration_is_one_select_whatever_the_number_of_passes(
    session: AsyncSession,
) -> None:
    await save_pass_outcomes(session, _all_ok(), NOW)
    statements: list[str] = []
    from sqlalchemy import event

    sync_engine = session.bind.sync_engine  # type: ignore[union-attr]

    def count(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(sync_engine, "before_cursor_execute", count)
    try:
        await save_pass_outcomes(session, _all_ok() | {"reaper": "X"}, NOW + timedelta(seconds=1))
    finally:
        event.remove(sync_engine, "before_cursor_execute", count)

    assert sum(1 for s in statements if s.lstrip().upper().startswith("SELECT")) == 1


# --- the scheduler's write never ends the loop ------------------------------------------------


async def test_a_database_that_refuses_the_write_is_logged_and_the_iteration_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Down:
        async def __aenter__(self) -> _Down:
            raise ConnectionRefusedError("database down")

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(scheduler, "session_factory", lambda: _Down())
    monkeypatch.setattr(scheduler, "logger", structlog.get_logger(scheduler.__name__))

    with capture_logs() as logs:
        await scheduler._save_pass_outcomes({"reaper": None})

    assert [entry["event"] for entry in logs] == ["scheduler_pass_status_write_failed"]


async def test_an_empty_iteration_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> object:
        raise AssertionError("nothing to write, so no session may be opened")

    monkeypatch.setattr(scheduler, "session_factory", refuse)
    await scheduler._save_pass_outcomes({})


# --- who may read it ------------------------------------------------------------------------


@pytest_asyncio.fixture
async def http(session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """The application on this test's session; overrides restored exactly as found."""
    previous = dict(app.dependency_overrides)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ops.test"
    ) as client:
        yield client
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def _as(roles: str) -> dict[str, str]:
    return {"X-Principal-Id": "ops-person", "X-Principal-Type": "USER", "X-Roles": roles}


@pytest.mark.parametrize("roles", ["PlatformAdmin", "Operations"])
async def test_a_platform_operator_reads_every_pass_failing_first(
    http: httpx.AsyncClient, session: AsyncSession, roles: str
) -> None:
    await save_pass_outcomes(session, _all_ok() | {"reaper": "OperationalError"},
                             datetime.now(UTC))

    response = await http.get("/v1/operations/scheduler-passes", headers=_as(roles))

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["failing"], body["stale"], body["never_run"]) == (1, 0, 0)
    assert body["stale_after_seconds"] == int(stale_after(POLL).total_seconds())
    assert body["items"][0]["pass_name"] == REAPER
    assert body["items"][0]["state"] == "FAILING"
    assert body["items"][0]["last_error_class"] == "OperationalError"
    assert len(body["items"]) == len(SCHEDULER_PASS_NAMES)


@pytest.mark.parametrize(
    "roles", ["OrganizationAdmin", "Auditor", "DataSteward", "Analyst", "Viewer", "Reviewer"]
)
async def test_anyone_else_is_refused(http: httpx.AsyncClient, roles: str) -> None:
    response = await http.get("/v1/operations/scheduler-passes", headers=_as(roles))

    assert response.status_code == 403
