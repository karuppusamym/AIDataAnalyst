"""R11-AUD03: the newly-created-table drafter's consumer no longer fails silently.

The default ten-service stack has no Redpanda. `aida.workflows.worker` started the
consumer as a task nobody awaited, `consumer.start()` raised inside it, and automatic
description drafting quietly never happened -- nothing logged until shutdown, and a
broker that came up later was never noticed. `supervise_newly_created_table_drafter`
runs the consumer in a loop with a capped exponential backoff and logs every failed
attempt as `newly_created_table_drafter_unavailable`.

What is exercised, with a fake consumer, a fake clock and a fake `sleep` (no broker,
no database, no real waiting):

1. the backoff sequence when the consumer fails N times and then starts;
2. the log level -- ERROR for the first failure and every tenth attempt, WARNING
   between -- and the fields an operator reads (attempt, next retry, bootstrap servers);
3. the backoff starting over after a healthy period, and not starting over for a
   consumer that dies soon after it started (the poisoned-message crash loop);
4. cancellation ending the loop promptly, in a wait and mid-run, with the default sleep;
5. a consumer that returns without being asked to stop being waited out, not spun on;
6. the consumer's own stopping state ending the loop;
7. the real consumer coroutine against a stub `AIOKafkaConsumer`: a failed `start()` is
   cleaned up, and a message whose handling fails is redelivered because its offset
   was not committed (per-message semantics are deliberately unchanged);
8. `run_worker` starting the supervised task when `auto_enqueue_on_ingest` is on and
   not when it is off, and logging -- not raising -- if that task ever dies;
9. the two series a scrape reads (the consumer-up gauge and the failed-attempts counter):
   no gauge series until the supervisor runs, 0 while waiting to retry, 1 only while the
   real consumer coroutine is consuming, 0 again however it ends, every failed attempt
   counted -- and `run_worker` opening the metrics listener, first, as `metadata-worker`.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from aiokafka.errors import KafkaConnectionError
from prometheus_client import REGISTRY
from structlog.testing import capture_logs

from aida import newly_created_table_drafter as drafter
from aida.newly_created_table_drafter import (
    DRAFTER_CONSUMER_GROUP,
    DRAFTER_CONSUMER_UP,
    DRAFTER_HEALTHY_AFTER_SECONDS,
    NEWLY_CREATED_TABLE_EVENT_TYPE,
    DrafterConsumerState,
    drafter_retry_delay_seconds,
    run_newly_created_table_drafter_consumer,
    supervise_newly_created_table_drafter,
)

UNAVAILABLE = "newly_created_table_drafter_unavailable"
BOOTSTRAP = "redpanda:9092"


@pytest.fixture(autouse=True)
def _fresh_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test a module logger with no history, so `capture_logs` can see it.

    `capture_logs` swaps the processors on the *current* structlog configuration. The
    application's logging setup (`atlas.platform.logging`) turns on `cache_logger_on_first_use`,
    so a module-level logger that an earlier test in the same process already drove keeps the
    processor list it was first bound to, and a later `configure_logging` (importing
    `aida.main`, or any test that calls it) replaces that list without the logger noticing:
    its events then go to stdout and never reach the capture. Nine of these tests failed in
    the whole suite and passed alone. A logger created for the test binds on its first call,
    inside the capture."""
    monkeypatch.setattr(drafter, "logger", structlog.get_logger(drafter.__name__))


@pytest.fixture(autouse=True)
def _no_gauge_series_yet() -> None:
    """Start every test as a worker whose supervisor has not run: no consumer-up series.

    That is the state the gauge is in, on purpose, until the supervisor starts (a labelled
    gauge has no series before `.labels()`), and an earlier test in this process would
    otherwise have created it. `remove` is a no-op when the series is not there."""
    DRAFTER_CONSUMER_UP.remove(DRAFTER_CONSUMER_GROUP)


@pytest.fixture(autouse=True)
def _bootstrap_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The supervisor and the consumer both read the broker address from settings;
    pinning it makes "the log names the bootstrap servers" an assertion rather than
    whatever the developer's environment happens to hold."""
    monkeypatch.setattr(
        drafter, "get_settings", lambda: SimpleNamespace(kafka_bootstrap_servers=BOOTSTRAP)
    )


class _Harness:
    """A stopped clock, a `sleep` that only records, and the state the loop shares."""

    def __init__(self) -> None:
        self.state = DrafterConsumerState()
        self.sleeps: list[float] = []
        self.now = 1_000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        # Yield once, like a real sleep, so nothing in the loop can starve the event loop.
        await asyncio.sleep(0)

    async def supervise(self, runner: Callable[..., Any]) -> None:
        await supervise_newly_created_table_drafter(
            runner, sleep=self.sleep, monotonic=self.clock, state=self.state
        )


