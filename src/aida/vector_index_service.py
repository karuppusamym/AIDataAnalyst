"""RT-1: build and serve the *persisted* vector index.

`vector_store.py` has implemented a persisted, rebuildable index since RT-1
landed, and nothing has ever called it: retrieval embedded the question *and
every candidate* on each query and compared them in memory. That works, and
it is why the vector stage is not wrong today -- but it pays an embedding
call per candidate per query, so the stage's cost grows with the estate and
with traffic at the same time.

This module is the missing half:

* `rebuild_vector_index` embeds catalog metadata once and upserts it into the
  configured index. Idempotent by `text_hash`: an object whose text has not
  changed is not re-embedded, so a second run over an unchanged estate costs
  one query and no model calls.
* `index_freshness` answers whether the persisted index may be used for a
  given organization, so `retrieval.hybrid_retrieve` can prefer it and fall
  back to the live path when it is stale, empty, or built under a different
  embedding model.

**Value-freedom (INV-6).** Only metadata text is embedded -- object names and
types -- never a source row. The index stores the vector and a hash of the
text, never the text.

**Fail closed (INV-4).** With no embedding provider configured, this refuses
rather than backfilling with a hash double. That was a real defect once: a
SHA-256 digest has no semantic structure, and feeding one into fusion under
the name "vector" gave ranking a signal that was noise.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.context import get_correlation_id
from aida.embedding_provider import (
    EmbeddingUnavailable,
    index_signature,
    resolve_embedding_provider,
)
from aida.events import record_audit
from aida.models import (
    Embedding,
    GlossaryTerm,
    MetadataColumn,
    MetadataTable,
)
from aida.secrets import SecretResolver
from aida.security import SecurityContext
from aida.vector_retrieval import build_embedding_text
from aida.vector_store import EmbeddingRecord, EmbeddingRef, resolve_vector_index

#: Owner types this builder indexes. Deliberately a closed list: an owner
#: type that reaches the index without a matching read path in retrieval is
#: cost with no benefit, and one that reaches it carrying business values
#: would be an INV-6 breach.
INDEXED_OWNER_TYPES = ("TABLE", "COLUMN", "GLOSSARY_TERM")


@dataclass(frozen=True, slots=True)
class RebuildResult:
    organization_id: UUID
    signature: str
    considered: int
    embedded: int
    skipped_unchanged: int
    backend: str


@dataclass(frozen=True, slots=True)
class IndexFreshness:
    """Whether the persisted index may serve this organization's queries."""

    usable: bool
    reason: str
    entries: int
    signature: str
    built_at: datetime | None
    age_minutes: float | None


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _system_context(organization_id: UUID) -> SecurityContext:
    return SecurityContext(
        principal_id="system:vector-index",
        principal_type="SERVICE",
        organization_id=organization_id,
        roles=frozenset({"Operations"}),
    )


async def _indexable_objects(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID | None
) -> list[tuple[str, str, str]]:
    """`(owner_type, owner_id, text)` for everything worth embedding.

    Three statements regardless of estate size -- one per owner type -- not
    one per object. Only ACTIVE objects: a deprecated table answering a
    semantic search is a wrong answer with a confident score.
    """
    rows: list[tuple[str, str, str]] = []

    table_stmt = select(MetadataTable.id, MetadataTable.name).where(
        MetadataTable.organization_id == organization_id,
        MetadataTable.status == "ACTIVE",
    )
    if datasource_id is not None:
        table_stmt = table_stmt.where(MetadataTable.datasource_id == datasource_id)
    for table_id, name in (await session.execute(table_stmt)).all():
        rows.append(
            ("TABLE", str(table_id), build_embedding_text(name=name, object_type="TABLE"))
        )

    column_stmt = (
        select(MetadataColumn.id, MetadataColumn.name)
        .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
        .where(
            MetadataColumn.organization_id == organization_id,
            MetadataColumn.status == "ACTIVE",
            MetadataTable.status == "ACTIVE",
        )
    )
    if datasource_id is not None:
        column_stmt = column_stmt.where(MetadataTable.datasource_id == datasource_id)
    for column_id, name in (await session.execute(column_stmt)).all():
        rows.append(
            ("COLUMN", str(column_id), build_embedding_text(name=name, object_type="COLUMN"))
        )

    # Glossary terms are organization-wide rather than per-datasource, so a
    # datasource-scoped rebuild deliberately leaves them alone rather than
    # re-embedding the whole glossary on every source's schedule.
    if datasource_id is None:
        # `term_key` is the term's stable business name; `lifecycle_status`
        # is the published/deprecated axis on this model (there is no `name`
        # or `status` column -- the definition text lives on the version).
        term_stmt = select(GlossaryTerm.id, GlossaryTerm.term_key).where(
            GlossaryTerm.organization_id == organization_id,
            GlossaryTerm.lifecycle_status == "PUBLISHED",
        )
        for term_id, name in (await session.execute(term_stmt)).all():
            rows.append(
                (
                    "GLOSSARY_TERM",
                    str(term_id),
                    build_embedding_text(name=name, object_type="GLOSSARY_TERM"),
                )
            )
    return rows


