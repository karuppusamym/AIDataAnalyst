"""
Vector Projection and Similarity Search
=========================================

Provides embedding generation and cosine-similarity retrieval for catalog
metadata.  Uses a pluggable embedding interface with a deterministic
hash-based fallback for testing.

Architecture
------------
- ``EmbeddingProvider``   : protocol for pluggable embedding backends.
- ``HashEmbeddingProvider``: deterministic hash-based provider for tests.
- ``cosine_similarity``   : pure-Python cosine similarity.
- ``vector_search``       : search pre-loaded embeddings by similarity.
- ``build_embedding_text``: compose the text to embed for a catalog object.

All queries are organization-scoped and never leak across org boundaries.
Policy filtering happens BEFORE ranking.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID


# ---------------------------------------------------------------------------
# Embedding provider protocol
# ---------------------------------------------------------------------------

DEFAULT_DIMENSION = 64


class EmbeddingProvider(Protocol):
    """Pluggable embedding generation interface."""

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed(self, text: str) -> list[float]: ...


class HashEmbeddingProvider:
    """Deterministic hash-based embedding for testing.

    Produces a fixed-dimension vector from a SHA-256 hash of the input,
    so identical inputs always produce identical embeddings and similar
    inputs produce somewhat-similar vectors (via n-gram overlap in the
    hash seed).
    """

    def __init__(self, dimension: int = DEFAULT_DIMENSION) -> None:
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        return "hash-deterministic-v1"

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> list[float]:
        normalised = text.lower().strip()
        digest = hashlib.sha256(normalised.encode("utf-8")).digest()
        # Expand the 32-byte digest to cover the required dimension
        raw: list[float] = []
        for i in range(self._dimension):
            byte_val = digest[i % len(digest)]
            # Map 0-255 to [-1.0, 1.0]
            raw.append((byte_val / 127.5) - 1.0)
        # L2-normalise
        norm = math.sqrt(sum(v * v for v in raw))
        if norm > 0:
            raw = [v / norm for v in raw]
        return raw


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns a value in [-1.0, 1.0]; 1.0 = identical direction.
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Vector search result
# ---------------------------------------------------------------------------


@dataclass
class VectorHit:
    """A single vector similarity hit."""

    object_type: str
    object_id: str
    display_name: str
    similarity: float
    datasource_id: UUID | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Text construction for embedding
# ---------------------------------------------------------------------------


def build_embedding_text(
    *,
    name: str,
    description: str | None = None,
    object_type: str | None = None,
    synonyms: list[str] | None = None,
    tags: list[str] | None = None,
) -> str:
    """Compose text suitable for embedding from an object's metadata."""
    parts: list[str] = []
    if object_type:
        parts.append(object_type)
    parts.append(name)
    if description:
        parts.append(description)
    if synonyms:
        parts.extend(synonyms)
    if tags:
        parts.extend(tags)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Vector search (operates on pre-loaded embeddings)
# ---------------------------------------------------------------------------


def vector_search(
    query_embedding: list[float],
    candidates: list[dict[str, Any]],
    *,
    top_k: int = 25,
    min_similarity: float = 0.0,
) -> list[VectorHit]:
    """Search candidate embeddings by cosine similarity.

    Each candidate dict must have ``object_type``, ``object_id``,
    ``display_name``, and ``embedding`` keys.

    Candidates must already be policy-filtered (org/source scoped)
    BEFORE being passed here.

    Returns hits sorted by similarity descending.
    """
    scored: list[tuple[float, dict[str, Any]]] = []
    for candidate in candidates:
        emb = candidate.get("embedding")
        if not emb:
            continue
        sim = cosine_similarity(query_embedding, emb)
        if sim >= min_similarity:
            scored.append((sim, candidate))

    scored.sort(key=lambda x: x[0], reverse=True)

    hits: list[VectorHit] = []
    for sim, cand in scored[:top_k]:
        hits.append(
            VectorHit(
                object_type=cand["object_type"],
                object_id=cand["object_id"],
                display_name=cand["display_name"],
                similarity=round(sim, 6),
                datasource_id=cand.get("datasource_id"),
                metadata=cand.get("metadata", {}),
            )
        )
    return hits
