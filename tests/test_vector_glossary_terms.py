"""R11-FP08: a glossary term is in the persisted vector index, under the name the live path embeds.

The collector filtered `GlossaryTerm.lifecycle_status == "PUBLISHED"` -- a value nothing writes,
because terms are ACTIVE or DEPRECATED -- so no glossary term had ever been indexed and every
question that surfaced one paid a provider call to embed it live. And a term's name is not its
`term_key`: a GLOSSARY_TERM hit's display name is its APPROVED version's `display_name`, so an
entry built from the key would not have matched what the live path embeds. These tests pin the
fix, one clause each:

* **the right terms** -- ACTIVE, with an APPROVED version; not deprecated, not a proposal, not a
  superseded or rejected definition; the newest approved version if a history ever held two;
* **the same text, both sides** -- the index composes `GLOSSARY_TERM <display name>` through
  `compose_vector_texts`, exactly what `retrieval_stages._live_vector_scores` embeds for the hit the
  real lexical stage produces, and the stored fingerprint is that text's;
* **not stale on arrival** -- `stale_index_entries` compares fingerprints, so an entry that
  disagrees with the live text would be reported stale at once and never served. An indexed term is
  served from the index and costs the question's one embedding, not a second call for itself;
* **stale when it is** -- a term renamed by a newly approved version is kept out of the persisted
  search until a rebuild re-embeds it;
* **scope** -- organization-wide, so a datasource-scoped rebuild leaves terms alone (as before),
  and another organization's terms are never read (INV-5).

Real SQLite, the real lexical stage, the real index builder and the real brute-force index. The
only embedding provider is a deterministic in-process stub, and a guard makes any attempt to
resolve a real one fail the test: nothing here can reach a provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida import retrieval_stages as stages
from aida import vector_index_service
from aida.embedding_provider import EmbeddingBatch
from aida.models import (
    DataDomain,
    DataSource,
    Embedding,
    GlossaryTerm,
    GlossaryTermVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticMetric,
    TermSemanticBinding,
)
from aida.retrieval_stages import RetrievalRequest, run_vector_channel, select_authorized_candidates
from aida.vector_index_service import (
    _indexable_objects,
    compose_vector_texts,
    rebuild_vector_index,
    stale_index_entries,
    text_fingerprint,
)
from atlas.platform.config import Settings
from atlas.platform.db import Base

QUESTION = "net exposure"
TERM_KEY = "net_exposure_v2"
DISPLAY_NAME = "Net Exposure"
RENAMED = "Net Counterparty Exposure"
DEFINITION = "Exposure to a counterparty after collateral has been netted against it."
#: Words that live only in the definition and the synonyms, which the lexical stage reads and the
#: embedded text has never carried on either side.
DEFINITION_MARKER = "zz_definition_marker_collateral"
SYNONYM_MARKER = "zz_synonym_marker"
_PAST = datetime(2026, 9, 1, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        vector_index_backend="postgres_bruteforce",
        vector_index_max_age_minutes=1440,
    )


class _RecordingProvider:
    """Deterministic vectors computed in-process, and every text it was asked to embed."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        self.calls.append(list(texts))
        vectors = tuple((float(len(text)), 1.0, 0.5) for text in texts)
        return EmbeddingBatch(vectors=vectors, model_id="stub", provider="stub", dimensions=3)

    @property
    def embedded(self) -> list[str]:
        return [text for call in self.calls for text in call]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _RecordingProvider:
    """The stub, wherever the index or the stage resolves a provider -- and nothing real."""
    stub = _RecordingProvider()

    def _no_real_provider(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test resolved a real embedding provider")

    monkeypatch.setattr("aida.embedding_provider.resolve_embedding_provider", _no_real_provider)
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: stub
    )
    monkeypatch.setattr(stages, "resolve_embedding_provider", lambda *a, **k: stub)
    return stub


