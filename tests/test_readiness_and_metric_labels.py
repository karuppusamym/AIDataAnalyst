"""F17 + F18 -- bounded metric cardinality and bounded readiness probes.

F17: `/metrics`' `path` label fell back to the raw URL for any request that
matched no route, so distinct 404 URLs minted distinct Prometheus series --
a memory-growth vector reachable by anyone who can reach the port.

F18: `/health/ready` declared Temporal UP whenever the startup client object
existed. An object's existence is not a connectivity check. Background task
health and projection lag were not represented, and every dependency gated the
verdict equally, so an optional subsystem being down reported a total outage.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida import main as main_module
from aida.db import Base
from aida.readiness import (
    DOWN,
    NOT_CONFIGURED,
    UP,
    evaluate_readiness,
    probe_background_task,
    probe_postgresql,
    probe_temporal,
    reset_last_success,
)
from atlas.platform.config import Settings

# ---------------------------------------------------------------------------
# F17: unmatched paths collapse onto one metric series
# ---------------------------------------------------------------------------


def _path_labels() -> set[str]:
    """Every distinct `path` label currently present on the request counter."""
    return {
        sample.labels["path"]
        for metric in main_module.REQUEST_COUNT.collect()
        for sample in metric.samples
    }


def test_many_distinct_unknown_urls_produce_one_series_not_n() -> None:
    """The property, stated as the review states it: series count must not grow
    proportionally with the number of distinct unknown URLs tried.

    `TestClient` is used without its context manager on purpose -- these
    requests must not need application startup (no Temporal, no database); the
    middleware under test runs either way.
    """
    client = TestClient(main_module.app)
    before = _path_labels()

    for index in range(40):
        assert client.get(f"/definitely-not-a-route/{index}/{'x' * index}").status_code == 404

    added = _path_labels() - before
    assert added == {main_module.UNMATCHED_PATH_LABEL}, (
        f"40 distinct unknown URLs added {len(added)} metric label values: {sorted(added)}. "
        "Unmatched paths must collapse onto one constant label."
    )


def test_matched_routes_still_report_their_own_template() -> None:
    """The fix must not blunt the metric for real traffic -- a matched route
    keeps its own path template, which is the whole value of the label.
    """
    client = TestClient(main_module.app)
    client.get("/health/live")
    assert "/health/live" in _path_labels()


# ---------------------------------------------------------------------------
# F18: probes are real and bounded
# ---------------------------------------------------------------------------


class _SlowSession:
    """A session factory whose connection never arrives -- a partition, not a
    refusal. This is the case a timeout exists for; without one the readiness
    endpoint simply hangs and an orchestrator learns nothing.
    """

    async def __aenter__(self) -> _SlowSession:
        await asyncio.sleep(30)
        raise AssertionError("the probe should have timed out long before this")

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FailingSession:
    async def __aenter__(self) -> _FailingSession:
        raise ConnectionError("db unreachable (test)")

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _HealthyTemporalService:
    def __init__(self, healthy: bool) -> None:
        self._healthy = healthy

    async def check_health(self) -> bool:
        return self._healthy


class _TemporalClient:
    """A client that exposes the real `service_client.check_health` surface."""

    def __init__(self, healthy: bool) -> None:
        self.service_client = _HealthyTemporalService(healthy)


class _HangingTemporalClient:
    class _Service:
        async def check_health(self) -> bool:
            await asyncio.sleep(30)
            raise AssertionError("the probe should have timed out long before this")

    def __init__(self) -> None:
        self.service_client = self._Service()


@pytest_asyncio.fixture
async def sqlite_sessions() -> AsyncIterator[Any]:
    """A working session factory backed by in-memory SQLite, so the postgres,
    outbox-backlog and workspace-posture probes all have something real to
    query without standing up a database.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_postgres_probe_is_bounded_by_its_timeout(sqlite_sessions: Any) -> None:
    started = time.monotonic()
    result = await probe_postgresql(timeout_seconds=0.1, session_factory=_SlowSession)
    elapsed = time.monotonic() - started

    assert result.state == DOWN
    assert result.detail is not None and "timeout" in result.detail
    assert elapsed < 5.0, f"probe took {elapsed}s -- the timeout was not enforced"


async def test_postgres_probe_reports_down_rather_than_raising(sqlite_sessions: Any) -> None:
    """A readiness endpoint that 500s tells an orchestrator less than one that
    reports DOWN, so no probe failure is allowed to escape.
    """
    result = await probe_postgresql(timeout_seconds=1.0, session_factory=_FailingSession)
    assert result.state == DOWN
    assert result.required is True


async def test_temporal_probe_calls_the_health_rpc_not_just_object_existence() -> None:
    """The finding, directly: a client object that exists but whose server is
    not serving must report DOWN.
    """
    down = await probe_temporal(_TemporalClient(healthy=False), timeout_seconds=1.0, enabled=True)
    up = await probe_temporal(_TemporalClient(healthy=True), timeout_seconds=1.0, enabled=True)

    assert down.state == DOWN
    assert up.state == UP
    assert up.detail == "check_health"


