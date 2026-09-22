"""Read a request body that must not be larger than a stated limit (R11-AUD11).

The two upload routes that take the raw request body -- the model workbook import
(`aida.model_import_api`) and the OKF bundle import and preview (`aida.okf_import_api`) -- each have
a size limit and each refused a body whose declared `Content-Length` was over it before reading a
byte. A body sent with `Transfer-Encoding: chunked` declares no length. For that case they called
`await request.body()`, which holds the ENTIRE body in memory, and only then checked its size. So on
the API's own port -- the UI's nginx already refuses over its own `client_max_body_size` -- one
request could make the process allocate as much as the sender cared to send: the limit was checked
after the memory it exists to protect had been spent. The server does not help: uvicorn (0.35.0,
the pinned version) buffers a request only up to a high-water mark before it stops reading until
the application asks for more, and has no body-size option of its own.

`read_body_within` reads `request.stream()` a chunk at a time and refuses the moment the running
total would pass the limit. What it holds while reading is at most the limit plus the one chunk in
hand: the chunk that would take the total past the limit is refused without being added to the
buffer. The last step, joining the chunks into one `bytes`, briefly needs a second copy of what was
accepted, which is what `request.body()` did as well: the bound is on what an over-limit sender can
make the process hold, not a change to what an accepted upload costs.

A generic module in `aida`, alongside `pagination` and `timeutil`, because the helper knows nothing
about either route: the limit and the refusal a caller wants are arguments. (`aida.graphql_api`
has a private `_read_body` of the same shape that raises its own document refusal instead; it was
left alone and could adopt this.)
"""

from __future__ import annotations

from contextlib import aclosing
from typing import Any

from fastapi import HTTPException, Request


async def read_body_within(
    request: Request, limit: int, *, detail: str | dict[str, Any]
) -> bytes:
    """The request body, or HTTP 413 with `detail` as soon as it is known to exceed `limit` bytes.

    Two checks, in this order, and each refuses without reading further:

    1. A declared `Content-Length` over the limit is refused before any of the body is read.
       (A value that is not a plain number is ignored here, as the routes always did; the
       streaming check below does not depend on it.)
    2. Otherwise the body is read a chunk at a time, and the first chunk that would take the
       running total past `limit` is refused unread. This is the check a chunked body -- which
       declares no length -- meets, and it also catches a body longer than it declared.

    A body of exactly `limit` bytes is accepted. An empty body is returned as `b""` for the caller
    to judge; this only enforces the ceiling.

    `detail` is what the caller's 413 has always said, so a route's message does not change.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=detail)
    chunks: list[bytes] = []
    total = 0
    # `aclosing`: leaving early, by the 413 or by a client that hangs up, closes the stream at once
    # instead of leaving it to be finalized whenever the event loop gets to it.
    async with aclosing(request.stream()) as stream:
        async for chunk in stream:
            if total + len(chunk) > limit:
                raise HTTPException(status_code=413, detail=detail)
            total += len(chunk)
            chunks.append(chunk)
    return b"".join(chunks)
