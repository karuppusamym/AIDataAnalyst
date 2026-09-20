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
types, and (R11-FP08) a routine's *approved, Atlas-authored* description --
never a source row and never a routine body. The index stores the vector and a
hash of the text, never the text.

**One composer, two callers (R11-FP08).** `compose_vector_texts` is the only
function that decides what an object's embedded text is. The rebuild below and
the live path in `retrieval_stages` both call it, so the text a persisted
vector encodes and the text the live path would embed cannot drift apart
again -- and because they share it, a persisted entry can be checked against
the text it *should* encode (`stale_index_entries`) before its score is used.

**Fail closed (INV-4).** With no embedding provider configured, this refuses
rather than backfilling with a hash double. That was a real defect once: a
SHA-256 digest has no semantic structure, and feeding one into fusion under
the name "vector" gave ranking a signal that was noise.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.context import get_correlation_id
from aida.embedding_provider import (
    EmbeddingUnavailable,
    index_signature,
    resolve_embedding_provider,
)
from aida.envelope_models import (
    MetadataRoutine,
    RoutineDocumentation,
    RoutineDocumentationVersion,
)
from aida.events import record_audit
from aida.models import (
    Embedding,
    GlossaryTerm,
    GlossaryTermVersion,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
)
from aida.secrets import SecretResolver
from aida.security import SecurityContext
from aida.vector_retrieval import build_embedding_text
from aida.vector_store import EmbeddingRecord, EmbeddingRef, resolve_vector_index

_log = structlog.get_logger(__name__)

#: Owner types this builder indexes. Deliberately a closed list: an owner
#: type that reaches the index without a matching read path in retrieval is
#: cost with no benefit, and one that reaches it carrying business values
#: would be an INV-6 breach.
INDEXED_OWNER_TYPES = ("TABLE", "COLUMN", "ROUTINE", "GLOSSARY_TERM")


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


# --------------------------------------------------------------------------- #
# R11-FP08: the one composer of embedded text
# --------------------------------------------------------------------------- #

#: Routine ids per `IN (...)` when loading approved descriptions. A rebuild can
#: carry thousands of routines, and asyncpg refuses a statement with more than
#: 32,767 bind parameters; a chunk keeps every statement far inside that.
_DESCRIPTION_LOAD_CHUNK = 500


def vector_text(*, owner_type: str, name: str, approved_description: str | None = None) -> str:
    """The text a vector of this object encodes. Pure; `compose_vector_texts` feeds it.

    R11-FP08: a ROUTINE's text is its display name *plus its approved
    description*, so a routine described in business language is reachable by
    meaning in the vector stage as it already is in the lexical one. Every other
    owner type is exactly the `build_embedding_text(name, object_type)` it always
    was -- byte for byte, so a table's, column's or tool's stored hash does not
    move and nothing outside routines is re-embedded by this change.

    What is deliberately *not* here, and why:

    * **The routine body** -- never. It is source code holding the estate's
      largest indirect-injection surface, and even redacted it is source-derived
      text; the description draft never quotes it either
      (`test_routine_description_body_states.py`). Nothing reads
      `body_sql_redacted` on the way to this function.
    * **The source's own comment** (`MetadataRoutine.source_description`) --
      a rescan rewords it, and it has not been reviewed. The lexical stage reads
      it because a comment is still words the source uses; embedding it would
      put unreviewed text into a signal that is scored as meaning.
    * **An unapproved description** -- the caller passes only the APPROVED
      version's text (`approved_routine_descriptions`). A draft is a proposal
      nobody has decided; SUPERSEDED and WITHDRAWN text is what Atlas no longer
      asserts.

    Embedding the approved description is not an INV-6 question: it is
    Atlas-authored, reviewed prose, not a value read from a source.
    """
    return build_embedding_text(
        name=name,
        object_type=owner_type,
        description=approved_description if owner_type == "ROUTINE" else None,
    )


