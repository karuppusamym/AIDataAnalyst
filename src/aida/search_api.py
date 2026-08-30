"""
Global Search API Router
=========================

Provides cross-source, cross-type global search with facets and typeahead.

Endpoints
---------
- ``GET /v1/search``         -- cross-source, cross-type global search
- ``GET /v1/search/suggest`` -- typeahead suggestions for command palette

All endpoints are organization-scoped and policy-filtered.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from aida.db import get_session
from aida.full_text_index import build_ts_query, full_text_rank
from aida.models import (
    DataSource,
    MetadataBusinessAnnotation,
    MetadataColumn,
    MetadataTable,
)
from aida.platform_schemas import (
    RetrievalEvidence,
    SearchFacet,
    SearchResult,
    SearchSuggestion,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["search"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_evidence_for_result(
    *,
    object_type: str,
    object_id: str,
    display_name: str,
    score: float,
) -> RetrievalEvidence:
    """Build a minimal evidence payload for a search result."""
    return RetrievalEvidence(
        object_type=object_type,
        object_id=object_id,
        display_name=display_name,
        final_score=score,
        fusion_method="lexical",
        factors=[
            {
                "signal": "lexical",
                "raw_score": score,
                "weight": 1.0,
                "weighted_score": score,
                "rank": None,
            }
        ],
        graph_expansion_path=[],
        source_signals=["lexical"],
        metadata={},
    )


# ---------------------------------------------------------------------------
# GET /v1/search
# ---------------------------------------------------------------------------


@router.get("/search", response_model=dict[str, Any])
async def global_search(
    q: str = Query(min_length=1, max_length=500, description="Search query"),
    organization_id: UUID = Query(description="Organization scope"),
    datasource_id: UUID | None = Query(default=None, description="Optional datasource filter"),
    object_type: str | None = Query(default=None, description="Filter by type: TABLE, COLUMN, ANNOTATION"),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Cross-source, cross-type global search with facets.

    Policy filtering is applied BEFORE ranking:
    - Results are scoped to the caller's organization.
    - Optional datasource_id narrows to one source.
    - No source values appear in control plane responses.
    """
    enforce_organization(context, organization_id)

    # Build query tokens for scoring
    ts_query = build_ts_query(q)
    if not ts_query:
        return {"items": [], "facets": [], "total": 0, "limit": limit, "offset": offset}

    query_tokens = ts_query.replace(" & ", " ").split()
    documents: list[dict[str, Any]] = []

    # Fetch tables (policy-filtered by org BEFORE scoring)
    if object_type is None or object_type == "TABLE":
        table_filters = [func.lower(MetadataTable.name).contains(t) for t in query_tokens[:10]]
        table_stmt = select(MetadataTable).where(
            MetadataTable.organization_id == organization_id,
            MetadataTable.status == "ACTIVE",
            or_(*table_filters) if table_filters else true(),
        )
        if datasource_id:
            table_stmt = table_stmt.where(MetadataTable.datasource_id == datasource_id)
        table_stmt = table_stmt.limit(500)

        table_rows = (await session.scalars(table_stmt)).all()
        for table in table_rows:
            text = " ".join(filter(None, [table.name, table.source_description]))
            documents.append({
                "object_type": "TABLE",
                "object_id": str(table.id),
                "display_name": table.name,
                "qualified_name": table.name,
                "text": text,
                "datasource_id": str(table.datasource_id),
                "metadata": {"table_id": str(table.id)},
            })

    # Fetch columns
    if object_type is None or object_type == "COLUMN":
        col_filters = [func.lower(MetadataColumn.name).contains(t) for t in query_tokens[:10]]
        col_stmt = (
            select(MetadataColumn)
            .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.status == "ACTIVE",
                MetadataColumn.status == "ACTIVE",
                or_(*col_filters) if col_filters else true(),
            )
        )
        if datasource_id:
            col_stmt = col_stmt.where(MetadataTable.datasource_id == datasource_id)
        col_stmt = col_stmt.limit(500)

        col_rows = (await session.scalars(col_stmt)).all()
        for col in col_rows:
            text = " ".join(filter(None, [col.name, col.physical_type]))
            documents.append({
                "object_type": "COLUMN",
                "object_id": str(col.id),
                "display_name": col.name,
                "qualified_name": col.name,
                "text": text,
                "datasource_id": None,
                "metadata": {
                    "column_id": str(col.id),
                    "table_id": str(col.table_id),
                },
            })

    # Rank and paginate
    hits = full_text_rank(q, documents)
    total = len(hits)
    page = hits[offset : offset + limit]

    # Build facets
    facet_counts: dict[str, int] = {}
    for hit in hits:
        facet_counts[hit.object_type] = facet_counts.get(hit.object_type, 0) + 1
    facets = [
        SearchFacet(field="object_type", value=k, count=v)
        for k, v in sorted(facet_counts.items())
    ]

    results = [
        SearchResult(
            object_type=hit.object_type,
            object_id=hit.object_id,
            display_name=hit.display_name,
            qualified_name=hit.qualified_name,
            score=hit.ts_rank,
            evidence=_build_evidence_for_result(
                object_type=hit.object_type,
                object_id=hit.object_id,
                display_name=hit.display_name,
                score=hit.ts_rank,
            ),
            datasource_id=UUID(hit.datasource_id) if hit.datasource_id else None,
        )
        for hit in page
    ]

    return {
        "items": [r.model_dump() for r in results],
        "facets": [f.model_dump() for f in facets],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ---------------------------------------------------------------------------
# GET /v1/search/suggest
# ---------------------------------------------------------------------------


@router.get("/search/suggest", response_model=list[SearchSuggestion])
async def search_suggest(
    q: str = Query(min_length=1, max_length=200, description="Prefix query"),
    organization_id: UUID = Query(description="Organization scope"),
    limit: int = Query(default=10, ge=1, le=50),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "DataAdmin", "DataSteward", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
) -> list[SearchSuggestion]:
    """Typeahead suggestions for the command palette.

    Returns top matching table and column names scoped to the organization.
    """
    enforce_organization(context, organization_id)

    prefix = q.lower().strip()
    if not prefix:
        return []

    # Search tables by prefix
    table_rows = (
        await session.scalars(
            select(MetadataTable)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.status == "ACTIVE",
                func.lower(MetadataTable.name).contains(prefix),
            )
            .limit(limit)
        )
    ).all()

    suggestions: list[SearchSuggestion] = []
    for table in table_rows:
        suggestions.append(
            SearchSuggestion(
                text=table.name,
                object_type="TABLE",
                object_id=str(table.id),
                display_name=table.name,
                qualified_name=table.name,
                score=1.0,
            )
        )

    return suggestions[:limit]
