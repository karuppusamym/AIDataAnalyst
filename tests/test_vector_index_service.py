"""RT-1: the persisted vector index, and the freshness rule retrieval uses.

The behaviours that matter are the ones that decide whether a search is
served from the index or falls back:

* freshness names *why* it is unusable rather than returning a bare False;
* a model change makes the old vectors unusable, not merely stale;
* a catalog change after the last build is staleness even when the index is
  young, because that is the case that actually returns wrong results;
* rebuild is idempotent, so a schedule costs one query on an unchanged estate.

Plus the fail-closed property: with no embedding provider, this refuses
rather than backfilling with a hash double.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida.config import Settings
from aida.db import Base
from aida.embedding_provider import EmbeddingBatch, EmbeddingUnavailable
from aida.models import (
    DataSource,
    Embedding,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.vector_index_service import (
    index_freshness,
    rebuild_vector_index,
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "vector_index_backend": "postgres_bruteforce",
        "vector_index_max_age_minutes": 1440,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


class _StubEmbeddingProvider:
    """Deterministic vectors, so a test asserts on wiring rather than on a
    model. Length-3 so the dimension check in the store has something real."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.calls.append(list(texts))
        vectors = tuple((float(len(text)), 1.0, 0.5) for text in texts)
        return EmbeddingBatch(
            vectors=vectors, model_id="stub", provider="stub", dimensions=3
        )


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_estate(
    session: AsyncSession, *, tables: int = 2
) -> tuple[Organization, DataSource, list[MetadataTable]]:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    lob = LineOfBusiness(organization_id=org.id, name="Retail", code="RETAIL")
    session.add(lob)
    await session.flush()
    from aida.models import DataDomain

    domain = DataDomain(
        organization_id=org.id, line_of_business_id=lob.id, name="Finance", code="FIN"
    )
    session.add(domain)
    await session.flush()
    project = Project(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:6]}",
    )
    session.add(project)
    await session.flush()
    datasource = DataSource(
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="warehouse",
        connector_type="POSTGRES",
        dialect="postgres",
        environment="TEST",
        credential_reference="vault://x",
    )
    session.add(datasource)
    await session.flush()
    catalog = MetadataCatalog(
        organization_id=org.id, datasource_id=datasource.id, name="wh", fingerprint="fp"
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    made: list[MetadataTable] = []
    for index in range(tables):
        table = MetadataTable(
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=f"accounts_{index}",
            object_type="TABLE",
            status="ACTIVE",
            fingerprint="fp",
        )
        session.add(table)
        await session.flush()
        session.add(
            MetadataColumn(
                organization_id=org.id,
                table_id=table.id,
                name="balance",
                ordinal_position=1,
                physical_type="NUMERIC",
                nullable=False,
                status="ACTIVE",
                fingerprint="fp",
            )
        )
        made.append(table)
    await session.flush()
    return org, datasource, made


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_empty_index_is_not_usable_and_says_so(session: AsyncSession) -> None:
    org, _ds, _tables = await _seed_estate(session)
    freshness = await index_freshness(session, org.id, settings=_settings())
    assert freshness.usable is False
    assert freshness.reason == "EMPTY"
    assert freshness.entries == 0


@pytest.mark.asyncio
async def test_a_disabled_backend_is_reported_as_disabled(session: AsyncSession) -> None:
    org, _ds, _tables = await _seed_estate(session)
    freshness = await index_freshness(
        session, org.id, settings=_settings(vector_index_backend="disabled")
    )
    assert freshness.usable is False
    assert freshness.reason == "DISABLED"


@pytest.mark.asyncio
async def test_a_fresh_index_is_usable(session: AsyncSession, monkeypatch) -> None:
    org, _ds, tables = await _seed_estate(session)
    provider = _StubEmbeddingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: provider
    )
    await rebuild_vector_index(session, org.id, settings=_settings())
    await session.flush()
    # Every table's `updated_at` predates the index build, so the catalog has
    # not moved on.
    freshness = await index_freshness(
        session, org.id, settings=_settings(), now=datetime.now(UTC) + timedelta(seconds=1)
    )
    assert freshness.usable is True, freshness.reason
    assert freshness.entries > 0


@pytest.mark.asyncio
async def test_an_old_index_is_stale(session: AsyncSession, monkeypatch) -> None:
    org, _ds, _tables = await _seed_estate(session)
    provider = _StubEmbeddingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: provider
    )
    await rebuild_vector_index(session, org.id, settings=_settings())
    await session.flush()
    later = datetime.now(UTC) + timedelta(minutes=2000)
    freshness = await index_freshness(session, org.id, settings=_settings(), now=later)
    assert freshness.usable is False
    assert freshness.reason == "STALE"


@pytest.mark.asyncio
async def test_a_catalog_change_after_the_build_is_staleness(
    session: AsyncSession, monkeypatch
) -> None:
    """The subtler staleness, and the one that actually returns wrong
    results: the index is young but is missing objects."""
    org, _ds, tables = await _seed_estate(session)
    provider = _StubEmbeddingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: provider
    )
    await rebuild_vector_index(session, org.id, settings=_settings())
    await session.flush()

    tables[0].updated_at = datetime.now(UTC) + timedelta(minutes=5)
    await session.flush()

    freshness = await index_freshness(
        session, org.id, settings=_settings(), now=datetime.now(UTC) + timedelta(minutes=6)
    )
    assert freshness.usable is False
    assert freshness.reason == "STALE_CATALOG_MOVED"