async def approved_routine_descriptions(
    session: AsyncSession, organization_id: UUID, routine_ids: Iterable[str | UUID]
) -> dict[str, str]:
    """`str(routine_id) -> approved description`, for the routines that have one.

    Only the APPROVED version, and the newest one if a history ever holds two --
    the `routine_description_service.current_routine_descriptions` rule, restated
    here with `organization_id` on *both* tables (INV-5): that helper keys on
    routine id alone, and this read feeds a ranking every tenant's questions
    reach. An id that does not parse as a UUID has no description rather than
    raising: a hit id is data, and a malformed one must not end the stage.
    """
    wanted: list[UUID] = []
    for routine_id in routine_ids:
        try:
            wanted.append(routine_id if isinstance(routine_id, UUID) else UUID(str(routine_id)))
        except ValueError:
            continue
    found: dict[str, str] = {}
    for start in range(0, len(wanted), _DESCRIPTION_LOAD_CHUNK):
        chunk = wanted[start : start + _DESCRIPTION_LOAD_CHUNK]
        rows = await session.execute(
            select(RoutineDocumentation.routine_id, RoutineDocumentationVersion.description)
            .join(
                RoutineDocumentation,
                RoutineDocumentation.id == RoutineDocumentationVersion.documentation_id,
            )
            .where(
                RoutineDocumentation.routine_id.in_(chunk),
                RoutineDocumentation.organization_id == organization_id,
                RoutineDocumentationVersion.organization_id == organization_id,
                RoutineDocumentationVersion.status == "APPROVED",
            )
            # Ascending, so the last write per routine is the newest approved one.
            .order_by(RoutineDocumentationVersion.version)
        )
        for routine_id, description in rows.all():
            found[str(routine_id)] = description
    return found


async def compose_vector_texts(
    session: AsyncSession,
    organization_id: UUID,
    objects: Sequence[tuple[str, str, str]],
) -> list[str]:
    """The embedded text for each `(owner_type, owner_id, display_name)`, in order.

    **The only way embedded text is composed**, on both sides of the vector
    stage: `_indexable_objects` below (what a persisted vector encodes) and
    `retrieval_stages._live_vector_scores` (what the live path embeds). Before
    R11-FP08 each side called `build_embedding_text` itself and the two were
    kept equal by a comment; that is why a description could not be added --
    adding it to one side would have produced vectors of text the other side
    never composes, a divergence the index's coverage report cannot see because
    the entry *is* present. `test_vector_routine_descriptions` asserts both
    sides produce identical text for the same object.

    One read at most (approved routine descriptions), and none at all when no
    ROUTINE is among the objects -- so a pool of tables, columns and tools costs
    exactly what it did.
    """
    routine_ids = [owner_id for owner_type, owner_id, _name in objects if owner_type == "ROUTINE"]
    descriptions = (
        await approved_routine_descriptions(session, organization_id, routine_ids)
        if routine_ids
        else {}
    )
    return [
        vector_text(
            owner_type=owner_type,
            name=name,
            approved_description=descriptions.get(owner_id) if owner_type == "ROUTINE" else None,
        )
        for owner_type, owner_id, name in objects
    ]


def text_fingerprint(text: str) -> str:
    """The `text_hash` a persisted entry stores for `text` -- public for the stale check."""
    return _text_hash(text)


