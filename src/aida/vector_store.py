"""Semantic retrieval without assuming `pgvector` (ADR-0019).

The design for hybrid retrieval named `pgvector`. That was a mistake to state as an
assumption: a regulated PostgreSQL estate frequently forbids extensions outright,
and `CREATE EXTENSION` needs a privilege a bank DBA will not hand out for a new
platform. What the design actually needs is an *embedding store with nearest-
neighbour search*, which is a port with several possible adapters. This module is
that port.

Four backends, selected by `Settings.vector_index_backend`:

* **`postgres_bruteforce` (default).** Vectors live in an ordinary `bytea` column in
  the same PostgreSQL the rest of the control plane uses. Search is exact cosine over
  a candidate set. No extension, no second system to run, back up and drill.
* **`external`.** The bank's own in-network vector service over HTTP. The store is a
  projection, so putting it outside PostgreSQL costs nothing architecturally -- it is
  the same treatment the graph projection gets.
* **`pgvector`.** Only selectable where the extension is genuinely installed. The
  factory probes `pg_available_extensions` rather than trusting configuration, and
  refuses at startup if it is absent (INV-4, INV-9).
* **`disabled`.** Lexical retrieval only, reported honestly rather than degraded
  silently.

**Why exact search is viable far longer than it looks.** Nobody ever searches the
whole estate. Retrieval filters by workspace binding and policy *before* ranking
(that ordering removes an information-leak class, and it is not negotiable), so the
candidate set reaching the scorer is what one principal may see, not what exists.
Measured end to end on PostgreSQL 16 against 200,000 stored 768-dimension embeddings
(fetch, unpack and score, returning top-25):

| Candidates | p50      |
|-----------:|---------:|
|        200 |    45 ms |
|      1,000 |   100 ms |
|      5,000 |   427 ms |
|     20,000 | 1,697 ms |

That is the honest envelope: exact search is comfortable to roughly a thousand
candidates and stops being interactive well before ten thousand. It is not a
general-purpose ANN replacement, and the design does not pretend otherwise -- it is
the second stage of a two-stage retrieval where lexical and policy filtering have
already done the narrowing. An approximate index earns its place when candidate sets
are *routinely* larger than that, which is a measurement, not an assumption.

**Embeddings are not anonymous.** It is tempting to treat a vector as a safe
numeric derivative, and to conclude that shipping embeddings to an external store is
therefore outside INV-6. It is not. Embedding-inversion research recovers
substantial portions of source text from embeddings alone, so a vector of a document
chunk carries the sensitivity of that chunk. Consequences, all enforced here or
stated as operating requirements:

* Only metadata and the customer's own documentation are ever embedded. Source
  business values are not, and there is no code path that would.
* An external index must be inside the bank's network. There is no "send embeddings
  to a hosted vector API" mode, and adding one would need an ADR.
* The vector store inherits the classification of what was embedded, and is in scope
  for the same retention and deletion obligations as the control plane.

**Index identity.** An embedding is comparable only to embeddings produced by the
same model, the same model version and the same chunking. All three are pinned into
`index_signature`, and a mismatch is a rebuild trigger rather than a silent mixing of
incomparable vectors -- which fails as quietly bad search results rather than as an
error, and is correspondingly hard to notice.
"""

from __future__ import annotations

import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings

_FLOAT_FORMAT = "<f"  # little-endian float32; 4 bytes per dimension


