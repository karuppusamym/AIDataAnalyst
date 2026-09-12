"""F18: bounded readiness probes with a required/optional dependency contract.

The finding: `/health/ready` actively queried PostgreSQL but declared Temporal
UP whenever `app.state.temporal_client` was a non-`None` object. An object's
existence is not a connectivity check -- a client whose server died an hour ago
is the same object it was when the server was healthy -- so readiness could
report a dependency as available long after it stopped being available.
Background task health and projection lag were not represented at all, and every
dependency gated the verdict equally, so an optional subsystem being down
reported a universal outage.

This module answers three questions the old endpoint conflated:

**Is the probe real, and is it bounded?** Every probe here does actual work
against the thing it names, and every probe runs under an explicit
`asyncio.wait_for`. A readiness endpoint that can hang is worse than one that
lies: an orchestrator waiting on a hung probe learns nothing at all, and the
probe timeout becomes whichever timeout the load balancer happens to have.
A timed-out probe is DOWN with `timeout` in its detail -- never UP, and never
a raised exception that turns the endpoint itself into a 500.

**Is this dependency required?** `required=True` means the process cannot serve
its purpose without it and readiness returns 503. PostgreSQL is the only one for
the API process: it is the system of record behind essentially every route.
Temporal is optional -- AU-12 already made the app start and serve degraded
through a Temporal outage, so reporting a universal outage for it would
contradict the behaviour that change deliberately built. Background tasks and
the outbox backlog are optional signals for the same reason: a stalled archive
sweep is an operational problem, not a reason to take the API out of rotation.

**How stale is "UP"?** Each successful probe stamps a last-success time, and the
report carries the age of that stamp alongside the current state, plus the
outbox backlog depth and the age of its oldest pending row (this deployment's
projection-lag signal -- the outbox is what the graph projector and publisher
consume). "UP, last succeeded 0.2s ago" and "UP, last succeeded never" are
different claims and the report makes both sayable.

The same question is asked of outbound delivery. `probe_delivery_backlog`
reports how many governance notifications and SIEM events are still owed to a
destination, how many have dead-lettered, and how old the oldest undelivered
one is -- so that a wedged delivery worker or a destination that has been
refusing all night is visible from `/health/ready` rather than only from the
worker's logs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from sqlalchemy import func, select, text

from aida import __version__
from aida.authorization_posture import (
    CONTROL_NAME as WORKSPACE_AUTHORIZATION_CONTROL,
)
from aida.authorization_posture import (
    PostureReport,
    evaluate_posture,
    unresolvable_posture,
)
from aida.db import session_factory as default_session_factory
from aida.delivery_intents import (
    KIND_NOTIFICATION,
    KIND_SIEM,
    STATE_DEAD_LETTER,
    STATE_DELIVERING,
    STATE_PENDING,
    STATE_RETRYING,
)
from aida.models import DeliveryIntent, OutboxEvent
from aida.schemas import HealthResponse
from atlas.platform.config import Settings

UP = "UP"
DOWN = "DOWN"
# The dependency is not part of this deployment's shape at all (Temporal
# disabled). Distinct from UP so nobody reads "we checked and it was fine" into
# "we did not check because it does not apply".
NOT_CONFIGURED = "NOT_CONFIGURED"

POSTGRESQL = "postgresql"
TEMPORAL = "temporal"
AUDIT_ARCHIVE_TASK = "audit_archive_task"
TEMPORAL_RECONNECT_TASK = "temporal_reconnect_task"
OUTBOX_BACKLOG = "outbox_backlog"
DELIVERY_BACKLOG = "delivery_backlog"

#: Delivery-intent states that still owe a destination something. `DELIVERING`
#: is here on purpose: a claimed intent has not been acknowledged, and a worker
#: that died mid-attempt leaves rows in exactly this state until the claim
#: expires -- which is the stall an operator most needs to see.
UNDELIVERED_STATES: tuple[str, ...] = (STATE_PENDING, STATE_RETRYING, STATE_DELIVERING)

#: Terminal failure. Never retried again, so it needs a human, not patience.
#: `DISCARDED` and `DUPLICATE` are deliberately not counted here: both are
#: correct, intended outcomes (nothing configured; an equivalent message
#: already delivered), and paging on them would train operators to ignore this.
FAILED_STATES: tuple[str, ...] = (STATE_DEAD_LETTER,)

#: Short names for the two kinds sharing the ledger, for the per-kind counts.
_KIND_LABELS: dict[str, str] = {KIND_NOTIFICATION: "notification", KIND_SIEM: "siem"}

# Wall-clock time of the last successful probe, per probe name. Process-local
# and deliberately not persisted: it answers "has this process seen the
# dependency work recently", which is exactly the question a restart should
# reset. Never used to *skip* a probe -- only to report staleness alongside a
# freshly measured state.
_last_success: dict[str, datetime] = {}


def reset_last_success() -> None:
    """Clear the last-success stamps. For tests; never called in production."""
    _last_success.clear()


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One dependency's freshly measured state."""

    name: str
    state: str
    required: bool
    detail: str | None = None
    duration_ms: float = 0.0

    @property
    def healthy(self) -> bool:
        """NOT_CONFIGURED counts as healthy: a dependency this deployment does
        not use cannot make it unready.
        """
        return self.state in (UP, NOT_CONFIGURED)