async def stale_index_entries(
    session: AsyncSession,
    organization_id: UUID,
    expected: Mapping[tuple[str, str], str],
    *,
    settings: Settings,
) -> set[tuple[str, str]]:
    """The `(owner_type, owner_id)` keys whose persisted vector must not be used.

    `expected` maps each key to the `text_fingerprint` of the text
    `compose_vector_texts` produces for it *now*. A key is stale when the index
    holds no entry for it under the current signature, or holds one embedded
    from different text.

    **Why this exists (R11-FP08).** An approved routine description is
    published, superseded and withdrawn, and none of those touches the catalog
    row `index_freshness` watches -- so an index built while a description was
    approved stayed `USABLE` after it was withdrawn, and the vector stage kept
    ranking the routine on text a reviewer had retired. Comparing fingerprints
    per entry makes that impossible by construction, on every path that changes
    a description, including ones added later: nothing has to remember to fire
    a re-index hook. The stale entry is embedded live instead (one provider call
    for the stale few, not the pool), and the next `rebuild_vector_index`
    re-embeds it -- its text hash no longer matches -- after which it is served
    from the index again.

    The same check closes two older gaps in passing, because a *missing* entry
    is stale too: a routine or column discovered after the last build (the
    freshness rule only watches tables), and a glossary term approved since it
    -- both used to leave the stage with no vector score at all under
    `PERSISTED_INDEX`, the R11-B2 shape. (Until 2026-09-20 *every* glossary term
    was missing, because the collector filtered on a lifecycle nothing writes;
    see `_indexable_objects`.)

    One statement, narrowed on the indexed `owner_id` and matched on the full
    pair in Python -- the portable form `PostgresBruteForceIndex.search` uses.
    """
    if not expected:
        return set()
    signature = index_signature(settings)
    rows = await session.execute(
        select(Embedding.owner_type, Embedding.owner_id, Embedding.text_hash).where(
            Embedding.organization_id == organization_id,
            Embedding.index_signature == signature,
            Embedding.chunk_index == 0,
            Embedding.owner_id.in_({owner_id for _type, owner_id in expected}),
        )
    )
    stored = {
        (owner_type, owner_id): text_hash
        for owner_type, owner_id, text_hash in rows.all()
        if (owner_type, owner_id) in expected
    }
    return {key for key, fingerprint in expected.items() if stored.get(key) != fingerprint}