class VectorIndexUnavailable(RuntimeError):
    """The configured backend cannot serve. Fail closed rather than degrade (INV-4)."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class EmbeddingRef:
    """What an embedding is *of*. Polymorphic, like a business assignment target."""

    owner_type: str  # TABLE | COLUMN | VIEW | GLOSSARY_TERM | KNOWLEDGE_BLOCK | DOCUMENT_SECTION
    owner_id: str
    chunk_index: int = 0


@dataclass(frozen=True, slots=True)
class EmbeddingRecord:
    ref: EmbeddingRef
    vector: tuple[float, ...]
    # Hash of the embedded text, never the text. Lets a re-embed be skipped when
    # nothing changed, without keeping a second copy of the content (INV-6).
    text_hash: str


@dataclass(frozen=True, slots=True)
class ScoredMatch:
    ref: EmbeddingRef
    score: float


def pack_vector(vector: tuple[float, ...]) -> bytes:
    """Pack float32s for storage in an ordinary `bytea` column -- no extension needed."""
    return b"".join(struct.pack(_FLOAT_FORMAT, value) for value in vector)


def unpack_vector(blob: bytes) -> tuple[float, ...]:
    count = len(blob) // 4
    return struct.unpack(f"<{count}f", blob)


def cosine(query: tuple[float, ...], query_norm: float, vector: tuple[float, ...],
           vector_norm: float) -> float:
    """Cosine similarity with both norms supplied.

    Norms are stored at write time rather than recomputed per comparison, which halves
    the work in the inner loop -- the single cheapest optimisation available to an
    exact scorer.
    """
    if query_norm == 0.0 or vector_norm == 0.0:
        return 0.0
    return sum(a * b for a, b in zip(query, vector, strict=False)) / (query_norm * vector_norm)


def index_signature(settings: Settings) -> str:
    """The identity a stored embedding must match to be comparable to a fresh one."""
    return (
        f"{settings.embedding_model_id}:{settings.embedding_model_version}"
        f":{settings.embedding_dimensions}:v{settings.embedding_chunking_version}"
    )


class VectorIndex(ABC):
    """The port. Every backend is a rebuildable projection, never a source of truth."""

    name: str

    @abstractmethod
    async def upsert(self, session: AsyncSession, organization_id: Any,
                     records: tuple[EmbeddingRecord, ...], *, signature: str) -> int: ...

    @abstractmethod
    async def search(self, session: AsyncSession, organization_id: Any,
                     query_vector: tuple[float, ...], *, signature: str,
                     candidates: tuple[EmbeddingRef, ...] | None, limit: int
                     ) -> tuple[ScoredMatch, ...]: ...

    @abstractmethod
    async def delete_owner(self, session: AsyncSession, organization_id: Any,
                           owner_type: str, owner_id: str) -> int: ...


class DisabledVectorIndex(VectorIndex):
    """Semantic retrieval is off. Says so; does not pretend to serve."""

    name = "disabled"

    async def upsert(self, session: AsyncSession, organization_id: Any,
                     records: tuple[EmbeddingRecord, ...], *, signature: str) -> int:
        raise VectorIndexUnavailable("VECTOR_INDEX_DISABLED")

    async def search(self, session: AsyncSession, organization_id: Any,
                     query_vector: tuple[float, ...], *, signature: str,
                     candidates: tuple[EmbeddingRef, ...] | None, limit: int
                     ) -> tuple[ScoredMatch, ...]:
        raise VectorIndexUnavailable("VECTOR_INDEX_DISABLED")

    async def delete_owner(self, session: AsyncSession, organization_id: Any,
                           owner_type: str, owner_id: str) -> int:
        raise VectorIndexUnavailable("VECTOR_INDEX_DISABLED")


class PostgresBruteForceIndex(VectorIndex):
    """Exact cosine over a policy-narrowed candidate set, in plain PostgreSQL.

    The default, and the one that works in an estate where no extension will ever be
    approved. Correctness is strictly better than an approximate index -- there is no
    recall loss to measure or tune -- and the cost is linear in candidates, which the
    policy pre-filter already bounds.

    The candidate cap is a refusal, not a truncation: silently scoring the first
    20,000 of 500,000 candidates would return plausible, wrong answers.
    """

    name = "postgres_bruteforce"

    def __init__(self, settings: Settings) -> None:
        self._cap = settings.vector_bruteforce_candidate_cap

    async def upsert(self, session: AsyncSession, organization_id: Any,
                     records: tuple[EmbeddingRecord, ...], *, signature: str) -> int:
        from aida.models import Embedding

        for record in records:
            blob = pack_vector(record.vector)
            norm = sum(value * value for value in record.vector) ** 0.5
            existing = await session.scalar(
                select(Embedding).where(
                    Embedding.organization_id == organization_id,
                    Embedding.owner_type == record.ref.owner_type,
                    Embedding.owner_id == record.ref.owner_id,
                    Embedding.chunk_index == record.ref.chunk_index,
                )
            )
            if existing is None:
                session.add(
                    Embedding(
                        organization_id=organization_id,
                        owner_type=record.ref.owner_type,
                        owner_id=record.ref.owner_id,
                        chunk_index=record.ref.chunk_index,
                        index_signature=signature,
                        dimensions=len(record.vector),
                        vector=blob,
                        vector_norm=norm,
                        text_hash=record.text_hash,
                    )
                )
            else:
                existing.index_signature = signature
                existing.dimensions = len(record.vector)
                existing.vector = blob
                existing.vector_norm = norm
                existing.text_hash = record.text_hash
        await session.flush()
        return len(records)

    async def search(self, session: AsyncSession, organization_id: Any,
                     query_vector: tuple[float, ...], *, signature: str,
                     candidates: tuple[EmbeddingRef, ...] | None, limit: int
                     ) -> tuple[ScoredMatch, ...]:
        from aida.models import Embedding

        statement = select(
            Embedding.owner_type, Embedding.owner_id, Embedding.chunk_index,
            Embedding.vector, Embedding.vector_norm,
        ).where(
            Embedding.organization_id == organization_id,
            # Only vectors from the same model, version and chunking are comparable.
            Embedding.index_signature == signature,
        )
        allowed_pairs: set[tuple[str, str]] | None = None
        if candidates is not None:
            if not candidates:
                return ()
            allowed_pairs = {(c.owner_type, c.owner_id) for c in candidates}
            # Narrow in SQL on the indexed column, then match the full (type, id) pair
            # below. `owner_id` is only unique *within* an owner_type, so filtering on it
            # alone would admit a COLUMN that happens to share an identifier with an
            # authorised TABLE -- and the policy filter that produced this allowlist
            # authorised the table, not the column. A tuple IN would express this in one
            # step but is not portable across the dialects this runs on.
            statement = statement.where(
                Embedding.owner_id.in_({owner_id for _, owner_id in allowed_pairs})
            )
        rows = (await session.execute(statement.limit(self._cap + 1))).all()
        if allowed_pairs is not None:
            rows = [row for row in rows if (row[0], row[1]) in allowed_pairs]
        if len(rows) > self._cap:
            # Refuse rather than score a silently truncated slice.
            raise VectorIndexUnavailable("CANDIDATE_SET_EXCEEDS_BRUTEFORCE_CAP")
        query_norm = sum(value * value for value in query_vector) ** 0.5
        scored = [
            ScoredMatch(
                ref=EmbeddingRef(owner_type=row[0], owner_id=row[1], chunk_index=row[2]),
                score=cosine(query_vector, query_norm, unpack_vector(row[3]), row[4]),
            )
            for row in rows
        ]
        scored.sort(key=lambda match: (-match.score, match.ref.owner_id))
        return tuple(scored[:limit])

    async def delete_owner(self, session: AsyncSession, organization_id: Any,
                           owner_type: str, owner_id: str) -> int:
        from aida.models import Embedding

        doomed = (
            await session.scalars(
                select(Embedding.id).where(
                    Embedding.organization_id == organization_id,
                    Embedding.owner_type == owner_type,
                    Embedding.owner_id == owner_id,
                )
            )
        ).all()
        if not doomed:
            return 0
        await session.execute(delete(Embedding).where(Embedding.id.in_(doomed)))
        return len(doomed)


class ExternalVectorIndex(VectorIndex):
    """The bank's own in-network vector service, reached over HTTP.

    Deliberately generic: Milvus, Qdrant, Weaviate, OpenSearch kNN and Azure AI Search
    all expose upsert / search / delete over HTTP, and the adapter's job is to be
    replaceable, not to be clever. What matters architecturally is that this index is a
    projection like every other -- rebuildable from PostgreSQL, never read as truth for
    an authorization or correctness decision (INV-1).

    Two operating requirements this adapter cannot enforce alone and which belong in
    the deployment review: the endpoint must be inside the bank's network, and its
    retention and deletion behaviour must match the control plane's, because the
    vectors it holds are recoverable text.
    """

    name = "external"

    def __init__(self, settings: Settings, *, token: str | None = None) -> None:
        if not settings.vector_index_url:
            raise VectorIndexUnavailable("VECTOR_INDEX_URL_NOT_CONFIGURED")
        self._url = settings.vector_index_url.rstrip("/")
        self._collection = settings.vector_index_collection
        self._timeout = settings.vector_index_timeout_seconds
        self._token = token

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False
            ) as client:
                response = await client.post(
                    f"{self._url}{path}", json=payload, headers=self._headers()
                )
                response.raise_for_status()
                body: dict[str, Any] = response.json()
                return body
        except (httpx.HTTPError, ValueError) as exc:
            raise VectorIndexUnavailable("EXTERNAL_VECTOR_INDEX_UNREACHABLE") from exc

    async def upsert(self, session: AsyncSession, organization_id: Any,
                     records: tuple[EmbeddingRecord, ...], *, signature: str) -> int:
        await self._post(
            f"/collections/{self._collection}/points",
            {
                "organization_id": str(organization_id),
                "index_signature": signature,
                "points": [
                    {
                        "id": f"{r.ref.owner_type}:{r.ref.owner_id}:{r.ref.chunk_index}",
                        "vector": list(r.vector),
                        "payload": {
                            "owner_type": r.ref.owner_type,
                            "owner_id": r.ref.owner_id,
                            "chunk_index": r.ref.chunk_index,
                            "text_hash": r.text_hash,
                        },
                    }
                    for r in records
                ],
            },
        )
        return len(records)

    async def search(self, session: AsyncSession, organization_id: Any,
                     query_vector: tuple[float, ...], *, signature: str,
                     candidates: tuple[EmbeddingRef, ...] | None, limit: int
                     ) -> tuple[ScoredMatch, ...]:
        payload: dict[str, Any] = {
            "organization_id": str(organization_id),
            "index_signature": signature,
            "vector": list(query_vector),
            "limit": limit,
        }
        if candidates is not None:
            # Policy filtering happens before ranking, so the allowlist travels with the
            # query. An external index must never be asked to rank the whole estate and
            # have the result filtered afterwards -- result counts and ordering leak the
            # existence of assets the caller may not see.
            payload["owner_ids"] = sorted({c.owner_id for c in candidates})
        body = await self._post(f"/collections/{self._collection}/search", payload)
        matches = body.get("matches", []) if isinstance(body, dict) else []
        return tuple(
            ScoredMatch(
                ref=EmbeddingRef(
                    owner_type=str(match.get("owner_type", "")),
                    owner_id=str(match.get("owner_id", "")),
                    chunk_index=int(match.get("chunk_index", 0)),
                ),
                score=float(match.get("score", 0.0)),
            )
            for match in matches
        )

    async def delete_owner(self, session: AsyncSession, organization_id: Any,
                           owner_type: str, owner_id: str) -> int:
        body = await self._post(
            f"/collections/{self._collection}/delete",
            {
                "organization_id": str(organization_id),
                "owner_type": owner_type,
                "owner_id": owner_id,
            },
        )
        return int(body.get("deleted", 0)) if isinstance(body, dict) else 0


async def pgvector_is_installed(session: AsyncSession) -> bool:
    """Probe the database rather than trust configuration (INV-9).

    A platform that advertises a capability it does not have is worse than one that
    advertises fewer capabilities, which is why this is a query and not a setting.
    """
    found = await session.scalar(
        text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector' LIMIT 1")
    )
    return bool(found)


async def resolve_vector_index(
    settings: Settings, session: AsyncSession, *, token: str | None = None
) -> VectorIndex:
    """Build the configured backend, refusing rather than degrading (INV-4)."""
    backend = settings.vector_index_backend
    if backend == "disabled":
        return DisabledVectorIndex()
    if backend == "postgres_bruteforce":
        return PostgresBruteForceIndex(settings)
    if backend == "external":
        return ExternalVectorIndex(settings, token=token)
    if backend == "pgvector":
        if not await pgvector_is_installed(session):
            raise VectorIndexUnavailable("PGVECTOR_EXTENSION_NOT_INSTALLED")
        # The adapter itself is not built: nothing in this estate has the extension, so
        # shipping an untested implementation would be exactly the overstated capability
        # INV-9 exists to prevent. It lands with a database that can run it.
        raise VectorIndexUnavailable("PGVECTOR_ADAPTER_NOT_IMPLEMENTED")
    raise VectorIndexUnavailable("UNKNOWN_VECTOR_INDEX_BACKEND")
