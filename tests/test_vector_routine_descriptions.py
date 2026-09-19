"""R11-FP08: an approved routine description reaches the vector stage; a withdrawn one leaves it.

The first FP08 slice put an APPROVED routine description into the lexical stage and deliberately
kept it out of the embedded text, because the persisted index and the live path each composed
that text themselves and had to stay byte-identical: adding the description to one side would
have been a divergence the index's coverage report cannot see. These tests pin the change that
moves both sides together:

* **one composer** -- the index builder and the live path produce identical text for the same
  object, and for a routine that text carries its approved description;
* **only APPROVED** -- a pending draft, a superseded version and a withdrawn one embed nothing;
* **never the body, never the source's comment** -- the routine body is source-derived code and
  the comment is unreviewed, so neither enters the text on either side;
* **re-embed on change** -- a changed description changes the stored fingerprint, so a rebuild
  re-embeds exactly that routine; and *before* any rebuild, the vector stage refuses the stale
  vector and embeds the routine's current text live -- a withdrawn description stops steering
  retrieval on the next question, not on the next scheduled rebuild;
* **tenant scope** (INV-5) -- another organization never reads this one's descriptions.

Real SQLite, the real lexical stage (`select_authorized_candidates`), the real index builder and
the real brute-force index; only the embedding provider is a recording stub.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import aida.models  # noqa: F401 -- registers every table on the metadata
from aida import retrieval_stages as stages
from aida import vector_index_service
from aida.config import Settings
from aida.db import Base
from aida.embedding_provider import EmbeddingBatch
from aida.envelope_models import (
    MetadataRoutine,
    RoutineDescriptionDraft,
    RoutineDocumentationVersion,
)
from aida.models import (
    DataDomain,
    DataSource,
    Embedding,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.retrieval_stages import RetrievalRequest, run_vector_channel, select_authorized_candidates
from aida.routine_description_service import publish_routine_documentation_version
from aida.vector_index_service import (
    _indexable_objects,
    approved_routine_descriptions,
    compose_vector_texts,
    rebuild_vector_index,
    text_fingerprint,
)

QUESTION = "deposit balances"
ROUTINE_KEY_NAME = "ops.close_deposit_balances"
APPROVED_TEXT = "Recomputes the overnight closing balance held for every deposit account."
REVISED_TEXT = "Rebuilds each deposit account's closing balance from the posted ledger."
#: Words that exist only in the body and in the source's own comment. If either ever reaches
#: an embedded text, one of these is in it.
BODY_MARKER = "zz_body_marker_column"
COMMENT_MARKER = "zz_unreviewed_source_comment"
#: The stored, redacted body -- a constant, not composed, so it reads as the fixture it is.
ROUTINE_BODY = "BEGIN SELECT zz_body_marker_column FROM ops.deposit_accounts; END;"


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        vector_index_backend="postgres_bruteforce",
        vector_index_max_age_minutes=1440,
    )


class _RecordingProvider:
    """Deterministic vectors, and every text it was asked to embed, per call."""

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
    stub = _RecordingProvider()
    monkeypatch.setattr(
        "aida.vector_index_service.resolve_embedding_provider", lambda *a, **k: stub
    )
    monkeypatch.setattr(stages, "resolve_embedding_provider", lambda *a, **k: stub)
    return stub


async def _estate(session: AsyncSession) -> dict[str, Any]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"R{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Finance",
        code=f"F{uuid4().hex[:6]}",
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
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="ops", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="deposit_accounts",
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="close_deposit_balances",
        signature="(date)",
        routine_type="PROCEDURE",
        language="plpgsql",
        body_sql_redacted=ROUTINE_BODY,
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        source_description=COMMENT_MARKER,
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add_all([table, routine])
    await session.flush()
    return {
        "org": org,
        "datasource": datasource,
        "schema": schema,
        "table": table,
        "routine": routine,
    }


async def _publish(
    session: AsyncSession, routine: MetadataRoutine, description: str
) -> RoutineDocumentationVersion:
    """Through the real approval path, which supersedes the previous APPROVED version."""
    return await publish_routine_documentation_version(
        session,
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        description=description,
        created_by="steward@bank.example",
        approved_by="lead@bank.example",
        approved_at=datetime(2026, 9, 18, tzinfo=UTC),
    )


async def _withdraw_all(session: AsyncSession) -> None:
    """The status a withdrawal decision leaves (`description_withdrawal`): the text a reviewer
    retired. The mechanism under test reads status only, so the two-person withdrawal flow
    itself is `test_description_withdrawal`'s subject, not this file's."""
    await session.execute(
        update(RoutineDocumentationVersion)
        .where(RoutineDocumentationVersion.status == "APPROVED")
        .values(status="WITHDRAWN")
    )
    await session.flush()


def _request(estate: dict[str, Any]) -> RetrievalRequest:
    return RetrievalRequest(
        datasource=estate["datasource"],
        question=QUESTION,
        settings=_settings(),
        organization_id=estate["org"].id,
    )


async def _index_texts(session: AsyncSession, estate: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {
        (owner_type, owner_id): text
        for owner_type, owner_id, text in await _indexable_objects(
            session, estate["org"].id, None
        )
    }


# ---------------------------------------------------------------------------
# One composer, identical on both sides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_sides_embed_an_approved_description_in_identical_text(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    """The invariant that silently broke before: index text == live text, for the same object.

    The live side is driven from real lexical hits, so the display name the lexical stage
    produces is what the composer receives -- not a hand-built stand-in for it.
    """
    estate = await _estate(session)
    await _publish(session, estate["routine"], APPROVED_TEXT)
    request = _request(estate)

    index_texts = await _index_texts(session, estate)
    pool = await select_authorized_candidates(session, request)
    live_candidates = [hit for hit in pool.authorized if hit.object_type in ("ROUTINE", "TABLE")]
    await stages._live_vector_scores(session, provider, request, live_candidates)
    # The first text of the live call is the question; the rest are the candidates, in order.
    live_texts = {
        (hit.object_type, str(hit.object_id)): text
        for hit, text in zip(live_candidates, provider.calls[-1][1:], strict=True)
    }

    routine_key = ("ROUTINE", str(estate["routine"].id))
    table_key = ("TABLE", str(estate["table"].id))
    assert routine_key in live_texts and table_key in live_texts, pool.authorized
    for key in (routine_key, table_key):
        assert live_texts[key] == index_texts[key], (
            f"{key}: the persisted index and the live path composed different text for the "
            "same object -- a stored vector would then describe text the live path never embeds"
        )
    assert live_texts[routine_key] == f"ROUTINE {ROUTINE_KEY_NAME} {APPROVED_TEXT}"
    # Nothing outside routines moved: a table's text, and so its stored hash, is unchanged.
    assert live_texts[table_key] == "TABLE deposit_accounts"


@pytest.mark.asyncio
async def test_only_the_approved_version_is_ever_embedded(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    estate = await _estate(session)
    routine = estate["routine"]
    # A draft nobody has decided.
    session.add(
        RoutineDescriptionDraft(
            id=uuid4(),
            organization_id=routine.organization_id,
            datasource_id=routine.datasource_id,
            routine_id=routine.id,
            drafted_text="A draft sentence nobody has approved.",
            text_fingerprint="f" * 64,
            accuracy_score=0.9,
            clarity_score=0.9,
            style_score=0.9,
            completeness_score=0.9,
            overall_score=0.9,
            evidence={},
            status="PENDING_APPROVAL",
            created_by="steward@bank.example",
        )
    )
    await session.flush()
    key = ("ROUTINE", str(routine.id))
    assert (await _index_texts(session, estate))[key] == f"ROUTINE {ROUTINE_KEY_NAME}"

    await _publish(session, routine, APPROVED_TEXT)
    await _publish(session, routine, REVISED_TEXT)  # supersedes APPROVED_TEXT
    text = (await _index_texts(session, estate))[key]
    assert REVISED_TEXT in text
    assert APPROVED_TEXT not in text, "a SUPERSEDED version is text the platform replaced"

    await _withdraw_all(session)
    assert (await _index_texts(session, estate))[key] == f"ROUTINE {ROUTINE_KEY_NAME}", (
        "a WITHDRAWN version is text a reviewer retired; it must not be embedded"
    )


@pytest.mark.asyncio
async def test_the_body_and_the_source_comment_never_enter_the_embedded_text(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    estate = await _estate(session)
    await _publish(session, estate["routine"], APPROVED_TEXT)
    request = _request(estate)

    index_texts = await _index_texts(session, estate)
    pool = await select_authorized_candidates(session, request)
    await stages._live_vector_scores(session, provider, request, pool.authorized)

    assert BODY_MARKER in ROUTINE_BODY  # the marker is really in the body the index could read
    for text in [*index_texts.values(), *provider.embedded]:
        assert BODY_MARKER not in text, "the routine body reached an embedded text"
        assert COMMENT_MARKER not in text, "the source's unreviewed comment reached embedded text"


@pytest.mark.asyncio
async def test_another_organization_never_reads_this_ones_descriptions(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    """INV-5: the description read restates the organization on both tables."""
    estate = await _estate(session)
    await _publish(session, estate["routine"], APPROVED_TEXT)
    routine_id = str(estate["routine"].id)

    assert await approved_routine_descriptions(session, estate["org"].id, [routine_id]) == {
        routine_id: APPROVED_TEXT
    }
    assert await approved_routine_descriptions(session, uuid4(), [routine_id]) == {}
    assert await compose_vector_texts(
        session, uuid4(), [("ROUTINE", routine_id, ROUTINE_KEY_NAME)]
    ) == [f"ROUTINE {ROUTINE_KEY_NAME}"]
    # A malformed hit id is data, not a crash.
    assert await approved_routine_descriptions(session, estate["org"].id, ["not-a-uuid"]) == {}


# ---------------------------------------------------------------------------
# Re-embed on change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_changed_description_re_embeds_exactly_that_routine_on_rebuild(
    session: AsyncSession, provider: _RecordingProvider
) -> None:
    estate = await _estate(session)
    routine = estate["routine"]
    org_id = estate["org"].id
    await _publish(session, routine, APPROVED_TEXT)

    first = await rebuild_vector_index(session, org_id, settings=_settings())
    await session.flush()
    assert first.embedded == first.considered

    await _publish(session, routine, REVISED_TEXT)
    revised = await rebuild_vector_index(session, org_id, settings=_settings())
    await session.flush()
    assert (revised.embedded, revised.skipped_unchanged) == (1, revised.considered - 1)
    assert provider.calls[-1] == [f"ROUTINE {ROUTINE_KEY_NAME} {REVISED_TEXT}"]

    await _withdraw_all(session)
    withdrawn = await rebuild_vector_index(session, org_id, settings=_settings())
    await session.flush()
    assert (withdrawn.embedded, withdrawn.skipped_unchanged) == (1, withdrawn.considered - 1)
    assert provider.calls[-1] == [f"ROUTINE {ROUTINE_KEY_NAME}"]
    stored = await session.scalar(
        select(Embedding.text_hash).where(
            Embedding.owner_type == "ROUTINE", Embedding.owner_id == str(routine.id)
        )
    )
    assert stored == text_fingerprint(f"ROUTINE {ROUTINE_KEY_NAME}")


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
    assert {c.metadata["vector_path"] for c in result.contributions} == {"PERSISTED_INDEX"}
    return {(c.object_type, c.object_id): c.raw_score for c in result.contributions}, searched


@pytest.mark.asyncio
async def test_a_withdrawn_description_stops_steering_the_vector_stage_before_any_rebuild(
    session: AsyncSession, provider: _RecordingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this row exists to prevent: retired text ranking through a stale vector.

    `index_freshness` watches the catalog, and a withdrawal does not touch it -- so the index
    stays USABLE while the routine's stored vector still encodes the withdrawn sentence.
    """
    estate = await _estate(session)
    await _publish(session, estate["routine"], APPROVED_TEXT)
    await rebuild_vector_index(session, estate["org"].id, settings=_settings())
    await session.flush()
    routine_key = ("ROUTINE", str(estate["routine"].id))

    # Control: while the description stands, the routine is served from the index -- the
    # check costs no provider call for an entry that is still current.
    provider.calls.clear()
    scores, searched = await _vector_stage(session, estate, monkeypatch)
    assert routine_key in searched and routine_key in scores
    assert provider.calls == [[QUESTION]], "a current entry must not be re-embedded live"

    await _withdraw_all(session)
    provider.calls.clear()
    scores, searched = await _vector_stage(session, estate, monkeypatch)

    assert routine_key not in searched, (
        "the persisted search was asked to score a vector of withdrawn text"
    )
    assert routine_key in scores, "the routine lost its vector score instead of being re-embedded"
    live = [text for call in provider.calls[1:] for text in call]
    assert f"ROUTINE {ROUTINE_KEY_NAME}" in live
    assert not [text for text in provider.embedded if APPROVED_TEXT in text], (
        "withdrawn text reached the embedding provider"
    )
    # The table's entry is untouched and still served from the index.
    assert ("TABLE", str(estate["table"].id)) in searched


@pytest.mark.asyncio
async def test_a_routine_discovered_after_the_build_is_scored_not_dropped(
    session: AsyncSession, provider: _RecordingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing entry is stale too. `index_freshness` watches tables only, so a routine added
    after the build left the index USABLE and the routine silently unscored (the R11-B2 shape)."""
    estate = await _estate(session)
    await rebuild_vector_index(session, estate["org"].id, settings=_settings())
    await session.flush()
    late = MetadataRoutine(
        id=uuid4(),
        organization_id=estate["org"].id,
        datasource_id=estate["datasource"].id,
        schema_id=estate["schema"].id,
        name="deposit_balance_audit",
        signature="()",
        routine_type="FUNCTION",
        language="sql",
        body_sql_redacted="SELECT 1",
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        status="ACTIVE",
        fingerprint="fp",
    )
    session.add(late)
    await session.flush()

    scores, searched = await _vector_stage(session, estate, monkeypatch)

    assert ("ROUTINE", str(late.id)) in scores
    assert ("ROUTINE", str(late.id)) not in searched