class ReadinessResponse(HealthResponse):
    """`/health/ready`'s body.

    A superset of `HealthResponse`: `dependencies` keeps its existing meaning
    (every probe, merged, name -> state) so existing consumers and tests are
    unaffected, and the new fields add the contract that was missing.
    `required`/`optional` say which probes actually gate the verdict, `controls`
    reports enforcement posture per control (F11) rather than reachability, and
    `signals` carries staleness and lag as strings.
    """

    required: dict[str, str] = {}
    optional: dict[str, str] = {}
    controls: dict[str, str] = {}
    signals: dict[str, str] = {}


async def _bounded(
    name: str,
    *,
    required: bool,
    timeout_seconds: float,
    probe: Callable[[], Awaitable[str | None]],
) -> ProbeResult:
    """Run one probe under an explicit timeout, converting every outcome into a
    `ProbeResult`.

    The probe coroutine returns an optional detail string on success and raises
    on failure. Nothing it can do -- raise, hang, return -- escapes this
    function, which is what lets the endpoint promise a bounded response
    regardless of what any single dependency is doing.
    """
    started = perf_counter()
    try:
        detail = await asyncio.wait_for(probe(), timeout=timeout_seconds)
    except TimeoutError:
        return ProbeResult(
            name=name,
            state=DOWN,
            required=required,
            detail=f"timeout after {timeout_seconds}s",
            duration_ms=(perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001 - every failure is a DOWN, by design
        return ProbeResult(
            name=name,
            state=DOWN,
            required=required,
            detail=f"{type(exc).__name__}: {exc}"[:200],
            duration_ms=(perf_counter() - started) * 1000,
        )
    _last_success[name] = datetime.now(UTC)
    return ProbeResult(
        name=name,
        state=UP,
        required=required,
        detail=detail,
        duration_ms=(perf_counter() - started) * 1000,
    )


async def probe_postgresql(
    *, timeout_seconds: float, session_factory: Callable[[], Any] = default_session_factory
) -> ProbeResult:
    """`SELECT 1` on a real connection. The API's only required dependency."""

    async def _run() -> str | None:
        async with session_factory() as session:
            await session.execute(text("SELECT 1"))
        return None

    return await _bounded(POSTGRESQL, required=True, timeout_seconds=timeout_seconds, probe=_run)


async def probe_temporal(
    client: object | None, *, timeout_seconds: float, enabled: bool
) -> ProbeResult:
    """A live health RPC against Temporal, not an `is not None` check.

    `ServiceClient.check_health` is the real gRPC health check; it is what makes
    this probe capable of reporting a client object whose server has since died.
    When the installed client exposes no such method (or a test double stands in
    for it), the probe degrades to the old existence check *and says so in
    `detail`* rather than silently claiming a connectivity check it did not
    perform -- an honest weaker claim, which is the whole point of F18.
    """
    if not enabled:
        return ProbeResult(
            name=TEMPORAL,
            state=NOT_CONFIGURED,
            required=False,
            detail="temporal_enabled=False",
        )
    if client is None:
        return ProbeResult(
            name=TEMPORAL,
            state=DOWN,
            required=False,
            detail="no client (startup connect failed or reconnect pending)",
        )

    service_client = getattr(client, "service_client", None)
    check_health = getattr(service_client, "check_health", None)
    if check_health is None:
        return ProbeResult(
            name=TEMPORAL,
            state=UP,
            required=False,
            detail="client present; no check_health on this client -- existence check only",
        )

    async def _run() -> str | None:
        healthy = await check_health()
        if not healthy:
            raise RuntimeError("temporal reported not serving")
        return "check_health"

    return await _bounded(TEMPORAL, required=False, timeout_seconds=timeout_seconds, probe=_run)


def probe_background_task(name: str, task: object | None) -> ProbeResult:
    """A background loop's health, read from the task object -- no I/O, no timeout.

    Three outcomes worth distinguishing: not started (the feature is off ->
    NOT_CONFIGURED), still running (UP), and finished (DOWN, with the exception
    if it died of one). A background sweep that raised and vanished is the exact
    failure mode `_audit_archive_loop`'s own docstring calls out, and until now
    nothing reported it.
    """
    if task is None:
        return ProbeResult(name=name, state=NOT_CONFIGURED, required=False, detail="not started")
    done = getattr(task, "done", None)
    if done is None or not done():
        return ProbeResult(name=name, state=UP, required=False, detail="running")
    exception = None
    try:
        exception = task.exception()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - cancelled/not-ready tasks raise here
        exception = None
    detail = f"stopped: {type(exception).__name__}" if exception else "stopped"
    return ProbeResult(name=name, state=DOWN, required=False, detail=detail)


async def probe_outbox_backlog(
    *,
    timeout_seconds: float,
    now: datetime | None = None,
    session_factory: Callable[[], Any] = default_session_factory,
) -> ProbeResult:
    """Projection lag: how many outbox rows are pending and how old the oldest is.

    Optional, and deliberately never DOWN for being *large* -- this module does
    not own the threshold at which a backlog is an incident. It reports the two
    numbers; alerting decides. It is DOWN only when the backlog could not be
    measured at all, which is itself information (and, in practice, arrives
    alongside a DOWN PostgreSQL probe).
    """
    detail_holder: dict[str, str] = {}

    async def _run() -> str | None:
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(func.count(), func.min(OutboxEvent.occurred_at)).where(
                        OutboxEvent.status == "PENDING"
                    )
                )
            ).one()
        pending = int(row[0] or 0)
        oldest: datetime | None = row[1]
        detail_holder["pending"] = str(pending)
        if oldest is not None:
            reference = now or datetime.now(UTC)
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=UTC)
            detail_holder["oldest_age_seconds"] = f"{(reference - oldest).total_seconds():.1f}"
        return f"pending={pending}"

    result = await _bounded(
        OUTBOX_BACKLOG, required=False, timeout_seconds=timeout_seconds, probe=_run
    )
    if not detail_holder:
        return result
    return replace(
        result,
        detail=";".join(f"{key}={value}" for key, value in sorted(detail_holder.items())),
    )