async def _estate(session: AsyncSession) -> dict[str, Any]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Risk", code=f"R{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Exposure",
        code=f"E{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="primary",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
    )
    session.add_all([org, lob, domain, project, datasource])
    await session.flush()
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="risk", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="positions",
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(table)
    await session.flush()
    return {"org": org, "project": project, "datasource": datasource, "table": table}


async def _term(
    session: AsyncSession,
    estate: dict[str, Any],
    *,
    term_key: str = TERM_KEY,
    display_name: str = DISPLAY_NAME,
    lifecycle_status: str = "ACTIVE",
    version_status: str = "APPROVED",
    version: int = 1,
    term: GlossaryTerm | None = None,
    bind: bool = False,
) -> GlossaryTerm:
    """A term with one version; `term=` adds a further version to an existing one."""
    org = estate["org"]
    if term is None:
        term = GlossaryTerm(
            id=uuid4(),
            organization_id=org.id,
            term_key=term_key,
            lifecycle_status=lifecycle_status,
        )
        session.add(term)
        await session.flush()
    session.add(
        GlossaryTermVersion(
            id=uuid4(),
            organization_id=org.id,
            term_id=term.id,
            version=version,
            status=version_status,
            display_name=display_name,
            definition=f"{DEFINITION} {DEFINITION_MARKER}",
            synonyms=[SYNONYM_MARKER],
            created_by="steward",
            approved_by="reviewer" if version_status == "APPROVED" else None,
            approved_at=_PAST if version_status == "APPROVED" else None,
        )
    )
    await session.flush()
    if bind:
        # Only a term with an ACTIVE binding to a metric of the datasource's project surfaces as
        # a GLOSSARY_TERM hit in the lexical stage, which is where the live path gets its name.
        metric = SemanticMetric(
            id=uuid4(),
            organization_id=org.id,
            project_id=estate["project"].id,
            slug=f"metric-{uuid4().hex[:8]}",
        )
        session.add(metric)
        await session.flush()
        session.add(
            TermSemanticBinding(
                id=uuid4(),
                organization_id=org.id,
                term_id=term.id,
                semantic_object_type="METRIC",
                semantic_object_id=metric.id,
                status="ACTIVE",
                requested_by="steward",
                approved_by="reviewer",
                approved_at=_PAST,
            )
        )
        await session.flush()
    return term


def _request(estate: dict[str, Any]) -> RetrievalRequest:
    return RetrievalRequest(
        datasource=estate["datasource"],
        question=QUESTION,
        settings=_settings(),
        organization_id=estate["org"].id,
    )


async def _index_texts(
    session: AsyncSession, estate: dict[str, Any], datasource_id: Any = None
) -> dict[tuple[str, str], str]:
    return {
        (owner_type, owner_id): text
        for owner_type, owner_id, text in await _indexable_objects(
            session, estate["org"].id, datasource_id
        )
    }


def _key(term: GlossaryTerm) -> tuple[str, str]:
    return ("GLOSSARY_TERM", str(term.id))


# ---------------------------------------------------------------------------
# The right terms, under the right name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_active_term_is_indexed_under_its_approved_display_name_not_its_key(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    estate = await _estate(session)
    term = await _term(session, estate)

    texts = await _index_texts(session, estate)

    assert texts[_key(term)] == f"GLOSSARY_TERM {DISPLAY_NAME}"
    assert not [text for text in texts.values() if TERM_KEY in text], "the key is not the name"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lifecycle_status", "version_status"),
    [
        ("DEPRECATED", "APPROVED"),
        ("ACTIVE", "DRAFT"),
        ("ACTIVE", "REVIEW_REQUIRED"),
        ("ACTIVE", "REJECTED"),
        ("ACTIVE", "SUPERSEDED"),
        ("ACTIVE", "DEPRECATED"),
        ("PUBLISHED", "APPROVED"),
    ],
    ids=lambda value: str(value),
)
async def test_a_term_that_is_not_active_and_approved_is_not_indexed(
    session: AsyncSession,
    provider: _RecordingProvider,
    lifecycle_status: str,
    version_status: str,
) -> None:
    """Deprecated terms, proposals nobody decided, retired definitions -- and `PUBLISHED`, the
    lifecycle the old filter asked for, which nothing has ever written."""
    estate = await _estate(session)
    term = await _term(
        session, estate, lifecycle_status=lifecycle_status, version_status=version_status
    )

    assert _key(term) not in await _index_texts(session, estate)