@pytest.fixture
def harness() -> _Harness:
    return _Harness()


def _no_broker() -> KafkaConnectionError:
    return KafkaConnectionError(f"Unable to bootstrap from {BOOTSTRAP}")


class _Runner:
    """A consumer that does what the script says, one entry per attempt.

    An exception is raised; a callable is awaited with the shared state. Once the
    script is used up the consumer starts and is then told to stop, which is a healthy
    consumer shutting down and how a test ends the loop.
    """

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    async def __call__(self, state: DrafterConsumerState) -> None:
        self.calls += 1
        if not self.script:
            state.started_at = 0.0
            state.stopping = True
            return
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        await step(state)


def _up_for(harness: _Harness, seconds: float, then: Exception) -> Callable[..., Any]:
    """The consumer starts, stays up `seconds` (on the fake clock), then fails."""

    async def step(state: DrafterConsumerState) -> None:
        state.started_at = harness.clock()
        harness.now += seconds
        raise then

    return step


def _unavailable(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] == UNAVAILABLE]


# -- the backoff -------------------------------------------------------------------


def test_the_delay_doubles_from_two_seconds_to_a_cap_of_sixty_and_stays_there() -> None:
    assert [drafter_retry_delay_seconds(n) for n in range(1, 9)] == [
        2.0,
        4.0,
        8.0,
        16.0,
        32.0,
        60.0,
        60.0,
        60.0,
    ]
    # A very long outage must not overflow the exponent.
    assert drafter_retry_delay_seconds(10_000) == 60.0


async def test_a_consumer_that_fails_eight_times_and_then_starts_is_retried_on_the_backoff(
    harness: _Harness,
) -> None:
    runner = _Runner([_no_broker()] * 8)

    with capture_logs() as logs:
        await harness.supervise(runner)

    assert runner.calls == 9, "eight failures, then the attempt that started and was told to stop"
    assert harness.sleeps == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    assert [entry["next_retry_seconds"] for entry in _unavailable(logs)] == harness.sleeps


async def test_whatever_the_consumer_raises_the_supervisor_survives_it(harness: _Harness) -> None:
    """The consumer runs in a task nobody awaits; nothing it raises may end that task."""
    runner = _Runner(
        [
            _no_broker(),
            RuntimeError("write refused"),
            ValueError("bad payload"),
            KeyError("payload"),
        ]
    )

    with capture_logs() as logs:
        await harness.supervise(runner)

    assert runner.calls == 5
    assert [entry["error_type"] for entry in _unavailable(logs)] == [
        "KafkaConnectionError",
        "RuntimeError",
        "ValueError",
        "KeyError",
    ]


# -- the log line ------------------------------------------------------------------


async def test_the_first_failure_and_every_tenth_attempt_are_errors_and_the_rest_warnings(
    harness: _Harness,
) -> None:
    runner = _Runner([_no_broker()] * 21)

    with capture_logs() as logs:
        await harness.supervise(runner)

    events = _unavailable(logs)
    assert [entry["attempt"] for entry in events] == list(range(1, 22))
    errors = {entry["attempt"] for entry in events if entry["log_level"] == "error"}
    warnings = {entry["attempt"] for entry in events if entry["log_level"] == "warning"}
    assert errors == {1, 10, 20}
    assert warnings == set(range(1, 22)) - errors, "every other retry is a warning, not an error"

    first = events[0]
    assert first["log_level"] == "error"
    assert first["attempt"] == 1
    assert first["next_retry_seconds"] == 2.0
    assert first["bootstrap_servers"] == BOOTSTRAP
    assert first["error_type"] == "KafkaConnectionError"
    assert isinstance(first["exc_info"], KafkaConnectionError), "the ERROR line carries the cause"
    assert "exc_info" not in events[1], "a warning per retry does not repeat the traceback"


# -- a healthy period, and the lack of one -----------------------------------------


async def test_a_consumer_that_stayed_up_before_it_failed_starts_a_new_incident(
    harness: _Harness,
) -> None:
    runner = _Runner(
        [
            _no_broker(),
            _no_broker(),
            _up_for(harness, DRAFTER_HEALTHY_AFTER_SECONDS, then=RuntimeError("dropped")),
            _no_broker(),
        ]
    )

    with capture_logs() as logs:
        await harness.supervise(runner)

    # 2 s, 4 s; then up for the healthy period and failing -> back to 2 s, not 8 s.
    assert harness.sleeps == [2.0, 4.0, 2.0, 4.0]
    events = _unavailable(logs)
    assert [entry["attempt"] for entry in events] == [1, 2, 1, 2]
    assert [entry["log_level"] for entry in events] == ["error", "warning", "error", "warning"]


