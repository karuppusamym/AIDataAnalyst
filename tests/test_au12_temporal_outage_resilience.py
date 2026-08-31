"""AU-12 -- surviving a Temporal outage.

`main.py`'s `lifespan` used to `await Client.connect(...)` with no timeout and
no try/except: an unreachable or slow-to-respond Temporal server hung or
raised right there, and since `lifespan` runs before the ASGI server starts
serving any traffic, that took down every route -- including the read-only,
non-Temporal-dependent ones (catalog browsing, search, `/health/live`
itself) -- not just the Temporal-dependent ones.

These tests drive real application startup (`aida.main.lifespan`, via the
ASGI lifespan protocol, exactly like
`test_observability.test_lifespan_wires_tracing_and_metrics_...`) with a
fake `Client` standing in for `temporalio.client.Client`, so a real failure
(raise) and a real timeout (never-returning connect) are exercised without
needing a live Temporal server -- there is none in this test environment.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from aida import main as main_module


class _FailingTemporalClient:
    """Stands in for `temporalio.client.Client`: every `connect()` raises,
    simulating an unreachable Temporal server (connection refused, DNS
    failure, etc. all surface the same way to `_connect_temporal`).
    """

    @staticmethod
    async def connect(address: str, *, namespace: str) -> "_FailingTemporalClient":
        raise ConnectionError("temporal unreachable (test)")


class _HangingTemporalClient:
    """Stands in for `temporalio.client.Client`: `connect()` never returns
    within any reasonable test duration, simulating a network partition
    rather than an immediate refusal -- the case `temporal_connect_timeout_seconds`
    exists for, since `Client.connect` has no timeout of its own.
    """

    @staticmethod
    async def connect(address: str, *, namespace: str) -> "_HangingTemporalClient":
        await asyncio.sleep(30)
        raise AssertionError("_connect_temporal should have timed out long before this fired")


class _FlakyThenOkTemporalClient:
    """Stands in for `temporalio.client.Client`: fails on the first
    `connect()` (the outage `lifespan` hits at startup), then succeeds on
    every subsequent call -- simulating Temporal coming back while the app
    is already running, for the background reconnect loop to pick up.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def connect(self, address: str, *, namespace: str) -> object:
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("temporal unreachable (test)")
        return object()  # sentinel standing in for a real, connected Client


@pytest.fixture(autouse=True)
def _fast_temporal_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here fully controls its own fake `Client.connect`, so the
    bounded timeout only needs to be short enough that a genuinely-hanging
    fake (see `_HangingTemporalClient`) doesn't slow the suite down.
    """
    monkeypatch.setattr(main_module.settings, "temporal_connect_timeout_seconds", 0.2)


def test_temporal_outage_at_startup_does_not_crash_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DoD 1: a Temporal connect failure at startup must not prevent the
    app from starting -- entering the TestClient's lifespan context must
    not raise, matching a real ASGI server's startup sequence.
    """
    monkeypatch.setattr(main_module.settings, "temporal_enabled", True)
    monkeypatch.setattr(main_module, "Client", _FailingTemporalClient)

    with TestClient(main_module.app) as client:
        assert main_module.app.state.temporal_client is None
        # A non-Temporal-dependent route stays up during the outage.
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json()["status"] == "UP"


def test_readiness_reports_temporal_down_during_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DoD 1: once `/health/ready` can actually execute (it couldn't before
    -- the process never got that far), it must genuinely report the
    degraded state rather than a stale/optimistic value.
    """
    monkeypatch.setattr(main_module.settings, "temporal_enabled", True)
    monkeypatch.setattr(main_module, "Client", _FailingTemporalClient)

    with TestClient(main_module.app) as client:
        ready = client.get("/health/ready")
        assert ready.json()["dependencies"]["temporal"] == "DOWN"


def test_temporal_connect_is_bounded_by_timeout_not_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit finding was "no timeout" specifically, not only "no
    try/except" -- a connect that hangs (network partition) rather than
    raising immediately must still be bounded, or startup would simply hang
    instead of crashing. Proven by a fake `connect()` that sleeps for 30s
    while `temporal_connect_timeout_seconds` is 0.2s: startup must return
    in well under 30s.
    """
    monkeypatch.setattr(main_module.settings, "temporal_enabled", True)
    monkeypatch.setattr(main_module, "Client", _HangingTemporalClient)

    started = time.monotonic()
    with TestClient(main_module.app):
        elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"startup took {elapsed}s -- the connect timeout was not enforced"
    assert main_module.app.state.temporal_client is None


def test_readiness_recovers_to_up_once_reconnect_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DoD 2: the background reconnect loop started by `lifespan` after a
    failed startup connect must actually publish the recovered client onto
    `app.state.temporal_client`, and `/health/ready` (which reads that same
    attribute) must flip back to `temporal: UP` on its own, with no restart.
    """
    monkeypatch.setattr(main_module.settings, "temporal_enabled", True)
    monkeypatch.setattr(main_module.settings, "temporal_reconnect_interval_seconds", 0.05)
    monkeypatch.setattr(main_module, "Client", _FlakyThenOkTemporalClient())

    with TestClient(main_module.app) as client:
        # Startup hit the (first, failing) call -- degraded on entry.
        assert main_module.app.state.temporal_client is None
        assert client.get("/health/ready").json()["dependencies"]["temporal"] == "DOWN"

        # Give the background reconnect loop (0.05s interval) a few cycles
        # to run its (now-succeeding) retry and publish the new client.
        deadline = time.monotonic() + 3.0
        while main_module.app.state.temporal_client is None and time.monotonic() < deadline:
            time.sleep(0.05)

        assert main_module.app.state.temporal_client is not None
        assert client.get("/health/ready").json()["dependencies"]["temporal"] == "UP"


def test_temporal_reconnect_task_is_cancelled_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors `_audit_archive_loop`'s existing shutdown-cancellation
    contract: a still-retrying reconnect loop must not be left dangling
    (and logging warnings forever) after the app itself has shut down.
    """
    monkeypatch.setattr(main_module.settings, "temporal_enabled", True)
    monkeypatch.setattr(main_module, "Client", _FailingTemporalClient)

    with TestClient(main_module.app):
        task = main_module.app.state.temporal_reconnect_task
        assert task is not None
        assert not task.done()

    assert task.cancelled() or task.done()