async def rebuild_vector_index(
    session: AsyncSession,
    organization_id: UUID,
    *,
    settings: Settings,
    datasource_id: UUID | None = None,
    batch_size: int = 128,
    max_objects: int = 20_000,
) -> RebuildResult:
    """Embed this organization's metadata and upsert it into the index.

    Bounded on purpose: `max_objects` refuses rather than truncating, for the
    same reason the brute-force index caps its candidate set. A silent
    partial index is worse than a refusal, because retrieval would then serve
    confident answers from a fraction of the estate.
    """
    provider = resolve_embedding_provider(settings, SecretResolver(settings))
    index = await resolve_vector_index(settings, session)
    signature = index_signature(settings)

    objects = await _indexable_objects(session, organization_id, datasource_id)
    if len(objects) > max_objects:
        raise EmbeddingUnavailable(
            f"VECTOR_INDEX_REBUILD_TOO_LARGE: {len(objects)} objects exceeds "
            f"max_objects={max_objects}; narrow by datasource or raise the bound"
        )

    existing = {
        (owner_type, owner_id): text_hash
        for owner_type, owner_id, text_hash in (
            await session.execute(
                select(Embedding.owner_type, Embedding.owner_id, Embedding.text_hash).where(
                    Embedding.organization_id == organization_id,
                    Embedding.index_signature == signature,
                )
            )
        ).all()
    }

    pending = [
        (owner_type, owner_id, text)
        for owner_type, owner_id, text in objects
        if existing.get((owner_type, owner_id)) != _text_hash(text)
    ]
    skipped = len(objects) - len(pending)

    embedded = 0
    for start in range(0, len(pending), batch_size):
        chunk = pending[start : start + batch_size]
        batch = await provider.embed([text for _t, _i, text in chunk])
        records = tuple(
            EmbeddingRecord(
                ref=EmbeddingRef(owner_type=owner_type, owner_id=owner_id, chunk_index=0),
                vector=tuple(vector),
                text_hash=_text_hash(text),
            )
            for (owner_type, owner_id, text), vector in zip(chunk, batch.vectors, strict=True)
        )
        embedded += await index.upsert(
            session, organization_id, records, signature=signature
        )

    record_audit(
        session,
        _system_context(organization_id),
        action="retrieval.vector_index.rebuild",
        resource_type="vector_index",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "considered": len(objects),
            "embedded": embedded,
            "skipped_unchanged": skipped,
            "signature": signature,
            "backend": index.name,
            "datasource_id": str(datasource_id) if datasource_id else None,
        },
    )
    return RebuildResult(
        organization_id=organization_id,
        signature=signature,
        considered=len(objects),
        embedded=embedded,
        skipped_unchanged=skipped,
        backend=index.name,
    )


async def index_freshness(
    session: AsyncSession,
    organization_id: UUID,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> IndexFreshness:
    """Whether the persisted index may serve queries for this organization.

    Four ways it may not, each reported by name rather than collapsed into a
    bare False, because "why did my search fall back" is the first question
    an operator asks:

    * `DISABLED` -- the backend is off.
    * `EMPTY` -- nothing has been indexed under the current signature. Note
      the signature: a model change makes the old vectors unusable rather
      than merely stale, and comparing across models would be meaningless.
    * `STALE` -- the newest entry is older than the configured maximum age,
      or older than the newest catalog change. Either says the estate has
      moved on.
    * `USABLE` -- serve from the index.
    """
    moment = now or datetime.now(UTC)
    if settings.vector_index_backend == "disabled":
        return IndexFreshness(False, "DISABLED", 0, "", None, None)

    signature = index_signature(settings)
    row = (
        await session.execute(
            select(func.count(), func.max(Embedding.updated_at)).where(
                Embedding.organization_id == organization_id,
                Embedding.index_signature == signature,
            )
        )
    ).one()
    entries, built_at = int(row[0] or 0), row[1]
    if entries == 0 or built_at is None:
        return IndexFreshness(False, "EMPTY", entries, signature, None, None)

    if built_at.tzinfo is None:
        built_at = built_at.replace(tzinfo=UTC)
    age_minutes = (moment - built_at).total_seconds() / 60.0
    if age_minutes > settings.vector_index_max_age_minutes:
        return IndexFreshness(False, "STALE", entries, signature, built_at, age_minutes)

    # A catalog change after the last build means the index is missing
    # objects, which is a subtler staleness than age and the one that
    # actually returns wrong results.
    newest_table = await session.scalar(
        select(func.max(MetadataTable.updated_at)).where(
            MetadataTable.organization_id == organization_id,
            MetadataTable.status == "ACTIVE",
        )
    )
    if newest_table is not None:
        if newest_table.tzinfo is None:
            newest_table = newest_table.replace(tzinfo=UTC)
        if newest_table > built_at:
            return IndexFreshness(
                False, "STALE_CATALOG_MOVED", entries, signature, built_at, age_minutes
            )

    return IndexFreshness(True, "USABLE", entries, signature, built_at, age_minutes)


async def search_persisted_index(
    session: AsyncSession,
    organization_id: UUID,
    query_vector: tuple[float, ...],
    *,
    settings: Settings,
    candidates: tuple[EmbeddingRef, ...] | None,
    limit: int,
) -> tuple[tuple[str, str, float], ...]:
    """`(owner_type, owner_id, score)` from the persisted index.

    `candidates` is the policy-narrowed set: passing it keeps the invariant
    that policy filters *before* ranking, which is the property that makes
    this platform's search safe to point at a bank's estate.
    """
    index = await resolve_vector_index(settings, session)
    matches = await index.search(
        session,
        organization_id,
        query_vector,
        signature=index_signature(settings),
        candidates=candidates,
        limit=limit,
    )
    return tuple(
        (match.ref.owner_type, match.ref.owner_id, match.score) for match in matches
    )


def stale_after(settings: Settings) -> timedelta:
    return timedelta(minutes=settings.vector_index_max_age_minutes)