async def test_a_consumer_that_dies_soon_after_starting_does_not_reset_the_backoff(
    harness: _Harness,
) -> None:
    """The poisoned-message loop: the consumer starts, meets the same message and dies,
    again and again. Started is not the same as healthy, so the wait keeps growing."""
    runner = _Runner(
        [_up_for(harness, DRAFTER_HEALTHY_AFTER_SECONDS - 1, then=RuntimeError("poison"))] * 4
    )

    await harness.supervise(runner)

    assert harness.sleeps == [2.0, 4.0, 8.0, 16.0]


# -- stopping ----------------------------------------------------------------------


async def test_cancellation_while_waiting_between_attempts_ends_the_loop_promptly(
    harness: _Harness,
) -> None:
    waiting = asyncio.Event()

    async def wait_for_cancellation(seconds: float) -> None:
        waiting.set()
        await asyncio.Event().wait()  # only cancellation ends this

    runner = _Runner([_no_broker()] * 100)
    task = asyncio.create_task(
        supervise_newly_created_table_drafter(
            runner, sleep=wait_for_cancellation, monotonic=harness.clock, state=harness.state
        )
    )
    await asyncio.wait_for(waiting.wait(), timeout=1)

    task.cancel()
    done, pending = await asyncio.wait({task}, timeout=1)

    assert task in done and not pending, "the loop did not stop on cancellation"
    assert task.cancelled()
    assert runner.calls == 1, "cancelled in the wait, so no further attempt was made"


async def test_the_default_sleep_is_cancelled_at_once_and_not_run_out(harness: _Harness) -> None:
    """No `sleep` injected: the real `asyncio.sleep(2)` must not hold up shutdown."""
    failed = asyncio.Event()

    async def fails_once(state: DrafterConsumerState) -> None:
        failed.set()
        raise _no_broker()

    task = asyncio.create_task(
        supervise_newly_created_table_drafter(fails_once, state=harness.state)
    )
    await asyncio.wait_for(failed.wait(), timeout=1)
    await asyncio.sleep(0)  # let the supervisor reach its wait

    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=1)

    assert task in done, "cancelling did not interrupt the two-second wait"
    assert task.cancelled()


async def test_cancellation_while_the_consumer_runs_reaches_the_consumers_own_cleanup(
    harness: _Harness,
) -> None:
    running = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def consumer(state: DrafterConsumerState) -> None:
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    task = asyncio.create_task(
        supervise_newly_created_table_drafter(
            consumer, sleep=harness.sleep, monotonic=harness.clock, state=harness.state
        )
    )
    await asyncio.wait_for(running.wait(), timeout=1)

    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=1)

    assert task in done and task.cancelled()
    assert cleaned_up.is_set(), "the cancellation must not be swallowed on its way through"
    assert harness.sleeps == [], "a cancelled consumer is not a failure to be retried"


async def test_a_consumer_that_returns_without_being_stopped_is_waited_out_not_spun_on(
    harness: _Harness,
) -> None:
    calls = 0

    async def returns_at_once(state: DrafterConsumerState) -> None:
        nonlocal calls
        calls += 1
        if calls > 20:  # a loop that spins would get here: end the test instead of hanging it
            state.stopping = True

    async def stop_after_three_waits(seconds: float) -> None:
        harness.sleeps.append(seconds)
        if len(harness.sleeps) == 3:
            harness.state.stopping = True

    with capture_logs() as logs:
        await supervise_newly_created_table_drafter(
            returns_at_once,
            sleep=stop_after_three_waits,
            monotonic=harness.clock,
            state=harness.state,
        )

    assert calls == 3, "one attempt per wait; a spinning loop would have made twenty"
    assert harness.sleeps == [2.0, 4.0, 8.0]
    events = _unavailable(logs)
    assert [entry["error_type"] for entry in events] == [None, None, None]
    assert events[0]["log_level"] == "error", "returning early is as unavailable as raising"


async def test_a_consumer_that_was_told_to_stop_ends_the_loop_without_a_wait_or_a_log(
    harness: _Harness,
) -> None:
    async def stops(state: DrafterConsumerState) -> None:
        state.stopping = True

    with capture_logs() as logs:
        await harness.supervise(stops)

    assert harness.sleeps == []
    assert _unavailable(logs) == []


