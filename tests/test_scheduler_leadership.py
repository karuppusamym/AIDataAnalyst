"""R11-AUD04: only the fleet-scheduler replica that holds the leadership lock runs passes.

`run_scheduler` was a bare loop, so a second replica ran every periodic pass again. These tests
put the loop on a fake lock that several replicas share -- the same contention PostgreSQL's
`pg_try_advisory_lock` provides -- and check the behaviour the design promises: one leader and
the rest standing by, no pass started without a verified lock, an in-flight pass allowed to
finish, a standby taking over, and the lock released on the way out.

The lock is fake on purpose and the fake models what matters about the real one: each connection
is a *session*, the server grants the lock to one session, and a session that dies without the
server noticing (a partition) keeps the lock until the server does. What is real here is
`run_scheduler` and `SchedulerLeadership`; what is faked is the database, Temporal and the sleeps
between ticks. `PostgresAdvisoryLockProvider` is exercised against a fake engine, which checks
its control flow (timeouts, discarding a dead connection, never stacking a second hold) but not
the SQL. The one test that runs the SQL against a real PostgreSQL is at the bottom, and it
skips unless `AIDA_SCHEDULER_LEADER_TEST_DATABASE_URL` names a scratch database -- check for the
`s` before reading a green run as proof of it.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import structlog
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from structlog.testing import capture_logs

from aida import scheduler_leadership
from aida.scheduler_leadership import (
    LOCK_CALL_TIMEOUT_SECONDS,
    SCHEDULER_LEADER_LOCK_KEY,
    STANDBY_RETRY_SECONDS,
    PostgresAdvisoryLockProvider,
    SchedulerLeadership,
    SoleLeaderLockProvider,
    default_lock_provider,
)
from aida.workflows import scheduler
from atlas.platform.config import Settings

_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
def _fresh_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test a module logger with no history, so `capture_logs` can see it.

    `capture_logs` swaps the processors on the *current* structlog configuration. The
    application's logging setup (`atlas.platform.logging`) turns on `cache_logger_on_first_use`,
    so a module-level logger that an earlier test in the same process already drove keeps the
    processor list it was first bound to, and a later `configure_logging` (importing
    `aida.main`, or any test that calls it) replaces that list without the logger noticing:
    its events then go to stdout and never reach the capture. The same defect failed nine
    tests of the drafter supervisor in the whole suite. A logger created for the test binds
    on its first call, inside the capture."""
    monkeypatch.setattr(
        scheduler_leadership, "_log", structlog.get_logger(scheduler_leadership.__name__)
    )


@pytest.fixture(autouse=True)
def _fresh_process_gauge() -> None:
    """Start every test as a replica that has just started: the gauge at its import value.

    The gauge is process-wide and a real process runs one election, so a real standby never
    inherits a `1`. Tests run many elections in one process, and an earlier one that ended
    leading (most do not release) would otherwise be read as this one's history."""
    scheduler_leadership.SCHEDULER_IS_LEADER.set(0)


_LIFECYCLE = {
    "scheduler_standby",
    "scheduler_became_leader",
    "scheduler_lost_leadership",
    "scheduler_leadership_released",
}


def _events(logs: list[dict[str, Any]], names: set[str] = _LIFECYCLE) -> list[str]:
    return [entry["event"] for entry in logs if entry["event"] in names]


# --- a lock several replicas contend for ------------------------------------------------------


class SharedFakeLock:
    """The one advisory lock, as the server sees it."""

    def __init__(self) -> None:
        #: The session holding the lock, or None.
        self.holder: int | None = None
        #: Sessions their client lost but the server has not noticed -- a partition. They keep
        #: the lock until `server_notices_dead_sessions`, as a real one does until TCP gives up.
        self.dead: set[int] = set()
        self._sessions = 0

    def open_session(self) -> int:
        self._sessions += 1
        return self._sessions

    def server_notices_dead_sessions(self) -> None:
        if self.holder in self.dead:
            self.holder = None
        self.dead.clear()


