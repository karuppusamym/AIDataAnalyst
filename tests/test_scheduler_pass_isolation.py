"""One failing maintenance pass must not end the scheduler, and must not go unseen (R11-VAL04).

`run_scheduler_iteration` awaited about two dozen passes in a row with nothing around any of
them, so an exception out of one left the iteration, left `run_scheduler`'s loop, released the
leadership lock and stopped every other pass: the reaper, the expiry sweeps, delivery, scans.
Nothing showed which pass it was.

A behavioural test of the real passes needs a Temporal client and a database each, so these
tests put a stub on every pass (found by reading the iteration's own source, so a pass added
later is covered without an edit here) and make chosen ones raise.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from prometheus_client import REGISTRY
from structlog.testing import capture_logs

from aida.config import Settings
from aida.workflows import scheduler

_SCHEDULER = Path(scheduler.__file__)


def _guarded_calls() -> dict[str, str]:
    """`{label: function name}` for every `_isolated("label", function(...))` in the iteration."""
    tree = ast.parse(_SCHEDULER.read_text(encoding="utf-8"), filename=str(_SCHEDULER))
    iteration = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_scheduler_iteration"
    )
    calls: dict[str, str] = {}
    for node in ast.walk(iteration):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_isolated"
        ):
            label, step = node.args
            assert isinstance(label, ast.Constant) and isinstance(step, ast.Call)
            assert isinstance(step.func, ast.Name)
            calls[str(label.value)] = step.func.id
    return calls


PASSES = _guarded_calls()


@pytest.fixture(autouse=True)
def _fresh_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test a module logger with no history, so `capture_logs` can see it.

    The drafter-supervisor tests failed the same way in the whole suite, because the
    application turns on `cache_logger_on_first_use`."""
    monkeypatch.setattr(scheduler, "logger", structlog.get_logger(scheduler.__name__))