async def test_an_error_on_the_way_out_is_logged_and_still_ends_the_loop(
    harness: _Harness,
) -> None:
    async def fails_while_stopping(state: DrafterConsumerState) -> None:
        state.stopping = True
        raise _no_broker()

    with capture_logs() as logs:
        await harness.supervise(fails_while_stopping)

    assert harness.sleeps == []
    assert _unavailable(logs) == []
    (entry,) = [
        e for e in logs if e["event"] == "newly_created_table_drafter_failed_while_stopping"
    ]
    assert entry["log_level"] == "warning"
    assert entry["error_type"] == "KafkaConnectionError"


async def test_a_state_that_is_already_stopping_never_starts_the_consumer(
    harness: _Harness,
) -> None:
    harness.state.stopping = True
    runner = _Runner([_no_broker()])

    await harness.supervise(runner)

    assert runner.calls == 0


# -- the real consumer coroutine, against a stub AIOKafkaConsumer ------------------


class _FakeKafka:
    """Stands in for `aiokafka.AIOKafkaConsumer`.

    `start()` fails as scripted; `log` is the topic and `committed` the group's offset,
    so a consumer that starts again resumes where the last one committed -- which is
    what makes "not committed" mean "redelivered". Every consumer built is kept.
    """

    def __init__(self) -> None:
        self.start_failures: list[Exception] = []
        self.stop_failure: Exception | None = None
        self.log: list[bytes] = []
        self.committed = 0
        self.on_commit: Callable[[], None] = lambda: None
        #: A consumer that has read the whole log waits for more, as a real one does, instead of
        #: ending: what a test needs to hold a consumer "up" and then cancel it.
        self.block_when_drained = False
        #: Set by any consumer's `start()` once it has succeeded, so a test can wait for it.
        self.a_consumer_started = asyncio.Event()
        self.consumers: list[_FakeConsumer] = []

    def __call__(self, *topics: str, **kwargs: Any) -> _FakeConsumer:
        consumer = _FakeConsumer(self, topics, kwargs)
        self.consumers.append(consumer)
        return consumer


class _FakeConsumer:
    def __init__(self, kafka: _FakeKafka, topics: tuple[str, ...], kwargs: dict[str, Any]) -> None:
        self.kafka = kafka
        self.topics = topics
        self.kwargs = kwargs
        self.position = kafka.committed
        self.started = False
        self.stops = 0
        self.commits = 0

    async def start(self) -> None:
        if self.kafka.start_failures:
            raise self.kafka.start_failures.pop(0)
        self.started = True
        self.kafka.a_consumer_started.set()

    async def stop(self) -> None:
        self.stops += 1
        if self.kafka.stop_failure is not None:
            raise self.kafka.stop_failure

    async def commit(self) -> None:
        self.commits += 1
        self.kafka.committed = self.position
        self.kafka.on_commit()

    def __aiter__(self) -> _FakeConsumer:
        return self

    async def __anext__(self) -> SimpleNamespace:
        if self.position >= len(self.kafka.log):
            if self.kafka.block_when_drained:
                await asyncio.Event().wait()  # only cancellation ends this
            raise StopAsyncIteration
        message = SimpleNamespace(value=self.kafka.log[self.position])
        self.position += 1
        return message


class _Session:
    def begin(self) -> _Transaction:
        return _Transaction()


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _SessionScope:
    async def __aenter__(self) -> _Session:
        return _Session()

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def _event(event_type: str, event_id: str) -> bytes:
    return json.dumps(
        {"event_id": event_id, "event_type": event_type, "payload": {"table_id": event_id}}
    ).encode()


@pytest.fixture
def kafka(monkeypatch: pytest.MonkeyPatch) -> _FakeKafka:
    fake = _FakeKafka()
    monkeypatch.setattr("aiokafka.AIOKafkaConsumer", fake)
    monkeypatch.setattr(drafter, "session_factory", lambda: _SessionScope())
    return fake