@pytest.mark.asyncio
async def test_a_different_embedding_model_makes_the_index_unusable_not_stale(
    session: AsyncSession, monkeypatch
) -> None:
    """Comparing vectors across models is meaningless, so a model change is
    an empty index under the new signature rather than an old one."""
    org, _ds, _tables = await _seed_estate(session)
    provider = _StubEmbeddingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: provider
    )
    await rebuild_vector_index(session, org.id, settings=_settings())
    await session.flush()

    other = _settings(embedding_model_id="a-different-model")
    freshness = await index_freshness(session, org.id, settings=other)
    assert freshness.usable is False
    assert freshness.reason == "EMPTY"


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebuild_embeds_tables_columns_and_is_organization_scoped(
    session: AsyncSession, monkeypatch
) -> None:
    org, _ds, _tables = await _seed_estate(session, tables=2)
    other_org, _ods, _ot = await _seed_estate(session, tables=2)
    provider = _StubEmbeddingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: provider
    )

    result = await rebuild_vector_index(session, org.id, settings=_settings())
    await session.flush()

    assert result.considered == 4  # 2 tables + 2 columns
    assert result.embedded == 4
    rows = (
        await session.scalars(
            select(Embedding).where(Embedding.organization_id == other_org.id)
        )
    ).all()
    assert rows == []


@pytest.mark.asyncio
async def test_rebuild_is_idempotent_and_costs_no_model_calls_when_unchanged(
    session: AsyncSession, monkeypatch
) -> None:
    org, _ds, _tables = await _seed_estate(session, tables=2)
    provider = _StubEmbeddingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: provider
    )

    await rebuild_vector_index(session, org.id, settings=_settings())
    await session.flush()
    calls_after_first = len(provider.calls)

    second = await rebuild_vector_index(session, org.id, settings=_settings())
    await session.flush()

    assert second.embedded == 0
    assert second.skipped_unchanged == second.considered
    assert len(provider.calls) == calls_after_first, "an unchanged estate re-embedded"


@pytest.mark.asyncio
async def test_rebuild_records_an_audit_row(session: AsyncSession, monkeypatch) -> None:
    from aida.models import AuditEvent

    org, _ds, _tables = await _seed_estate(session)
    provider = _StubEmbeddingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: provider
    )
    await rebuild_vector_index(session, org.id, settings=_settings())
    await session.flush()
    rows = (
        await session.scalars(
            select(AuditEvent).where(AuditEvent.action == "retrieval.vector_index.rebuild")
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].details["embedded"] > 0


@pytest.mark.asyncio
async def test_rebuild_fails_closed_with_no_embedding_provider(
    session: AsyncSession, monkeypatch
) -> None:
    """INV-4. Backfilling with a hash double was a real defect once: a
    SHA-256 digest has no semantic structure, so fusion ranked on noise."""
    org, _ds, _tables = await _seed_estate(session)

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise EmbeddingUnavailable("EMBEDDING_PROVIDER_NOT_CONFIGURED")

    monkeypatch.setattr("aida.vector_index_service.resolve_embedding_provider", _refuse)
    with pytest.raises(EmbeddingUnavailable):
        await rebuild_vector_index(session, org.id, settings=_settings())


@pytest.mark.asyncio
async def test_rebuild_refuses_rather_than_building_a_partial_index(
    session: AsyncSession, monkeypatch
) -> None:
    """A silently partial index is worse than a refusal: retrieval would
    serve confident answers from a fraction of the estate."""
    org, _ds, _tables = await _seed_estate(session, tables=3)
    provider = _StubEmbeddingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: provider
    )
    with pytest.raises(EmbeddingUnavailable, match="TOO_LARGE"):
        await rebuild_vector_index(session, org.id, settings=_settings(), max_objects=2)


@pytest.mark.asyncio
async def test_a_deprecated_table_is_not_indexed(
    session: AsyncSession, monkeypatch
) -> None:
    """A deprecated table answering a semantic search is a wrong answer with
    a confident score."""
    org, _ds, tables = await _seed_estate(session, tables=2)
    tables[0].status = "DEPRECATED"
    await session.flush()
    provider = _StubEmbeddingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: provider
    )
    result = await rebuild_vector_index(session, org.id, settings=_settings())
    # one live table + its column; the deprecated table's column is excluded
    # too because the column query joins through an ACTIVE table.
    assert result.considered == 2


@pytest.mark.asyncio
async def test_the_index_stores_a_hash_not_the_text(
    session: AsyncSession, monkeypatch
) -> None:
    """INV-6: the index keeps a vector and a digest, never the text."""
    org, _ds, _tables = await _seed_estate(session, tables=1)
    provider = _StubEmbeddingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: provider
    )
    await rebuild_vector_index(session, org.id, settings=_settings())
    await session.flush()
    rows = (
        await session.scalars(
            select(Embedding).where(Embedding.organization_id == org.id)
        )
    ).all()
    assert rows
    for row in rows:
        assert len(row.text_hash) == 64
        assert "accounts_" not in row.text_hash