async def test_temporal_probe_is_bounded_when_the_health_rpc_hangs() -> None:
    started = time.monotonic()
    result = await probe_temporal(_HangingTemporalClient(), timeout_seconds=0.1, enabled=True)
    elapsed = time.monotonic() - started

    assert result.state == DOWN
    assert elapsed < 5.0


async def test_temporal_probe_says_so_when_it_can_only_check_existence() -> None:
    """A weaker check is acceptable; claiming a connectivity check that was not
    performed is not. The degraded path must be visible in the detail.
    """
    result = await probe_temporal(object(), timeout_seconds=1.0, enabled=True)
    assert result.state == UP
    assert result.detail is not None and "existence check only" in result.detail


async def test_disabled_temporal_is_not_configured_rather_than_up() -> None:
    result = await probe_temporal(None, timeout_seconds=1.0, enabled=False)
    assert result.state == NOT_CONFIGURED


def test_background_task_probe_distinguishes_never_started_from_died() -> None:
    """`_audit_archive_loop`'s own docstring names a sweep that raised and
    vanished as the failure mode to catch; until F18 nothing reported it.
    """

    class _Done:
        def done(self) -> bool:
            return True

        def exception(self) -> Exception:
            return RuntimeError("archive loop died")

    class _Running:
        def done(self) -> bool:
            return False

    assert probe_background_task("t", None).state == NOT_CONFIGURED
    assert probe_background_task("t", _Running()).state == UP
    died = probe_background_task("t", _Done())
    assert died.state == DOWN
    assert died.detail is not None and "RuntimeError" in died.detail


# ---------------------------------------------------------------------------
# F18: required vs optional -- an optional outage is not a universal one
# ---------------------------------------------------------------------------


async def test_temporal_outage_does_not_make_the_api_unready(sqlite_sessions: Any) -> None:
    """AU-12 already made the app start and serve through a Temporal outage.
    Reporting a universal outage for it would contradict that behaviour --
    which is the "optional subsystem failures reported separately" half of F18.
    """
    reset_last_success()
    report = await evaluate_readiness(
        Settings(temporal_enabled=True),
        temporal_client=None,
        background_tasks={},
        session_factory=sqlite_sessions,
    )

    assert report.status == UP
    assert report.optional["temporal"] == DOWN
    assert report.required == {"postgresql": UP}
    assert report.dependencies["temporal"] == DOWN


async def test_database_outage_does_make_the_api_unready() -> None:
    reset_last_success()
    report = await evaluate_readiness(
        Settings(temporal_enabled=False),
        temporal_client=None,
        background_tasks={},
        session_factory=_FailingSession,
    )

    assert report.status == DOWN
    assert report.required["postgresql"] == DOWN


async def test_report_carries_lag_and_staleness_signals(sqlite_sessions: Any) -> None:
    """ "UP" and "UP, last succeeded never" are different claims. The report has
    to be able to make both, and to carry the outbox backlog this deployment
    uses as its projection-lag signal.
    """
    reset_last_success()
    report = await evaluate_readiness(
        Settings(temporal_enabled=False),
        temporal_client=None,
        background_tasks={},
        session_factory=sqlite_sessions,
    )

    assert report.signals["postgresql.last_success_age_seconds"] != "never"
    assert report.signals["temporal.last_success_age_seconds"] == "never"
    assert report.signals["outbox_backlog.detail"] == "pending=0"
    assert "workspace_authorization.declared" in report.signals


async def test_readiness_reports_the_enforcement_control_alongside_dependencies(
    sqlite_sessions: Any,
) -> None:
    """F11's operator-facing signal: `controls` answers "is this enforcing",
    which `dependencies` (reachability) structurally cannot.
    """
    reset_last_success()
    report = await evaluate_readiness(
        Settings(temporal_enabled=False),
        temporal_client=None,
        background_tasks={},
        session_factory=sqlite_sessions,
    )
    assert report.controls["workspace_authorization"] == "OBSERVING"


async def test_readiness_reports_scope_unresolved_when_the_inventory_is_unreadable() -> None:
    reset_last_success()
    report = await evaluate_readiness(
        Settings(temporal_enabled=False),
        temporal_client=None,
        background_tasks={},
        session_factory=_FailingSession,
    )
    assert report.controls["workspace_authorization"] == "SCOPE_UNRESOLVED"


def test_liveness_stays_dependency_free() -> None:
    """Liveness answers "restart this process". A liveness check that consults
    a database turns a database outage into a rolling restart of every replica,
    so it must not acquire dependencies as readiness gains them.
    """
    client = TestClient(main_module.app)
    body = client.get("/health/live")
    assert body.status_code == 200
    assert body.json()["status"] == "UP"
    assert body.json()["dependencies"] == {}