async def probe_delivery_backlog(
    settings: Settings,
    *,
    timeout_seconds: float,
    now: datetime | None = None,
    session_factory: Callable[[], Any] = default_session_factory,
) -> ProbeResult:
    """Outbound delivery lag: what is still owed, what died, and how stale it is.

    The delivery worker (`aida.delivery_intents.run_delivery_worker_pass`) is
    the only thing in the platform that opens a socket to Slack, Teams or a SOC
    collector, and it runs from the fleet scheduler. Until this probe existed,
    the only way to find out that it had stopped draining -- or that every
    attempt was being rejected -- was to read worker logs. A queue that is
    quietly not moving looks exactly like a queue that is empty from the
    outside, which is the failure this reports.

    **Age, not depth, is the signal.** A backlog of 900 draining normally is
    healthy; a backlog of 3 whose oldest row was requested yesterday means the
    worker is wedged, the scheduler is dead, or the destination has been
    refusing for a day. So `oldest_age_seconds` is measured from
    `requested_at` -- the timestamp that commits with the business decision --
    and reported alongside the counts rather than behind them.

    **`worker=` is reported because "off" is not "broken".** `delivery_worker_
    enabled` defaults to False, and a deployment that has deliberately not
    opted in accrues a growing, permanently un-drained backlog that is working
    as configured. Alerting that cannot tell that apart from a wedged worker
    would fire on every default install, so the state of the switch is part of
    the reading.

    Optional, and -- like `probe_outbox_backlog` -- never DOWN for being large:
    this module does not own the threshold at which a backlog is an incident.
    It is DOWN only when the backlog could not be measured at all.
    """
    detail_holder: dict[str, str] = {}
    tracked = (*UNDELIVERED_STATES, *FAILED_STATES)

    async def _run() -> str | None:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        DeliveryIntent.kind,
                        DeliveryIntent.state,
                        func.count(),
                        func.min(DeliveryIntent.requested_at),
                    )
                    .where(DeliveryIntent.state.in_(tracked))
                    .group_by(DeliveryIntent.kind, DeliveryIntent.state)
                )
            ).all()

        queued = 0
        failed = 0
        queued_by_kind: dict[str, int] = {}
        failed_by_kind: dict[str, int] = {}
        oldest: datetime | None = None
        for kind, state, count, earliest in rows:
            count = int(count or 0)
            label = _KIND_LABELS.get(kind, str(kind).lower())
            if state in FAILED_STATES:
                failed += count
                failed_by_kind[label] = failed_by_kind.get(label, 0) + count
                continue
            queued += count
            queued_by_kind[label] = queued_by_kind.get(label, 0) + count
            if earliest is not None:
                if earliest.tzinfo is None:
                    earliest = earliest.replace(tzinfo=UTC)
                oldest = earliest if oldest is None else min(oldest, earliest)

        detail_holder["queued"] = str(queued)
        detail_holder["failed"] = str(failed)
        detail_holder["worker"] = "enabled" if settings.delivery_worker_enabled else "disabled"
        for label, count in queued_by_kind.items():
            detail_holder[f"queued_{label}"] = str(count)
        # Split too: a dead-lettered SIEM security event and a dead-lettered
        # chat message need different people, and a single `failed` count
        # cannot say which one is sitting there.
        for label, count in failed_by_kind.items():
            detail_holder[f"failed_{label}"] = str(count)
        if oldest is not None:
            reference = now or datetime.now(UTC)
            detail_holder["oldest_age_seconds"] = f"{(reference - oldest).total_seconds():.1f}"
        return f"queued={queued}"

    result = await _bounded(
        DELIVERY_BACKLOG, required=False, timeout_seconds=timeout_seconds, probe=_run
    )
    if not detail_holder:
        return result
    return replace(
        result,
        detail=";".join(f"{key}={value}" for key, value in sorted(detail_holder.items())),
    )


