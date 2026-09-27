"""One request-body bound for every route, on the API's own port (R11-AUD11).

The UI's nginx has refused an oversized body since R11-AUD05 and R11-AUD11 (1 MiB on `/v1/`,
64 MiB on the two metadata-envelope routes, 32 MiB on the three uploads, 8 MiB on `/mcp`,
128 KiB on `/graphql`). The API itself bounded only the uploads and GraphQL, inside their
handlers. Everywhere else FastAPI reads the whole body -- to parse it as the route's JSON model
-- before any dependency runs, the role guard included, and uvicorn has no body-size option. So
on port 8000, which the compose stack publishes, an unauthenticated caller could make the process
hold as much as it cared to send before being told 401.

`RequestBodyLimitMiddleware` applies the same numbers the proxy does, in front of every route:

* a declared `Content-Length` over the route's limit is refused with 413 before a byte is read;
* a body with no declared length (`Transfer-Encoding: chunked`) is counted as the application
  reads it, and refused with 413 the moment the count passes the limit;
* routes whose handler already bounds its own body, with a refusal of its own that clients read
  (the workbook import's message, the OKF import's `ARCHIVE_TOO_LARGE` reason code, GraphQL's
  document refusal), are left to that handler, so what they answer does not change.

`tests/test_api_body_limits.py` holds every number here to `ui-next/nginx.conf`'s, through
`scripts/check_proxy_contract.py`, so the two cannot drift apart.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, Final

from fastapi import HTTPException

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

MIB: Final = 1024 * 1024

#: What any route not named below may receive: nginx's 1 MiB `/v1/` default.
DEFAULT_BODY_LIMIT: Final = 1 * MIB

#: Routes with a larger bound, first match wins. The patterns are nginx's own locations.
ROUTE_BODY_LIMITS: Final[tuple[tuple[re.Pattern[str], int], ...]] = (
    # The synchronous metadata envelope and one batch chunk (R11-AUD05).
    (
        re.compile(
            r"^/v1/(datasources/[^/]+/metadata-ingestions|metadata-ingestion-batches/[^/]+/chunks)/?$"
        ),
        64 * MIB,
    ),
    # The MCP JSON-RPC transport.
    (re.compile(r"^/mcp/?$"), 8 * MIB),
)

#: Routes whose handler bounds its own body and answers with a refusal of its own. The upload
#: handlers use `aida.request_body.read_body_within` at 32 MiB; GraphQL reads through
#: `graphql_api._read_body` at `graphql_max_request_bytes`.
HANDLER_BOUNDED_ROUTES: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"^/v1/(datasources/[^/]+/model/import|context-product-versions/[^/]+/okf-bundle/imports(/preview)?)/?$"
    ),
    re.compile(r"^/graphql/?$"),
)


def body_limit_for(path: str) -> int | None:
    """The body bound for `path` in bytes, or `None` when its handler owns the bound."""
    if any(pattern.search(path) for pattern in HANDLER_BOUNDED_ROUTES):
        return None
    for pattern, limit in ROUTE_BODY_LIMITS:
        if pattern.search(path):
            return limit
    return DEFAULT_BODY_LIMIT


def _detail(limit: int) -> str:
    size = f"{limit // MIB} MiB" if limit % MIB == 0 else f"{limit // 1024} KiB"
    return f"the request body exceeds the {size} limit for this route"


class RequestBodyLimitMiddleware:
    """Refuse a request whose body passes its route's bound; see the module docstring."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = body_limit_for(str(scope.get("path", "")))
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _refuse(send, limit)
            return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Raised inside the handler's own body read, so the application's exception
                    # handling turns it into the same 413 the declared-length check sends.
                    raise HTTPException(status_code=413, detail=_detail(limit))
            return message

        await self.app(scope, bounded_receive, send)


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers") or ():
        if name.lower() == b"content-length":
            text = value.decode("latin-1").strip()
            return int(text) if text.isdigit() else None
    return None


async def _refuse(send: Send, limit: int) -> None:
    body = json.dumps({"detail": _detail(limit)}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
