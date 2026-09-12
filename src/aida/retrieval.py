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

The enhanced pipeline
---------------------
`hybrid_retrieve` above is the lexical stage and is complete in itself.
`hybrid_retrieve_enhanced` is not a longer version of it -- it is a
*composition* of the stages defined in `aida.retrieval_stages`: authorized
candidates (which calls `hybrid_retrieve`), the vector channel, the graph
channel, the merge rule, trust, fusion, evidence. Each of those is one rule and
is documented where it lives. What this module keeps is the lexical scan itself,
the hit type every caller consumes, and the composition's order.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from aida.business_annotation_versions import current_version_alias
from aida.config import Settings
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
    QueryExecution,
    SemanticMetric,
    SemanticMetricVersion,
    TermSemanticBinding,
)
from aida.quality_coupling import resolve_table_ids
from aida.retrieval_metrics import RETRIEVAL_SECONDS

if TYPE_CHECKING:
    # Annotation-only (this module defers the real import to call time, to
    # break the cycle `retrieval_stages` -> `retrieval` -> `retrieval_stages`).
    from aida.retrieval_stages import CancellationToken

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
    candidate_limit: int | None = None,
    cancel: CancellationToken | None = None,
) -> list[HybridRetrievalHit]:
    """Compose the hybrid retrieval stages; hold no rule of its own.

    Each stage is defined in `aida.retrieval_stages` and is independently
    readable there: authorized candidates, the vector channel, the graph
    channel, the merge rule, trust, fusion, evidence. What lives here is only
    the order they run in and the two cross-cutting properties that order
    makes possible --

    * a cancellation check at every stage boundary, so a retrieval whose
      caller has gone away (or whose deadline has passed) stops between
      stages rather than finishing work nobody will read; and
    * one per-retrieval log line carrying every stage's candidate count,
      latency and mean score, so "which channel is slow" and "which channel
      has stopped contributing" are answerable without a profiler.

    `candidate_limit` bounds the authorized set explicitly. `cancel` defaults
    to no cancellation, so every existing caller behaves exactly as before.

    Backward compatible: falls back gracefully when vector or graph data is
    not available, and every channel records why it was skipped.
    """
    # Deferred import, matching the pattern this function already used for
    # `fusion_ranking`/`graph_retrieval`/`vector_*`: `retrieval_stages` imports
    # `HybridRetrievalHit` and `hybrid_retrieve` from this module, so importing
    # it at module scope would be an import cycle.
    from aida.retrieval_stages import (
        NeverCancelled,
        RetrievalRequest,
        assemble_evidence,
        check_cancelled,
        fuse,
        merge_contributions,
        run_graph_channel,
        run_trust_channel,
        run_vector_channel,
        select_authorized_candidates,
    )

    started = time.perf_counter()
    request = RetrievalRequest(
        datasource=datasource,
        question=question,
        settings=settings,
        organization_id=organization_id or datasource.organization_id,
        preferred_tool_version_id=preferred_tool_version_id,
        fusion_method=fusion_method,
        include_vector=include_vector,
        include_graph=include_graph,
        max_hops=max_hops,
        candidate_limit=candidate_limit,
        cancel=cancel or NeverCancelled(),
    )

    check_cancelled(request, "lexical")
    pool = await select_authorized_candidates(session, request)

    check_cancelled(request, "vector")
    merge_contributions(pool, await run_vector_channel(session, request, pool))

    check_cancelled(request, "graph")
    merge_contributions(pool, await run_graph_channel(session, request, pool))

    check_cancelled(request, "trust")
    merge_contributions(pool, await run_trust_channel(session, request, pool))

    check_cancelled(request, "fusion")
    ranked, config = fuse(request, pool)

    check_cancelled(request, "evidence")
    hits = assemble_evidence(pool, ranked, config)

    elapsed = time.perf_counter() - started
    RETRIEVAL_SECONDS.observe(elapsed)
    logger.info(
        "retrieval_completed",
        datasource_id=str(datasource.id),
        candidates=len(pool.candidates),
        candidate_bound=request.authorized_limit,
        candidate_bound_applied=pool.truncated,
        returned=len(hits),
        fusion_method=config.method,
        seconds=round(elapsed, 4),
        stages=pool.evidence(),
    )
    return hits

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
