"""R11-MP25: a per-source bound on concurrent queries, held across replicas.

`aida.lob_concurrency` (QG-3) keeps one line of business from starving the
others, but only inside one process, and it bounds a line of business rather
than a database. With N API replicas, N times its limit can land on one bank
database, and nothing stops a single source taking all of them. This bound is
per datasource and lives in Redis, so every replica counts the same slots.

**How a slot is held.** A Redis sorted set per datasource; each member is one
in-flight query, scored by the moment its lease runs out. Acquiring is one Lua
call -- drop expired leases, count, add if below the limit -- so two replicas
cannot both take the last slot, and the time comes from Redis (`TIME`), not
from replicas whose clocks may disagree. The lease is the query timeout plus a
margin: a query cannot outlive its own timeout, so a slot held by a replica
that died mid-query frees itself when the lease runs out, with no reaper.

**Waiting.** A query past the limit waits, polling with a short backoff, up to
`source_query_queue_timeout_seconds`, then is refused with
`SourceConcurrencyDenied` -- the same bounded wait QG-3 uses.

**When Redis is unreachable.** Staging and production refuse the query (a
bound that cannot be counted is not granted); every other environment runs it
unbounded, so a developer without Redis is not locked out. Both are counted
in `aida_source_query_slots_total` and logged. Off by default
(`source_query_concurrency_enabled`): it needs the Redis service.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Final, cast
from uuid import UUID

import structlog
from prometheus_client import Counter
from redis.exceptions import RedisError

from aida.outbound_clients import shared_redis
from atlas.platform.config import Settings

logger = structlog.get_logger(__name__)

#: Margin on top of the query timeout before an unreleased slot expires.
LEASE_MARGIN_SECONDS: Final = 30
_POLL_FIRST_SECONDS: Final = 0.05
_POLL_MAX_SECONDS: Final = 0.25

_ACQUIRE = """
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[1]) then
  redis.call('ZADD', KEYS[1], now_ms + tonumber(ARGV[2]), ARGV[3])
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
  return 1
end
return 0
"""

SOURCE_QUERY_SLOTS = Counter(
    "aida_source_query_slots_total",
    (
        "Per-source query slot decisions (R11-MP25), by closed-set outcome: "
        "ACQUIRED at once, WAITED then acquired, REFUSED after the wait bound, "
        "UNBOUNDED when Redis was unreachable outside staging and production, "
        "UNAVAILABLE when it was unreachable where the query is refused."
    ),
    labelnames=("outcome",),
)


class SourceConcurrencyDenied(RuntimeError):
    """No slot for this source within the wait bound, or no store to count it."""

    def __init__(
        self, datasource_id: UUID, *, limit: int, waited_seconds: float, unavailable: bool = False
    ) -> None:
        reason = (
            "the concurrency store is unreachable"
            if unavailable
            else f"its limit of {limit} concurrent queries held for {waited_seconds:.2f}s"
        )
        super().__init__(f"source {datasource_id} refused a query: {reason}")
        self.datasource_id = datasource_id
        self.limit = limit
        self.waited_seconds = waited_seconds
        self.unavailable = unavailable


def source_limit(settings: Settings, datasource_id: UUID) -> int:
    """The limit for this source: its override where one is set, else the default."""
    return settings.source_query_max_concurrent_overrides.get(
        str(datasource_id), settings.source_query_max_concurrent
    )


def _key(datasource_id: UUID) -> str:
    return f"aida:source-slots:{datasource_id}"


def _lease_ms(settings: Settings) -> int:
    return int((settings.query_timeout_seconds + LEASE_MARGIN_SECONDS) * 1000)


async def _try_acquire(settings: Settings, key: str, limit: int, token: str) -> bool:
    client = shared_redis(settings.redis_url)
    taken = await cast(
        Awaitable[object],
        client.eval(_ACQUIRE, 1, key, str(limit), str(_lease_ms(settings)), token),
    )
    return int(str(taken)) == 1


async def _release(settings: Settings, key: str, token: str) -> None:
    try:
        await cast(Awaitable[object], shared_redis(settings.redis_url).zrem(key, token))
    except RedisError as exc:
        # The lease expires on its own; a failed release only holds the slot longer.
        logger.warning("source_query_slot_release_failed", error=type(exc).__name__)


async def _wait_for_slot(
    settings: Settings, datasource_id: UUID, key: str, limit: int, token: str
) -> None:
    started = time.monotonic()
    deadline = started + settings.source_query_queue_timeout_seconds
    delay = _POLL_FIRST_SECONDS
    waited = False
    while not await _try_acquire(settings, key, limit, token):
        waited = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            SOURCE_QUERY_SLOTS.labels(outcome="REFUSED").inc()
            elapsed = time.monotonic() - started
            logger.warning(
                "source_query_slot_refused",
                datasource_id=str(datasource_id),
                limit=limit,
                waited_seconds=round(elapsed, 3),
            )
            raise SourceConcurrencyDenied(datasource_id, limit=limit, waited_seconds=elapsed)
        await asyncio.sleep(min(delay * (0.5 + random.random()), remaining))  # noqa: S311
        delay = min(delay * 2, _POLL_MAX_SECONDS)
    SOURCE_QUERY_SLOTS.labels(outcome="WAITED" if waited else "ACQUIRED").inc()


@asynccontextmanager
async def source_query_slot(settings: Settings, datasource_id: UUID) -> AsyncIterator[None]:
    """Hold one of this source's slots for the block, across every replica."""
    if not settings.source_query_concurrency_enabled:
        yield
        return
    limit = source_limit(settings, datasource_id)
    key = _key(datasource_id)
    token = uuid.uuid4().hex
    try:
        await _wait_for_slot(settings, datasource_id, key, limit, token)
        held = True
    except RedisError as exc:
        fail_closed = settings.environment in {"staging", "production"}
        SOURCE_QUERY_SLOTS.labels(outcome="UNAVAILABLE" if fail_closed else "UNBOUNDED").inc()
        logger.warning(
            "source_query_slot_store_unavailable",
            datasource_id=str(datasource_id),
            error=type(exc).__name__,
            refused=fail_closed,
        )
        if fail_closed:
            raise SourceConcurrencyDenied(
                datasource_id, limit=limit, waited_seconds=0.0, unavailable=True
            ) from exc
        held = False
    if not held:
        yield
        return
    try:
        yield
    finally:
        await _release(settings, key, token)
