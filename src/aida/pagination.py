"""Keyset (cursor) pagination helpers.

``LIMIT n OFFSET m`` pagination forces the database to walk and discard the
first ``m`` matching rows before it can return anything -- cost grows with how
deep a caller has paged, not with how many rows they actually asked for. That
is fatal at the scale this platform's catalog module targets (1M tables, and
per its own scale note, ~30M columns): a steward scrolling to page 50,000 of
a table list would force a scan-and-discard of 5,000,000 rows on every request.

Keyset pagination instead asks the index for "the next N rows strictly after
this key" using an opaque cursor that encodes the last row a caller saw. The
predicate compiles to a row-value comparison, e.g.::

    WHERE (name, id) > (:last_name, :last_id) ORDER BY name, id LIMIT :n

which a composite index whose leading columns match the query's equality
filters and whose trailing columns match ``(name, id)`` can satisfy with a
single index range seek -- cost bounded by page size alone, independent of
how many pages came before it.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, Select, tuple_


class InvalidCursor(ValueError):
    """Raised when a client-supplied cursor cannot be decoded or is malformed."""


def encode_cursor(*values: Any) -> str:
    """Encode the last-seen row's ordering key as an opaque, URL-safe cursor.

    The encoding is deliberately simple (base64 of a JSON string array) -- it
    carries no meaning to the client, but round-trips exactly through
    ``decode_cursor`` regardless of the underlying column types (UUIDs and
    integers are stringified on the way in).
    """
    payload = json.dumps([str(value) for value in values], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str, *, arity: int) -> tuple[str, ...]:
    """Decode a cursor produced by ``encode_cursor``, validating its shape.

    Raises ``InvalidCursor`` for anything that isn't exactly a JSON array of
    ``arity`` strings -- a tampered, truncated, or stale-format cursor should
    fail loudly (as a 400) rather than silently mis-paginate.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        parts = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 -- any decode failure means an invalid cursor
        raise InvalidCursor("cursor is not valid") from exc
    if (
        not isinstance(parts, list)
        or len(parts) != arity
        or not all(isinstance(part, str) for part in parts)
    ):
        raise InvalidCursor("cursor does not match the expected shape")
    return tuple(parts)


def apply_keyset(
    statement: Select[Any],
    columns: Sequence[ColumnElement[Any]],
    last_values: tuple[Any, ...],
) -> Select[Any]:
    """Restrict ``statement`` to rows ordered strictly after ``last_values``.

    ``columns`` must be the same columns (in the same order) as the
    statement's ``ORDER BY``. The comparison is a single SQL row-value
    predicate -- not one ``AND``-chained comparison per column -- so it is
    exactly the shape a composite index on ``columns`` can satisfy in one
    seek.
    """
    if len(columns) != len(last_values):
        raise ValueError("columns and last_values must have the same arity")
    return statement.where(tuple_(*columns) > tuple_(*last_values))
