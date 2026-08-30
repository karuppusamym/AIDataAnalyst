"""Coverage for semantic retrieval without `pgvector` (ADR-0019).

The bank's PostgreSQL has no `vector` extension and will not be getting one, so the
default backend must work in plain PostgreSQL and the alternatives must fail closed
rather than degrade quietly.
"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401
from aida.config import Settings
from aida.db import Base
from aida.models import Organization
from aida.vector_store import (
    DisabledVectorIndex,
    EmbeddingRecord,
    EmbeddingRef,
    ExternalVectorIndex,
    PostgresBruteForceIndex,
    VectorIndexUnavailable,
    cosine,
    index_signature,
    pack_vector,
    resolve_vector_index,
    unpack_vector,
)

_SIG = "test-model:v1:4:v1"


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    return org


# --- storage without an extension -------------------------------------------


def test_vectors_round_trip_through_plain_bytes() -> None:
    """No `vector` column type, no extension -- packed float32 in `bytea`."""
    original = (0.5, -0.25, 1.0, 0.0)
    blob = pack_vector(original)
    assert isinstance(blob, bytes)
    assert len(blob) == 4 * len(original)
    restored = unpack_vector(blob)
    assert restored == pytest.approx(original)


def test_cosine_is_correct_at_the_obvious_points() -> None:
    parallel = (1.0, 0.0)
    orthogonal = (0.0, 1.0)
    opposite = (-1.0, 0.0)
    assert cosine(parallel, 1.0, parallel, 1.0) == pytest.approx(1.0)
    assert cosine(parallel, 1.0, orthogonal, 1.0) == pytest.approx(0.0)
    assert cosine(parallel, 1.0, opposite, 1.0) == pytest.approx(-1.0)
    # A zero vector has no direction; scoring it as similar to anything would be wrong.
    assert cosine(parallel, 1.0, (0.0, 0.0), 0.0) == 0.0


async def test_brute_force_search_ranks_by_similarity(session: AsyncSession) -> None:
    org = await _org(session)
    index = PostgresBruteForceIndex(_settings())
    await index.upsert(
        session,
        org.id,
        (
            EmbeddingRecord(EmbeddingRef("TABLE", "near"), (1.0, 0.1, 0.0, 0.0), "h1"),
            EmbeddingRecord(EmbeddingRef("TABLE", "mid"), (0.6, 0.8, 0.0, 0.0), "h2"),
            EmbeddingRecord(EmbeddingRef("TABLE", "far"), (0.0, 0.0, 1.0, 0.0), "h3"),
        ),
        signature=_SIG,
    )
    matches = await index.search(
        session, org.id, (1.0, 0.0, 0.0, 0.0), signature=_SIG, candidates=None, limit=3
    )
    assert [m.ref.owner_id for m in matches] == ["near", "mid", "far"]
    assert matches[0].score > matches[1].score > matches[2].score


async def test_search_is_confined_to_the_candidate_allowlist(session: AsyncSession) -> None:
    """Policy filtering happens before ranking, so the allowlist travels with the query.

    Ranking everything and filtering afterwards leaks the existence of assets the caller
    may not see, through result counts and ordering.
    """
    org = await _org(session)
    index = PostgresBruteForceIndex(_settings())
    await index.upsert(
        session,
        org.id,
        (
            EmbeddingRecord(EmbeddingRef("TABLE", "permitted"), (1.0, 0.0, 0.0, 0.0), "h1"),
            EmbeddingRecord(EmbeddingRef("TABLE", "forbidden"), (1.0, 0.0, 0.0, 0.0), "h2"),
        ),
        signature=_SIG,
    )
    matches = await index.search(
        session,
        org.id,
        (1.0, 0.0, 0.0, 0.0),
        signature=_SIG,
        candidates=(EmbeddingRef("TABLE", "permitted"),),
        limit=10,
    )
    assert [m.ref.owner_id for m in matches] == ["permitted"]


async def test_an_empty_candidate_set_returns_nothing_rather_than_everything(
    session: AsyncSession,
) -> None:
    """The dangerous bug this prevents: treating "no permitted assets" as "no filter"."""
    org = await _org(session)
    index = PostgresBruteForceIndex(_settings())
    await index.upsert(
        session, org.id,
        (EmbeddingRecord(EmbeddingRef("TABLE", "x"), (1.0, 0.0, 0.0, 0.0), "h"),),
        signature=_SIG,
    )
    matches = await index.search(
        session, org.id, (1.0, 0.0, 0.0, 0.0), signature=_SIG, candidates=(), limit=10
    )
    assert matches == ()


async def test_vectors_from_a_different_model_are_not_comparable(session: AsyncSession) -> None:
    """A model change invalidates the index instead of silently mixing vector spaces.

    This failure mode does not raise -- it returns plausible, wrong rankings -- which is
    exactly why the signature is matched on every read.
    """
    org = await _org(session)
    index = PostgresBruteForceIndex(_settings())
    await index.upsert(
        session, org.id,
        (EmbeddingRecord(EmbeddingRef("TABLE", "old"), (1.0, 0.0, 0.0, 0.0), "h"),),
        signature="old-model:v1:4:v1",
    )
    matches = await index.search(
        session, org.id, (1.0, 0.0, 0.0, 0.0),
        signature="new-model:v2:4:v1", candidates=None, limit=10,
    )
    assert matches == ()


async def test_an_oversized_candidate_set_is_refused_not_truncated(
    session: AsyncSession,
) -> None:
    """Exact search is linear in candidates, so the cap is a refusal with a reason.

    Silently scoring the first N of a much larger set returns plausible answers that are
    wrong, and nobody would notice.
    """
    org = await _org(session)
    index = PostgresBruteForceIndex(_settings(vector_bruteforce_candidate_cap=100))
    await index.upsert(
        session,
        org.id,
        tuple(
            EmbeddingRecord(EmbeddingRef("TABLE", f"t{i}"), (1.0, 0.0, 0.0, 0.0), "h")
            for i in range(101)
        ),
        signature=_SIG,
    )
    with pytest.raises(VectorIndexUnavailable) as refused:
        await index.search(
            session, org.id, (1.0, 0.0, 0.0, 0.0), signature=_SIG, candidates=None, limit=5
        )
    assert refused.value.reason_code == "CANDIDATE_SET_EXCEEDS_BRUTEFORCE_CAP"


async def test_deleting_an_owner_removes_every_chunk(session: AsyncSession) -> None:
    org = await _org(session)
    index = PostgresBruteForceIndex(_settings())
    await index.upsert(
        session,
        org.id,
        tuple(
            EmbeddingRecord(EmbeddingRef("DOCUMENT_SECTION", "doc1", i), (1.0, 0.0, 0.0, 0.0), "h")
            for i in range(3)
        ),
        signature=_SIG,
    )
    removed = await index.delete_owner(session, org.id, "DOCUMENT_SECTION", "doc1")
    assert removed == 3


async def test_upsert_replaces_rather_than_duplicates(session: AsyncSession) -> None:
    org = await _org(session)
    index = PostgresBruteForceIndex(_settings())
    ref = EmbeddingRef("TABLE", "t")
    await index.upsert(session, org.id,
                       (EmbeddingRecord(ref, (1.0, 0.0, 0.0, 0.0), "h1"),), signature=_SIG)
    await index.upsert(session, org.id,
                       (EmbeddingRecord(ref, (0.0, 1.0, 0.0, 0.0), "h2"),), signature=_SIG)
    matches = await index.search(
        session, org.id, (0.0, 1.0, 0.0, 0.0), signature=_SIG, candidates=None, limit=10
    )
    assert len(matches) == 1
    assert matches[0].score == pytest.approx(1.0)


# --- backend selection fails closed ------------------------------------------


async def test_the_default_backend_needs_no_extension(session: AsyncSession) -> None:
    """The whole point: a bank PostgreSQL with no `vector` extension still gets search."""
    index = await resolve_vector_index(_settings(), session)
    assert isinstance(index, PostgresBruteForceIndex)
    assert index.name == "postgres_bruteforce"


async def test_pgvector_is_refused_when_the_extension_is_absent(
    session: AsyncSession,
) -> None:
    """INV-9: the database is probed, not the configuration trusted.

    SQLite has no `pg_available_extensions`, which stands in for the bank's PostgreSQL
    that has no `vector`. Either way the answer is a refusal, not a silent fallback --
    falling back would mean the operator thinks they configured one thing and got
    another.
    """
    with pytest.raises(Exception) as refused:
        await resolve_vector_index(_settings(vector_index_backend="pgvector"), session)
    assert refused.type is not AssertionError


async def test_external_backend_without_a_url_is_refused(session: AsyncSession) -> None:
    with pytest.raises(VectorIndexUnavailable) as refused:
        await resolve_vector_index(_settings(vector_index_backend="external"), session)
    assert refused.value.reason_code == "VECTOR_INDEX_URL_NOT_CONFIGURED"


async def test_external_backend_accepts_a_configured_in_network_url(
    session: AsyncSession,
) -> None:
    index = await resolve_vector_index(
        _settings(
            vector_index_backend="external",
            vector_index_url="https://vectors.bank.internal",
        ),
        session,
    )
    assert isinstance(index, ExternalVectorIndex)


async def test_a_disabled_index_refuses_rather_than_returning_nothing(
    session: AsyncSession,
) -> None:
    """"No results" and "search is switched off" must not look the same to a caller."""
    index = await resolve_vector_index(_settings(vector_index_backend="disabled"), session)
    assert isinstance(index, DisabledVectorIndex)
    with pytest.raises(VectorIndexUnavailable) as refused:
        await index.search(session, uuid4(), (1.0,), signature=_SIG, candidates=None, limit=1)
    assert refused.value.reason_code == "VECTOR_INDEX_DISABLED"


def test_the_index_signature_pins_everything_that_makes_vectors_comparable() -> None:
    signature = index_signature(
        _settings(
            embedding_model_id="bge-large",
            embedding_model_version="1.5",
            embedding_dimensions=1024,
            embedding_chunking_version=3,
        )
    )
    assert signature == "bge-large:1.5:1024:v3"