async def test_a_broker_that_appears_late_is_picked_up_and_each_failed_start_is_cleaned_up(
    harness: _Harness, kafka: _FakeKafka
) -> None:
    kafka.start_failures = [_no_broker()] * 3
    kafka.log = [_event("catalog.table.created.v1", "e1")]  # one that is not the drafter's
    # SIGTERM arrives once that message is committed: the consumer finishes it and stops.
    kafka.on_commit = lambda: setattr(harness.state, "stopping", True)

    with capture_logs() as logs:
        await supervise_newly_created_table_drafter(
            run_newly_created_table_drafter_consumer,
            sleep=harness.sleep,
            monotonic=harness.clock,
            state=harness.state,
        )

    assert harness.sleeps == [2.0, 4.0, 8.0]
    assert len(kafka.consumers) == 4
    assert [c.stops for c in kafka.consumers] == [1, 1, 1, 1], (
        "a start() that failed must still be stopped, or every retry leaks a client"
    )
    assert [c.started for c in kafka.consumers] == [False, False, False, True]
    assert kafka.consumers[-1].commits == 1
    assert kafka.consumers[-1].kwargs["bootstrap_servers"] == BOOTSTRAP
    assert kafka.consumers[-1].kwargs["enable_auto_commit"] is False, "manual commit is unchanged"
    assert kafka.consumers[-1].kwargs["group_id"] == DRAFTER_CONSUMER_GROUP, (
        "the gauge is labelled with the group the consumer actually joins"
    )

    assert len(_unavailable(logs)) == 3
    started = [e for e in logs if e["event"] == "newly_created_table_drafter_started"]
    assert len(started) == 1 and started[0]["bootstrap_servers"] == BOOTSTRAP
    stopped = [e for e in logs if e["event"] == "newly_created_table_drafter_stopped"]
    assert len(stopped) == 1, "only a consumer that started reports having stopped"
    assert harness.state.started_at is not None