async def _indexable_objects(
    session: AsyncSession, organization_id: UUID, datasource_id: UUID | None
) -> list[tuple[str, str, str]]:
    """`(owner_type, owner_id, text)` for everything worth embedding.

    Four statements regardless of estate size -- one per owner type -- not
    one per object, plus the approved-description read `compose_vector_texts`
    makes for routines. Only ACTIVE objects: a deprecated table answering a
    semantic search is a wrong answer with a confident score.
    """
    named: list[tuple[str, str, str]] = []

    table_stmt = select(MetadataTable.id, MetadataTable.name).where(
        MetadataTable.organization_id == organization_id,
        MetadataTable.status == "ACTIVE",
    )
    if datasource_id is not None:
        table_stmt = table_stmt.where(MetadataTable.datasource_id == datasource_id)
    for table_id, name in (await session.execute(table_stmt)).all():
        named.append(("TABLE", str(table_id), name))

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
        named.append(("COLUMN", str(column_id), name))

    # R11-FP11: routines are retrieval candidates like tables and columns, and were the only
    # kind the index did not cover: every question paid a provider call to embed them live
    # (`retrieval_stages` logs that as `retrieval_vector_index_gap`). The name is the one a
    # ROUTINE hit carries as its display name -- `schema.name` -- so the index and the live path
    # start from the same identity.
    #
    # R11-FP08, 2026-09-18: the text now also carries the routine's APPROVED description. The
    # first FP08 slice deliberately left it out, and recorded why: the embedded text has to be
    # byte-identical to what `retrieval_stages._live_vector_scores` composes, and a description
    # added here alone would have been a divergence the coverage gap cannot see (the entry is
    # present; only its text is wrong), while adding it to both sides needed a re-embed on every
    # publish and withdrawal that this builder had no notion of. Both halves now move together:
    # both sides compose through `compose_vector_texts`, and the re-embed is not a trigger at
    # all but a fingerprint -- the stored `text_hash` no longer matches once a description
    # changes, so `stale_index_entries` keeps the vector stage off the old vector and this
    # builder's own `skipped_unchanged` comparison re-embeds exactly the routines that moved.
    routine_stmt = (
        select(MetadataSchema.name, MetadataRoutine.id, MetadataRoutine.name)
        .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
        .where(
            MetadataRoutine.organization_id == organization_id,
            MetadataRoutine.status == "ACTIVE",
        )
    )
    if datasource_id is not None:
        routine_stmt = routine_stmt.where(MetadataRoutine.datasource_id == datasource_id)
    for schema_name, routine_id, name in (await session.execute(routine_stmt)).all():
        named.append(("ROUTINE", str(routine_id), f"{schema_name}.{name}"))

    # Glossary terms are organization-wide rather than per-datasource, so a
    # datasource-scoped rebuild deliberately leaves them alone rather than
    # re-embedding the whole glossary on every source's schedule.
    #
    # R11-FP08, 2026-09-20: this used to filter `lifecycle_status == 'PUBLISHED'` -- a value
    # nothing in this codebase writes, since terms are ACTIVE or DEPRECATED -- so no glossary
    # term had ever been indexed and every question that surfaced one paid a provider call to
    # embed it live. Two constraints fix what an entry must be, and both come from the live
    # path, which this collector has to match exactly:
    #
    # * **The name is the approved version's `display_name`, never `term_key`.** A GLOSSARY_TERM
    #   hit carries the APPROVED `GlossaryTermVersion.display_name` (`retrieval.hybrid_retrieve`,
    #   the lexical stage), and `compose_vector_texts` builds the text from that name -- so an
    #   entry built from the key would encode text the live path never composes, and
    #   `stale_index_entries` would report it stale on arrival and never serve it.
    # * **The text comes from the same approved version the lexical stage reads.** Only APPROVED
    #   counts: a draft or a version awaiting review is a proposal, and SUPERSEDED, REJECTED or
    #   DEPRECATED text is what Atlas no longer asserts. An approval supersedes the previous
    #   approved version, so a term has one; if a history ever held two, the newest wins, as it
    #   does for a routine's description. A DEPRECATED term is not indexed at all.
    #
    # Only the display name reaches the text (`vector_text` adds the owner type and nothing
    # else): the definition and synonyms feed the lexical stage and are not embedded on either
    # side. Both organizations' ids are restated (INV-5): on the term and on its version.
    if datasource_id is None:
        term_stmt = (
            select(GlossaryTerm.id, GlossaryTermVersion.display_name)
            .join(GlossaryTermVersion, GlossaryTermVersion.term_id == GlossaryTerm.id)
            .where(
                GlossaryTerm.organization_id == organization_id,
                GlossaryTerm.lifecycle_status == "ACTIVE",
                GlossaryTermVersion.organization_id == organization_id,
                GlossaryTermVersion.status == "APPROVED",
            )
            # Ascending, so the last write per term is the newest approved version.
            .order_by(GlossaryTermVersion.version)
        )
        current_names: dict[UUID, str] = {}
        for term_id, display_name in (await session.execute(term_stmt)).all():
            current_names[term_id] = display_name
        named.extend(
            ("GLOSSARY_TERM", str(term_id), display_name)
            for term_id, display_name in current_names.items()
        )

    texts = await compose_vector_texts(session, organization_id, named)
    return [
        (owner_type, owner_id, text)
        for (owner_type, owner_id, _name), text in zip(named, texts, strict=True)
    ]


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
    # `INDEXED_OWNER_TYPES` described a closed list that nothing enforced, so a
    # collector gaining a fourth owner type would have reached the index -- and,
    # if it carried business values, breached INV-6 -- with only a comment
    # standing against it. Checked here rather than inside the collector so the
    # guard survives a second collector being added beside it.
    seen_types = {owner_type for owner_type, _id, _text in objects}
    unexpected = sorted(seen_types - set(INDEXED_OWNER_TYPES))
    if unexpected:
        raise EmbeddingUnavailable(
            f"VECTOR_INDEX_OWNER_TYPE_NOT_INDEXABLE: {', '.join(unexpected)}; "
            f"indexable types are {', '.join(INDEXED_OWNER_TYPES)}"
        )
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




