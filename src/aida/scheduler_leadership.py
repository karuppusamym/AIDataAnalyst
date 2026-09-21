"""R11-AUD04: at most one `fleet-scheduler` replica runs the periodic passes.

`run_scheduler` (`aida.workflows.scheduler`) is a loop over `run_scheduler_iteration`, which
makes 23 periodic calls (as of 2026-09-20) and then admits due scan policies. Only that last
step was safe to run in two replicas at once: `process_scan_policy` claims each due `ScanPolicy`
under a row lock and starts its workflow under a deterministic id. Most of the calls before it
rate-limit themselves with an in-process cadence tracker (`_last_run_at` in the reaper, the expiry
sweeps and the rest), and a tracker in one process cannot see another's, so a second replica ran
each of those passes again in every cadence window. The architecture documents said a leader is
elected and a standby takes over; nothing did either. This module is the election.

**The mechanism** is a PostgreSQL *session-level advisory lock*. Every replica repeatedly
calls `pg_try_advisory_lock(SCHEDULER_LEADER_LOCK_KEY)`; the database grants it to exactly
one session and refuses everyone else, without blocking. The replica that holds it is the
leader and the only one that runs `run_scheduler_iteration`; the rest are standbys that retry
every `STANDBY_RETRY_SECONDS`.

Why an advisory lock and not a lease row: it needs no table and no migration, and the server
releases it the moment the holding session ends, so a leader that crashes cannot leave a stale
lease behind for a standby to wait out. There is no heartbeat writer, and no comparison of
clocks between hosts, which is where lease rows usually go wrong.

**The lock lives on a connection of its own.** A session lock belongs to a PostgreSQL
backend, not to a transaction. Taken on a pooled connection it would go back into the pool
still held, be handed to some unrelated request, and be released -- or not -- whenever that
connection happened to be recycled, none of which the scheduler would be told about. So
`PostgresAdvisoryLockProvider` opens a private engine on `NullPool` (closing the connection
really closes the socket) and keeps one connection on it for as long as the replica leads. That
connection runs in AUTOCOMMIT: a default connection would sit `idle in transaction` for days
and hold back vacuum for the whole database. It costs the leader one connection on top of the
pooled budget in `Docs/10-architecture/13-connection-pool-and-worker-budgets.md`; a standby
holds one only for the moment each retry takes.

**It fails closed.** A replica leads only while a round trip on its lock connection has just
succeeded. `SchedulerLeadership.confirm` runs that check immediately before every iteration,
and every step -- connect, acquire, liveness check, unlock -- has a timeout, because a query
sent down a dead socket does not fail, it waits. A raised error, a timeout or an unexpected
answer all mean "not the leader"; nothing is ever assumed. A leader that loses its connection
stops starting passes, logs `scheduler_lost_leadership`, and goes back to retrying as a
standby.

**A pass that is running when leadership is lost is allowed to finish.** Passes write to the
database and to Temporal; interrupting one halfway leaves more to reconcile than letting it
end. The check happens before each iteration, so the overlap with a new leader is bounded by
one iteration. That bound is the honest limit of this design: an advisory lock is exclusion,
not fencing -- nothing stamps a term number into what a pass writes, so a leader whose session
was dropped while it was busy is not stopped from finishing what it started.

**Failover time** is the standby retry interval plus however long PostgreSQL takes to notice
the old session is gone. If the leader's process dies, the operating system closes its socket
and PostgreSQL sees that at once. If its host or network vanishes without closing anything,
PostgreSQL finds out only through TCP keepalives, which default to the operating system's
value (about two hours on Linux); set `tcp_keepalives_idle`, `tcp_keepalives_interval` and
`tcp_keepalives_count` on the server, or on the scheduler's role, to shorten that. The
direction of the failure is safe -- the isolated leader's own liveness check times out and it
stops -- but there is then no scheduler until the lock is released. No failover drill has been
run.

**What it does not do.** The passes' cadence trackers are per-process, so a standby that takes
over starts with empty ones and runs each rate-limited pass once immediately. The footprint
gauges that `run_footprint_metrics_pass` publishes come only from the leader's registry. The
connection must reach PostgreSQL directly or through a session-mode pooler: behind a
transaction-mode pooler (see the sizing note in
`Docs/10-architecture/13-connection-pool-and-worker-budgets.md`) the "session" is not stable
and the lock means nothing.

**Non-PostgreSQL dialects** -- SQLite, which the test suite uses -- have no cross-process lock
worth taking, so `SoleLeaderLockProvider` reports the replica as the sole leader. That is
exactly the behaviour before this module existed, and `default_lock_provider` logs that it was
chosen so nobody mistakes it for a guard.

**Who leads right now.** PostgreSQL reports a bigint advisory key in `pg_locks` with its high
half in `classid`, its low half in `objid` and `objsubid = 1`, so:

    SELECT l.pid, a.client_addr, a.backend_start
    FROM pg_locks l JOIN pg_stat_activity a USING (pid)
    WHERE l.locktype = 'advisory' AND l.objsubid = 1 AND l.granted
      AND l.classid = 1635019873 AND l.objid = 1936094051;

names the leader's connection. The provider is injectable (`LeaderLockProvider`) so the loop
in `run_scheduler` can be tested against a fake lock two replicas share.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from typing import Protocol

import structlog
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

_log = structlog.get_logger(__name__)

#: The advisory-lock key every scheduler replica contends for: the eight bytes `atlasfsc`
#: ("atlas fleet-scheduler") read as one big-endian integer. It is a fixed constant because two
#: replicas can only exclude each other if they name the same key, and it is a bigint (the top
#: bit is clear, so it is positive) because `pg_try_advisory_lock` takes one. Advisory keys
#: share one namespace per database; nothing else in this repository takes an advisory lock
#: under this value, and `PostgresAdvisoryLockProvider` takes a `key` so a test can contend for
#: a different one.
SCHEDULER_LEADER_LOCK_KEY = 0x61746C6173667363

#: How long a standby waits between attempts to take the lock. Short, because it is the
#: replica's contribution to failover time; cheap, because an attempt is one connect and one
#: query. It is a constant, not a setting: nothing about it is deployment-specific.
STANDBY_RETRY_SECONDS = 5.0

#: The bound on each round trip to PostgreSQL made on behalf of the lock (connect, acquire,
#: liveness, unlock, close). A query sent down a dead socket waits instead of failing, so
#: without this a network partition would leave the leader "checking" for as long as the
#: kernel takes to give up -- during which it would run passes it cannot vouch for.
LOCK_CALL_TIMEOUT_SECONDS = 5.0

# Constants, never built from input. `CAST(... AS bigint)` makes the parameter's type explicit
# so the driver need not infer it from a function that has both a one- and a two-argument form.
_TRY_LOCK = text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))")
_UNLOCK = text("SELECT pg_advisory_unlock(CAST(:key AS bigint))")
_LIVENESS = text("SELECT 1")


class LeaderLockProvider(Protocol):
    """One replica's handle on the single scheduler-leadership lock.

    Implementations must fail closed: `try_acquire` and `still_held` return `False` rather
    than raise when they cannot be sure, and `release` never raises. `SchedulerLeadership`
    treats an exception from any of them the same way, so a provider that does raise cannot
    make a replica a leader -- but a provider that returns `True` on a guess can.
    """

    async def try_acquire(self) -> bool:
        """Try, without blocking, to become the leader. True only if the lock is now held."""
        ...

    async def still_held(self) -> bool:
        """Verify by a live round trip -- not from memory -- that the lock is still held."""
        ...

    async def release(self) -> None:
        """Give the lock up and close whatever holds it. Safe to call when nothing is held."""
        ...


class SoleLeaderLockProvider:
    """The lock on a database with nothing to contend on: always granted, never lost.

    Used for non-PostgreSQL dialects, i.e. SQLite in tests and local runs, where a second
    scheduler process against the same database is not a supported deployment. It reproduces
    the behaviour from before leader election existed, on purpose and visibly.
    """

    async def try_acquire(self) -> bool:
        return True

    async def still_held(self) -> bool:
        return True

    async def release(self) -> None:
        return None


class PostgresAdvisoryLockProvider:
    """`pg_try_advisory_lock` on one dedicated, long-lived connection.

    Invariant: `self._connection` is set if and only if the lock was granted on it and it has
    not since failed a liveness check. Nothing else is ever remembered about being the leader,
    which is what keeps "no verified live lock connection, no leader" true by construction.
    """

    def __init__(
        self,
        database_url: str,
        *,
        key: int = SCHEDULER_LEADER_LOCK_KEY,
        call_timeout_seconds: float = LOCK_CALL_TIMEOUT_SECONDS,
    ) -> None:
        self._database_url = database_url
        self._key = key
        self._call_timeout = call_timeout_seconds
        self._engine: AsyncEngine | None = None
        self._connection: AsyncConnection | None = None

    def _lock_engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = create_async_engine(
                self._database_url,
                # Never the shared pool: a pooled connection goes back into it still holding
                # a session lock. With `NullPool`, closing the connection ends the session.
                poolclass=NullPool,
                # No transaction, ever: see the module docstring on `idle in transaction`.
                isolation_level="AUTOCOMMIT",
                # INV-6 / ADR-0014, the same reason `atlas.platform.db.get_engine` sets it.
                hide_parameters=True,
            )
        return self._engine

    async def try_acquire(self) -> bool:
        if self._connection is not None:
            # `pg_try_advisory_lock` is re-entrant within a session: asking again on the
            # connection that already holds it would succeed and stack a second hold, which
            # one unlock would then not release. Verify the hold instead.
            return await self.still_held()
        connection: AsyncConnection | None = None
        refused_cleanly = False
        try:
            connection = await asyncio.wait_for(
                self._lock_engine().connect(), timeout=self._call_timeout
            )
            granted = await asyncio.wait_for(
                connection.scalar(_TRY_LOCK, {"key": self._key}), timeout=self._call_timeout
            )
            if granted is True:
                self._connection = connection
                return True
            refused_cleanly = granted is False
            return False
        except Exception as exc:  # noqa: BLE001 -- fail closed: any error means "not the leader"
            _log.warning(
                "scheduler_leader_lock_error", phase="acquire", error_class=type(exc).__name__
            )
            return False
        finally:
            # A refused or half-finished attempt must not keep a connection open (or, if the
            # grant arrived and the reply did not, keep a lock nobody is tracking).
            if connection is not None and connection is not self._connection:
                await self._discard(connection, healthy=refused_cleanly)

    async def still_held(self) -> bool:
        connection = self._connection
        if connection is None:
            return False
        try:
            await asyncio.wait_for(connection.execute(_LIVENESS), timeout=self._call_timeout)
        except Exception as exc:  # noqa: BLE001 -- fail closed: any error means "lost"
            _log.warning(
                "scheduler_leader_lock_error", phase="liveness", error_class=type(exc).__name__
            )
            self._connection = None
            await self._discard(connection, healthy=False)
            return False
        return True

    async def release(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            unlocked = False
            try:
                await asyncio.wait_for(
                    connection.execute(_UNLOCK, {"key": self._key}), timeout=self._call_timeout
                )
                unlocked = True
            except Exception as exc:  # noqa: BLE001 -- closing the session drops it regardless
                _log.warning(
                    "scheduler_leader_lock_error", phase="release", error_class=type(exc).__name__
                )
            await self._discard(connection, healthy=unlocked)
        engine, self._engine = self._engine, None
        if engine is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(engine.dispose(), timeout=self._call_timeout)

    async def _discard(self, connection: AsyncConnection, *, healthy: bool) -> None:
        """End the session, gracefully if it answered and by force if it did not.

        `AsyncConnection.invalidate` is the forceful path: SQLAlchemy tries a two-second
        graceful close and falls back to terminating the socket, where a plain `close` would
        first send a ROLLBACK down a connection that may never answer. Either way the backend
        ends, which is what releases a session-level lock. Nothing here may raise.
        """
        if not healthy:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(connection.invalidate(), timeout=self._call_timeout)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(connection.close(), timeout=self._call_timeout)


def default_lock_provider(database_url: str) -> LeaderLockProvider:
    """The real provider for a deployment: PostgreSQL enforces, anything else does not."""
    dialect = make_url(database_url).get_backend_name()
    if dialect == "postgresql":
        return PostgresAdvisoryLockProvider(database_url)
    _log.warning(
        "scheduler_leadership_not_enforced",
        dialect=dialect,
        reason="no cross-process lock on this dialect; this replica is the sole leader",
    )
    return SoleLeaderLockProvider()


def _replica_id() -> str:
    """A label for one scheduler process in the log: `hostname:pid` (the container id, in a
    container). It is what lets a failover be read back out of the logs afterwards."""
    return f"{socket.gethostname()}:{os.getpid()}"


class SchedulerLeadership:
    """The election, as one replica sees it: `confirm()` once per tick, before a pass.

    Logs three lifecycle lines and nothing else per tick: `scheduler_standby` once for each
    stretch of standing by (not once per retry), `scheduler_became_leader` on acquiring, and
    `scheduler_lost_leadership` when a leader's check fails. After a loss the replica is a
    standby again and says so.
    """

    def __init__(self, provider: LeaderLockProvider, *, replica: str | None = None) -> None:
        self._provider = provider
        self._replica = replica or _replica_id()
        self._leader = False
        self._standby_announced = False

    @property
    def is_leader(self) -> bool:
        return self._leader

    async def confirm(self) -> bool:
        """True only if this replica is verifiably the leader at this moment.

        A leader is re-verified on every call. A replica that has just lost the lock tries to
        take it straight back in the same call: when the connection merely blipped and
        PostgreSQL has already dropped the old session, that resumes the passes a tick
        sooner, and when it has not, the attempt is refused and the replica is a standby.
        """
        if self._leader:
            if await self._still_held():
                return True
            self._leader = False
            self._standby_announced = False
            _log.warning("scheduler_lost_leadership", replica=self._replica)
        if await self._try_acquire():
            self._leader = True
            self._standby_announced = False
            _log.info("scheduler_became_leader", replica=self._replica)
            return True
        if not self._standby_announced:
            self._standby_announced = True
            _log.info("scheduler_standby", replica=self._replica)
        return False

    async def release(self) -> None:
        """Give the lock up on the way out. Idempotent, and never raises."""
        was_leader, self._leader = self._leader, False
        try:
            await self._provider.release()
        except Exception as exc:  # noqa: BLE001 -- shutdown must finish; the session dies anyway
            _log.warning(
                "scheduler_leader_lock_error",
                phase="release",
                error_class=type(exc).__name__,
                replica=self._replica,
            )
        if was_leader:
            _log.info("scheduler_leadership_released", replica=self._replica)

    async def _try_acquire(self) -> bool:
        try:
            return await self._provider.try_acquire() is True
        except Exception as exc:  # noqa: BLE001 -- fail closed
            _log.warning(
                "scheduler_leader_lock_error",
                phase="acquire",
                error_class=type(exc).__name__,
                replica=self._replica,
            )
            return False

    async def _still_held(self) -> bool:
        try:
            return await self._provider.still_held() is True
        except Exception as exc:  # noqa: BLE001 -- fail closed
            _log.warning(
                "scheduler_leader_lock_error",
                phase="liveness",
                error_class=type(exc).__name__,
                replica=self._replica,
            )
            return False
