"""
Atlas Hybrid Retrieval Engine
==============================

Provides a significantly improved metadata retrieval strategy for the
GovernedAgentOrchestrator, replacing the simple lexical LIKE scan with
a two-stage hybrid BM25 + weighted scoring approach.

Architecture
------------
Stage 1: Candidate fetch
  Pull up to agent_retrieval_scan_limit rows from each object type
  (tables, columns, tools, dbt resources, business annotations, metrics)
  using the existing org/datasource scope filters.

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
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.models import (
    BusinessDomain,
    BusinessEntity,
    DataSource,
    DbtArtifactImport,
    DbtProject,
    DbtResource,
    GovernedTool,
    GovernedToolVersion,
    MetadataBusinessAnnotation,
    MetadataColumn,
    MetadataTable,
)

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

    # ------------------------------------------------------------------
    # Sort by score desc, cap at retrieval_limit
    # ------------------------------------------------------------------
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:retrieval_limit]