class FakeLockProvider:
    """One replica's handle: a `LeaderLockProvider` over `SharedFakeLock`."""

    def __init__(self, lock: SharedFakeLock, name: str) -> None:
        self.lock = lock
        self.name = name
        #: The session this handle holds the lock with, if it believes it does.
        self.session: int | None = None
        self.calls: list[str] = []
        #: Calls that raise instead of answering, to check the loop fails closed.
        self.raises: set[str] = set()

    def _maybe_raise(self, call: str) -> None:
        if call in self.raises:
            raise ConnectionError(f"{call} failed")

    async def try_acquire(self) -> bool:
        self.calls.append("try_acquire")
        self._maybe_raise("try_acquire")
        session = self.lock.open_session()
        if self.lock.holder is None:
            self.lock.holder = session
            self.session = session
            return True
        return False

    async def still_held(self) -> bool:
        self.calls.append("still_held")
        self._maybe_raise("still_held")
        if self.session is None:
            return False
        alive = self.lock.holder == self.session and self.session not in self.lock.dead
        if not alive:
            self.session = None  # discarded, as the real provider discards a dead connection
        return alive

    async def release(self) -> None:
        self.calls.append("release")
        if (
            self.session is not None
            and self.lock.holder == self.session
            and self.session not in self.lock.dead
        ):
            self.lock.holder = None
        self.session = None

    def sever(self) -> None:
        """The connection dies and the server has not noticed: the lock stays taken."""
        assert self.session is not None
        self.lock.dead.add(self.session)

    def terminate(self) -> None:
        """An administrator terminates the backend: the server frees the lock at once."""
        assert self.session is not None
        if self.lock.holder == self.session:
            self.lock.holder = None


# --- SchedulerLeadership, one tick at a time --------------------------------------------------


async def test_the_first_replica_to_ask_becomes_leader_and_says_so_once() -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    leadership = SchedulerLeadership(provider, replica="a")

    with capture_logs() as logs:
        assert await leadership.confirm() is True
        assert await leadership.confirm() is True
        assert await leadership.confirm() is True

    assert leadership.is_leader
    assert _events(logs) == ["scheduler_became_leader"]
    # Acquired once, then re-verified on every later tick -- never asked for again.
    assert provider.calls == ["try_acquire", "still_held", "still_held"]


async def test_a_standby_does_not_lead_retries_and_logs_standby_once() -> None:
    lock = SharedFakeLock()
    leader = SchedulerLeadership(FakeLockProvider(lock, "a"), replica="a")
    standby_provider = FakeLockProvider(lock, "b")
    standby = SchedulerLeadership(standby_provider, replica="b")
    assert await leader.confirm() is True

    with capture_logs() as logs:
        assert [await standby.confirm() for _ in range(4)] == [False] * 4

    assert not standby.is_leader
    assert _events(logs) == ["scheduler_standby"]  # once, not once per retry
    assert standby_provider.calls == ["try_acquire"] * 4


async def test_only_one_of_two_replicas_on_one_lock_is_leader() -> None:
    lock = SharedFakeLock()
    a = SchedulerLeadership(FakeLockProvider(lock, "a"), replica="a")
    b = SchedulerLeadership(FakeLockProvider(lock, "b"), replica="b")

    results = [await a.confirm(), await b.confirm(), await a.confirm(), await b.confirm()]

    assert results == [True, False, True, False]
    assert (a.is_leader, b.is_leader) == (True, False)


async def test_a_standby_takes_over_when_the_leader_releases() -> None:
    lock = SharedFakeLock()
    a = SchedulerLeadership(FakeLockProvider(lock, "a"), replica="a")
    b = SchedulerLeadership(FakeLockProvider(lock, "b"), replica="b")
    assert await a.confirm() is True
    assert await b.confirm() is False

    with capture_logs() as logs:
        await a.release()
        assert await b.confirm() is True

    assert not a.is_leader
    assert _events(logs) == ["scheduler_leadership_released", "scheduler_became_leader"]


async def test_a_severed_lock_connection_loses_leadership_until_the_server_notices() -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    leadership = SchedulerLeadership(provider, replica="a")
    assert await leadership.confirm() is True

    with capture_logs() as logs:
        provider.sever()
        # The liveness check fails, so this replica is not the leader -- and the retry in the
        # same tick is refused, because the server still thinks the dead session holds the lock.
        assert await leadership.confirm() is False
        assert await leadership.confirm() is False
        # PostgreSQL finally notices the dead session. Regained leadership resumes.
        lock.server_notices_dead_sessions()
        assert await leadership.confirm() is True

    assert _events(logs) == [
        "scheduler_lost_leadership",
        "scheduler_standby",
        "scheduler_became_leader",
    ]


async def test_a_lost_leader_takes_the_lock_straight_back_when_the_server_has_freed_it() -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    leadership = SchedulerLeadership(provider, replica="a")
    assert await leadership.confirm() is True

    with capture_logs() as logs:
        provider.terminate()
        assert await leadership.confirm() is True  # lost, and regained, inside one tick

    assert _events(logs) == ["scheduler_lost_leadership", "scheduler_became_leader"]


