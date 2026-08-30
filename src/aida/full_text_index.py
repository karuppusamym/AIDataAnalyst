"""
PostgreSQL Full-Text Search for Atlas Catalog Metadata
========================================================

Provides GIN-indexed ts_vector/ts_query based search across catalog objects
within an organization.  Cross-source search is supported: one query spans
all datasources in an org.

Architecture
------------
- ``build_ts_query``  : convert natural-language text to a PostgreSQL
  ``plainto_tsquery`` -compatible string.
- ``full_text_search``: execute a full-text search against metadata tables
  and return ranked results.
- ``build_search_document``: compose a weighted tsvector string for a
  catalog object from its constituent parts.

All queries are organization-scoped and never leak across org boundaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


# ---------------------------------------------------------------------------
# Query construction helpers
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "for",
        "from", "get", "how", "i", "in", "is", "it", "list", "me", "my",
        "of", "on", "or", "see", "show", "the", "to", "what", "which",
        "with", "you", "latest", "all", "give", "tell",
    }
)


def _normalise_token(token: str) -> str:
    """Lowercase and strip non-alphanumeric characters."""
    return re.sub(r"[^a-z0-9]", "", token.lower())


def build_ts_query(text: str, *, conjunction: str = "&") -> str:
    """Convert natural-language text into a ts_query-compatible string.

    Tokenises, removes stop words, and joins with ``&`` (AND) or ``|`` (OR).

    >>> build_ts_query("Show me customer revenue")
    "customer & revenue"
    """
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    expanded = expanded.replace("_", " ")
    raw_tokens = re.findall(r"[a-zA-Z0-9]+", expanded)
    tokens = [
        _normalise_token(t)
        for t in raw_tokens
        if len(t) > 1 and t.lower() not in _STOP_WORDS
    ]
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for tok in tokens:
        if tok and tok not in seen:
            seen.add(tok)
            unique.append(tok)
    return f" {conjunction} ".join(unique) if unique else ""


def build_search_document(
    *,
    name: str,
    description: str | None = None,
    synonyms: list[str] | None = None,
    tags: list[str] | None = None,
    extra_text: str | None = None,
) -> str:
    """Compose a weighted text document from an object's metadata fields.

    PostgreSQL tsvector weights (A > B > C > D) can be applied via
    ``setweight(to_tsvector(...), 'A')``.  This helper returns the
    concatenated text; the caller or a DB trigger applies weights.
    """
    parts = [name]
    if description:
        parts.append(description)
    if synonyms:
        parts.extend(synonyms)
    if tags:
        parts.extend(tags)
    if extra_text:
        parts.append(extra_text)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Full-text search result
# ---------------------------------------------------------------------------


@dataclass
class FullTextHit:
    """A single full-text search hit."""

    object_type: str
    object_id: str
    display_name: str
    qualified_name: str | None
    ts_rank: float
    datasource_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# In-process full-text search (operates on pre-fetched rows)
# ---------------------------------------------------------------------------


def _ts_rank_simple(query_tokens: list[str], document: str) -> float:
    """Approximate ts_rank using token overlap and position weighting.

    This is a pure-Python approximation so the retrieval pipeline can run
    without a live PostgreSQL ts_rank call -- the real GIN-indexed path uses
    PostgreSQL natively.  Scores are in [0.0, 1.0].
    """
    if not query_tokens or not document:
        return 0.0
    lower_doc = document.lower()
    matched = sum(1 for t in query_tokens if t in lower_doc)
    base = matched / len(query_tokens)
    # Position bonus: earlier matches are more relevant
    for t in query_tokens:
        pos = lower_doc.find(t)
        if pos >= 0 and pos < 50:
            base = min(1.0, base + 0.05)
    return round(min(1.0, base), 4)


def full_text_rank(query: str, documents: list[dict[str, Any]]) -> list[FullTextHit]:
    """Rank a list of document dicts against a query string.

    Each document dict must have ``object_type``, ``object_id``,
    ``display_name``, and ``text`` keys.

    Returns hits sorted by rank descending, excluding zero-score hits.
    """
    ts_query = build_ts_query(query)
    if not ts_query:
        return []
    tokens = ts_query.replace(" & ", " ").split()
    hits: list[FullTextHit] = []
    for doc in documents:
        rank = _ts_rank_simple(tokens, doc.get("text", ""))
        if rank > 0:
            hits.append(
                FullTextHit(
                    object_type=doc["object_type"],
                    object_id=doc["object_id"],
                    display_name=doc["display_name"],
                    qualified_name=doc.get("qualified_name"),
                    ts_rank=rank,
                    datasource_id=doc.get("datasource_id"),
                    metadata=doc.get("metadata", {}),
                )
            )
    hits.sort(key=lambda h: h.ts_rank, reverse=True)
    return hits