@pytest.mark.asyncio
async def test_the_approved_definition_is_the_one_embedded(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    """Approving a version supersedes the previous one; the older name is text Atlas replaced."""
    estate = await _estate(session)
    term = await _term(session, estate, display_name="Old Exposure", version_status="SUPERSEDED")
    await _term(session, estate, term=term, display_name=DISPLAY_NAME, version=2)

    texts = await _index_texts(session, estate)

    assert texts[_key(term)] == f"GLOSSARY_TERM {DISPLAY_NAME}"
    assert not [text for text in texts.values() if "Old Exposure" in text]


@pytest.mark.asyncio
async def test_if_a_history_ever_held_two_approved_versions_the_newest_is_embedded(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    estate = await _estate(session)
    term = await _term(session, estate, display_name="Older Exposure", version=1)
    await _term(session, estate, term=term, display_name=DISPLAY_NAME, version=2)

    assert (await _index_texts(session, estate))[_key(term)] == f"GLOSSARY_TERM {DISPLAY_NAME}"


@pytest.mark.asyncio
async def test_neither_the_definition_nor_the_synonyms_are_embedded(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    """They feed the lexical stage. The live path has never embedded them, so the index must not
    -- a stored vector of text the live path never composes is the divergence FP08 exists to end."""
    estate = await _estate(session)
    await _term(session, estate)
    await rebuild_vector_index(session, estate["org"].id, settings=_settings())

    for text in [*provider.embedded, *(await _index_texts(session, estate)).values()]:
        assert DEFINITION_MARKER not in text and SYNONYM_MARKER not in text


# ---------------------------------------------------------------------------
# One text, both sides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_index_and_the_live_path_compose_the_same_text_for_a_glossary_hit(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    """Driven from the real lexical stage, so the display name is the one the hit carries."""
    estate = await _estate(session)
    term = await _term(session, estate, bind=True)
    request = _request(estate)

    index_text = (await _index_texts(session, estate))[_key(term)]
    pool = await select_authorized_candidates(session, request)
    glossary = [hit for hit in pool.authorized if hit.object_type == "GLOSSARY_TERM"]
    assert [hit.object_id for hit in glossary] == [str(term.id)], pool.authorized
    assert glossary[0].display_name == DISPLAY_NAME, "a hit's name is the approved version's"
    await stages._live_vector_scores(session, provider, request, glossary)
    live_text = provider.calls[-1][1]  # the first text of a live call is the question

    assert live_text == index_text == f"GLOSSARY_TERM {DISPLAY_NAME}"


@pytest.mark.asyncio
async def test_a_rebuild_embeds_the_term_once_and_stores_that_texts_fingerprint(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    estate = await _estate(session)
    term = await _term(session, estate)
    org_id = estate["org"].id

    first = await rebuild_vector_index(session, org_id, settings=_settings())
    await session.flush()

    assert f"GLOSSARY_TERM {DISPLAY_NAME}" in provider.embedded
    assert first.embedded == first.considered
    stored = await session.scalar(
        select(Embedding.text_hash).where(
            Embedding.owner_type == "GLOSSARY_TERM", Embedding.owner_id == str(term.id)
        )
    )
    assert stored == text_fingerprint(f"GLOSSARY_TERM {DISPLAY_NAME}")

    calls = len(provider.calls)
    second = await rebuild_vector_index(session, org_id, settings=_settings())
    assert (second.embedded, second.skipped_unchanged) == (0, second.considered)
    assert len(provider.calls) == calls, "an unchanged estate costs no embedding call"


@pytest.mark.asyncio
async def test_an_indexed_term_is_not_reported_stale(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    """The stale check hashes the text a hit composes *now* and compares it with the stored hash."""
    estate = await _estate(session)
    term = await _term(session, estate, bind=True)
    org_id = estate["org"].id
    pool = await select_authorized_candidates(session, _request(estate))
    glossary = [hit for hit in pool.authorized if hit.object_type == "GLOSSARY_TERM"]
    expected_texts = await compose_vector_texts(
        session,
        org_id,
        [(hit.object_type, str(hit.object_id), hit.display_name) for hit in glossary],
    )
    expected = {
        (hit.object_type, str(hit.object_id)): text_fingerprint(text)
        for hit, text in zip(glossary, expected_texts, strict=True)
    }
    assert set(expected) == {_key(term)}
    before = await stale_index_entries(session, org_id, expected, settings=_settings())
    assert before == {_key(term)}, "control: with no index built, the term is missing, so stale"

    await rebuild_vector_index(session, org_id, settings=_settings())
    await session.flush()

    assert await stale_index_entries(session, org_id, expected, settings=_settings()) == set()


async def _vector_stage(
    session: AsyncSession, estate: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[tuple[str, str], float], list[tuple[str, str]]]:
    """Run the real vector stage; return its scores and what the persisted search was asked."""
    searched: list[tuple[str, str]] = []
    real_search = vector_index_service.search_persisted_index

    async def _recording_search(*args: Any, **kwargs: Any) -> Any:
        searched.extend((ref.owner_type, ref.owner_id) for ref in kwargs["candidates"])
        return await real_search(*args, **kwargs)

    monkeypatch.setattr(vector_index_service, "search_persisted_index", _recording_search)
    request = _request(estate)
    pool = await select_authorized_candidates(session, request)
    result = await run_vector_channel(session, request, pool)
    assert result.report.skipped_reason is None
    return {(c.object_type, c.object_id): c.raw_score for c in result.contributions}, searched


@pytest.mark.asyncio
async def test_the_vector_stage_serves_an_indexed_term_from_the_index_with_no_live_embed(
    session: AsyncSession, provider: _RecordingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = await _estate(session)
    term = await _term(session, estate, bind=True)
    await rebuild_vector_index(session, estate["org"].id, settings=_settings())
    await session.flush()

    provider.calls.clear()
    scores, searched = await _vector_stage(session, estate, monkeypatch)

    assert _key(term) in searched, "the persisted search was asked to score the term"
    assert _key(term) in scores
    assert provider.calls == [[QUESTION]], "a current entry costs the question's one embedding"


@pytest.mark.asyncio
async def test_before_the_index_holds_the_term_it_is_embedded_live_as_it_always_was(
    session: AsyncSession, provider: _RecordingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the change is the collector, not a new way to be scored. A term the index does
    not hold yet -- approved since the last build -- is embedded live and still scored."""
    estate = await _estate(session)
    await rebuild_vector_index(session, estate["org"].id, settings=_settings())  # tables only
    term = await _term(session, estate, bind=True)
    await session.flush()

    provider.calls.clear()
    scores, searched = await _vector_stage(session, estate, monkeypatch)

    assert _key(term) not in searched and _key(term) in scores
    assert f"GLOSSARY_TERM {DISPLAY_NAME}" in [t for call in provider.calls[1:] for t in call]


@pytest.mark.asyncio
async def test_a_term_renamed_by_a_new_approval_is_kept_off_its_old_vector_until_rebuilt(
    session: AsyncSession, provider: _RecordingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = await _estate(session)
    term = await _term(session, estate, bind=True)
    org_id = estate["org"].id
    await rebuild_vector_index(session, org_id, settings=_settings())
    await session.flush()

    # A new version is approved and supersedes the first: the catalog row `index_freshness`
    # watches does not move, so the index stays USABLE while this term's vector is out of date.
    older = await session.scalar(
        select(GlossaryTermVersion).where(GlossaryTermVersion.term_id == term.id)
    )
    assert older is not None
    older.status = "SUPERSEDED"
    await _term(session, estate, term=term, display_name=RENAMED, version=2)

    provider.calls.clear()
    scores, searched = await _vector_stage(session, estate, monkeypatch)
    assert _key(term) not in searched, "the persisted search scored a vector of a retired name"
    assert _key(term) in scores
    assert f"GLOSSARY_TERM {RENAMED}" in [t for call in provider.calls[1:] for t in call]

    rebuilt = await rebuild_vector_index(session, org_id, settings=_settings())
    await session.flush()
    assert (rebuilt.embedded, rebuilt.skipped_unchanged) == (1, rebuilt.considered - 1)
    assert provider.calls[-1] == [f"GLOSSARY_TERM {RENAMED}"]
    provider.calls.clear()
    _, searched = await _vector_stage(session, estate, monkeypatch)
    assert _key(term) in searched and provider.calls == [[QUESTION]]


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_datasource_scoped_rebuild_leaves_glossary_terms_alone(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    """Terms are organization-wide: one source's schedule does not re-embed the whole glossary."""
    estate = await _estate(session)
    term = await _term(session, estate)

    scoped = await _index_texts(session, estate, estate["datasource"].id)
    unscoped = await _index_texts(session, estate)

    assert _key(term) not in scoped
    assert _key(term) in unscoped


@pytest.mark.asyncio
async def test_another_organizations_terms_are_never_indexed(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    """INV-5: the collector restates the organization on the term and on its version."""
    ours = await _estate(session)
    theirs = await _estate(session)
    our_term = await _term(session, ours, term_key="ours")
    their_term = await _term(session, theirs, term_key="theirs", display_name="Their Exposure")

    ours_indexed = await _index_texts(session, ours)
    theirs_indexed = await _index_texts(session, theirs)

    assert _key(our_term) in ours_indexed and _key(their_term) not in ours_indexed
    assert _key(their_term) in theirs_indexed and _key(our_term) not in theirs_indexed
    assert not [text for text in ours_indexed.values() if "Their Exposure" in text]


@pytest.mark.asyncio
async def test_a_versions_organization_must_match_its_terms(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    """A version row filed under another organization is not this organization's definition."""
    ours = await _estate(session)
    theirs = await _estate(session)
    term = await _term(session, ours, version_status="DRAFT")
    foreign = GlossaryTermVersion(
        id=uuid4(),
        organization_id=theirs["org"].id,
        term_id=term.id,
        version=2,
        status="APPROVED",
        display_name="Foreign Definition",
        definition="filed under another organization",
        synonyms=[],
        created_by="steward",
    )
    session.add(foreign)
    await session.flush()

    assert _key(term) not in await _index_texts(session, ours)


@pytest.mark.asyncio
async def test_a_term_of_another_organization_is_not_ours_even_with_a_version_filed_here(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    """The other half of the restatement: the term's own organization counts as well as the
    version's, so a row filed inconsistently cannot carry another tenant's term into this index."""
    ours = await _estate(session)
    theirs = await _estate(session)
    foreign_term = await _term(session, theirs, version_status="DRAFT")
    session.add(
        GlossaryTermVersion(
            id=uuid4(),
            organization_id=ours["org"].id,
            term_id=foreign_term.id,
            version=2,
            status="APPROVED",
            display_name="Filed Under The Wrong Organization",
            definition="a version of another organization's term",
            synonyms=[],
            created_by="steward",
        )
    )
    await session.flush()

    assert _key(foreign_term) not in await _index_texts(session, ours)
