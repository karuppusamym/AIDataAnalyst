"""The hybrid retrieval pipeline, one stage per invariant.

`retrieval.hybrid_retrieve_enhanced` used to be a single ~580-line function in
which authorized-candidate selection, three scoring channels, trust demotion,
fusion and evidence assembly all shared one namespace and one set of mutable
locals. The 2026-09-05 review (R02) asked for it to be split "along exactly
those boundaries with typed stage inputs/outputs" -- and, explicitly, *not* for
helpers extracted to shorten a function. So each stage below is a rule that can
be stated on its own:

* `select_authorized_candidates` -- what this caller is entitled to see, and
  how many of it. Policy filtering happens here and nowhere else; every later
  stage may only re-score or expand *within* what this returned.
* `run_vector_channel` -- semantic similarity, and only when a real embedding
  model is behind it. A hash is not a semantic signal.
* `run_graph_channel` -- reachability through already-governed relationships
  (foreign keys, dbt `DEPENDS_ON`, a governed tool's declared tables).
* `merge_contributions` -- the single authoritative rule for how a channel's
  score becomes a candidate signal, and how a channel may introduce a
  candidate an earlier channel did not have.
* `run_trust_channel` -- demotion by open quality incidents and promotion by
  recorded usage, both from persisted evidence.
* `fuse` -- ranking, the only place a `FusionConfig` is applied.
* `assemble_evidence` -- turning a ranked candidate into an inspectable hit,
  including the one rule about a governed tool's operational score.

Two properties the old function did not have, both asked for by the same
review row:

**The candidate set is bounded explicitly.** `RetrievalRequest.candidate_limit`
caps the authorized set at selection time and says so in a metric, rather than
relying on a downstream fusion `top_k` to hide however many candidates the
lexical scan happened to produce.

**Cancellation is cooperative and checked at stage boundaries.** A retrieval
that has already spent its budget stops between stages instead of running every
remaining channel to completion for an answer nobody is waiting for.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.config import Settings
from aida.embedding_provider import (
    AsyncEmbeddingProvider,
    EmbeddingUnavailable,
    resolve_embedding_provider,
)
from aida.fusion_ranking import (
    FusionConfig,
    RankedCandidate,
    SignalScore,
    build_evidence,
    fuse_results,
)
from aida.graph_retrieval import GraphEdge, GraphNode, KnowledgeGraph, expand_graph
from aida.models import (
    DataSource,
    DbtLineageEdge,
    DbtResource,
    MetadataConstraint,
    MetadataTable,
)
from aida.quality_coupling import demote_in_retrieval, fetch_open_incidents, resolve_table_ids
from aida.retrieval_metrics import (
    RETRIEVAL_CANCELLED,
    RETRIEVAL_CANDIDATES_BOUNDED,
    RETRIEVAL_CHANNEL_CANDIDATES,
    RETRIEVAL_CHANNEL_CONTRIBUTED,
    RETRIEVAL_CHANNEL_MEAN_SCORE,
    RETRIEVAL_CHANNEL_SECONDS,
    RETRIEVAL_CHANNEL_SKIPPED,
    SkipReason,
)
from aida.secrets import SecretResolver

if TYPE_CHECKING:
    from aida.retrieval import HybridRetrievalHit

logger = structlog.get_logger(__name__)

# RT-6: the number of recorded executions against a table beyond which its
# usage_popularity raw_score saturates at 1.0. 10 real executions is a small,
# deliberately conservative bar -- enough to separate "never queried" from
# "actually used" without requiring warehouse-scale traffic to move it at all.
USAGE_POPULARITY_SATURATION = 10


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class RetrievalCancelled(RuntimeError):
    """Raised at a stage boundary when the caller's token says to stop.

    Deliberately not `asyncio.CancelledError`: this is a *cooperative* stop the
    pipeline chose, and a caller that catches it can still return the partial
    ranking it already has. Conflating the two would make "the client hung up"
    indistinguishable from "the event loop is tearing this task down".
    """


class CancellationToken(Protocol):
    """Anything that can say "stop" between stages."""

    def cancelled(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class NeverCancelled:
    """The default. Retrieval runs to completion unless a caller says otherwise."""

    def cancelled(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class DeadlineToken:
    """Cancel once a wall-clock budget is spent.

    A real, self-driven cancellation source rather than a hook waiting for a
    caller: a retrieval that has already used its whole budget in the lexical
    and vector channels stops there instead of also paying for graph expansion
    on an answer that will arrive too late to be used.
    """

    deadline: float

    @classmethod
    def after(cls, seconds: float) -> DeadlineToken:
        return cls(deadline=time.monotonic() + seconds)

    def cancelled(self) -> bool:
        return time.monotonic() >= self.deadline


@dataclass(frozen=True, slots=True)
class PredicateToken:
    """Cancel when an external predicate says so -- e.g. a disconnected client."""

    predicate: Callable[[], bool]

    def cancelled(self) -> bool:
        return bool(self.predicate())


# ---------------------------------------------------------------------------
# Stage inputs and outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """Everything every stage is allowed to depend on.

    Frozen and passed whole so that no stage can quietly acquire a new input:
    adding one is a visible change to this type, which is what makes each stage
    independently readable.
    """

    datasource: DataSource
    question: str
    settings: Settings
    organization_id: UUID
    preferred_tool_version_id: UUID | None = None
    fusion_method: str = "rrf"
    include_vector: bool = True
    include_graph: bool = True
    max_hops: int = 2
    candidate_limit: int | None = None
    cancel: CancellationToken = field(default_factory=NeverCancelled)

    @property
    def result_limit(self) -> int:
        """How many ranked hits the caller gets back."""
        return self.settings.agent_retrieval_limit

    @property
    def authorized_limit(self) -> int:
        """The explicit bound on the authorized-candidate set.

        Defaults to `agent_retrieval_limit` -- the same number the lexical
        stage already applied -- so behaviour is unchanged unless a caller
        asks for a different bound. What changes is that the bound is now
        stated and measured here rather than being an emergent property of
        whichever downstream cut happened to be smallest.
        """
        return self.candidate_limit or self.settings.agent_retrieval_limit


@dataclass(frozen=True, slots=True)
class ChannelReport:
    """What one channel cost and what it was worth.

    `scored` is how many candidates the channel produced a score for;
    `contributed` is how many of those no earlier channel had already found.
    The difference is the channel's marginal value, which is the number that
    tells an operator whether a channel is earning its latency.
    """

    channel: str
    scored: int
    contributed: int
    seconds: float
    mean_score: float
    skipped_reason: SkipReason | None = None

    def evidence(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "scored": self.scored,
            "contributed": self.contributed,
            "seconds": round(self.seconds, 4),
            "mean_score": round(self.mean_score, 4),
            "skipped_reason": self.skipped_reason.value if self.skipped_reason else None,
        }


@dataclass(frozen=True, slots=True)
class SignalContribution:
    """One channel's opinion about one object.

    A channel emits these rather than mutating a shared candidate dict, so
    "how a score becomes a signal" stays a single rule in
    `merge_contributions` instead of being restated once per channel.
    """

    object_type: str
    object_id: str
    display_name: str
    signal: str
    raw_score: float
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.object_type}:{self.object_id}"


@dataclass(frozen=True, slots=True)
class ChannelResult:
    contributions: list[SignalContribution]
    report: ChannelReport


@dataclass(slots=True)
class CandidatePool:
    """The accumulating candidate set and the reports that explain it."""

    authorized: list[HybridRetrievalHit]
    candidates: dict[str, RankedCandidate]
    reports: list[ChannelReport] = field(default_factory=list)
    truncated: bool = False

    def evidence(self) -> list[dict[str, Any]]:
        return [report.evidence() for report in self.reports]


def _observe(report: ChannelReport) -> ChannelReport:
    RETRIEVAL_CHANNEL_SECONDS.labels(report.channel).observe(report.seconds)
    RETRIEVAL_CHANNEL_CANDIDATES.labels(report.channel).observe(report.scored)
    RETRIEVAL_CHANNEL_CONTRIBUTED.labels(report.channel).observe(report.contributed)
    RETRIEVAL_CHANNEL_MEAN_SCORE.labels(report.channel).observe(report.mean_score)
    if report.skipped_reason is not None:
        RETRIEVAL_CHANNEL_SKIPPED.labels(report.channel, report.skipped_reason.value).inc()
    return report


def _mean(scores: list[float]) -> float:
    return sum(scores) / len(scores) if scores else 0.0


def check_cancelled(request: RetrievalRequest, channel: str) -> None:
    """The stage boundary. Called before a stage starts, never inside one, so a
    cancelled retrieval always stops at a point where the pool is consistent."""
    if request.cancel.cancelled():
        RETRIEVAL_CANCELLED.labels(channel).inc()
        raise RetrievalCancelled(f"retrieval cancelled before the {channel} stage")


# ---------------------------------------------------------------------------
# Stage 1 -- authorized candidates
# ---------------------------------------------------------------------------


async def select_authorized_candidates(
    session: AsyncSession, request: RetrievalRequest
) -> CandidatePool:
    """The policy-narrowed set, explicitly bounded.

    `hybrid_retrieve` is the authority for *what this caller may see*: it
    applies the org/datasource scope filters and the published/ACTIVE status
    filters for every object type. This stage adds exactly one thing --
    `candidate_limit`, applied and counted here, so the size of the set every
    later stage works on is a stated number rather than whatever the lexical
    scan produced.
    """
    from aida.retrieval import hybrid_retrieve

    started = time.perf_counter()
    hits = await hybrid_retrieve(
        session,
        datasource=request.datasource,
        question=request.question,
        settings=request.settings,
        preferred_tool_version_id=request.preferred_tool_version_id,
    )
    truncated = len(hits) > request.authorized_limit
    if truncated:
        RETRIEVAL_CANDIDATES_BOUNDED.inc()
        logger.info(
            "retrieval_candidate_bound_applied",
            scanned=len(hits),
            bound=request.authorized_limit,
            datasource_id=str(request.datasource.id),
        )
        hits = hits[: request.authorized_limit]

    candidates = {
        f"{hit.object_type}:{hit.object_id}": RankedCandidate(
            object_type=hit.object_type,
            object_id=hit.object_id,
            display_name=hit.display_name,
            signals=[SignalScore(signal="lexical", raw_score=hit.score)],
            metadata=hit.metadata,
        )
        for hit in hits
    }
    report = _observe(
        ChannelReport(
            channel="lexical",
            scored=len(hits),
            contributed=len(candidates),
            seconds=time.perf_counter() - started,
            mean_score=_mean([hit.score for hit in hits]),
        )
    )
    return CandidatePool(
        authorized=list(hits), candidates=candidates, reports=[report], truncated=truncated
    )


# ---------------------------------------------------------------------------
# Stage 2 -- vector channel
# ---------------------------------------------------------------------------


async def run_vector_channel(
    session: AsyncSession, request: RetrievalRequest, pool: CandidatePool
) -> ChannelResult:
    """Semantic similarity over the already-authorized set.

    The stage runs only with a real embedding model behind it. It used to build
    a `HashEmbeddingProvider()` unconditionally and feed the result into fusion
    as a signal named "vector" -- but a SHA-256 digest has no semantic
    structure, so that score was noise carrying the name of a signal, and
    fusion could rank on it. With no provider configured the stage is skipped
    and the reason recorded, which is a smaller answer rather than a
    confidently wrong one (INV-4, INV-9).

    RT-1: the *persisted* index is preferred when it is fresh. The live path
    embeds every candidate on every query, which is correct but pays a model
    call per candidate per query -- cost that grows with the estate and with
    traffic at the same time. The fallback is not a degradation: it is the same
    computation, and it is what runs whenever the index is empty, stale, built
    under a different embedding model, or the estate has changed since the last
    build. Which path ran is recorded per hit (`vector_path`) so "why was this
    ranked here" stays answerable.

    Policy still filters before ranking: the candidate set handed to the index
    is exactly the authorized set, so the index can only reorder what the
    caller was already entitled to.
    """
    from aida.vector_index_service import index_freshness, search_persisted_index
    from aida.vector_retrieval import build_embedding_text, vector_search
    from aida.vector_store import EmbeddingRef, VectorIndexUnavailable

    started = time.perf_counter()

    def skipped(reason: SkipReason) -> ChannelResult:
        return ChannelResult(
            contributions=[],
            report=_observe(
                ChannelReport(
                    channel="vector",
                    scored=0,
                    contributed=0,
                    seconds=time.perf_counter() - started,
                    mean_score=0.0,
                    skipped_reason=reason,
                )
            ),
        )

    if not request.include_vector:
        return skipped(SkipReason.DISABLED)

    embedding_provider: AsyncEmbeddingProvider
    try:
        embedding_provider = resolve_embedding_provider(
            request.settings, SecretResolver(request.settings)
        )
    except EmbeddingUnavailable as exc:
        logger.info(
            "retrieval_vector_stage_skipped",
            reason=str(exc),
            datasource_id=str(request.datasource.id),
        )
        return skipped(SkipReason.PROVIDER_UNAVAILABLE)

    authorized = pool.authorized
    freshness = await index_freshness(session, request.organization_id, settings=request.settings)
    hit_by_key = {f"{hit.object_type}:{hit.object_id}": hit for hit in authorized}
    vector_path = "PERSISTED_INDEX" if freshness.usable else "LIVE_EMBED"
    logger.info(
        "retrieval_vector_stage_path",
        path=vector_path,
        reason=freshness.reason,
        indexed_entries=freshness.entries,
        datasource_id=str(request.datasource.id),
    )

    scored: list[tuple[str, str, float]] = []
    if freshness.usable:
        batch = await embedding_provider.embed([request.question])
        query_emb = tuple(batch.vectors[0])
        refs = tuple(
            EmbeddingRef(owner_type=hit.object_type, owner_id=str(hit.object_id))
            for hit in authorized
        )
        try:
            scored = list(
                await search_persisted_index(
                    session,
                    request.organization_id,
                    query_emb,
                    settings=request.settings,
                    # `refs`, never `refs or None`: an empty authorized set is
                    # the policy filter's answer, and `None` means "no candidate
                    # filter" to `search_persisted_index`, which then ranks the
                    # whole organization's index. Passing the empty tuple hits
                    # its `if not candidates: return ()` guard instead, so a
                    # caller authorized for nothing retrieves nothing.
                    candidates=refs,
                    limit=request.result_limit,
                )
            )
        except VectorIndexUnavailable as exc:
            # The index went away between the freshness check and the search.
            # Fall back rather than losing the stage.
            logger.info("retrieval_vector_index_unavailable", reason=str(exc))
            vector_path = "LIVE_EMBED"
            freshness = replace(freshness, usable=False)

    if not freshness.usable:
        # One batched call for the question and every candidate text, rather
        # than a call per candidate: the provider bills and rate-limits per
        # request, and N+1 network round trips inside a retrieval path is a
        # latency budget spent on nothing.
        candidate_texts = [
            build_embedding_text(name=hit.display_name, object_type=hit.object_type)
            for hit in authorized
        ]
        batch = await embedding_provider.embed([request.question, *candidate_texts])
        query_emb_list = list(batch.vectors[0])
        candidate_embeddings = [list(v) for v in batch.vectors[1:]]
        vector_candidates: list[dict[str, Any]] = [
            {
                "object_type": hit.object_type,
                "object_id": hit.object_id,
                "display_name": hit.display_name,
                "embedding": emb,
                "datasource_id": hit.metadata.get("datasource_id"),
                "metadata": hit.metadata,
            }
            for hit, emb in zip(authorized, candidate_embeddings, strict=True)
        ]
        scored = [
            (vhit.object_type, str(vhit.object_id), vhit.similarity)
            for vhit in vector_search(query_emb_list, vector_candidates, top_k=request.result_limit)
        ]

    contributions = [
        SignalContribution(
            object_type=object_type,
            object_id=object_id,
            display_name=(
                source.display_name
                if (source := hit_by_key.get(f"{object_type}:{object_id}"))
                else str(object_id)
            ),
            signal="vector",
            raw_score=similarity,
            metadata={
                **(dict(source.metadata) if source else {}),
                "vector_path": vector_path,
            },
        )
        for object_type, object_id, similarity in scored
    ]
    new_keys = {c.key for c in contributions} - set(pool.candidates)
    return ChannelResult(
        contributions=contributions,
        report=_observe(
            ChannelReport(
                channel="vector",
                scored=len(contributions),
                contributed=len(new_keys),
                seconds=time.perf_counter() - started,
                mean_score=_mean([c.raw_score for c in contributions]),
            )
        ),
    )


# ---------------------------------------------------------------------------
# Stage 3 -- graph channel
# ---------------------------------------------------------------------------


async def _build_knowledge_graph(
    session: AsyncSession, request: RetrievalRequest, authorized: list[HybridRetrievalHit]
) -> KnowledgeGraph:
    """Real edges, not just seed nodes.

    A graph with nodes but no edges lets BFS reach only depth 0 (the seeds
    themselves, which `expand_graph` does not even emit as hits) -- expansion
    *past* what lexical and vector already found is the entire point of RT-2,
    so the edge source has to be real governed metadata, not a placeholder.
    Three already-governed edge sources feed it: `MetadataConstraint` foreign
    keys (approved, datasource-scoped table-to-table relationships), dbt
    `DEPENDS_ON` `DbtLineageEdge` rows resolved through each side's
    `matched_table_id` (a real manifest dependency a table's FKs never capture,
    e.g. a staging model with no declared constraint), and a candidate
    `GOVERNED_TOOL` hit's own declared `referenced_tables` (so a table
    reachable only through a governed tool's SQL still expands).
    """
    from aida.retrieval import _latest_dbt_artifact_import_ids

    datasource = request.datasource
    org_id = request.organization_id
    scan_limit = request.settings.agent_retrieval_scan_limit
    kg = KnowledgeGraph()
    for hit in authorized:
        kg.add_node(
            GraphNode(
                node_id=f"{hit.object_type}:{hit.object_id}",
                node_type=hit.object_type,
                display_name=hit.display_name,
                organization_id=org_id,
                datasource_id=hit.metadata.get("datasource_id"),
            )
        )

    def ensure_table_node(table_id: Any, name: str) -> str:
        node_id = f"TABLE:{table_id}"
        if kg.get_node(node_id) is None:
            kg.add_node(
                GraphNode(
                    node_id=node_id,
                    node_type="TABLE",
                    display_name=name,
                    organization_id=org_id,
                    datasource_id=datasource.id,
                )
            )
        return node_id

    fk_rows = (
        await session.execute(
            select(MetadataConstraint, MetadataTable)
            .join(MetadataTable, MetadataTable.id == MetadataConstraint.table_id)
            .where(
                MetadataConstraint.datasource_id == datasource.id,
                MetadataConstraint.organization_id == org_id,
                MetadataConstraint.constraint_type == "FOREIGN_KEY",
                MetadataConstraint.status == "ACTIVE",
                MetadataConstraint.referenced_table_id.is_not(None),
                MetadataTable.status == "ACTIVE",
            )
            .limit(scan_limit)
        )
    ).all()
    referenced_ids = {constraint.referenced_table_id for constraint, _table in fk_rows}
    referenced_tables: dict[UUID, MetadataTable] = {}
    if referenced_ids:
        referenced_tables = {
            table.id: table
            for table in (
                await session.scalars(
                    select(MetadataTable).where(
                        MetadataTable.id.in_(referenced_ids),
                        MetadataTable.status == "ACTIVE",
                    )
                )
            ).all()
        }
    for constraint, table in fk_rows:
        target_table = referenced_tables.get(constraint.referenced_table_id)
        if target_table is None:
            continue
        kg.add_edge(
            GraphEdge(
                source_id=ensure_table_node(table.id, table.name),
                target_id=ensure_table_node(target_table.id, target_table.name),
                edge_type="FOREIGN_KEY",
            )
        )

    # RT-2 follow-up: dbt `DEPENDS_ON` edges. Only the latest artifact snapshot
    # per ACTIVE dbt project is read (the same scope `hybrid_retrieve`'s
    # dbt-resource stage uses), and only edges where BOTH ends resolved to a
    # real, ACTIVE `MetadataTable` are added -- an unmatched dbt node
    # contributes no graph edge here, it just is not a table-level
    # relationship yet.
    dbt_artifact_ids = await _latest_dbt_artifact_import_ids(session, datasource=datasource)
    if dbt_artifact_ids:
        source_resource = aliased(DbtResource)
        target_resource = aliased(DbtResource)
        dbt_edge_rows = (
            await session.execute(
                select(source_resource, target_resource)
                .select_from(DbtLineageEdge)
                .join(source_resource, source_resource.id == DbtLineageEdge.source_resource_id)
                .join(target_resource, target_resource.id == DbtLineageEdge.target_resource_id)
                .where(
                    DbtLineageEdge.artifact_import_id.in_(dbt_artifact_ids),
                    DbtLineageEdge.organization_id == org_id,
                    DbtLineageEdge.edge_type == "DEPENDS_ON",
                    source_resource.matched_table_id.is_not(None),
                    target_resource.matched_table_id.is_not(None),
                )
                .limit(scan_limit)
            )
        ).all()
        dbt_table_ids = {
            table_id
            for source, target in dbt_edge_rows
            for table_id in (source.matched_table_id, target.matched_table_id)
        }
        dbt_tables: dict[UUID, MetadataTable] = {}
        if dbt_table_ids:
            dbt_tables = {
                table.id: table
                for table in (
                    await session.scalars(
                        select(MetadataTable).where(
                            MetadataTable.id.in_(dbt_table_ids),
                            MetadataTable.status == "ACTIVE",
                        )
                    )
                ).all()
            }
        for source, target in dbt_edge_rows:
            source_table = dbt_tables.get(source.matched_table_id)
            target_table = dbt_tables.get(target.matched_table_id)
            if source_table is None or target_table is None:
                continue
            kg.add_edge(
                GraphEdge(
                    source_id=ensure_table_node(source_table.id, source_table.name),
                    target_id=ensure_table_node(target_table.id, target_table.name),
                    edge_type="DBT_DEPENDS_ON",
                )
            )

    # RT-2 follow-up: a `GOVERNED_TOOL` candidate's own declared
    # `referenced_tables` (already-published tool metadata, no further approval
    # needed to read) become TOOL -> TABLE edges, so a table a governed tool
    # queries -- but that has no FK/dbt relationship to anything already
    # surfaced -- is still reachable by expansion.
    tool_hits = [hit for hit in authorized if hit.object_type == "GOVERNED_TOOL"]
    graph_tool_name_pool = {
        name for hit in tool_hits for name in (hit.metadata.get("referenced_tables") or [])
    }
    if graph_tool_name_pool:
        tool_table_ids = await resolve_table_ids(
            session, datasource=datasource, table_names=sorted(graph_tool_name_pool)
        )
        for hit in tool_hits:
            tool_node_id = f"{hit.object_type}:{hit.object_id}"
            for name in hit.metadata.get("referenced_tables") or []:
                table_id = tool_table_ids.get(name)
                if table_id is None:
                    continue
                kg.add_edge(
                    GraphEdge(
                        source_id=tool_node_id,
                        target_id=ensure_table_node(table_id, name),
                        edge_type="TOOL_REFERENCES_TABLE",
                    )
                )
    return kg


async def run_graph_channel(
    session: AsyncSession, request: RetrievalRequest, pool: CandidatePool
) -> ChannelResult:
    """Expansion to objects reachable from the authorized seeds by governed edges.

    Every node this can reach is reached *from* a seed the caller was already
    entitled to, through a relationship a human or a build already approved --
    so expansion widens what is shown without widening what is permitted.
    """
    started = time.perf_counter()

    def skipped(reason: SkipReason) -> ChannelResult:
        return ChannelResult(
            contributions=[],
            report=_observe(
                ChannelReport(
                    channel="graph",
                    scored=0,
                    contributed=0,
                    seconds=time.perf_counter() - started,
                    mean_score=0.0,
                    skipped_reason=reason,
                )
            ),
        )

    if not request.include_graph:
        return skipped(SkipReason.DISABLED)
    if not pool.authorized:
        return skipped(SkipReason.NO_SEEDS)

    kg = await _build_knowledge_graph(session, request, pool.authorized)
    seed_ids = [f"{hit.object_type}:{hit.object_id}" for hit in pool.authorized[:10]]
    graph_hits = expand_graph(
        kg,
        seed_ids,
        allowed_org_id=request.organization_id,
        max_hops=request.max_hops,
        max_results=request.result_limit,
    )

    contributions = []
    for ghit in graph_hits:
        # `GraphHit.object_id` is the graph's own node id (`f"{type}:{id}"`,
        # per the construction above), not a bare object id -- unwrap it here
        # rather than leaking the composite string into
        # `RankedCandidate.object_id`, which every other caller (e.g.
        # `_model_context`'s `UUID(hit.object_id)`) expects to be the raw id.
        raw_object_id = ghit.object_id.removeprefix(f"{ghit.object_type}:")
        contributions.append(
            SignalContribution(
                object_type=ghit.object_type,
                object_id=raw_object_id,
                display_name=ghit.display_name,
                signal="graph",
                raw_score=ghit.proximity_score,
                metadata={**ghit.metadata, "graph_expansion_path": ghit.expansion_path},
            )
        )
    new_keys = {c.key for c in contributions} - set(pool.candidates)
    return ChannelResult(
        contributions=contributions,
        report=_observe(
            ChannelReport(
                channel="graph",
                scored=len(contributions),
                contributed=len(new_keys),
                seconds=time.perf_counter() - started,
                mean_score=_mean([c.raw_score for c in contributions]),
            )
        ),
    )


# ---------------------------------------------------------------------------
# Merge -- the single rule for turning a contribution into a signal
# ---------------------------------------------------------------------------

# Metadata keys a channel may attach to a candidate it did not create. Anything
# a channel wants to say about an existing candidate has to be one of these, so
# a channel cannot silently overwrite the identity or provenance fields the
# lexical stage established.
_MERGEABLE_METADATA_KEYS = frozenset({"vector_path", "graph_expansion_path"})


def merge_contributions(pool: CandidatePool, result: ChannelResult) -> None:
    """Fold one channel's contributions into the pool.

    One rule, stated once: a contribution for a candidate that already exists
    appends its signal and may set (never overwrite) one of a closed set of
    provenance keys; a contribution for a candidate that does not exist creates
    one carrying that single signal. Every channel used to restate this inline,
    which is how `vector_path` came to be set with `setdefault` in one branch
    and plain assignment in another.
    """
    for contribution in result.contributions:
        existing = pool.candidates.get(contribution.key)
        if existing is not None:
            existing.signals.append(
                SignalScore(signal=contribution.signal, raw_score=contribution.raw_score)
            )
            for key in _MERGEABLE_METADATA_KEYS & set(contribution.metadata):
                existing.metadata.setdefault(key, contribution.metadata[key])
            continue
        pool.candidates[contribution.key] = RankedCandidate(
            object_type=contribution.object_type,
            object_id=contribution.object_id,
            display_name=contribution.display_name,
            signals=[SignalScore(signal=contribution.signal, raw_score=contribution.raw_score)],
            metadata=dict(contribution.metadata),
        )
    pool.reports.append(result.report)


# ---------------------------------------------------------------------------
# Stage 4 -- trust
# ---------------------------------------------------------------------------


def _candidate_table_ids(pool: CandidatePool) -> tuple[dict[str, set[UUID]], set[str]]:
    """Which tables each candidate depends on, and the tool-declared table names
    still needing resolution. Extracted as its own step because "what does this
    candidate stand on" is the question both the quality and usage signals ask,
    and answering it twice is how they would drift apart."""
    candidate_table_ids: dict[str, set[UUID]] = {}
    tool_name_pool: set[str] = set()
    for key, candidate in pool.candidates.items():
        table_ids: set[UUID] = set()
        if candidate.object_type == "TABLE":
            table_ids.add(UUID(candidate.object_id))
        else:
            for field_name in ("table_id", "source_table_id"):
                raw = candidate.metadata.get(field_name)
                if raw:
                    table_ids.add(raw if isinstance(raw, UUID) else UUID(str(raw)))
            if candidate.object_type == "GOVERNED_TOOL":
                tool_name_pool.update(candidate.metadata.get("referenced_tables") or [])
        candidate_table_ids[key] = table_ids
    return candidate_table_ids, tool_name_pool


async def run_trust_channel(
    session: AsyncSession, request: RetrievalRequest, pool: CandidatePool
) -> ChannelResult:
    """Quality/trust demotion (RT-7/DQ-3) and usage popularity (RT-6).

    Both are derived from persisted runtime evidence rather than placeholders,
    and both batch their shared lookups per retrieval call. Unlike the lexical,
    vector and graph channels this one never introduces a candidate: it scores
    what the earlier channels already surfaced, and a demotion is a ranking
    signal, never a filter -- an object with an open quality incident is still
    shown, with the incident attached, rather than silently disappearing.
    """
    from aida.retrieval import _table_execution_counts

    started = time.perf_counter()
    candidate_table_ids, tool_name_pool = _candidate_table_ids(pool)

    if tool_name_pool:
        tool_table_ids = await resolve_table_ids(
            session, datasource=request.datasource, table_names=sorted(tool_name_pool)
        )
        for key, candidate in pool.candidates.items():
            if candidate.object_type != "GOVERNED_TOOL":
                continue
            for name in candidate.metadata.get("referenced_tables") or []:
                resolved = tool_table_ids.get(name)
                if resolved is not None:
                    candidate_table_ids[key].add(resolved)

    all_table_ids: set[UUID] = set()
    for ids in candidate_table_ids.values():
        all_table_ids.update(ids)

    incidents = (
        await fetch_open_incidents(
            session, datasource=request.datasource, table_ids=list(all_table_ids)
        )
        if all_table_ids
        else []
    )
    usage_counts = await _table_execution_counts(
        session,
        datasource=request.datasource,
        table_ids=all_table_ids,
        scan_limit=request.settings.agent_retrieval_scan_limit,
    )

    trust_scores: list[float] = []
    for key, candidate in pool.candidates.items():
        ids = candidate_table_ids.get(key) or set()
        if ids:
            per_table_scores = {
                str(table_id): demote_in_retrieval(str(table_id), incidents) for table_id in ids
            }
            quality_trust_score = min(per_table_scores.values())
            popularity_count = max(usage_counts.get(tid, 0) for tid in ids)
            demoted_ids = sorted(
                table_id for table_id, score in per_table_scores.items() if score < 1.0
            )
        else:
            quality_trust_score = 1.0
            popularity_count = 0
            demoted_ids = []
        if demoted_ids:
            candidate.metadata["quality_trust_demotion"] = {
                "reason": "OPEN_QUALITY_INCIDENT",
                "demoted_table_ids": demoted_ids,
                "worst_factor": quality_trust_score,
            }
        usage_popularity_score = min(1.0, popularity_count / USAGE_POPULARITY_SATURATION)
        candidate.signals.append(
            SignalScore(signal="quality_trust", raw_score=round(quality_trust_score, 4))
        )
        candidate.signals.append(
            SignalScore(signal="usage_popularity", raw_score=round(usage_popularity_score, 4))
        )
        trust_scores.append(quality_trust_score)

    # The trust stage writes directly onto the candidates it scores (it adds no
    # new ones), so it reports rather than contributes: `merge_contributions`
    # would have nothing to merge.
    return ChannelResult(
        contributions=[],
        report=_observe(
            ChannelReport(
                channel="trust",
                scored=len(pool.candidates),
                contributed=0,
                seconds=time.perf_counter() - started,
                mean_score=_mean(trust_scores),
            )
        ),
    )


# ---------------------------------------------------------------------------
# Stage 5 -- fusion
# ---------------------------------------------------------------------------


def fuse(
    request: RetrievalRequest, pool: CandidatePool
) -> tuple[list[RankedCandidate], FusionConfig]:
    """Rank the merged candidate set. The only place a `FusionConfig` is built."""
    started = time.perf_counter()
    config = FusionConfig(method=request.fusion_method)
    ranked = fuse_results(list(pool.candidates.values()), config=config, top_k=request.result_limit)
    pool.reports.append(
        _observe(
            ChannelReport(
                channel="fusion",
                scored=len(pool.candidates),
                contributed=len(ranked),
                seconds=time.perf_counter() - started,
                mean_score=_mean([candidate.final_score for candidate in ranked]),
            )
        )
    )
    return ranked, config


# ---------------------------------------------------------------------------
# Stage 6 -- evidence
# ---------------------------------------------------------------------------


def assemble_evidence(
    pool: CandidatePool, ranked: list[RankedCandidate], config: FusionConfig
) -> list[HybridRetrievalHit]:
    """Turn ranked candidates into hits whose ranking is fully inspectable.

    One rule needs stating and is stated only here: `GovernedPlanner.plan()`
    gates GOVERNED_TOOL selection on `hit.score >= agent_tool_match_threshold`,
    a [0,1] match-confidence figure the lexical stage produces (BM25 + boosts,
    capped at 1.0). A fusion score is a different, relative ranking quantity on
    its own scale (RRF's is ~1/rrf_k) -- handing it to that threshold would
    silently change which governed tools the planner will ever select. So a
    tool hit keeps its lexical score as `.score`, while the real fused score
    stays fully visible in `retrieval_evidence.final_score`. Every other object
    type (nothing else is threshold-gated) gets the richer fused score.
    """
    from aida.retrieval import HybridRetrievalHit, RetrievalEvidence

    started = time.perf_counter()
    result_hits: list[HybridRetrievalHit] = []
    for candidate in ranked:
        evidence_factors = build_evidence(candidate, config)
        evidence = RetrievalEvidence(
            object_type=candidate.object_type,
            object_id=candidate.object_id,
            display_name=candidate.display_name,
            final_score=candidate.final_score,
            fusion_method=config.method,
            factors=[
                {
                    "signal": factor.signal,
                    "raw_score": factor.raw_score,
                    "weight": factor.weight,
                    "weighted_score": factor.weighted_score,
                    "rank": factor.rank,
                }
                for factor in evidence_factors
            ],
            graph_expansion_path=candidate.metadata.get("graph_expansion_path", []),
            source_signals=[signal.signal for signal in candidate.signals],
            metadata=candidate.metadata,
        )
        if candidate.object_type == "GOVERNED_TOOL":
            lexical_signal = candidate.get_signal("lexical")
            operational_score = (
                lexical_signal.raw_score if lexical_signal else candidate.final_score
            )
        else:
            operational_score = candidate.final_score
        result_hits.append(
            HybridRetrievalHit(
                object_type=candidate.object_type,
                object_id=candidate.object_id,
                display_name=candidate.display_name,
                score=operational_score,
                reason_codes=[signal.signal for signal in candidate.signals],
                metadata={
                    **candidate.metadata,
                    "retrieval_evidence": {
                        "final_score": evidence.final_score,
                        "fusion_method": evidence.fusion_method,
                        "factors": evidence.factors,
                        "source_signals": evidence.source_signals,
                    },
                },
            )
        )
    pool.reports.append(
        _observe(
            ChannelReport(
                channel="evidence",
                scored=len(ranked),
                contributed=len(result_hits),
                seconds=time.perf_counter() - started,
                mean_score=_mean([hit.score for hit in result_hits]),
            )
        )
    )
    return result_hits


__all__ = [
    "USAGE_POPULARITY_SATURATION",
    "CancellationToken",
    "CandidatePool",
    "ChannelReport",
    "ChannelResult",
    "DeadlineToken",
    "NeverCancelled",
    "PredicateToken",
    "RetrievalCancelled",
    "RetrievalRequest",
    "SignalContribution",
    "assemble_evidence",
    "check_cancelled",
    "fuse",
    "merge_contributions",
    "run_graph_channel",
    "run_trust_channel",
    "run_vector_channel",
    "select_authorized_candidates",
]