# --------------------------------------------------------------------------- #
# Scheduler entry: keep the index fresh, or stop paying for it twice
# --------------------------------------------------------------------------- #

_index_rebuild_last_run_at: datetime | None = None


async def run_vector_index_rebuild_pass(
    settings: Settings, *, now: datetime | None = None
) -> int | None:
    """Rebuild the stalest organizations' vector indexes on a cadence.

    `rebuild_vector_index` shipped with RT-1 and was reachable only from
    `POST /v1/organizations/{id}/retrieval/vector-index/rebuild` -- an endpoint
    whose UI does not exist (R11-X5 records the cluster as missing one). So the
    index was built only if an operator knew to call it, and after the estate
    next changed it went stale, `index_freshness` correctly stopped trusting
    it, and the vector channel fell back to embedding **every candidate on
    every query**.

    That fallback is the expensive shape RT-1 exists to remove: a provider call
    per candidate per query, so the bill grows with the estate and the traffic
    at the same time. Nothing is wrong at that point and nothing says anything
    either -- retrieval still returns good answers. A cost regression that
    presents as correct behaviour is exactly the kind a schedule prevents and a
    dashboard does not.

    Returns `None` when the pass was skipped (disabled, not yet due, or no
    provider configured) and the number of organizations rebuilt when it ran --
    the same shape as `business_graph.run_rollup_rebuild_pass`, whose structure
    this follows deliberately rather than inventing a second cadence idiom.

    One organization's failure is logged and skipped rather than aborting the
    sweep, and the cost of a skip is bounded and non-corrupting: that
    organization's index stays exactly as stale as it already was, and the
    vector channel goes on answering by the live path.

    **A missing provider is a skip, not an error.** `embedding_provider`
    defaults to `unset`, which is the shipped state, so an unconfigured
    deployment must not log an exception every tick -- it must do nothing, once,
    and say why at info level.
    """
    from aida.db import session_factory

    global _index_rebuild_last_run_at
    if not settings.vector_index_rebuild_enabled:
        return None
    effective_now = now or datetime.now(UTC)
    interval = timedelta(seconds=settings.vector_index_rebuild_interval_seconds)
    if (
        _index_rebuild_last_run_at is not None
        and (effective_now - _index_rebuild_last_run_at) < interval
    ):
        return None

    try:
        resolve_embedding_provider(settings, SecretResolver(settings))
    except EmbeddingUnavailable as exc:
        _log.info("vector_index_rebuild_skipped", reason=str(exc))
        # Stamped so an unconfigured deployment asks once per interval rather
        # than resolving a provider it does not have on every scheduler tick.
        _index_rebuild_last_run_at = effective_now
        return None

    async with session_factory() as session:
        organization_ids = list(
            (
                await session.scalars(
                    select(Organization.id)
                    .where(Organization.status == "ACTIVE")
                    .order_by(Organization.created_at)
                    .limit(settings.vector_index_rebuild_batch_size)
                )
            ).all()
        )

    rebuilt = 0
    for organization_id in organization_ids:
        async with session_factory() as session:
            try:
                result = await rebuild_vector_index(
                    session, organization_id, settings=settings
                )
                await session.commit()
            except Exception:
                await session.rollback()
                _log.exception(
                    "vector_index_rebuild_failed", organization_id=str(organization_id)
                )
                continue
        rebuilt += 1
        _log.info(
            "vector_index_rebuilt",
            organization_id=str(organization_id),
            considered=result.considered,
            embedded=result.embedded,
            skipped_unchanged=result.skipped_unchanged,
            backend=result.backend,
        )
    # Stamped after the sweep and even when nothing was rebuilt, so an estate
    # with no indexable objects does not re-query every tick.
    _index_rebuild_last_run_at = effective_now
    return rebuilt
