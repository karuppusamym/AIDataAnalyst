"""R11-MP24: bounded connection pools for governed PostgreSQL reads.

Every governed read used to open two connections -- one for the EXPLAIN cost
gate, one to execute -- and close both, so a question paid two TCP, TLS and
authentication round trips to the bank's database. The execution path now
borrows from a small pool per source.

* **Keyed by a digest of the DSN and the command timeout.** A credential
  rotation or a changed DSN is a different key, so no connection opened with an
  old credential is ever reused; the old pool's connections close as they idle
  out. The DSN itself is never kept as a key or logged.
* **Bounded.** At most `POOL_MAX_SIZE` connections per source, none held open
  while idle for longer than `POOL_IDLE_SECONDS`, and at most `MAX_POOLS` pools
  per process (the least recently used is closed past that).
* **Loop-bound**, like every asyncpg pool: one registry per running event loop.
* **Clean on every borrow.** asyncpg resets a connection when it returns to the
  pool (`RESET ALL`, closes cursors, drops listeners and advisory locks), and
  the connector still opens a read-only transaction with a statement timeout
  for each statement it runs.

Discovery, profiling and the connection test keep opening their own connection:
they run rarely, and a pool would only hold connections open for them.
"""

from __future__ import annotations

import asyncio
import hashlib
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import asyncpg

POOL_MAX_SIZE: Final = 8
POOL_IDLE_SECONDS: Final = 60.0
MAX_POOLS: Final = 64
ACQUIRE_TIMEOUT_SECONDS: Final = 30.0

_BY_LOOP: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, OrderedDict[str, asyncpg.Pool[Any]]
] = weakref.WeakKeyDictionary()


def _key(dsn: str, command_timeout: float) -> str:
    return hashlib.sha256(f"{command_timeout}\x00{dsn}".encode()).hexdigest()


async def _pool(dsn: str, command_timeout: float) -> asyncpg.Pool[Any]:
    loop = asyncio.get_running_loop()
    pools = _BY_LOOP.get(loop)
    if pools is None:
        pools = _BY_LOOP[loop] = OrderedDict()
    key = _key(dsn, command_timeout)
    pool = pools.get(key)
    if pool is not None and not pool.is_closing():
        pools.move_to_end(key)
        return pool
    pool = await asyncpg.create_pool(
        dsn,
        min_size=0,
        max_size=POOL_MAX_SIZE,
        max_inactive_connection_lifetime=POOL_IDLE_SECONDS,
        command_timeout=command_timeout,
    )
    pools[key] = pool
    while len(pools) > MAX_POOLS:
        _, evicted = pools.popitem(last=False)
        await evicted.close()
    return pool


@asynccontextmanager
async def borrow(dsn: str, *, command_timeout: float) -> AsyncIterator[asyncpg.Connection[Any]]:
    """One pooled connection for the block, returned (and reset) afterwards."""
    pool = await _pool(dsn, command_timeout)
    async with pool.acquire(timeout=ACQUIRE_TIMEOUT_SECONDS) as connection:
        yield connection


def pool_count() -> int:
    """Pools open on this loop (tests and diagnostics)."""
    try:
        return len(_BY_LOOP.get(asyncio.get_running_loop(), ()))
    except RuntimeError:
        return 0


async def close_postgres_pools() -> None:
    """Close this loop's pools (process shutdown)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for pool in (_BY_LOOP.pop(loop, None) or OrderedDict()).values():
        await pool.close()