async def probe_workspace_authorization_posture(
    settings: Settings,
    *,
    timeout_seconds: float,
    session_factory: Callable[[], Any] = default_session_factory,
) -> PostureReport:
    """F11's control state, bounded like every other probe.

    A failure to read the workspace inventory becomes `SCOPE_UNRESOLVED`, not
    `OBSERVING`: see `authorization_posture` for why those must not collapse.
    """

    async def _run() -> PostureReport:
        async with session_factory() as session:
            return await evaluate_posture(session, settings)

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_seconds)
    except TimeoutError:
        return unresolvable_posture(settings, detail=f"timeout after {timeout_seconds}s")
    except Exception as exc:  # noqa: BLE001 - any read failure is "cannot tell"
        return unresolvable_posture(settings, detail=f"{type(exc).__name__}: {exc}"[:120])


def _staleness_signals(names: tuple[str, ...], *, now: datetime) -> dict[str, str]:
    signals: dict[str, str] = {}
    for name in names:
        stamp = _last_success.get(name)
        signals[f"{name}.last_success_age_seconds"] = (
            "never" if stamp is None else f"{(now - stamp).total_seconds():.1f}"
        )
    return signals


async def _model_route_signals(settings: Settings) -> dict[str, str]:
    """R11-B16: which approved model routes the provider no longer serves.

    Reads the state the scheduled sweep recorded -- it makes **no** provider
    call, because a readiness scrape whose latency and cost a third party
    decides is not a readiness check. `never_checked` is reported separately
    from `unreachable` so an operator can tell "all good" from "nothing has
    looked yet", which is the distinction this signal exists to preserve.

    Failure is swallowed to a reason string rather than propagated: a route
    health summary must never be what takes readiness down, or the least
    important probe here becomes the most dangerous.
    """
    from aida.model_route_health import unreachable_route_summary

    try:
        summary = await unreachable_route_summary(settings)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        return {"model_routes.detail": f"unavailable: {type(exc).__name__}"}
    unreachable = summary["unreachable"]
    detail = (
        f"approved={summary['approved']};unreachable={len(unreachable)};"
        f"never_checked={summary['never_checked']};"
        f"sweep={'enabled' if summary['enabled'] else 'disabled'}"
    )
    signals = {"model_routes.detail": detail}
    if unreachable:
        signals["model_routes.unreachable"] = ", ".join(unreachable)
    return signals