async def test_an_error_while_verifying_the_lock_means_not_leader() -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    leadership = SchedulerLeadership(provider, replica="a")
    assert await leadership.confirm() is True

    provider.raises = {"still_held"}
    with capture_logs() as logs:
        assert await leadership.confirm() is False

    assert not leadership.is_leader
    assert _events(logs, _LIFECYCLE | {"scheduler_leader_lock_error"}) == [
        "scheduler_leader_lock_error",
        "scheduler_lost_leadership",
        "scheduler_standby",
    ]


async def test_an_error_while_acquiring_the_lock_means_standby() -> None:
    provider = FakeLockProvider(SharedFakeLock(), "a")
    provider.raises = {"try_acquire"}
    leadership = SchedulerLeadership(provider, replica="a")

    with capture_logs() as logs:
        assert await leadership.confirm() is False
        assert await leadership.confirm() is False

    assert not leadership.is_leader
    errors = [entry for entry in logs if entry["event"] == "scheduler_leader_lock_error"]
    assert [entry["phase"] for entry in errors] == ["acquire", "acquire"]
    assert _events(logs) == ["scheduler_standby"]


@pytest.mark.parametrize("answer", [None, 1, "t", 0, ""])
async def test_only_a_true_answer_makes_a_leader(answer: object) -> None:
    class Sloppy:
        async def try_acquire(self) -> bool:
            return answer  # type: ignore[return-value]

        async def still_held(self) -> bool:
            return answer  # type: ignore[return-value]

        async def release(self) -> None:
            return None

    assert await SchedulerLeadership(Sloppy(), replica="x").confirm() is False


async def test_release_is_idempotent_and_survives_a_failing_provider() -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    leadership = SchedulerLeadership(provider, replica="a")
    assert await leadership.confirm() is True

    await leadership.release()
    assert lock.holder is None
    assert not leadership.is_leader
    await leadership.release()  # second call: nothing held, nothing raised

    class Broken:
        async def try_acquire(self) -> bool:
            return True

        async def still_held(self) -> bool:
            return True

        async def release(self) -> None:
            raise ConnectionError("gone")

    broken = SchedulerLeadership(Broken(), replica="b")
    assert await broken.confirm() is True
    await broken.release()  # must not raise: shutdown has to finish
    assert not broken.is_leader


# --- what a scrape reads: the gauge and the transition counter --------------------------------
#
# The registry is per process and the series are process-wide, so one `SchedulerLeadership` per
# test decides them; the counters are read as before/after deltas because every earlier test in
# the process has already moved them.


def _sample(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels or None)


def _leader_gauge() -> float | None:
    return _sample("aida_scheduler_is_leader")


def _transition_counts() -> tuple[float, float]:
    """(acquired, lost) so far, as a scrape would read them."""
    acquired = _sample("aida_scheduler_leadership_transitions_total", transition="acquired")
    lost = _sample("aida_scheduler_leadership_transitions_total", transition="lost")
    assert acquired is not None and lost is not None, "a transition series is missing"
    return acquired, lost


def _moved_since(before: tuple[float, float]) -> tuple[float, float]:
    acquired, lost = _transition_counts()
    return acquired - before[0], lost - before[1]


async def test_the_gauge_is_one_while_this_replica_leads_and_zero_after_it_releases() -> None:
    before = _transition_counts()
    leadership = SchedulerLeadership(FakeLockProvider(SharedFakeLock(), "a"), replica="a")

    assert await leadership.confirm() is True
    assert _leader_gauge() == 1
    assert await leadership.confirm() is True  # re-verified on the next tick, still leading
    assert _leader_gauge() == 1

    await leadership.release()
    assert _leader_gauge() == 0
    # One acquisition. A clean release is not a loss: the process is going away with its counters.
    assert _moved_since(before) == (1, 0)


async def test_a_standby_reads_zero_however_often_it_retries_and_counts_no_transition() -> None:
    before = _transition_counts()
    lock = SharedFakeLock()
    lock.holder = lock.open_session()  # some other replica leads
    standby = SchedulerLeadership(FakeLockProvider(lock, "b"), replica="b")

    for _ in range(3):
        assert await standby.confirm() is False
        assert _leader_gauge() == 0

    assert _moved_since(before) == (0, 0)  # refused attempts are not transitions


