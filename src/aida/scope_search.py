"""Server-side substring search for the tenant-scoped picker list routes.

**Why this is a module and not four inline blocks.** F15 (see
`Docs/review-2026-09-05/REVIEW.md`) adds a `q=` filter to four list routes
that live in two different routers (`aida.operational_api` and
`atlas.modules.identity_tenancy.router`). A search predicate copied four
times is a predicate that drifts four ways -- one copy forgetting
``func.lower`` makes that route's search silently case-sensitive, which
looks like "no results" to a user and like nothing at all to a reviewer.

**The convention this reuses, rather than inventing a second one.** The
catalog routes (`list_catalog_rows`, `list_tables`) already search with
``func.lower(<column>).contains(<lowered term>)``, and
`migrations/versions/f9a2b3c4d5e6_catalog_scale_indexes.py` already indexes
exactly that shape with a `pg_trgm` GIN index on ``lower(<column>)``. Using
the same shape here means the same index strategy works and a reader who
knows one search route knows them all.

**Deliberate deviation, and why.** The catalog routes declare
``q: str | None = Query(default=None, min_length=2, ...)``. These routes
omit ``min_length`` -- an empty ``q=`` (a picker whose search box was
cleared) must behave exactly as no ``q`` at all rather than 422, since the
client half of F15 shipped before the server half and may send either. That
is why normalization lives here: ``None``, ``""`` and ``"   "`` all collapse
to "no filter", so a caller cannot accidentally turn a cleared search box
into an error or into ``contains("")``.

**This filters, it never widens.** The predicate returned here is one more
``AND``-ed term on top of the tenant filter its caller already applied. It
can only remove rows from a page the caller was already entitled to see;
there is no code path by which a search term reaches rows outside the
caller's organization. `tests/test_scope_picker_search.py` asserts that for
each of the four routes.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, SQLColumnExpression, func, or_

__all__ = ["search_predicate"]


def search_predicate(
    q: str | None, *columns: SQLColumnExpression[str]
) -> ColumnElement[bool] | None:
    """Case-insensitive "contains" across `columns`, or `None` when `q` is absent.

    Returning `None` rather than a tautology is what keeps an absent `q`
    byte-identical to the pre-F15 query: no extra `WHERE` term is appended at
    all, so the plan for an unfiltered page is unchanged.
    """
    if q is None:
        return None
    term = q.strip().lower()
    if not term:
        return None
    return or_(*(func.lower(column).contains(term) for column in columns))
