"""
Atlas Hybrid Retrieval Engine
==============================

Provides a significantly improved metadata retrieval strategy for the
GovernedAgentOrchestrator, replacing the simple lexical LIKE scan with
a two-stage hybrid BM25 + weighted scoring approach.

Architecture
------------
Stage 1: Candidate fetch
  Pull up to agent_retrieval_scan_limit rows from each object type (tables,
  columns, tools, business annotations, dbt resources, published semantic
  metrics, and glossary terms bound to a semantic object) using the existing
  org/datasource scope filters. SM-2: an ACTIVE glossary-term<->semantic-object
  binding folds the term's definition/synonyms into the metric's candidate
  text (and the metric's identity into the term's hit metadata), so the
  binding participates in scoring in both directions instead of being a
  static link nobody reads at query time.

Stage 2: Hybrid scoring
  Score each candidate with three additive signals:

  a) BM25-style token overlap
     - Tokenise query and candidate text into lowercase tokens
     - Score = (matched tokens / total query tokens), boosted by IDF
       approximation (penalise very common words)
     - Avoids the need for a vector index at this stage; can be replaced
       with pgvector in Phase 2 when the embedding column is added

  b) Exact-phrase bonus (+0.2)
     - If the full query string appears verbatim (lowercased) in the
       candidate text, add a strong exact-match bonus

  c) Governed-tool priority boost (+0.25)
     - Published governed tools are strongly preferred over raw table hits;
       this matches the planner strategy priority order

Stage 3: Ranking & deduplication
  Sort by score descending, deduplicate by object_id, cap at
  agent_retrieval_limit (default 25).

This module is designed to be a drop-in replacement for the retrieval
block inside GovernedRetriever. The public interface is identical:

    hits = await hybrid_retrieve(session, datasource=ds, question=q, ...)

Usage
-----
Import and call from agent_intelligence.GovernedRetriever.retrieve() or
directly from GovernedAgentOrchestrator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.embedding_provider import (
    AsyncEmbeddingProvider,
    EmbeddingUnavailable,
    resolve_embedding_provider,
)
from aida.models import (
    BusinessDomain,
    BusinessEntity,
    DataSource,
    DbtArtifactImport,
    DbtProject,
    DbtResource,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernedTool,
    GovernedToolVersion,
    MetadataBusinessAnnotation,
    MetadataColumn,
    MetadataTable,
    SemanticMetric,
    SemanticMetricVersion,
    TermSemanticBinding,
)
from aida.secrets import SecretResolver

# ---------------------------------------------------------------------------
# Text normalisation & tokenisation
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "for",
        "from", "get", "how", "i", "in", "is", "it", "list", "me", "my",
        "of", "on", "or", "see", "show", "the", "to", "what", "which",
        "with", "you", "latest", "all", "give", "tell",
    }
)


logger = structlog.get_logger(__name__)


def _tokenise(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, strip stop words, split snake_case."""
    # Split camelCase / snake_case before lowercasing
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    expanded = expanded.replace("_", " ")
    tokens = re.findall(r"[a-z0-9]+", expanded.lower())
    return [t for t in tokens if len(t) > 1 and t not in _STOP_WORDS]


def _idf_weight(token: str) -> float:
    """
    Simple heuristic IDF — penalise very short and very common-looking tokens.
    A full IDF index would require corpus stats; this approximation is enough
    to rank 'revenue' higher than 'data'.
    """
    if len(token) <= 2:
        return 0.5
    if len(token) <= 4:
        return 0.8
    return 1.0


def _bm25_score(query_tokens: list[str], candidate_text: str) -> float:
    """
    Lightweight BM25-inspired score: IDF-weighted token overlap ratio.
    Returns a value in [0.0, 1.0].
    """
    if not query_tokens or not candidate_text:
        return 0.0
    lower_text = candidate_text.lower().replace("_", " ")
    total_weight = sum(_idf_weight(t) for t in query_tokens)
    if total_weight == 0:
        return 0.0
    matched_weight = sum(
        _idf_weight(t) for t in query_tokens if t in lower_text
    )
    return min(1.0, matched_weight / total_weight)


