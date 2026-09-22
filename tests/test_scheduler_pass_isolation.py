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
    assert set(scheduler.SCHEDULER_PASS_NAMES) == set(PASSES) | {"scan_policy_selection"}
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_every_pass(monkeypatch, failing={"reaper"})
    reaper, delivery = _failures("reaper"), _failures("delivery_worker")

    await _iterate()
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