async def test_a_message_whose_handling_fails_is_not_committed_and_is_redelivered_after_the_restart(
    harness: _Harness, kafka: _FakeKafka, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-message semantics are unchanged: commit follows a successful handle. What the
    supervisor adds is that the restart, and so the redelivery, now happens."""
    kafka.log = [_event(NEWLY_CREATED_TABLE_EVENT_TYPE, "t1")]
    handled: list[str] = []

    async def handle(session: Any, payload: dict[str, Any]) -> None:
        handled.append(payload["table_id"])
        if len(handled) < 3:
            raise RuntimeError("write refused")

    monkeypatch.setattr(drafter, "handle_newly_created_table", handle)
    kafka.on_commit = lambda: setattr(harness.state, "stopping", True)

    with capture_logs() as logs:
        await supervise_newly_created_table_drafter(
            run_newly_created_table_drafter_consumer,
            sleep=harness.sleep,
            monotonic=harness.clock,
            state=harness.state,
        )

    assert handled == ["t1", "t1", "t1"], "delivered again after each failure"
    assert [c.commits for c in kafka.consumers] == [0, 0, 1]
    assert kafka.committed == 1
    assert harness.sleeps == [2.0, 4.0]
    assert [e["error_type"] for e in _unavailable(logs)] == ["RuntimeError", "RuntimeError"]
    assert all(c.stops == 1 for c in kafka.consumers)


async def test_run_bare_the_consumer_still_creates_its_own_state_and_raises_on_failure(
    kafka: _FakeKafka, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Called without a supervisor, as `python -m` and any old caller did: no retry."""
    monkeypatch.setattr(drafter, "_stop_on_signals", lambda state: None)
    kafka.start_failures = [_no_broker()]

    with pytest.raises(KafkaConnectionError):
        await run_newly_created_table_drafter_consumer()

    assert [c.stops for c in kafka.consumers] == [1]


async def test_a_stop_that_fails_does_not_replace_the_error_that_ended_the_consumer(
    kafka: _FakeKafka, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor has to report why the consumer ended, not what cleanup then tripped on."""
    monkeypatch.setattr(drafter, "_stop_on_signals", lambda state: None)
    kafka.start_failures = [_no_broker()]
    kafka.stop_failure = OSError("connection already closed")

    with capture_logs() as logs, pytest.raises(KafkaConnectionError):
        await run_newly_created_table_drafter_consumer()

    (entry,) = [e for e in logs if e["event"] == "newly_created_table_drafter_stop_failed"]
    assert entry["log_level"] == "warning"
    assert entry["error_type"] == "OSError"


# -- what a scrape reads -----------------------------------------------------------


def _up() -> float | None:
    """The consumer-up gauge as a scrape would read it: None when there is no series."""
    return REGISTRY.get_sample_value(
        "aida_newly_created_table_drafter_consumer_up", {"consumer_group": DRAFTER_CONSUMER_GROUP}
    )


def _failures() -> float:
    value = REGISTRY.get_sample_value("aida_newly_created_table_drafter_failures_total")
    assert value is not None, "the failed-attempts counter is missing"
    return value


def _starts() -> float:
    value = REGISTRY.get_sample_value("aida_newly_created_table_drafter_starts_total")
    assert value is not None, "the successful-starts counter is missing"
    return value


def _run_in_a_fresh_interpreter(code: str) -> list[str]:
    """What a process that has only imported the module publishes, without this process's
    history: an earlier test here has already created the gauge series."""
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
        "AIDA_ENVIRONMENT": "test",
    }
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


def test_a_worker_that_only_imports_the_module_publishes_no_consumer_up_series() -> None:
    """The reason the gauge is labelled. The worker imports this module whether or not
    `auto_enqueue_on_ingest` is on; an unlabelled gauge would be exported at 0 from the import
    and an alert on `== 0` would page for a feature somebody switched off."""
    values = _run_in_a_fresh_interpreter(
        "from prometheus_client import REGISTRY\n"
        "import aida.newly_created_table_drafter\n"
        "print(REGISTRY.get_sample_value('aida_newly_created_table_drafter_consumer_up',"
        " {'consumer_group': 'aida-newly-created-table-drafter-v1'}))\n"
        "print(REGISTRY.get_sample_value('aida_newly_created_table_drafter_failures_total'))\n"
        "print(REGISTRY.get_sample_value('aida_newly_created_table_drafter_starts_total'))\n"
    )

    # No gauge series; the counters are at 0, which an `increase()` reads as nothing happened.
    assert values == ["None", "0.0", "0.0"]


async def test_the_gauge_appears_at_zero_when_the_supervisor_starts_and_stays_there_while_waiting(
    harness: _Harness,
) -> None:
    assert _up() is None
    waits: list[float | None] = []

    async def note_the_gauge_then_wait(seconds: float) -> None:
        waits.append(_up())
        await harness.sleep(seconds)

    await supervise_newly_created_table_drafter(
        _Runner([_no_broker(), _no_broker()]),
        sleep=note_the_gauge_then_wait,
        monotonic=harness.clock,
        state=harness.state,
    )

    # Neither failed attempt had a consumer to say otherwise, and the wait after each one read 0.
    assert waits == [0, 0]


async def test_every_failed_attempt_is_counted_not_only_the_ones_the_log_escalates(
    harness: _Harness,
) -> None:
    before = _failures()

    await harness.supervise(_Runner([_no_broker()] * 12))

    assert _failures() - before == 12  # attempts 1 and 10 are ERRORs; the other ten are WARNINGs


async def test_a_consumer_that_returns_without_being_stopped_counts_as_a_failed_attempt(
    harness: _Harness,
) -> None:
    calls = 0

    async def returns_at_once(state: DrafterConsumerState) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            state.stopping = True

    before = _failures()
    await harness.supervise(returns_at_once)

    assert _failures() - before == 2  # the two early returns; the third was a stop


async def test_a_clean_stop_and_a_failure_while_stopping_are_not_failed_attempts(
    harness: _Harness,
) -> None:
    async def stops(state: DrafterConsumerState) -> None:
        state.stopping = True

    async def fails_while_stopping(state: DrafterConsumerState) -> None:
        state.stopping = True
        raise _no_broker()

    before = _failures()
    await harness.supervise(stops)
    harness.state.stopping = False
    await harness.supervise(fails_while_stopping)

    assert _failures() == before  # neither is the outage the counter is for


async def test_the_real_consumer_reads_one_only_while_consuming_and_zero_while_it_waits_to_retry(
    harness: _Harness, kafka: _FakeKafka, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two failed starts, then one that handles a message that fails once, then a good one.

    The gauge is read where a scrape could read it: in each wait between attempts, and inside
    the handler while a message is being processed."""
    kafka.start_failures = [_no_broker()] * 2
    kafka.log = [_event(NEWLY_CREATED_TABLE_EVENT_TYPE, "t1")]
    kafka.on_commit = lambda: setattr(harness.state, "stopping", True)
    while_handling: list[float | None] = []
    while_waiting: list[float | None] = []

    async def handle(session: Any, payload: dict[str, Any]) -> None:
        while_handling.append(_up())
        if len(while_handling) == 1:
            raise RuntimeError("write refused")

    async def note_the_gauge_then_wait(seconds: float) -> None:
        while_waiting.append(_up())
        await harness.sleep(seconds)

    monkeypatch.setattr(drafter, "handle_newly_created_table", handle)
    before = _failures()

    await supervise_newly_created_table_drafter(
        run_newly_created_table_drafter_consumer,
        sleep=note_the_gauge_then_wait,
        monotonic=harness.clock,
        state=harness.state,
    )

    assert while_handling == [1, 1], "up while a message is being handled, both times"
    assert while_waiting == [0, 0, 0], "down in the wait after each failed start and the failure"
    assert _failures() - before == 3
    assert _up() == 0, "and down at the end: the consumer returned when it was told to stop"


async def test_only_a_start_that_succeeded_is_counted_so_a_killing_message_reads_as_restarts(
    harness: _Harness, kafka: _FakeKafka, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reading `AtlasNewlyCreatedTableDrafterRestarting` rests on. Two starts with no broker,
    then a message that kills the consumer twice before it is handled: the failures counter rises
    four times and cannot tell the two causes apart; the starts counter rises only for the three
    consumers that joined the group, so a missing broker leaves it alone."""
    kafka.start_failures = [_no_broker()] * 2
    kafka.log = [_event(NEWLY_CREATED_TABLE_EVENT_TYPE, "t1")]
    kafka.on_commit = lambda: setattr(harness.state, "stopping", True)
    handled: list[str] = []

    async def handle(session: Any, payload: dict[str, Any]) -> None:
        handled.append(payload["table_id"])
        if len(handled) < 3:
            raise RuntimeError("write refused")

    monkeypatch.setattr(drafter, "handle_newly_created_table", handle)
    starts_before, failures_before = _starts(), _failures()

    await supervise_newly_created_table_drafter(
        run_newly_created_table_drafter_consumer,
        sleep=harness.sleep,
        monotonic=harness.clock,
        state=harness.state,
    )

    assert [c.started for c in kafka.consumers] == [False, False, True, True, True]
    assert _starts() - starts_before == 3
    assert _failures() - failures_before == 4


async def test_a_start_that_never_succeeds_is_never_counted_as_one(
    harness: _Harness, kafka: _FakeKafka
) -> None:
    """The default stack: no broker, so the consumer-down alert fires and the restart alert must
    stay quiet however long the supervisor keeps trying."""
    kafka.start_failures = [_no_broker()] * 12
    before = _starts()
    calls = 0

    async def stop_after_twelve(seconds: float) -> None:
        nonlocal calls
        calls += 1
        if calls == 12:
            harness.state.stopping = True
        await harness.sleep(seconds)

    await supervise_newly_created_table_drafter(
        run_newly_created_table_drafter_consumer,
        sleep=stop_after_twelve,
        monotonic=harness.clock,
        state=harness.state,
    )

    assert len(kafka.consumers) == 12
    assert _starts() == before


async def test_cancelling_a_consumer_that_is_up_takes_the_gauge_to_zero(
    harness: _Harness, kafka: _FakeKafka
) -> None:
    """The worker's shutdown cancels the supervised task: nothing may go on saying it is up."""
    kafka.block_when_drained = True
    task = asyncio.create_task(run_newly_created_table_drafter_consumer(harness.state))
    async with asyncio.timeout(2):
        await kafka.a_consumer_started.wait()
    # Nothing between `start()` returning and the consumer blocking for a message yields to this
    # test, so by the time it runs the consumer has said it is up.
    assert _up() == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _up() == 0
    assert [c.stops for c in kafka.consumers] == [1]


# -- the worker --------------------------------------------------------------------


class _FakeTemporalWorker:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def run(self) -> None:
        # Long enough for the tasks `run_worker` created to start, and for a done
        # callback to run, and no longer.
        for _ in range(3):
            await asyncio.sleep(0)


class _FakeTemporalClient:
    @staticmethod
    async def connect(*args: Any, **kwargs: Any) -> object:
        return object()


@pytest.fixture
def worker_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    from aida.workflows import worker as module

    monkeypatch.setattr(module, "Client", _FakeTemporalClient)
    monkeypatch.setattr(module, "Worker", _FakeTemporalWorker)
    # Reconfiguring structlog would outlive the test and break every `capture_logs` after it.
    monkeypatch.setattr(module, "configure_logging", lambda level: None)
    # The listener is opened from `Settings.worker_metrics_port`, which the namespace below has
    # no reason to carry. A test that is about the listener replaces this with a recorder.
    monkeypatch.setattr(module, "serve_worker_metrics", lambda settings, *, process: None)
    return module


def _worker_settings(*, auto_enqueue_on_ingest: bool) -> Callable[[], SimpleNamespace]:
    settings = SimpleNamespace(
        log_level="INFO",
        temporal_address="temporal:7233",
        temporal_namespace="default",
        temporal_task_queue="aida",
        auto_enqueue_on_ingest=auto_enqueue_on_ingest,
    )
    return lambda: settings


async def test_the_worker_starts_the_supervised_drafter_when_auto_enqueue_is_on(
    worker_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    async def fake_supervisor() -> None:
        events.append("started")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    monkeypatch.setattr(
        worker_module, "get_settings", _worker_settings(auto_enqueue_on_ingest=True)
    )
    monkeypatch.setattr(worker_module, "supervise_newly_created_table_drafter", fake_supervisor)

    await worker_module.run_worker()

    assert events == ["started", "cancelled"], "started beside the Temporal worker, stopped with it"


async def test_the_worker_does_not_start_the_drafter_when_auto_enqueue_is_off(
    worker_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    async def fake_supervisor() -> None:
        events.append("started")

    monkeypatch.setattr(
        worker_module, "get_settings", _worker_settings(auto_enqueue_on_ingest=False)
    )
    monkeypatch.setattr(worker_module, "supervise_newly_created_table_drafter", fake_supervisor)

    await worker_module.run_worker()

    assert events == []


async def test_a_supervisor_that_dies_anyway_is_logged_and_does_not_end_the_worker(
    worker_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor is written not to raise. If it ever does, that must not be the silent
    failure this item replaces, and it must not turn into the Temporal worker's."""

    async def broken_supervisor() -> None:
        raise RuntimeError("a bug in the supervisor")

    monkeypatch.setattr(
        worker_module, "get_settings", _worker_settings(auto_enqueue_on_ingest=True)
    )
    monkeypatch.setattr(worker_module, "supervise_newly_created_table_drafter", broken_supervisor)

    with capture_logs() as logs:
        await worker_module.run_worker()  # returns; nothing propagates out of the shutdown

    (entry,) = [e for e in logs if e["event"] == "newly_created_table_drafter_supervisor_failed"]
    assert entry["log_level"] == "error"
    assert entry["error_type"] == "RuntimeError"


async def test_the_worker_opens_its_metrics_listener_first_as_the_metadata_worker(
    worker_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gauges above are published into this process's registry, and only a listener lets a
    scrape read it. It comes before the Temporal connection, which can block or fail: a worker
    that cannot reach Temporal is exactly when its target should still answer."""
    order: list[str] = []
    settings = _worker_settings(auto_enqueue_on_ingest=False)()

    class _RecordingTemporalClient:
        @staticmethod
        async def connect(*args: Any, **kwargs: Any) -> object:
            order.append("temporal connect")
            return object()

    def record_the_listener(passed: object, *, process: str) -> None:
        assert passed is settings, "the listener reads the same settings the worker runs on"
        order.append(f"metrics listener: {process}")

    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "Client", _RecordingTemporalClient)
    monkeypatch.setattr(worker_module, "serve_worker_metrics", record_the_listener)

    await worker_module.run_worker()

    assert order == ["metrics listener: metadata-worker", "temporal connect"]


# --- a broker that drops after the start (found reviewing round 12, 2026-09-21) -------------


class _MetadataConsumer:
    """`topics()` takes one scripted answer per probe, so the test steps the probe one at a time."""

    def __init__(self) -> None:
        self.answers: asyncio.Queue[BaseException | None] = asyncio.Queue()
        self.answered = 0

    async def topics(self) -> set[str]:
        answer = await self.answers.get()
        self.answered += 1
        if answer is not None:
            raise answer
        return {"aida.platform.events.v1"}


async def _step(consumer: _MetadataConsumer, answer: BaseException | None) -> float | None:
    """Let the probe take one answer, then read the gauge it left."""
    before = consumer.answered
    consumer.answers.put_nowait(answer)
    for _ in range(200):
        await asyncio.sleep(0)
        if consumer.answered > before:
            break
    for _ in range(5):
        await asyncio.sleep(0)
    return _up()


async def test_a_broker_that_stops_answering_after_the_start_takes_the_gauge_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drafter, "DRAFTER_BROKER_PROBE_SECONDS", 0)
    drafter._set_consumer_up(1)
    consumer = _MetadataConsumer()

    with capture_logs() as logs:
        task = asyncio.ensure_future(drafter._probe_broker(consumer))
        seen = [
            await _step(consumer, KafkaConnectionError("gone")),
            await _step(consumer, KafkaConnectionError("still gone")),
            await _step(consumer, None),
        ]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert seen == [0, 0, 1]
    events = [entry["event"] for entry in logs]
    assert events.count("newly_created_table_drafter_broker_unreachable") == 1
    assert events.count("newly_created_table_drafter_broker_reachable_again") == 1


async def test_a_probe_that_hangs_is_a_failed_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drafter, "DRAFTER_BROKER_PROBE_SECONDS", 0)
    monkeypatch.setattr(drafter, "DRAFTER_BROKER_PROBE_TIMEOUT_SECONDS", 0.01)
    drafter._set_consumer_up(1)

    class _Hangs:
        async def topics(self) -> set[str]:
            await asyncio.sleep(10)
            return set()

    task = asyncio.ensure_future(drafter._probe_broker(_Hangs()))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _up() == 0
