"""R11-MP24: a bounded pool of idle SQL Server connections for governed execution.

python-tds is synchronous and has no connection reset, so this pool is deliberately narrow:

* **Execution only.** Only `execute_read_query` borrows. Its statements are `SELECT @@SPID`
  and the one SELECT the gateway's guard admitted, followed by a rollback -- none changes a
  session option. The EXPLAIN gate, which turns `SET SHOWPLAN_XML` on and off inside the
  session, keeps opening its own connection: a pooled one left in showplan mode after an
  error would answer the next query with a plan.
* **A connection that saw an error is never reused.** It is closed, not returned.
* **Keyed by a digest of the login and timeout**, so a rotated credential never borrows a
  connection opened with the old one, and the credential is never a key.
* **Bounded.** At most `POOL_MAX_IDLE` idle connections per source, none kept idle longer than
  `POOL_IDLE_SECONDS`, at most `MAX_POOLS` pools (least recently used closed first).

Thread-safe: borrows run on worker threads (`asyncio.to_thread`).
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any, Final

POOL_MAX_IDLE: Final = 8
POOL_IDLE_SECONDS: Final = 60.0
MAX_POOLS: Final = 64

_LOCK = threading.Lock()
_POOLS: OrderedDict[str, deque[tuple[Any, float]]] = OrderedDict()


def pool_key(*parts: object) -> str:
    return hashlib.sha256("\x00".join(str(part) for part in parts).encode()).hexdigest()


def _close(connection: Any) -> None:
    with suppress(Exception):
        connection.close()


def _take(key: str) -> Any | None:
    """A fresh idle connection for `key`, closing any that idled out."""
    now = time.monotonic()
    stale: list[Any] = []
    taken: Any | None = None
    with _LOCK:
        idle = _POOLS.get(key)
        if idle is not None:
            _POOLS.move_to_end(key)
            while idle:
                connection, since = idle.pop()
                if now - since <= POOL_IDLE_SECONDS:
                    taken = connection
                    break
                stale.append(connection)
    for connection in stale:
        _close(connection)
    return taken


def _give_back(key: str, connection: Any) -> None:
    evicted: list[Any] = []
    with _LOCK:
        idle = _POOLS.setdefault(key, deque())
        _POOLS.move_to_end(key)
        if len(idle) < POOL_MAX_IDLE:
            idle.append((connection, time.monotonic()))
            connection = None
        while len(_POOLS) > MAX_POOLS:
            _, dropped = _POOLS.popitem(last=False)
            evicted.extend(item for item, _ in dropped)
    if connection is not None:
        _close(connection)
    for item in evicted:
        _close(item)


@contextmanager
def borrow(key: str, connect: Callable[[], Any]) -> Iterator[Any]:
    """An idle connection for `key`, or a new one; rolled back and returned afterwards, or
    closed if the block raised."""
    connection = _take(key) or connect()
    try:
        yield connection
    except BaseException:
        _close(connection)
        raise
    try:
        connection.rollback()
    except Exception:
        _close(connection)
        return
    _give_back(key, connection)


def idle_count(key: str) -> int:
    with _LOCK:
        return len(_POOLS.get(key, ()))


def close_sqlserver_pools() -> None:
    """Close every idle connection (process shutdown)."""
    with _LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for idle in pools:
        for connection, _ in idle:
            _close(connection)
