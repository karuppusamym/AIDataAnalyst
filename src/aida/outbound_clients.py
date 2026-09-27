"""R11-MP23: process-wide outbound HTTP and Redis clients.

Every model provider call used to open and close its own `httpx.AsyncClient`,
so each paid the TCP and TLS handshake again, and every request-budget check
opened a Redis connection. Both now borrow a client shared by the process.

Clients are bound to the event loop that created them -- an httpx or
redis-asyncio connection pool cannot be used from another loop -- so the
registry holds one set per running loop. The API and the worker each run one
loop for their lifetime; a test suite that runs one loop per test simply gets
a fresh set per test. A caller that injects its own client (tests, and any code
that needs a transport of its own) is unaffected.

`close_outbound_clients` closes the current loop's clients; the API's lifespan
calls it at shutdown.
"""

from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass, field
from typing import Final

import httpx
from redis.asyncio import Redis

#: A model call is one request and one response; these bound how many sockets one
#: process holds open to providers, not how many calls may run (the gateway's
#: callers are already bounded by admission and quotas).
MODEL_HTTP_LIMITS: Final = httpx.Limits(
    max_connections=64, max_keepalive_connections=16, keepalive_expiry=30.0
)
REDIS_SOCKET_TIMEOUT_SECONDS: Final = 0.25


@dataclass(slots=True)
class _LoopClients:
    http: dict[float, httpx.AsyncClient] = field(default_factory=dict)
    redis: dict[str, Redis] = field(default_factory=dict)


_BY_LOOP: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopClients] = (
    weakref.WeakKeyDictionary()
)


def _current() -> _LoopClients:
    loop = asyncio.get_running_loop()
    clients = _BY_LOOP.get(loop)
    if clients is None:
        clients = _BY_LOOP[loop] = _LoopClients()
    return clients


def shared_http_client(*, timeout: float) -> httpx.AsyncClient:
    """The process's model HTTP client for this timeout. Redirects are not followed:
    a provider that redirects is answered as an error, never chased to another host."""
    clients = _current().http
    client = clients.get(timeout)
    if client is None or client.is_closed:
        client = clients[timeout] = httpx.AsyncClient(
            timeout=timeout, limits=MODEL_HTTP_LIMITS, follow_redirects=False
        )
    return client


def shared_redis(url: str) -> Redis:
    """The process's Redis client for this URL, with the short socket timeouts the
    request budgets have always used: a slow store is treated as an unreachable one."""
    clients = _current().redis
    client = clients.get(url)
    if client is None:
        client = clients[url] = Redis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
        )
    return client


async def close_outbound_clients() -> None:
    """Close this loop's shared clients (process shutdown)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    clients = _BY_LOOP.pop(loop, None)
    if clients is None:
        return
    for http in clients.http.values():
        await http.aclose()
    for redis in clients.redis.values():
        await redis.aclose()