class _Clock:
    """What `scheduler._now` reads: moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 21, 22, 0, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """A fixed clock, and no failure backoff carried in from another test."""
    fixed = _Clock()
    monkeypatch.setattr(scheduler, "_now", lambda: fixed.now)
    monkeypatch.setattr(scheduler, "_pass_backoff", {})
    return fixed


@pytest.fixture(autouse=True)
def _saved_outcomes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str | None]]:
    """Capture what each iteration would persist (R11-VAL04) instead of writing it anywhere.

    The stub database below has no `add`, and the real `session_factory` would reach whatever
    database the settings name. `tests/test_scheduler_pass_status.py` covers the write itself.
    """
    saved: list[dict[str, str | None]] = []

    async def record(outcomes: dict[str, str | None]) -> None:
        saved.append(dict(outcomes))

    monkeypatch.setattr(scheduler, "_save_pass_outcomes", record)
    return saved


class _Nothing:
    def all(self) -> list[Any]:
        return []


class _NoDatabase:
    """`session_factory()` for the one step that reads the database itself: no policy is due."""

    async def __aenter__(self) -> _NoDatabase:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def scalars(self, statement: object) -> _Nothing:
        return _Nothing()


class _Boom(RuntimeError):
    pass


def _stub_every_pass(monkeypatch: pytest.MonkeyPatch, failing: set[str]) -> list[str]:
    """Replace each pass with a recorder that raises for the labels in `failing`."""
    ran: list[str] = []

    def stub_for(label: str) -> Any:
        async def stub(*args: object, **kwargs: object) -> None:
            ran.append(label)
            if label in failing:
                raise _Boom(f"{label} broke")

        return stub

    for label, function in PASSES.items():
        monkeypatch.setattr(scheduler, function, stub_for(label))
    monkeypatch.setattr(scheduler, "session_factory", lambda: _NoDatabase())
    return ran


async def _iterate() -> int:
    return await scheduler.run_scheduler_iteration(object(), Settings(_env_file=None))  # type: ignore[arg-type]


def _failures(label: str) -> float:
    value = REGISTRY.get_sample_value(
        "aida_scheduler_pass_failures_total", {"scheduler_pass": label}
    )
    assert value is not None, f"no aida_scheduler_pass_failures_total series for {label}"
    return value


def test_the_labels_are_exactly_the_steps_the_iteration_guards() -> None:
    assert set(scheduler.SCHEDULER_PASS_NAMES) == set(PASSES) | {
        "scan_policy_selection",
        "scan_policy_processing",
    }
    assert len(scheduler.SCHEDULER_PASS_NAMES) == len(set(scheduler.SCHEDULER_PASS_NAMES))
    assert len(PASSES) > 20, "the scan of the iteration's source found almost nothing"
    for function in PASSES.values():
        assert callable(getattr(scheduler, function)), function


async def test_every_pass_runs_when_none_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = _stub_every_pass(monkeypatch, failing=set())

    assert await _iterate() == 0
    assert ran == list(PASSES)


@pytest.mark.parametrize("where", ["first", "middle", "last"])
async def test_a_failing_pass_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    labels = list(PASSES)
    failing = {"first": labels[0], "middle": labels[len(labels) // 2], "last": labels[-1]}[where]
    ran = _stub_every_pass(monkeypatch, failing={failing})

    assert await _iterate() == 0
    assert ran == labels


async def test_an_iteration_in_which_every_pass_fails_still_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = _stub_every_pass(monkeypatch, failing=set(PASSES))

    assert await _iterate() == 0
    assert ran == list(PASSES)


async def test_each_failure_is_counted_under_its_own_pass(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    _stub_every_pass(monkeypatch, failing={"reaper"})
    reaper, delivery = _failures("reaper"), _failures("delivery_worker")

    await _iterate()
    clock.advance(scheduler.PASS_FAILURE_BACKOFF_BASE_SECONDS)
    await _iterate()

    assert _failures("reaper") == reaper + 2
    assert _failures("delivery_worker") == delivery


async def test_a_failure_is_logged_with_its_pass_and_its_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_every_pass(monkeypatch, failing={"ownership_expiry"})

    with capture_logs() as logs:
        await _iterate()

    failed = [entry for entry in logs if entry["event"] == "scheduler_pass_failed"]
    assert [entry["scheduler_pass"] for entry in failed] == ["ownership_expiry"]
    assert failed[0]["log_level"] == "error"
    assert failed[0]["exc_info"] is True
    # The traceback is redacted by the platform's log processors; the class name is not.
    assert failed[0]["error_type"] == "_Boom"


async def test_a_failure_choosing_the_due_scan_policies_is_contained_and_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = _stub_every_pass(monkeypatch, failing=set())

    class _Refused(_NoDatabase):
        async def scalars(self, statement: object) -> _Nothing:
            raise _Boom("database unreachable")

    monkeypatch.setattr(scheduler, "session_factory", lambda: _Refused())
    before = _failures("scan_policy_selection")

    assert await _iterate() == 0
    assert ran == list(PASSES)
    assert _failures("scan_policy_selection") == before + 1


async def test_cancellation_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown cancels the loop; a pass must not turn that into "carry on"."""
    import asyncio

    class _Cancelled(asyncio.CancelledError):
        pass

    async def cancelled(*args: object, **kwargs: object) -> None:
        raise _Cancelled

    _stub_every_pass(monkeypatch, failing=set())
    monkeypatch.setattr(scheduler, PASSES["reaper"], cancelled)

    with pytest.raises(asyncio.CancelledError):
        await _iterate()


# --- R11-VAL04: what the iteration hands to the Operations screen ---------------------------


async def test_every_pass_outcome_is_handed_on_once_per_iteration(
    monkeypatch: pytest.MonkeyPatch, _saved_outcomes: list[dict[str, str | None]]
) -> None:
    _stub_every_pass(monkeypatch, failing={"reaper", "delivery_worker"})

    await _iterate()

    assert len(_saved_outcomes) == 1
    (outcomes,) = _saved_outcomes
    assert set(outcomes) == set(scheduler.SCHEDULER_PASS_NAMES)
    assert outcomes["reaper"] == outcomes["delivery_worker"] == "_Boom"
    assert {name for name, error in outcomes.items() if error is None} == (
        set(scheduler.SCHEDULER_PASS_NAMES) - {"reaper", "delivery_worker"}
    )


