"""Per-line-of-business concurrency control for query execution (QG-3).

Under contention -- many concurrent queries landing on the same data source, or
on the gateway as a whole -- an unbounded dispatcher lets whichever line of
business submits the most concurrent work monopolize the shared resource,
starving every other LOB out. This module is the fairness control: a bounded
number of concurrently in-flight executions per LOB, checked by
``QueryExecutionGateway.execute`` (``aida.query_gateway``) immediately before
the real source is asked to run the statement -- the actual contended
resource, not the (cheap, EXPLAIN-only) validation pass that runs ahead of it.

**Which "LOB" is this keyed by?** ``SecurityContext`` (the caller) carries no
LOB dimension -- confirmed absent from ``aida.security_types.SecurityContext``
and from every request header ``aida.security.get_security_context`` reads;
this platform's principal/tenancy model stops at ``organization_id``. The
dimension it actually carries end-to-end for a query *execution* is the
datasource's LOB: ``DataSource.line_of_business_id`` is a mandatory
(non-nullable) column (ADR-0018), and ``aida.cost_showback`` already treats it
as the authoritative per-LOB grouping key for the very same ``QueryExecution``
rows this module throttles the creation of. Keying the concurrency controller
the same way means "fair under contention" and "who consumed what" agree on
what a LOB's share of the gateway even means, instead of inventing a second,
disagreeing notion of tenancy.

**In-process, not cross-replica.** ``Docs/10-architecture/09-deployment-
topology.md`` names ``atlas-api`` as "N replicas behind a load balancer" -- a
purely in-process bound only holds *per replica*, so the platform-wide
concurrent-per-LOB total in production can reach (replica count x this
limit), not this limit alone. ``redis`` is already a real, configured
dependency (``pyproject.toml``, ``Settings.redis_url``) and is already used
for the close-cousin problem of per-consumer rate limiting
(``aida.mcp_budget``, wired into ``mcp_server.py``'s live tool-call path) --
a Redis-backed version of this controller, atomically checked the same way
``mcp_budget`` atomically increments, would be the natural next step for a
cross-replica-correct bound. It is deliberately not built here: this module's
fairness has to be provable by a test that runs with no external services
(this repository's whole test suite runs with no live Redis/Postgres/Neo4j --
see ``tests/support/doubles.py``'s module docstring), and a distributed
limiter's correctness -- especially its crash-safety, when a replica dies
mid-execution while holding a slot -- is not something an in-process test can
prove. Shipping an unverified distributed version would be a worse
deliverable than an honestly-scoped, fully-tested in-process one. Stated as
an open follow-up on QG-3's tracker row rather than silently left implicit.

**Reject vs. queue.** A request past its LOB's limit is not rejected on the
spot, and it does not queue forever either: it waits, bounded by
``Settings.query_gateway_lob_queue_timeout_seconds``, for a slot another
in-flight execution from the same LOB releases -- ordinary head-of-line
contention *within* one LOB's own quota resolves itself this way without the
caller seeing an error. Only a wait that outlives the bound raises
``LobConcurrencyDenied``, a clear, distinguishable refusal
(``aida.query_gateway.LobConcurrencyRejected`` wraps it into the gateway's
existing ``QueryRejected`` vocabulary, the same shape
``AuthorizationRejected`` already uses for ``AuthorizationDenied``) rather
than a silently-growing queue.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog

from atlas.platform.config import Settings

logger = structlog.get_logger(__name__)


class LobConcurrencyDenied(RuntimeError):
    """A LOB stayed at its concurrency limit longer than the configured bound.

    Carries ``lob_key``/``limit``/``waited_seconds`` rather than just a
    message so a caller (``aida.query_gateway``) can fold them into its own
    audited rejection detail without re-parsing a string.
    """

    def __init__(self, lob_key: str, *, limit: int, waited_seconds: float) -> None:
        super().__init__(
            f"line of business {lob_key} exceeded its concurrency limit of {limit} "
            f"(waited {waited_seconds:.2f}s for a free slot)"
        )
        self.lob_key = lob_key
        self.limit = limit
        self.waited_seconds = waited_seconds


class LobConcurrencyController:
    """A bounded number of concurrently-held slots per LOB key, in-process.

    One ``asyncio.Semaphore(default_max_concurrent)`` per LOB key, created
    lazily on first use and kept for the life of the controller so state
    persists across calls -- the entire point of "concurrent" is comparing
    in-flight work across *different* calls, which a semaphore recreated
    fresh per call could never do. ``resolve_lob_concurrency_controller``
    below is what makes "life of the controller" span the whole process
    rather than one request.
    """

    def __init__(self, *, default_max_concurrent: int, queue_timeout_seconds: float) -> None:
        if default_max_concurrent < 1:
            raise ValueError("default_max_concurrent must be at least 1")
        if queue_timeout_seconds <= 0:
            raise ValueError("queue_timeout_seconds must be positive")
        self.default_max_concurrent = default_max_concurrent
        self.queue_timeout_seconds = queue_timeout_seconds
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._in_flight: dict[str, int] = {}

    def _semaphore_for(self, lob_key: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(lob_key)
        if semaphore is None:
            # No `await` between the `get` above and this assignment, so this is
            # race-free under asyncio's single-threaded cooperative scheduling --
            # two concurrent callers racing on the same brand-new `lob_key` still
            # only ever run this line one at a time.
            semaphore = asyncio.Semaphore(self.default_max_concurrent)
            self._semaphores[lob_key] = semaphore
        return semaphore

    def in_flight(self, lob_key: str) -> int:
        """Executions from ``lob_key`` currently holding a slot (tests/observability)."""
        return self._in_flight.get(lob_key, 0)

    async def acquire(self, lob_key: str) -> None:
        """Hold one of ``lob_key``'s slots, waiting up to the configured bound.

        Raises ``LobConcurrencyDenied`` rather than waiting indefinitely once
        that bound is exceeded -- see the module docstring's "Reject vs.
        queue" note.
        """
        semaphore = self._semaphore_for(lob_key)
        started = time.monotonic()
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=self.queue_timeout_seconds)
        except TimeoutError as exc:
            waited = time.monotonic() - started
            logger.warning(
                "lob_concurrency.limit_exceeded",
                lob_key=lob_key,
                limit=self.default_max_concurrent,
                waited_seconds=round(waited, 3),
            )
            raise LobConcurrencyDenied(
                lob_key, limit=self.default_max_concurrent, waited_seconds=waited
            ) from exc
        self._in_flight[lob_key] = self._in_flight.get(lob_key, 0) + 1

    def release(self, lob_key: str) -> None:
        semaphore = self._semaphores.get(lob_key)
        if semaphore is None:  # pragma: no cover - defensive, cannot happen after acquire
            return
        self._in_flight[lob_key] = max(self._in_flight.get(lob_key, 0) - 1, 0)
        semaphore.release()

    @asynccontextmanager
    async def slot(self, lob_key: str) -> AsyncIterator[None]:
        """Acquire one of ``lob_key``'s slots for the duration of the block."""
        await self.acquire(lob_key)
        try:
            yield
        finally:
            self.release(lob_key)