def _exact_phrase_bonus(query: str, candidate_text: str) -> float:
    """Return 0.2 if the lowercased query appears as a substring of the candidate."""
    return 0.2 if query.lower() in candidate_text.lower() else 0.0


# ---------------------------------------------------------------------------
# RetrievalHit (mirrors agent_intelligence.RetrievalHit for drop-in use)
# ---------------------------------------------------------------------------


class HybridRetrievalHit:
    """Scored retrieval result compatible with GovernedRetriever output."""

    __slots__ = (
        "object_type", "object_id", "display_name",
        "score", "reason_codes", "metadata",
    )

    def __init__(
        self,
        object_type: str,
        object_id: str,
        display_name: str,
        score: float,
        reason_codes: list[str],
        metadata: dict[str, Any],
    ) -> None:
        self.object_type = object_type
        self.object_id = object_id
        self.display_name = display_name
        self.score = score
        self.reason_codes = reason_codes
        self.metadata = metadata

    def evidence(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "display_name": self.display_name,
            "score": self.score,
            "reason_codes": self.reason_codes,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Public retrieval function
# ---------------------------------------------------------------------------


async def hybrid_retrieve(
    session: AsyncSession,
    *,
    datasource: DataSource,
    question: str,
    settings: Settings,
    preferred_tool_version_id: UUID | None = None,
) -> list[HybridRetrievalHit]:
    """
    Two-stage hybrid retrieval returning the top-N scored metadata hits.

    Parameters
    ----------
    session               Async SQLAlchemy session
    datasource            The DataSource to scope retrieval within
    question              Natural-language question from the user / agent
    settings              Atlas Settings (governs limits)
    preferred_tool_version_id  Hint: if supplied and matches a published tool,
                          that tool receives an extra +0.35 priority boost

    Returns
    -------
    List of HybridRetrievalHit sorted by score descending, capped at
    settings.agent_retrieval_limit.
    """
    query_tokens = _tokenise(question)
    scan_limit = settings.agent_retrieval_scan_limit
    retrieval_limit = settings.agent_retrieval_limit

    hits: list[HybridRetrievalHit] = []
    seen_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Fetch all candidate objects concurrently
    # (sequential awaits — fine for typical catalog sizes)
    # ------------------------------------------------------------------

    # 1. Tables
    name_filters = [func.lower(MetadataTable.name).contains(t) for t in query_tokens[:10]]
    table_rows = (
        await session.scalars(
            select(MetadataTable)
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.organization_id == datasource.organization_id,
                MetadataTable.status == "ACTIVE",
                or_(*name_filters) if name_filters else true(),
            )
            .limit(scan_limit)
        )
    ).all()

    for table in table_rows:
        candidate_text = " ".join(
            filter(None, [table.name, table.source_description])
        )
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score > 0:
            hit_id = f"TABLE:{table.id}"
            if hit_id not in seen_ids:
                seen_ids.add(hit_id)
                hits.append(
                    HybridRetrievalHit(
                        object_type="TABLE",
                        object_id=str(table.id),
                        display_name=table.name,
                        score=score,
                        reason_codes=["BM25_TABLE_NAME"],
                        metadata={"table_id": str(table.id)},
                    )
                )

    # 2. Columns
    col_filters = [func.lower(MetadataColumn.name).contains(t) for t in query_tokens[:10]]
    column_rows = (
        await session.scalars(
            select(MetadataColumn)
            .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.organization_id == datasource.organization_id,
                MetadataTable.status == "ACTIVE",
                MetadataColumn.status == "ACTIVE",
                or_(*col_filters) if col_filters else true(),
            )
            .limit(scan_limit)
        )
    ).all()

    for col in column_rows:
        candidate_text = " ".join(filter(None, [col.name, col.physical_type]))
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score > 0:
            hit_id = f"COLUMN:{col.id}"
            if hit_id not in seen_ids:
                seen_ids.add(hit_id)
                hits.append(
                    HybridRetrievalHit(
                        object_type="COLUMN",
                        object_id=str(col.id),
                        display_name=col.name,
                        score=score,
                        reason_codes=["BM25_COLUMN_NAME"],
                        metadata={
                            "column_id": str(col.id),
                            "table_id": str(col.table_id),
                        },
                    )
                )

    # 3. Published governed tools (highest priority — boosted)
    tool_rows = (
        await session.execute(
            select(GovernedToolVersion, GovernedTool)
            .join(GovernedTool, GovernedTool.id == GovernedToolVersion.tool_id)
            .where(
                GovernedToolVersion.datasource_id == datasource.id,
                GovernedToolVersion.organization_id == datasource.organization_id,
                GovernedToolVersion.status == "PUBLISHED",
            )
            .limit(scan_limit)
        )
    ).all()

    for version, tool in tool_rows:
        candidate_text = " ".join(
            filter(None, [
                version.name,       # GovernedToolVersion.name is the human-readable display name
                tool.slug,          # slug is also useful for keyword matching
                version.description,
            ])
        )
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        # Governing-tool priority boost
        tool_boost = 0.25
        # Extra boost if this is the caller's preferred tool
        preferred_boost = 0.35 if (
            preferred_tool_version_id and preferred_tool_version_id == version.id
        ) else 0.0
        score = round(min(1.0, bm25 + exact + tool_boost + preferred_boost), 4)
        hit_id = f"TOOL_VERSION:{version.id}"
        if hit_id not in seen_ids:
            seen_ids.add(hit_id)
            hits.append(
                HybridRetrievalHit(
                    object_type="TOOL_VERSION",
                    object_id=str(version.id),
                    display_name=version.name,          # version.name is the display name
                    score=score,
                    reason_codes=["BM25_TOOL_NAME", "GOVERNED_TOOL_BOOST"],
                    metadata={
                        "tool_version_id": str(version.id),
                        "tool_id": str(tool.id),
                        "datasource_id": str(version.datasource_id),
                        # GovernedToolVersion has no primary_table_id; use referenced_tables list
                        "referenced_tables": version.referenced_tables or [],
                    },
                )
            )

    # 4. Business annotations (approved semantic enrichments)
    biz_rows = (
        await session.execute(
            select(MetadataBusinessAnnotation, BusinessDomain, BusinessEntity, MetadataTable)
            .join(BusinessDomain, BusinessDomain.id == MetadataBusinessAnnotation.domain_id)
            .join(BusinessEntity, BusinessEntity.id == MetadataBusinessAnnotation.entity_id)
            .join(MetadataTable, MetadataTable.id == MetadataBusinessAnnotation.table_id)
            .where(
                MetadataBusinessAnnotation.datasource_id == datasource.id,
                MetadataBusinessAnnotation.organization_id == datasource.organization_id,
                MetadataTable.status == "ACTIVE",
            )
            .limit(scan_limit)
        )
    ).all()

    for annotation, domain, entity, table in biz_rows:
        candidate_text = " ".join(
            filter(None, [
                annotation.business_name,
                annotation.business_description,
                domain.display_name,
                entity.display_name,
                annotation.grain_statement,
                " ".join(annotation.synonyms or []),
                " ".join(annotation.suggested_questions or []),
            ])
        )
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score > 0:
            hit_id = f"BIZ_ANNOTATION:{annotation.id}"
            if hit_id not in seen_ids:
                seen_ids.add(hit_id)
                hits.append(
                    HybridRetrievalHit(
                        object_type="BUSINESS_ANNOTATION",
                        object_id=str(annotation.id),
                        display_name=annotation.business_name or table.name,
                        score=score,
                        reason_codes=["BM25_BUSINESS_ANNOTATION"],
                        metadata={
                            "table_id": str(table.id),
                            "source_table_id": str(table.id),
                            "domain": domain.display_name,
                            "entity": entity.display_name,
                        },
                    )
                )

    # 5. dbt resources
    dbt_project_ids = list(
        await session.scalars(
            select(DbtProject.id).where(
                DbtProject.datasource_id == datasource.id,
                DbtProject.organization_id == datasource.organization_id,
                DbtProject.status == "ACTIVE",
            )
        )
    )
    if dbt_project_ids:
        artifact_rows = (
            await session.scalars(
                select(DbtArtifactImport)
                .where(DbtArtifactImport.dbt_project_id.in_(dbt_project_ids))
                .order_by(DbtArtifactImport.dbt_project_id, DbtArtifactImport.created_at.desc())
            )
        ).all()
        seen_projects: set[UUID] = set()
        latest_artifact_ids: list[UUID] = []
        for artifact in artifact_rows:
            if artifact.dbt_project_id not in seen_projects:
                latest_artifact_ids.append(artifact.id)
                seen_projects.add(artifact.dbt_project_id)

        if latest_artifact_ids:
            dbt_rows = (
                await session.scalars(
                    select(DbtResource)
                    .where(DbtResource.artifact_import_id.in_(latest_artifact_ids))
                    .limit(scan_limit)
                )
            ).all()
            for dbt_resource in dbt_rows:
                col_desc_text = (
                    " ".join(dbt_resource.column_descriptions.values())
                    if dbt_resource.column_descriptions
                    else ""
                )
                candidate_text = " ".join(
                    filter(None, [
                        dbt_resource.name,
                        dbt_resource.description,
                        dbt_resource.original_file_path,
                        col_desc_text,
                    ])
                )
                bm25 = _bm25_score(query_tokens, candidate_text)
                exact = _exact_phrase_bonus(question, candidate_text)
                score = round(min(1.0, bm25 + exact), 4)
                if score > 0:
                    hit_id = f"DBT_RESOURCE:{dbt_resource.id}"
                    if hit_id not in seen_ids:
                        seen_ids.add(hit_id)
                        hits.append(
                            HybridRetrievalHit(
                                object_type="DBT_RESOURCE",
                                object_id=str(dbt_resource.id),
                                display_name=dbt_resource.name,     # correct field
                                score=score,
                                reason_codes=["BM25_DBT_RESOURCE"],
                                metadata={
                                    "dbt_resource_id": str(dbt_resource.id),
                                    "resource_type": dbt_resource.resource_type,
                                },
                            )
                        )

    # 6. Semantic metrics (SM-2: a bound, ACTIVE glossary term's definition and
    #    synonyms are folded into the metric's retrievable text, so the binding
    #    actually participates in scoring rather than sitting as a link nobody
    #    reads at query time)
    metric_term_rows = (
        await session.execute(
            select(SemanticMetricVersion, SemanticMetric, GlossaryTermVersion)
            .join(SemanticMetric, SemanticMetric.id == SemanticMetricVersion.metric_id)
            .join(MetadataTable, MetadataTable.id == SemanticMetricVersion.source_table_id)
            .outerjoin(
                TermSemanticBinding,
                (TermSemanticBinding.semantic_object_type == "METRIC")
                & (TermSemanticBinding.semantic_object_id == SemanticMetric.id)
                & (TermSemanticBinding.status == "ACTIVE"),
            )
            .outerjoin(
                GlossaryTermVersion,
                (GlossaryTermVersion.term_id == TermSemanticBinding.term_id)
                & (GlossaryTermVersion.status == "APPROVED"),
            )
            .where(
                MetadataTable.datasource_id == datasource.id,
                MetadataTable.organization_id == datasource.organization_id,
                SemanticMetricVersion.status == "PUBLISHED",
            )
            .limit(scan_limit)
        )
    ).all()

    metrics_by_id: dict[str, tuple[Any, Any, list[Any]]] = {}
    for metric_version, metric, bound_term_version in metric_term_rows:
        entry = metrics_by_id.setdefault(str(metric.id), (metric_version, metric, []))
        if bound_term_version is not None:
            entry[2].append(bound_term_version)

    for metric_version, metric, bound_term_versions in metrics_by_id.values():
        term_text_parts: list[str] = []
        bound_term_ids: list[str] = []
        for term_version in bound_term_versions:
            term_text_parts.append(term_version.display_name)
            term_text_parts.append(term_version.definition)
            term_text_parts.extend(term_version.synonyms or [])
            bound_term_ids.append(str(term_version.term_id))
        candidate_text = " ".join(
            filter(
                None,
                [metric_version.name, metric_version.description, metric.slug, *term_text_parts],
            )
        )
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score > 0:
            hit_id = f"SEMANTIC_METRIC:{metric.id}"
            if hit_id not in seen_ids:
                seen_ids.add(hit_id)
                reason_codes = ["BM25_SEMANTIC_METRIC"]
                if bound_term_ids:
                    reason_codes.append("GLOSSARY_TERM_BOUND")
                hits.append(
                    HybridRetrievalHit(
                        object_type="SEMANTIC_METRIC",
                        object_id=str(metric.id),
                        display_name=metric_version.name,
                        score=score,
                        reason_codes=reason_codes,
                        metadata={
                            "metric_id": str(metric.id),
                            "metric_slug": metric.slug,
                            "bound_term_ids": bound_term_ids,
                        },
                    )
                )

    # 7. Glossary terms (SM-2: the other retrieval direction -- a term hit
    #    surfaces the semantic objects bound to it, so a search that lands on
    #    the term itself can resolve to the metric it governs)
    term_binding_rows = (
        await session.execute(
            select(GlossaryTermVersion, GlossaryTerm, SemanticMetric)
            .join(GlossaryTerm, GlossaryTerm.id == GlossaryTermVersion.term_id)
            .join(TermSemanticBinding, TermSemanticBinding.term_id == GlossaryTerm.id)
            .join(
                SemanticMetric,
                (TermSemanticBinding.semantic_object_type == "METRIC")
                & (SemanticMetric.id == TermSemanticBinding.semantic_object_id),
            )
            .where(
                GlossaryTermVersion.status == "APPROVED",
                GlossaryTerm.organization_id == datasource.organization_id,
                TermSemanticBinding.status == "ACTIVE",
                SemanticMetric.project_id == datasource.project_id,
            )
            .limit(scan_limit)
        )
    ).all()

    terms_by_id: dict[str, tuple[Any, Any, list[Any]]] = {}
    for term_version, term, bound_metric in term_binding_rows:
        entry = terms_by_id.setdefault(str(term.id), (term_version, term, []))
        entry[2].append(bound_metric)

    for term_version, term, bound_metrics in terms_by_id.values():
        term_text = [
            term_version.display_name,
            term_version.definition,
            *(term_version.synonyms or []),
        ]
        candidate_text = " ".join(filter(None, term_text))
        bm25 = _bm25_score(query_tokens, candidate_text)
        exact = _exact_phrase_bonus(question, candidate_text)
        score = round(min(1.0, bm25 + exact), 4)
        if score > 0:
            hit_id = f"GLOSSARY_TERM:{term.id}"
            if hit_id not in seen_ids:
                seen_ids.add(hit_id)
                hits.append(
                    HybridRetrievalHit(
                        object_type="GLOSSARY_TERM",
                        object_id=str(term.id),
                        display_name=term_version.display_name,
                        score=score,
                        reason_codes=["BM25_GLOSSARY_TERM", "SEMANTIC_OBJECT_BOUND"],
                        metadata={
                            "term_id": str(term.id),
                            "term_key": term.term_key,
                            "bound_semantic_object_ids": [str(m.id) for m in bound_metrics],
                        },
                    )
                )

    # ------------------------------------------------------------------
    # Sort by score desc, cap at retrieval_limit
    # ------------------------------------------------------------------
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:retrieval_limit]