async def test_the_scan_policy_step_hands_on_its_own_outcome(
    monkeypatch: pytest.MonkeyPatch, _saved_outcomes: list[dict[str, str | None]]
) -> None:
    _stub_every_pass(monkeypatch, failing=set())
    await _iterate()

    class _Refused(_NoDatabase):
        async def scalars(self, statement: object) -> _Nothing:
            raise _Boom("database unreachable")

    monkeypatch.setattr(scheduler, "session_factory", lambda: _Refused())
    await _iterate()

    assert [saved["scan_policy_selection"] for saved in _saved_outcomes] == [None, "_Boom"]


async def test_an_iteration_that_is_cancelled_hands_on_nothing(
    monkeypatch: pytest.MonkeyPatch, _saved_outcomes: list[dict[str, str | None]]
) -> None:
    import asyncio

    async def cancelled(*args: object, **kwargs: object) -> None:
        raise asyncio.CancelledError

    _stub_every_pass(monkeypatch, failing=set())
    monkeypatch.setattr(scheduler, PASSES["reaper"], cancelled)

    with pytest.raises(asyncio.CancelledError):
        await _iterate()
    assert _saved_outcomes == []


async def test_a_scan_policy_that_fails_to_be_admitted_is_counted_and_reported(
    monkeypatch: pytest.MonkeyPatch, _saved_outcomes: list[dict[str, str | None]]
) -> None:
    """Round 12 logged a failed admission but counted it nowhere."""
    _stub_every_pass(monkeypatch, failing=set())

    class _OnePolicyDue(_NoDatabase):
        async def scalars(self, statement: object) -> Any:
            class _Due:
                def all(self) -> list[str]:
                    return ["policy-1", "policy-2"]

            return _Due()

    admitted: list[str] = []

    async def process(policy_id: str, *args: object, **kwargs: object) -> bool:
        if policy_id == "policy-1":
            raise _Boom("admission broke")
        admitted.append(policy_id)
        return True

    monkeypatch.setattr(scheduler, "session_factory", lambda: _OnePolicyDue())
    monkeypatch.setattr(scheduler, "process_scan_policy", process)
    before = _failures("scan_policy_processing")

    assert await _iterate() == 1
    assert admitted == ["policy-2"]
    assert _failures("scan_policy_processing") == before + 1
    assert _saved_outcomes[-1]["scan_policy_processing"] == "_Boom"


# --- a failing pass backs off instead of failing on every poll ------------------------------


async def test_a_failing_pass_is_skipped_until_its_backoff_has_passed(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    ran = _stub_every_pass(monkeypatch, failing={"reaper"})
    before = _failures("reaper")

    await _iterate()
    clock.advance(scheduler.PASS_FAILURE_BACKOFF_BASE_SECONDS - 1)
    await _iterate()

    assert ran.count("reaper") == 1, "the reaper ran again inside its backoff"
    assert ran.count("delivery_worker") == 2, "a healthy pass must not be held back"
    assert _failures("reaper") == before + 1

    clock.advance(1)
    await _iterate()
    assert ran.count("reaper") == 2


async def test_the_backoff_doubles_and_is_capped_under_the_stale_bound(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    ran = _stub_every_pass(monkeypatch, failing={"reaper"})
    waits: list[float] = []
    for _ in range(6):
        await _iterate()
        failures, retry_at = scheduler._pass_backoff["reaper"]
        waits.append((retry_at - clock.now).total_seconds())
        clock.advance(waits[-1])

    assert waits == [30, 60, 120, 240, 240, 240]
    assert ran.count("reaper") == 6
    from aida.scheduler_pass_status import stale_after

    assert timedelta(seconds=scheduler.PASS_FAILURE_BACKOFF_MAX_SECONDS) < stale_after(10)


async def test_a_pass_that_succeeds_again_loses_its_backoff(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    failing = {"reaper"}
    ran = _stub_every_pass(monkeypatch, failing=failing)
    await _iterate()
    failing.clear()
    clock.advance(scheduler.PASS_FAILURE_BACKOFF_BASE_SECONDS)

    await _iterate()
    await _iterate()

    assert "reaper" not in scheduler._pass_backoff
    assert ran.count("reaper") == 3


async def test_a_pass_skipped_by_its_backoff_leaves_no_unawaited_coroutine(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, recwarn: pytest.WarningsRecorder
) -> None:
    import gc

    _stub_every_pass(monkeypatch, failing={"reaper"})
    await _iterate()
    await _iterate()
    gc.collect()

    assert not [w for w in recwarn if "was never awaited" in str(w.message)]

