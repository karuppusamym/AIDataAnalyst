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
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.business_annotation_versions import current_version_alias
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
    DbtLineageEdge,
    DbtProject,
    DbtResource,
    GlossaryTerm,
    GlossaryTermVersion,
    GovernedTool,
    GovernedToolVersion,
    MetadataBusinessAnnotation,
    MetadataColumn,
    MetadataConstraint,
    MetadataTable,
    QueryExecution,
    SemanticMetric,
    SemanticMetricVersion,
    TermSemanticBinding,
)
from aida.quality_coupling import demote_in_retrieval, fetch_open_incidents, resolve_table_ids
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
# Shared dbt-project helpers
# ---------------------------------------------------------------------------


async def _latest_dbt_artifact_import_ids(
    session: AsyncSession, *, datasource: DataSource
) -> list[UUID]:
    """The most recent `DbtArtifactImport` id per ACTIVE `DbtProject` bound to
    ``datasource``. Factored out of `hybrid_retrieve`'s stage-5 dbt-resource
    block (RT-2) so `hybrid_retrieve_enhanced`'s graph stage can resolve the
    same "latest snapshot per project" scope for dbt `depends_on` edges
    without a second, drifting copy of this resolution logic.
    """
    dbt_project_ids = list(
        await session.scalars(
            select(DbtProject.id).where(
                DbtProject.datasource_id == datasource.id,
                DbtProject.organization_id == datasource.organization_id,
                DbtProject.status == "ACTIVE",
            )
        )
    )
    if not dbt_project_ids:
        return []
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
    return latest_artifact_ids


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
        # Object type and metadata shape here are load-bearing, not cosmetic:
        # GovernedPlanner.plan() (agent_intelligence.py) filters
        # `hit.object_type == "GOVERNED_TOOL"` to find tool candidates at all, then reads
        # `hit.metadata["allowed_roles"]` and `hit.metadata["required_parameters"]` to decide
        # eligibility and whether to ask for clarification. Diverging from that contract
        # silently makes every governed tool invisible to the planner.
        parameters = version.parameter_schema
        hit_id = f"GOVERNED_TOOL:{version.id}"
        if hit_id not in seen_ids:
            seen_ids.add(hit_id)
            hits.append(
                HybridRetrievalHit(
                    object_type="GOVERNED_TOOL",
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
                        "slug": tool.slug,
                        "version": version.version,
                        "allowed_roles": version.allowed_roles,
                        "required_parameters": [
                            item["name"]
                            for item in parameters
                            if item.get("required", True) and item.get("default") is None
                        ],
                    },
                )
            )

    # 4. Business annotations (approved semantic enrichments)
    # AT-6: content lives on the current `MetadataBusinessAnnotationVersion`
    # (append-only, never mutated in place -- `business_annotation_versions.py`),
    # not on `MetadataBusinessAnnotation` itself. The hit's `metadata` carries
    # `annotation_version_id` precisely so the orchestrator's grounding-fragment
    # digest (AT-6, `agent_orchestrator._compute_grounding_fragment_digests`)
    # hashes -- and the run's evidence can later resolve back to -- this exact
    # version, even after a later approval supersedes it.
    version_alias, version_ranked = current_version_alias()
    biz_rows = (
        await session.execute(
            select(
                MetadataBusinessAnnotation,
                version_alias,
                BusinessDomain,
                BusinessEntity,
                MetadataTable,
            )
            .join(version_alias, version_alias.annotation_id == MetadataBusinessAnnotation.id)
            .join(BusinessDomain, BusinessDomain.id == MetadataBusinessAnnotation.domain_id)
            .join(BusinessEntity, BusinessEntity.id == MetadataBusinessAnnotation.entity_id)
            .join(MetadataTable, MetadataTable.id == MetadataBusinessAnnotation.table_id)
            .where(
                MetadataBusinessAnnotation.datasource_id == datasource.id,
                MetadataBusinessAnnotation.organization_id == datasource.organization_id,
                MetadataTable.status == "ACTIVE",
                version_ranked.c.rn == 1,
            )
            .limit(scan_limit)
        )
    ).all()

    for annotation, version, domain, entity, table in biz_rows:
        candidate_text = " ".join(
            filter(None, [
                version.business_name,
                version.business_description,
                domain.display_name,
                entity.display_name,
                version.grain_statement,
                " ".join(version.synonyms or []),
                " ".join(version.suggested_questions or []),
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
                        display_name=version.business_name or table.name,
                        score=score,
                        reason_codes=["BM25_BUSINESS_ANNOTATION"],
                        metadata={
                            "table_id": str(table.id),
                            "source_table_id": str(table.id),
                            "domain": domain.display_name,
                            "entity": entity.display_name,
                            "annotation_version_id": str(version.id),
                        },
                    )
                )

    # 5. dbt resources
    latest_artifact_ids = await _latest_dbt_artifact_import_ids(session, datasource=datasource)
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
                                "table_id": (
                                    str(dbt_resource.matched_table_id)
                                    if dbt_resource.matched_table_id
                                    else None
                                ),
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
                            # _model_context (agent_orchestrator.py) reads table_id or
                            # source_table_id off every hit to decide which tables to hydrate
                            # into the model's SQL-generation context; without this a metric
                            # hit contributes no table context.
                            "source_table_id": str(metric_version.source_table_id),
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


# RT-6: the number of recorded executions against a table beyond which its
# usage_popularity raw_score saturates at 1.0. 10 real executions is a small,
# deliberately conservative bar -- enough to separate "never queried" from
# "actually used" without requiring warehouse-scale traffic to move at all.
_USAGE_POPULARITY_SATURATION = 10


async def _table_execution_counts(
    session: AsyncSession,
    *,
    datasource: DataSource,
    table_ids: set[UUID],
    scan_limit: int,
) -> dict[UUID, int]:
    """RT-6: how many of this datasource's recent completed `QueryExecution`
    rows referenced each of ``table_ids`` -- a real, already-persisted usage
    signal (the same rows AG-6 reads via ``gateway_result.execution.referenced_tables``
    once a query finishes), not a new tracking mechanism.

    `QueryExecution.referenced_tables` stores SQL-qualified name strings, not
    ids, so names are resolved back to `MetadataTable` ids with the same
    `quality_coupling.resolve_table_ids` helper TL-3/AG-6 use for the same
    name-shape ambiguity, keeping one canonical resolution path rather than a
    second hand-rolled one here.
    """
    if not table_ids:
        return {}
    rows = (
        await session.scalars(
            select(QueryExecution.referenced_tables)
            .where(
                QueryExecution.datasource_id == datasource.id,
                QueryExecution.organization_id == datasource.organization_id,
                QueryExecution.status == "COMPLETED",
            )
            .order_by(QueryExecution.created_at.desc())
            .limit(scan_limit)
        )
    ).all()
    if not rows:
        return {}

    all_names: set[str] = set()
    for referenced_tables in rows:
        all_names.update(referenced_tables or [])
    if not all_names:
        return {}

    name_to_id = await resolve_table_ids(
        session, datasource=datasource, table_names=sorted(all_names)
    )

    counts: dict[UUID, int] = {}
    for referenced_tables in rows:
        # A table referenced twice in one query counts once for that execution --
        # this measures how many past *queries* touched the table, not raw
        # token-occurrence count.
        touched = {
            table_id
            for name in (referenced_tables or [])
            if (table_id := name_to_id.get(name)) is not None and table_id in table_ids
        }
        for table_id in touched:
            counts[table_id] = counts.get(table_id, 0) + 1
    return counts


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
        GraphEdge,
        GraphNode,
        KnowledgeGraph,
        expand_graph,
    )
    from aida.vector_index_service import index_freshness, search_persisted_index
    from aida.vector_retrieval import (
        build_embedding_text,
        vector_search,
    )
    from aida.vector_store import EmbeddingRef, VectorIndexUnavailable

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
        # RT-1: prefer the *persisted* index when it is fresh. The live path
        # below embeds every candidate on every query, which is correct but
        # pays a model call per candidate per query -- cost that grows with
        # the estate and with traffic at the same time. The persisted index
        # embeds only the question and compares against vectors built once.
        #
        # The fallback is not a degradation: it is the same computation, and
        # it is what runs whenever the index is empty, stale, built under a
        # different embedding model, or the estate has changed since the last
        # build. Which path ran is recorded per hit (`vector_path`) so
        # "why was this ranked here" stays answerable.
        freshness = await index_freshness(session, org_id, settings=settings)
        hit_by_key = {f"{hit.object_type}:{hit.object_id}": hit for hit in lexical_hits}
        vector_path = "PERSISTED_INDEX" if freshness.usable else "LIVE_EMBED"
        logger.info(
            "retrieval_vector_stage_path",
            path=vector_path,
            reason=freshness.reason,
            indexed_entries=freshness.entries,
            datasource_id=str(datasource.id),
        )

        scored: list[tuple[str, str, float]] = []
        if freshness.usable:
            batch = await embedding_provider.embed([question])
            query_emb = tuple(batch.vectors[0])
            # Policy still filters before ranking: the candidate set handed to
            # the index is exactly the policy-narrowed lexical set, so the
            # index can only reorder what the caller was already entitled to.
            refs = tuple(
                EmbeddingRef(owner_type=hit.object_type, owner_id=str(hit.object_id))
                for hit in lexical_hits
            )
            try:
                scored = list(
                    await search_persisted_index(
                        session,
                        org_id,
                        query_emb,
                        settings=settings,
                        candidates=refs or None,
                        limit=retrieval_limit,
                    )
                )
            except VectorIndexUnavailable as exc:
                # The index went away between the freshness check and the
                # search. Fall back rather than losing the stage.
                logger.info("retrieval_vector_index_unavailable", reason=str(exc))
                vector_path = "LIVE_EMBED"
                freshness = replace(freshness, usable=False)

        if not freshness.usable:
            # One batched call for the question and every candidate text, rather than a
            # call per candidate: the provider bills and rate-limits per request, and
            # N+1 network round trips inside a retrieval path is a latency budget spent
            # on nothing.
            candidate_texts = [
                build_embedding_text(name=hit.display_name, object_type=hit.object_type)
                for hit in lexical_hits
            ]
            batch = await embedding_provider.embed([question, *candidate_texts])
            query_emb_list = list(batch.vectors[0])
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

            scored = [
                (vhit.object_type, str(vhit.object_id), vhit.similarity)
                for vhit in vector_search(
                    query_emb_list, vector_candidates, top_k=retrieval_limit
                )
            ]

        for object_type, object_id, similarity in scored:
            key = f"{object_type}:{object_id}"
            source_hit = hit_by_key.get(key)
            if key in candidates:
                candidates[key].signals.append(
                    SignalScore(signal="vector", raw_score=similarity)
                )
                candidates[key].metadata.setdefault("vector_path", vector_path)
            else:
                metadata = dict(source_hit.metadata) if source_hit else {}
                metadata["vector_path"] = vector_path
                candidates[key] = RankedCandidate(
                    object_type=object_type,
                    object_id=object_id,
                    display_name=(
                        source_hit.display_name if source_hit else str(object_id)
                    ),
                    signals=[SignalScore(signal="vector", raw_score=similarity)],
                    metadata=metadata,
                )

    # ------------------------------------------------------------------
    # Stage 3: Graph expansion (if enabled)
    # ------------------------------------------------------------------
    # Real edges, not just seed nodes. A graph with nodes but no edges lets BFS reach
    # only depth 0 (the seeds themselves, which `expand_graph` doesn't even emit as
    # hits) -- expansion *past* what lexical/vector already found is the entire point
    # of RT-2, so the edge source has to be real governed metadata, not a placeholder.
    # Three real, already-governed edge sources feed the graph: `MetadataConstraint`
    # foreign keys (already-approved, datasource-scoped table-to-table relationships),
    # dbt `DEPENDS_ON` `DbtLineageEdge` rows resolved to their matched tables (the
    # `03-tracker.md` RT-2 follow-up -- a real dbt manifest dependency a table's FKs
    # never capture, e.g. a staging model with no declared constraint), and a
    # candidate `GOVERNED_TOOL` hit's own declared `referenced_tables` (so a table
    # reachable only through a governed tool's SQL, not a raw FK, still expands).
    if include_graph and lexical_hits:
        kg = KnowledgeGraph()
        for hit in lexical_hits:
            kg.add_node(GraphNode(
                node_id=f"{hit.object_type}:{hit.object_id}",
                node_type=hit.object_type,
                display_name=hit.display_name,
                organization_id=org_id,
                datasource_id=hit.metadata.get("datasource_id"),
            ))

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
                .limit(settings.agent_retrieval_scan_limit)
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
            source_node_id = f"TABLE:{table.id}"
            target_node_id = f"TABLE:{target_table.id}"
            if kg.get_node(source_node_id) is None:
                kg.add_node(GraphNode(
                    node_id=source_node_id,
                    node_type="TABLE",
                    display_name=table.name,
                    organization_id=org_id,
                    datasource_id=datasource.id,
                ))
            if kg.get_node(target_node_id) is None:
                kg.add_node(GraphNode(
                    node_id=target_node_id,
                    node_type="TABLE",
                    display_name=target_table.name,
                    organization_id=org_id,
                    datasource_id=datasource.id,
                ))
            kg.add_edge(GraphEdge(
                source_id=source_node_id,
                target_id=target_node_id,
                edge_type="FOREIGN_KEY",
            ))

        # RT-2 follow-up: dbt `DEPENDS_ON` edges, resolved through each side's
        # `matched_table_id`. Only the latest artifact snapshot per ACTIVE dbt
        # project is read (same scope `hybrid_retrieve`'s dbt-resource stage
        # uses), and only edges where BOTH ends resolved to a real, ACTIVE
        # `MetadataTable` are added -- an unmatched dbt node (no `matched_table_id`)
        # contributes no graph edge here, it just isn't a table-level relationship
        # yet.
        dbt_artifact_ids = await _latest_dbt_artifact_import_ids(session, datasource=datasource)
        if dbt_artifact_ids:
            source_resource = aliased(DbtResource)
            target_resource = aliased(DbtResource)
            dbt_edge_rows = (
                await session.execute(
                    select(source_resource, target_resource)
                    .select_from(DbtLineageEdge)
                    .join(
                        source_resource,
                        source_resource.id == DbtLineageEdge.source_resource_id,
                    )
                    .join(
                        target_resource,
                        target_resource.id == DbtLineageEdge.target_resource_id,
                    )
                    .where(
                        DbtLineageEdge.artifact_import_id.in_(dbt_artifact_ids),
                        DbtLineageEdge.organization_id == org_id,
                        DbtLineageEdge.edge_type == "DEPENDS_ON",
                        source_resource.matched_table_id.is_not(None),
                        target_resource.matched_table_id.is_not(None),
                    )
                    .limit(settings.agent_retrieval_scan_limit)
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
                source_node_id = f"TABLE:{source_table.id}"
                target_node_id = f"TABLE:{target_table.id}"
                if kg.get_node(source_node_id) is None:
                    kg.add_node(GraphNode(
                        node_id=source_node_id,
                        node_type="TABLE",
                        display_name=source_table.name,
                        organization_id=org_id,
                        datasource_id=datasource.id,
                    ))
                if kg.get_node(target_node_id) is None:
                    kg.add_node(GraphNode(
                        node_id=target_node_id,
                        node_type="TABLE",
                        display_name=target_table.name,
                        organization_id=org_id,
                        datasource_id=datasource.id,
                    ))
                kg.add_edge(GraphEdge(
                    source_id=source_node_id,
                    target_id=target_node_id,
                    edge_type="DBT_DEPENDS_ON",
                ))

        # RT-2 follow-up: a `GOVERNED_TOOL` candidate's own declared
        # `referenced_tables` (already-published tool metadata, no further
        # approval needed to read) become TOOL -> TABLE edges, so a table a
        # governed tool queries -- but that has no FK/dbt relationship to
        # anything already surfaced -- is still reachable by expansion.
        tool_hits = [h for h in lexical_hits if h.object_type == "GOVERNED_TOOL"]
        graph_tool_name_pool = {
            name
            for hit in tool_hits
            for name in (hit.metadata.get("referenced_tables") or [])
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
                    target_node_id = f"TABLE:{table_id}"
                    if kg.get_node(target_node_id) is None:
                        kg.add_node(GraphNode(
                            node_id=target_node_id,
                            node_type="TABLE",
                            display_name=name,
                            organization_id=org_id,
                            datasource_id=datasource.id,
                        ))
                    kg.add_edge(GraphEdge(
                        source_id=tool_node_id,
                        target_id=target_node_id,
                        edge_type="TOOL_REFERENCES_TABLE",
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
            # `GraphHit.object_id` is the graph's own node id (`f"{type}:{id}"`, per the
            # construction above), not a bare object id -- unwrap it here rather than
            # leaking the composite string into `RankedCandidate.object_id`, which every
            # other caller (e.g. `_model_context`'s `UUID(hit.object_id)`) expects to be
            # the raw id.
            raw_object_id = ghit.object_id.removeprefix(f"{ghit.object_type}:")
            key = f"{ghit.object_type}:{raw_object_id}"
            if key in candidates:
                candidates[key].signals.append(
                    SignalScore(signal="graph", raw_score=ghit.proximity_score)
                )
                candidates[key].metadata.setdefault("graph_expansion_path", ghit.expansion_path)
            else:
                candidates[key] = RankedCandidate(
                    object_type=ghit.object_type,
                    object_id=raw_object_id,
                    display_name=ghit.display_name,
                    signals=[SignalScore(signal="graph", raw_score=ghit.proximity_score)],
                    metadata={**ghit.metadata, "graph_expansion_path": ghit.expansion_path},
                )

    # ------------------------------------------------------------------
    # Stage 4: quality/trust demotion (RT-7/DQ-3) and usage/popularity
    # (RT-6). Both are derived from persisted runtime evidence rather than
    # placeholders, and both batch their shared lookups per retrieval call.
    # ------------------------------------------------------------------
    candidate_table_ids: dict[str, set[UUID]] = {}
    tool_name_pool: set[str] = set()
    for key, candidate in candidates.items():
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

    if tool_name_pool:
        tool_table_ids = await resolve_table_ids(
            session, datasource=datasource, table_names=sorted(tool_name_pool)
        )
        for key, candidate in candidates.items():
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
        await fetch_open_incidents(session, datasource=datasource, table_ids=list(all_table_ids))
        if all_table_ids
        else []
    )
    usage_counts = await _table_execution_counts(
        session,
        datasource=datasource,
        table_ids=all_table_ids,
        scan_limit=settings.agent_retrieval_scan_limit,
    )

    for key, candidate in candidates.items():
        ids = candidate_table_ids.get(key) or set()
        if ids:
            per_table_scores = {
                str(table_id): demote_in_retrieval(str(table_id), incidents)
                for table_id in ids
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
        usage_popularity_score = min(1.0, popularity_count / _USAGE_POPULARITY_SATURATION)
        candidate.signals.append(
            SignalScore(signal="quality_trust", raw_score=round(quality_trust_score, 4))
        )
        candidate.signals.append(
            SignalScore(signal="usage_popularity", raw_score=round(usage_popularity_score, 4))
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
            graph_expansion_path=candidate.metadata.get("graph_expansion_path", []),
            source_signals=[s.signal for s in candidate.signals],
            metadata=candidate.metadata,
        )

        # GovernedPlanner.plan() (agent_intelligence.py) gates GOVERNED_TOOL selection
        # on `hit.score >= Settings.agent_tool_match_threshold`, a [0,1] match-confidence
        # figure the lexical stage produces (BM25 + boosts, capped at 1.0). A fusion
        # score is a different, relative ranking quantity on its own scale (RRF's is
        # ~1/rrf_k) -- handing it to that threshold would silently change which
        # governed tools the planner will ever select, which is tool-selection
        # orchestration behaviour this integration does not touch. So a tool hit keeps
        # its lexical score as `.score`; the real fused score is still fully visible in
        # `retrieval_evidence.final_score` for inspection. Every other object type
        # (nothing else is threshold-gated) gets the richer fused score as `.score`.
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


# ---------------------------------------------------------------------------
# GROUP A: RT-9 cross-source retrieval + RT-5 global-search support
# ---------------------------------------------------------------------------


async def hybrid_retrieve_cross_source(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasources: list[DataSource],
    question: str,
    settings: Settings,
    fusion_method: str = "rrf",
    include_vector: bool = True,
    include_graph: bool = True,
    max_hops: int = 2,
    limit: int | None = None,
) -> list[HybridRetrievalHit]:
    """RT-9: one query, genuinely spanning every datasource in ``datasources``.

    Before this function, `hybrid_retrieve`/`hybrid_retrieve_enhanced` both took
    a single `DataSource` and scoped every candidate query to it -- real hybrid
    (lexical + vector + graph + fusion) retrieval never crossed a datasource
    boundary, only the separate, lexical-only `search_api.py::global_search`
    surface did (03-tracker.md RT-9's own honesty note: "the cross-*source*
    half remains a separate ... surface, not this one"). This closes that
    specific gap for the hybrid pipeline.

    Approach, stated plainly: each datasource's candidates are independently
    policy-scoped and fused by `hybrid_retrieve_enhanced` (unchanged -- FK/dbt
    graph edges and business annotations never cross a datasource's own
    boundary regardless), then the per-datasource fused results are merged and
    re-sorted by their already-computed `final_score`. This is a merge-and-sort
    over independently-fused rankings, not a second joint RRF pass across the
    combined candidate pool -- because every datasource run uses the same
    `fusion_method`/weights (RRF's rank-based score or the weighted-linear sum,
    both computed the same way regardless of pool size), so the resulting
    scores are on a comparable scale. Doing a true joint fusion would require
    running every signal (lexical scan, embedding batch, graph BFS) against
    the union of all datasources' candidates in one pass, which is a larger
    restructuring than this row's scope covers -- named here rather than
    silently presented as identical to a joint pass.

    Every hit keeps its full per-datasource `retrieval_evidence` (RT-3:
    every ranking factor stays inspectable) plus the originating
    `datasource_id`, so a caller can always tell which source a result came
    from and exactly why it ranked where it did.
    """
    if not datasources:
        return []

    per_source_limit = limit or settings.agent_retrieval_limit
    merged: list[HybridRetrievalHit] = []
    for ds in datasources:
        # Sequential, not `asyncio.gather`: all datasources share one
        # `AsyncSession`, which is not safe for concurrent use across
        # coroutines.
        source_hits = await hybrid_retrieve_enhanced(
            session,
            datasource=ds,
            question=question,
            settings=settings,
            organization_id=organization_id,
            fusion_method=fusion_method,
            include_vector=include_vector,
            include_graph=include_graph,
            max_hops=max_hops,
        )
        for hit in source_hits:
            hit.metadata = {**hit.metadata, "datasource_id": str(ds.id)}
            merged.append(hit)

    merged.sort(key=lambda h: h.score, reverse=True)
    return merged[:per_source_limit]