# Cached by the two config values themselves (the same shape as
# `aida.security._oidc_verifiers`'s cache-by-config-tuple), not by `id(settings)`:
# every real `Settings` instance in a running process is already the same object
# (`get_settings`'s `lru_cache`), but tests routinely construct their own
# `Settings(...)`. Per-value caching is what lets two tests configured with
# distinct limits get distinct, non-interfering controllers while every real
# request sharing one configuration also shares one set of semaphores -- the
# property this whole module exists to provide.
_controllers: dict[tuple[int, float], LobConcurrencyController] = {}


def resolve_lob_concurrency_controller(settings: Settings) -> LobConcurrencyController:
    """The process-wide controller for ``settings``'s configured limit/timeout.

    `QueryExecutionGateway` is constructed fresh per call site (`tool_api.py`,
    `mcp_server.py`, `api.py`, `agent_orchestrator.py`, `sql_validation_api.py`
    each build their own) rather than once at startup, so storing the semaphore
    registry as a plain instance attribute would give every request its own,
    always-empty registry and enforce nothing. Resolving through this
    module-level cache instead is what makes the bound real across requests.
    """
    key = (
        settings.query_gateway_lob_max_concurrent,
        settings.query_gateway_lob_queue_timeout_seconds,
    )
    controller = _controllers.get(key)
    if controller is None:
        controller = LobConcurrencyController(
            default_max_concurrent=key[0], queue_timeout_seconds=key[1]
        )
        _controllers[key] = controller
    return controller