async def test_a_lost_leadership_drops_the_gauge_and_counts_one_loss_not_one_per_tick() -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    leadership = SchedulerLeadership(provider, replica="a")
    assert await leadership.confirm() is True
    before = _transition_counts()

    provider.sever()
    assert await leadership.confirm() is False
    assert _leader_gauge() == 0
    assert _moved_since(before) == (0, 1)
    assert await leadership.confirm() is False  # still standing by: not a second loss
    assert _moved_since(before) == (0, 1)

    lock.server_notices_dead_sessions()  # PostgreSQL drops the dead session; the retry succeeds
    assert await leadership.confirm() is True
    assert _leader_gauge() == 1
    assert _moved_since(before) == (1, 1)


async def test_a_loss_regained_within_one_tick_is_counted_both_ways_and_ends_at_one() -> None:
    provider = FakeLockProvider(SharedFakeLock(), "a")
    leadership = SchedulerLeadership(provider, replica="a")
    assert await leadership.confirm() is True
    before = _transition_counts()

    provider.terminate()  # the server frees the lock at once, so the retry in the tick wins it
    assert await leadership.confirm() is True

    assert _leader_gauge() == 1
    assert _moved_since(before) == (1, 1)  # flapping shows as losses even when the gap is a tick


async def test_an_error_while_verifying_is_a_loss_and_an_error_while_acquiring_is_not() -> None:
    provider = FakeLockProvider(SharedFakeLock(), "a")
    leadership = SchedulerLeadership(provider, replica="a")
    assert await leadership.confirm() is True
    before = _transition_counts()

    provider.raises = {"still_held"}  # a leader that cannot verify is no leader
    assert await leadership.confirm() is False
    assert _leader_gauge() == 0
    assert _moved_since(before) == (0, 1)

    provider.raises = {"try_acquire"}  # a standby that cannot ask has lost nothing
    assert await leadership.confirm() is False
    assert _leader_gauge() == 0
    assert _moved_since(before) == (0, 1)


