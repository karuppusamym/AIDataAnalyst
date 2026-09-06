"""Shared parsing helpers for third-party lineage artifacts.

**Invariant this module exists to hold:** two ingesters reading the same
field out of two vendors' JSON produce the same value, or the difference is
a deliberate one stated somewhere.

`aida.bi_lineage` and `aida.dbt_artifacts` each carried a byte-identical
private copy of both functions below (`Docs/review-2026-09-05/REVIEW.md`
R07's exact-AST-duplicate scan named `_parse_generated_at`). Two copies of a
timestamp parser is not a style problem: a fix applied to one -- a new vendor
timezone suffix, a tightened length bound -- silently leaves the other
ingester reading the same manifest differently, and nothing fails.

The review's rule for consolidation was "agree the correct contract first,
then consolidate; sharing a bug is not a fix". Both copies were verified
identical before this module replaced them, so there is no behaviour change
here and no bug being promoted to shared status.

Deliberately dependency-light: stdlib only, so both ingesters and any future
one can import it without pulling a module graph behind it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

__all__ = ["optional_text", "parse_generated_at"]


def optional_text(value: Any, limit: int) -> str | None:
    """Coerce an untrusted artifact field to bounded text, or to None.

    `limit` is a hard truncation, not a validation: artifact producers are
    outside this system's control and a field's length is not a contract.
    Whitespace-only is None rather than the empty string, so "absent" and
    "present but blank" cannot be told apart downstream by accident.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def parse_generated_at(value: Any) -> datetime | None:
    """Parse an artifact's `generatedAt`/`generated_at` to an aware datetime.

    Returns None rather than raising: a manifest with an unparseable
    timestamp is still worth ingesting, and an ingestion that fails wholesale
    on one malformed metadata field loses the lineage it could have read.

    A naive timestamp is assumed UTC. That assumption is stated here rather
    than made twice: dbt and BI exports both emit UTC in practice, and
    guessing the local zone of whichever machine ran the export would produce
    a wrong answer that looks right.
    """
    text = optional_text(value, 100)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
