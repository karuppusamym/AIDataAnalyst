"""Timestamp comparison that survives the ORM boundary.

This module exists because the same defect was written three times in one day, which
makes it a class of bug rather than three mistakes.

Timestamps do not read back with their timezone awareness intact on every backend.
PostgreSQL `timestamptz` returns an aware datetime; SQLite returns a naive one. So a bare
`stored > supplied` or `stored == supplied` comparison against a `datetime.now(UTC)`
either raises `TypeError: can't compare offset-naive and offset-aware datetimes`, or --
far worse -- silently evaluates to the wrong answer on one backend only.

That second failure shape is the dangerous one: expiry checks and effective-dating are
exactly the comparisons that decide whether an access grant is still live, and a
backend-dependent answer is one no single test environment reveals.

Every comparison between a stored timestamp and a supplied one goes through here.
"""

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """Return `value` as UTC-aware, assuming naive values are already UTC.

    The assumption is safe in this codebase because every timestamp column is declared
    `DateTime(timezone=True)` and every write goes through `utc_now()`; a naive value
    therefore only ever appears because a backend dropped the tzinfo on the way out.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_expired(expires_at: datetime | None, moment: datetime) -> bool:
    """`None` never expires. Otherwise compare safely."""
    if expires_at is None:
        return False
    return as_utc(expires_at) <= as_utc(moment)


def is_live(expires_at: datetime | None, moment: datetime) -> bool:
    return not is_expired(expires_at, moment)


def same_instant(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return as_utc(left) == as_utc(right)