def _run_in_a_fresh_interpreter(code: str) -> list[str]:
    """What a process that has only imported the module publishes, without this process's
    history: every earlier test here has already created series."""
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    completed = subprocess.run(  # noqa: S603 -- our own interpreter, a fixed probe from this file
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.split()


def test_both_transition_series_exist_from_import_and_the_gauge_starts_at_zero() -> None:
    """A series that only appears with its first event makes `increase()` skip that event, and
    an election with no history looks like a scrape that failed."""
    values = _run_in_a_fresh_interpreter(
        "from prometheus_client import REGISTRY\n"
        "import aida.scheduler_leadership\n"
        "name = 'aida_scheduler_leadership_transitions_total'\n"
        "print(REGISTRY.get_sample_value(name, {'transition': 'acquired'}))\n"
        "print(REGISTRY.get_sample_value(name, {'transition': 'lost'}))\n"
        "print(REGISTRY.get_sample_value('aida_scheduler_is_leader'))\n"
    )

    assert values == ["0.0", "0.0", "0.0"]


# --- run_scheduler on the fake lock -----------------------------------------------------------

_REPLICA: contextvars.ContextVar[str] = contextvars.ContextVar("replica")


class _FakeTemporalClient:
    @staticmethod
    async def connect(*_args: object, **_kwargs: object) -> object:
        return object()


async def _fast_sleep(_delay: float, result: object = None) -> object:
    # Not zero: two replicas must actually take turns, and a standby's retry must not spin.
    await _real_sleep(0.001)
    return result


class Harness:
    """Everything `run_scheduler` reaches for besides the lock, replaced."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []
        #: Runs inside an iteration, after it is recorded as started. Set by the test.
        self.during: Callable[[str], Awaitable[None]] | None = None
        self.raise_in_pass = False
        self.tasks: list[asyncio.Task[None]] = []

    async def iteration(self, _client: object, _settings: object) -> int:
        name = _REPLICA.get()
        self.started.append(name)
        if self.raise_in_pass:
            raise RuntimeError("a pass blew up")
        if self.during is not None:
            await self.during(name)
        await asyncio.sleep(0)
        self.finished.append(name)
        return 0

    def start(self, name: str, provider: object | None = None) -> asyncio.Task[None]:
        token = _REPLICA.set(name)  # the new task copies the context, so it carries the name
        try:
            task = asyncio.get_running_loop().create_task(
                scheduler.run_scheduler(provider)  # type: ignore[arg-type]
            )
        finally:
            _REPLICA.reset(token)
        self.tasks.append(task)
        return task

    async def wait_until(self, predicate: Callable[[], bool], seconds: float = 5.0) -> None:
        async with asyncio.timeout(seconds):
            while not predicate():
                await _real_sleep(0.001)

    async def stop_all(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    harness = Harness()
    settings = Settings(
        scheduler_poll_seconds=1, database_url="sqlite+aiosqlite://", _env_file=None
    )
    monkeypatch.setattr(scheduler, "get_settings", lambda: settings)
    monkeypatch.setattr(scheduler, "configure_logging", lambda _level: None)
    monkeypatch.setattr(scheduler, "serve_worker_metrics", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "Client", _FakeTemporalClient)
    monkeypatch.setattr(scheduler, "run_scheduler_iteration", harness.iteration)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    return harness


async def test_two_replicas_on_one_lock_run_exactly_one_scheduler(harness: Harness) -> None:
    lock = SharedFakeLock()
    a, b = FakeLockProvider(lock, "a"), FakeLockProvider(lock, "b")
    harness.start("a", a)
    harness.start("b", b)

    await harness.wait_until(
        lambda: len(harness.started) >= 5
        and a.calls.count("try_acquire") + b.calls.count("try_acquire") >= 5
    )
    await harness.stop_all()

    assert len(set(harness.started)) == 1  # one replica ran every pass; the other, none
    leader, standby = (a, b) if set(harness.started) == {"a"} else (b, a)
    assert standby.calls.count("try_acquire") >= 3  # ... and kept retrying the whole time
    assert "still_held" not in standby.calls
    assert leader.calls.count("try_acquire") == 1  # the leader acquired once and only verified
    # Both released on the way out; the lock is free for whoever runs next.
    assert lock.holder is None
    assert "release" in a.calls and "release" in b.calls


async def test_the_standby_takes_over_when_the_leader_shuts_down(harness: Harness) -> None:
    lock = SharedFakeLock()
    tasks = {
        "a": harness.start("a", FakeLockProvider(lock, "a")),
        "b": harness.start("b", FakeLockProvider(lock, "b")),
    }
    await harness.wait_until(lambda: len(harness.started) >= 2)
    leader = harness.started[0]
    standby = "b" if leader == "a" else "a"

    tasks[leader].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[leader]
    await harness.wait_until(lambda: standby in harness.started)
    await harness.stop_all()

    assert harness.started.count(standby) >= 1


async def test_loss_of_leadership_stops_the_passes_and_regaining_it_resumes_them(
    harness: Harness,
) -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    with capture_logs() as logs:
        harness.start("a", provider)
        await harness.wait_until(lambda: len(harness.started) >= 3)

        provider.sever()  # the lock connection dies; the server has not noticed
        await harness.wait_until(lambda: provider.calls.count("try_acquire") >= 3)
        # Two refusals in: any pass that was already under way when the connection died has
        # finished, and the checks that followed it each refused to start another.
        frozen = list(harness.started)
        await harness.wait_until(lambda: provider.calls.count("try_acquire") >= 8)
        assert harness.started == frozen

        lock.server_notices_dead_sessions()  # PostgreSQL notices; the standby retry succeeds
        await harness.wait_until(lambda: len(harness.started) > len(frozen))
        await harness.stop_all()

    assert _events(logs) == [
        "scheduler_became_leader",
        "scheduler_lost_leadership",
        "scheduler_standby",
        "scheduler_became_leader",
        "scheduler_leadership_released",
    ]


async def test_a_pass_in_flight_when_leadership_is_lost_finishes_and_no_next_pass_starts(
    harness: Harness,
) -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    gate = asyncio.Event()

    async def hold_the_second_pass_open(_name: str) -> None:
        if len(harness.started) == 2:
            provider.sever()  # the connection dies in the middle of a pass
            await gate.wait()

    harness.during = hold_the_second_pass_open
    harness.start("a", provider)
    await harness.wait_until(lambda: len(harness.started) == 2)
    await _real_sleep(0.02)
    assert harness.finished == ["a"]  # the second pass is still running: it was not interrupted

    gate.set()
    await harness.wait_until(lambda: len(harness.finished) == 2)  # ... and it completed
    await harness.wait_until(lambda: provider.calls.count("try_acquire") >= 4)
    await harness.stop_all()

    assert len(harness.started) == 2  # the check that follows it refused to start a third


async def test_cancelling_the_scheduler_releases_the_lock(harness: Harness) -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    task = harness.start("a", provider)
    await harness.wait_until(lambda: len(harness.started) >= 1)
    assert lock.holder is not None

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lock.holder is None
    assert provider.calls[-1] == "release"


async def test_the_running_scheduler_reads_one_during_every_pass_and_zero_once_it_has_stopped(
    harness: Harness,
) -> None:
    """The gauge through `run_scheduler` itself, not only through `confirm()`: a scrape during a
    pass must see a leader, and one after the loop's `finally` must not."""
    seen: list[float | None] = []

    async def read_the_gauge(_name: str) -> None:
        seen.append(_leader_gauge())

    harness.during = read_the_gauge
    harness.start("a", FakeLockProvider(SharedFakeLock(), "a"))
    await harness.wait_until(lambda: len(seen) >= 3)
    await harness.stop_all()

    assert seen[:3] == [1, 1, 1]
    assert _leader_gauge() == 0  # `run_scheduler`'s `finally` released the lock


async def test_an_exception_out_of_a_pass_releases_the_lock(harness: Harness) -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    harness.raise_in_pass = True
    task = harness.start("a", provider)

    with pytest.raises(RuntimeError, match="a pass blew up"):
        await task

    assert lock.holder is None  # a crashed leader does not hold the standby off
    assert provider.calls[-1] == "release"


async def test_a_replica_that_cannot_verify_a_lock_runs_no_pass(harness: Harness) -> None:
    lock = SharedFakeLock()
    provider = FakeLockProvider(lock, "a")
    provider.raises = {"try_acquire", "still_held"}
    harness.start("a", provider)

    await harness.wait_until(lambda: provider.calls.count("try_acquire") >= 5)
    await harness.stop_all()

    assert harness.started == []  # fail closed: no lock, no passes -- and it kept trying


async def test_on_sqlite_the_replica_is_the_sole_leader(harness: Harness) -> None:
    # No provider passed: `run_scheduler` builds the default one from `settings.database_url`,
    # which the fixture points at SQLite.
    with capture_logs() as logs:
        harness.start("only")
        await harness.wait_until(lambda: len(harness.started) >= 3)
        await harness.stop_all()

    assert set(harness.started) == {"only"}
    assert "scheduler_leadership_not_enforced" in [entry["event"] for entry in logs]


def test_the_default_provider_is_chosen_by_dialect() -> None:
    assert isinstance(default_lock_provider("sqlite+aiosqlite://"), SoleLeaderLockProvider)
    assert isinstance(
        default_lock_provider("postgresql+asyncpg://user:pw@localhost:5432/db"),
        PostgresAdvisoryLockProvider,
    )  # constructing it opens nothing: the connection is made on the first `try_acquire`


async def test_the_sole_leader_provider_is_always_the_leader() -> None:
    provider = SoleLeaderLockProvider()

    assert await provider.try_acquire() is True
    assert await provider.still_held() is True
    await provider.release()
    assert await provider.still_held() is True


def test_the_lock_key_is_a_documented_positive_bigint() -> None:
    assert SCHEDULER_LEADER_LOCK_KEY == int.from_bytes(b"atlasfsc", "big")
    assert 0 < SCHEDULER_LEADER_LOCK_KEY < 2**63
    # The halves the module docstring gives for reading `pg_locks`.
    assert SCHEDULER_LEADER_LOCK_KEY >> 32 == 1635019873
    assert SCHEDULER_LEADER_LOCK_KEY & 0xFFFFFFFF == 1936094051
    assert "1635019873" in (scheduler_leadership.__doc__ or "")
    assert "1936094051" in (scheduler_leadership.__doc__ or "")


def test_the_timings_are_short_enough_to_mean_something() -> None:
    assert 0 < STANDBY_RETRY_SECONDS <= 10
    assert 0 < LOCK_CALL_TIMEOUT_SECONDS <= 10


# --- PostgresAdvisoryLockProvider against a fake engine ---------------------------------------


class FakeConnection:
    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        #: Every statement and the parameters it ran with, in order.
        self.executed: list[tuple[str, dict[str, Any] | None]] = []
        #: What was done to end the connection, in order: "close" and/or "invalidate".
        self.ended: list[str] = []
        self.fail_execute: Exception | None = None
        self.hang_execute = False

    async def _run(self, statement: object, params: dict[str, Any] | None) -> None:
        self.executed.append((str(statement), params))
        if self.hang_execute:
            await asyncio.Event().wait()
        if self.fail_execute is not None:
            raise self.fail_execute

    async def scalar(self, statement: object, params: dict[str, Any] | None = None) -> object:
        await self._run(statement, params)
        return self.engine.lock_answer

    async def execute(self, statement: object, params: dict[str, Any] | None = None) -> None:
        await self._run(statement, params)

    async def close(self) -> None:
        self.ended.append("close")

    async def invalidate(self) -> None:
        self.ended.append("invalidate")


class FakeEngine:
    def __init__(self) -> None:
        self.connections: list[FakeConnection] = []
        self.lock_answer: object = True
        self.connect_error: Exception | None = None
        self.hang_connect = False
        self.hang_execute = False
        self.disposed = False

    async def connect(self) -> FakeConnection:
        if self.connect_error is not None:
            raise self.connect_error
        if self.hang_connect:
            await asyncio.Event().wait()
        connection = FakeConnection(self)
        connection.hang_execute = self.hang_execute
        self.connections.append(connection)
        return connection

    async def dispose(self) -> None:
        self.disposed = True


class EngineFactory:
    """Stands in for `create_async_engine` and remembers how it was called."""

    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> FakeEngine:
        self.calls.append((args, kwargs))
        return self.engine


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> EngineFactory:
    factory = EngineFactory(FakeEngine())
    monkeypatch.setattr(scheduler_leadership, "create_async_engine", factory)
    return factory


_URL = "postgresql+asyncpg://user:pw@localhost:5432/db"


def _provider(**kwargs: Any) -> PostgresAdvisoryLockProvider:
    return PostgresAdvisoryLockProvider(_URL, call_timeout_seconds=0.05, **kwargs)


async def test_the_lock_engine_is_private_unpooled_autocommit_and_hides_parameters(
    fake_engine: EngineFactory,
) -> None:
    await _provider().try_acquire()

    ((args, kwargs),) = fake_engine.calls
    assert args == (_URL,)
    assert kwargs["poolclass"] is NullPool  # closing the connection must end the session
    assert kwargs["isolation_level"] == "AUTOCOMMIT"  # never `idle in transaction`
    assert kwargs["hide_parameters"] is True  # INV-6


async def test_the_real_engine_accepts_those_arguments() -> None:
    # No connection is made: this checks SQLAlchemy takes the arguments for the asyncpg dialect.
    provider = PostgresAdvisoryLockProvider(_URL)
    engine = provider._lock_engine()

    assert engine.dialect.name == "postgresql"
    assert isinstance(engine.pool, NullPool)
    await provider.release()  # disposes it


async def test_a_granted_lock_is_held_on_one_connection_and_never_stacked(
    fake_engine: EngineFactory,
) -> None:
    provider = _provider(key=42)

    assert await provider.try_acquire() is True
    assert await provider.try_acquire() is True  # asked again: verified, not re-acquired
    assert await provider.still_held() is True

    (connection,) = fake_engine.engine.connections
    statements = [(sql, params) for sql, params in connection.executed]
    assert "pg_try_advisory_lock" in statements[0][0]
    assert statements[0][1] == {"key": 42}
    assert sum("pg_try_advisory_lock" in sql for sql, _ in statements) == 1
    assert [sql for sql, _ in statements[1:]] == ["SELECT 1", "SELECT 1"]
    assert connection.ended == []  # still open: that is the lock


async def test_a_refused_lock_closes_its_connection_and_holds_nothing(
    fake_engine: EngineFactory,
) -> None:
    fake_engine.engine.lock_answer = False
    provider = _provider()

    assert await provider.try_acquire() is False
    assert await provider.still_held() is False

    (connection,) = fake_engine.engine.connections
    assert connection.ended == ["close"]  # answered cleanly: closed, not force-terminated
    fake_engine.engine.lock_answer = True
    assert await provider.try_acquire() is True  # a later attempt opens a fresh connection
    assert len(fake_engine.engine.connections) == 2


async def test_an_unexpected_answer_is_not_a_grant(fake_engine: EngineFactory) -> None:
    fake_engine.engine.lock_answer = None
    provider = _provider()

    assert await provider.try_acquire() is False

    (connection,) = fake_engine.engine.connections
    assert connection.ended == ["invalidate", "close"]  # we cannot tell what state it is in


async def test_a_failed_connect_is_a_refusal_and_is_logged(fake_engine: EngineFactory) -> None:
    fake_engine.engine.connect_error = OSError("database unreachable")

    with capture_logs() as logs:
        assert await _provider().try_acquire() is False

    (entry,) = [e for e in logs if e["event"] == "scheduler_leader_lock_error"]
    assert (entry["phase"], entry["error_class"]) == ("acquire", "OSError")
    assert fake_engine.engine.connections == []


async def test_a_hung_connect_times_out_instead_of_waiting_forever(
    fake_engine: EngineFactory,
) -> None:
    fake_engine.engine.hang_connect = True

    async with asyncio.timeout(2):
        assert await _provider().try_acquire() is False


async def test_a_hung_lock_query_times_out_and_the_connection_is_terminated(
    fake_engine: EngineFactory,
) -> None:
    fake_engine.engine.hang_execute = True

    async with asyncio.timeout(2):
        assert await _provider().try_acquire() is False

    (connection,) = fake_engine.engine.connections
    assert connection.ended == ["invalidate", "close"]  # if the grant arrived, this drops it


async def test_a_failed_liveness_check_loses_the_lock_and_terminates_the_connection(
    fake_engine: EngineFactory,
) -> None:
    provider = _provider()
    assert await provider.try_acquire() is True
    (connection,) = fake_engine.engine.connections
    connection.fail_execute = ConnectionResetError("connection was closed")

    with capture_logs() as logs:
        assert await provider.still_held() is False

    assert connection.ended == ["invalidate", "close"]
    assert [e["phase"] for e in logs if e["event"] == "scheduler_leader_lock_error"] == ["liveness"]
    assert await provider.still_held() is False  # nothing is remembered as held
    assert await provider.try_acquire() is True  # a new attempt opens a new connection
    assert len(fake_engine.engine.connections) == 2


async def test_a_hung_liveness_check_times_out_and_loses_the_lock(
    fake_engine: EngineFactory,
) -> None:
    provider = _provider()
    assert await provider.try_acquire() is True
    (connection,) = fake_engine.engine.connections
    connection.hang_execute = True

    async with asyncio.timeout(2):
        assert await provider.still_held() is False

    assert connection.ended == ["invalidate", "close"]


async def test_release_unlocks_then_closes_then_disposes_and_is_idempotent(
    fake_engine: EngineFactory,
) -> None:
    provider = _provider(key=7)
    assert await provider.try_acquire() is True
    (connection,) = fake_engine.engine.connections

    await provider.release()

    assert "pg_advisory_unlock" in connection.executed[-1][0]
    assert connection.executed[-1][1] == {"key": 7}
    assert connection.ended == ["close"]
    assert fake_engine.engine.disposed
    assert await provider.still_held() is False
    await provider.release()  # nothing left to release, and nothing raised
    assert connection.ended == ["close"]


async def test_release_still_ends_the_session_when_the_unlock_fails(
    fake_engine: EngineFactory,
) -> None:
    provider = _provider()
    assert await provider.try_acquire() is True
    (connection,) = fake_engine.engine.connections
    connection.fail_execute = ConnectionResetError("gone")

    await provider.release()  # must not raise

    assert connection.ended == ["invalidate", "close"]  # ending the session drops the lock
    assert fake_engine.engine.disposed


# --- the SQL, against a real PostgreSQL (skipped unless a scratch database is named) -----------

_PG_URL = os.environ.get("AIDA_SCHEDULER_LEADER_TEST_DATABASE_URL")


@pytest.mark.skipif(
    not _PG_URL,
    reason=(
        "set AIDA_SCHEDULER_LEADER_TEST_DATABASE_URL to a scratch PostgreSQL database to run "
        "the advisory-lock SQL for real; the fake-engine tests above cover only the control flow"
    ),
)
async def test_real_postgres_grants_the_lock_to_one_session_and_frees_it_when_it_ends() -> None:
    assert _PG_URL is not None
    # A key of its own, so this can never contend with a scheduler that is really running
    # against the same database under `SCHEDULER_LEADER_LOCK_KEY`.
    key = int.from_bytes(os.urandom(7), "big")
    first = PostgresAdvisoryLockProvider(_PG_URL, key=key)
    second = PostgresAdvisoryLockProvider(_PG_URL, key=key)
    observer = create_async_engine(_PG_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        assert await first.try_acquire() is True
        assert await second.try_acquire() is False  # refused at once, not blocked
        assert await first.still_held() is True
        assert await first.try_acquire() is True  # re-asking does not stack a second hold

        await first.release()  # a clean shutdown hands the lock over
        assert await first.still_held() is False
        assert await second.try_acquire() is True

        # An administrator terminates the leader's backend: the next check fails closed, and
        # the server frees the lock so the other replica can take it.
        assert second._connection is not None
        backend_pid = await second._connection.scalar(text("SELECT pg_backend_pid()"))
        async with observer.connect() as connection:
            await connection.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": backend_pid}
            )
        assert await second.still_held() is False
        async with asyncio.timeout(10):
            while not await first.try_acquire():
                await _real_sleep(0.05)
    finally:
        await first.release()
        await second.release()
        await observer.dispose()