# ---------------------------------------------------------------------------
# Enhanced hybrid retrieval with full-text, vector, graph, and fusion
# ---------------------------------------------------------------------------


@dataclass
class RetrievalEvidence:
    """Per-result evidence with factor breakdown.

    Every ranking factor is inspectable.  ``factors`` maps signal names
    (``lexical``, ``vector``, ``graph``, ``quality_trust``, ``usage_popularity``)
    to their weight/score detail.
    """

    object_type: str
    object_id: str
    display_name: str
    final_score: float
    fusion_method: str
    factors: list[dict[str, Any]]
    graph_expansion_path: list[str]
    source_signals: list[str]
    metadata: dict[str, Any]


async def hybrid_retrieve_enhanced(
    session: AsyncSession,
    *,
    datasource: DataSource,
    question: str,
    settings: Settings,
    preferred_tool_version_id: UUID | None = None,
    organization_id: UUID | None = None,
    fusion_method: str = "rrf",
    include_vector: bool = True,
    include_graph: bool = True,
    max_hops: int = 2,
) -> list[HybridRetrievalHit]:
    """Enhanced hybrid retrieval orchestrating full-text, vector, graph, and fusion.

    This is the new pipeline that orchestrates:
      1. Full-text search (ts_query-style)
      2. Vector similarity search
      3. Graph expansion from seed hits
      4. Fusion ranking with inspectable factors

    Backward compatible: falls back gracefully when vector/graph data
    is not available.
    """
    from aida.fusion_ranking import (
        FusionConfig,
        RankedCandidate,
        SignalScore,
        build_evidence,
        fuse_results,
    )
    from aida.graph_retrieval import (
        GraphNode,
        KnowledgeGraph,
        expand_graph,
    )
    from aida.vector_retrieval import (
        build_embedding_text,
        vector_search,
    )

    org_id = organization_id or datasource.organization_id
    # Tokenisation and the scan cap belong to `hybrid_retrieve`, which is called below and
    # applies both itself. Recomputing them here produced two unused locals and, worse, a
    # second place where a cap could drift out of step with the one actually enforced.
    retrieval_limit = settings.agent_retrieval_limit

    # ------------------------------------------------------------------
    # Stage 1: Lexical / BM25 retrieval (existing pipeline)
    # ------------------------------------------------------------------
    lexical_hits = await hybrid_retrieve(
        session,
        datasource=datasource,
        question=question,
        settings=settings,
        preferred_tool_version_id=preferred_tool_version_id,
    )

    # Build candidate index from lexical hits
    candidates: dict[str, RankedCandidate] = {}
    for hit in lexical_hits:
        key = f"{hit.object_type}:{hit.object_id}"
        candidates[key] = RankedCandidate(
            object_type=hit.object_type,
            object_id=hit.object_id,
            display_name=hit.display_name,
            signals=[SignalScore(signal="lexical", raw_score=hit.score)],
            metadata=hit.metadata,
        )

    # ------------------------------------------------------------------
    # Stage 2: Vector similarity (if enabled)
    # ------------------------------------------------------------------
    # The vector stage runs only with a real embedding model behind it. It used to build
    # `HashEmbeddingProvider()` unconditionally and feed the result into fusion as a
    # signal named "vector" -- but a SHA-256 digest has no semantic structure, so that
    # score was noise carrying the name of a signal, and fusion could rank on it. With no
    # provider configured the stage is skipped and the reason recorded, which is a
    # smaller answer rather than a confidently wrong one (INV-4, INV-9).
    embedding_provider: AsyncEmbeddingProvider | None = None
    vector_skipped_reason: str | None = None
    if include_vector:
        try:
            embedding_provider = resolve_embedding_provider(settings, SecretResolver(settings))
        except EmbeddingUnavailable as exc:
            vector_skipped_reason = str(exc)
            logger.info(
                "retrieval_vector_stage_skipped",
                reason=vector_skipped_reason,
                datasource_id=str(datasource.id),
            )

    if include_vector and embedding_provider is not None:
        # One batched call for the question and every candidate text, rather than a call
        # per candidate: the provider bills and rate-limits per request, and N+1 network
        # round trips inside a retrieval path is a latency budget spent on nothing.
        candidate_texts = [
            build_embedding_text(name=hit.display_name, object_type=hit.object_type)
            for hit in lexical_hits
        ]
        batch = await embedding_provider.embed([question, *candidate_texts])
        query_emb = list(batch.vectors[0])
        candidate_embeddings = [list(v) for v in batch.vectors[1:]]

        vector_candidates: list[dict[str, Any]] = []
        for hit, emb in zip(lexical_hits, candidate_embeddings, strict=True):
            vector_candidates.append({
                "object_type": hit.object_type,
                "object_id": hit.object_id,
                "display_name": hit.display_name,
                "embedding": emb,
                "datasource_id": hit.metadata.get("datasource_id"),
                "metadata": hit.metadata,
            })

        vector_hits = vector_search(
            query_emb,
            vector_candidates,
            top_k=retrieval_limit,
        )

        for vhit in vector_hits:
            key = f"{vhit.object_type}:{vhit.object_id}"
            if key in candidates:
                candidates[key].signals.append(
                    SignalScore(signal="vector", raw_score=vhit.similarity)
                )
            else:
                candidates[key] = RankedCandidate(
                    object_type=vhit.object_type,
                    object_id=vhit.object_id,
                    display_name=vhit.display_name,
                    signals=[SignalScore(signal="vector", raw_score=vhit.similarity)],
                    metadata=vhit.metadata or {},
                )

    # ------------------------------------------------------------------
    # Stage 3: Graph expansion (if enabled)
    # ------------------------------------------------------------------
    if include_graph and lexical_hits:
        kg = KnowledgeGraph()
        # Build minimal graph from seed hits (in production this would
        # load from the database; here we create nodes from the hits)
        for hit in lexical_hits:
            kg.add_node(GraphNode(
                node_id=f"{hit.object_type}:{hit.object_id}",
                node_type=hit.object_type,
                display_name=hit.display_name,
                organization_id=org_id,
                datasource_id=hit.metadata.get("datasource_id"),
            ))

        seed_ids = [f"{h.object_type}:{h.object_id}" for h in lexical_hits[:10]]
        graph_hits = expand_graph(
            kg,
            seed_ids,
            allowed_org_id=org_id,
            max_hops=max_hops,
            max_results=retrieval_limit,
        )

        for ghit in graph_hits:
            key = ghit.object_id
            if key in candidates:
                candidates[key].signals.append(
                    SignalScore(signal="graph", raw_score=ghit.proximity_score)
                )
            else:
                candidates[key] = RankedCandidate(
                    object_type=ghit.object_type,
                    object_id=ghit.object_id,
                    display_name=ghit.display_name,
                    signals=[SignalScore(signal="graph", raw_score=ghit.proximity_score)],
                    metadata=ghit.metadata,
                )

    # ------------------------------------------------------------------
    # Stage 4: Add placeholder signals (quality_trust, usage_popularity)
    # ------------------------------------------------------------------
    for candidate in candidates.values():
        # Quality trust placeholder -- will be populated by DQ-3 coupling
        candidate.signals.append(
            SignalScore(signal="quality_trust", raw_score=0.5)
        )
        # Usage popularity placeholder
        candidate.signals.append(
            SignalScore(signal="usage_popularity", raw_score=0.5)
        )

    # ------------------------------------------------------------------
    # Stage 5: Fusion ranking
    # ------------------------------------------------------------------
    config = FusionConfig(method=fusion_method)
    candidate_list = list(candidates.values())
    ranked = fuse_results(candidate_list, config=config, top_k=retrieval_limit)

    # ------------------------------------------------------------------
    # Convert back to HybridRetrievalHit with evidence
    # ------------------------------------------------------------------
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
                    "signal": f.signal,
                    "raw_score": f.raw_score,
                    "weight": f.weight,
                    "weighted_score": f.weighted_score,
                    "rank": f.rank,
                }
                for f in evidence_factors
            ],
            graph_expansion_path=[],
            source_signals=[s.signal for s in candidate.signals],
            metadata=candidate.metadata,
        )

        result_hits.append(
            HybridRetrievalHit(
                object_type=candidate.object_type,
                object_id=candidate.object_id,
                display_name=candidate.display_name,
                score=candidate.final_score,
                reason_codes=[s.signal for s in candidate.signals],
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

    return result_hits