async def evaluate_readiness(
    settings: Settings,
    *,
    temporal_client: object | None,
    background_tasks: dict[str, object | None],
    session_factory: Callable[[], Any] = default_session_factory,
    now: datetime | None = None,
) -> ReadinessResponse:
    """Run every probe and assemble the response.

    Probes run concurrently: the endpoint's worst case is then one timeout, not
    the sum of them, which is what keeps `readiness_probe_timeout_seconds`
    meaningful as *the* bound on this endpoint rather than a per-probe budget
    that silently multiplies as dependencies are added.
    """
    timeout_seconds = settings.readiness_probe_timeout_seconds
    reference = now or datetime.now(UTC)

    postgres, temporal, backlog, delivery, posture = await asyncio.gather(
        probe_postgresql(timeout_seconds=timeout_seconds, session_factory=session_factory),
        probe_temporal(
            temporal_client, timeout_seconds=timeout_seconds, enabled=settings.temporal_enabled
        ),
        probe_outbox_backlog(
            timeout_seconds=timeout_seconds, now=reference, session_factory=session_factory
        ),
        probe_delivery_backlog(
            settings,
            timeout_seconds=timeout_seconds,
            now=reference,
            session_factory=session_factory,
        ),
        probe_workspace_authorization_posture(
            settings, timeout_seconds=timeout_seconds, session_factory=session_factory
        ),
    )
    probes = [postgres, temporal, backlog, delivery]
    probes.extend(probe_background_task(name, task) for name, task in background_tasks.items())

    required = {probe.name: probe.state for probe in probes if probe.required}
    optional = {probe.name: probe.state for probe in probes if not probe.required}
    ready = all(probe.healthy for probe in probes if probe.required)

    signals = _staleness_signals((POSTGRESQL, TEMPORAL), now=reference)
    for probe in probes:
        if probe.detail:
            signals[f"{probe.name}.detail"] = probe.detail
        signals[f"{probe.name}.duration_ms"] = f"{probe.duration_ms:.1f}"
    signals.update(posture.as_signals())
    signals.update(await _model_route_signals(settings))

    return ReadinessResponse(
        status=UP if ready else DOWN,
        service=settings.service_name,
        version=__version__,
        dependencies={probe.name: probe.state for probe in probes},
        required=required,
        optional=optional,
        controls={WORKSPACE_AUTHORIZATION_CONTROL: posture.state},
        signals=signals,
    )
