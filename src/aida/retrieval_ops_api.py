"""RT-1: operator control over the persisted vector index.

No business logic of its own -- it delegates to `vector_index_service`, which
owns the rules.

**Not here: a steward worklist.** `stewardship_api.list_documentation_worklist`
(AT-5) already ranks what a steward should document next, and a second ranked
backlog would be the "two catalogues" seam this platform's own competitive
research names as a thing to never build. `stewardship_worklist.py` holds the
richer `usage x impact x deficit` scorer as a pure function for AT-5 to adopt;
it is deliberately not exposed as a rival endpoint.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import get_session
from aida.embedding_provider import EmbeddingUnavailable
from aida.models import Organization
from aida.schemas import ApiModel
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.vector_index_service import index_freshness, rebuild_vector_index
from aida.vector_store import VectorIndexUnavailable

router = APIRouter(prefix="/v1", tags=["retrieval-ops"])

INDEX_OPERATORS = ("PlatformAdmin", "Operations", "MetadataAdmin")
INDEX_READERS = (*INDEX_OPERATORS, "Auditor", "DataSteward", "Analyst")
WORKLIST_READERS = (
    "PlatformAdmin",
    "DataSteward",
    "MetadataAdmin",
    "Reviewer",
    "Operations",
    "Analyst",
    "Auditor",
)


class VectorIndexStatusRead(ApiModel):
    organization_id: UUID
    usable: bool
    reason: str
    entries: int
    signature: str
    built_at: datetime | None
    age_minutes: float | None
    backend: str
    max_age_minutes: int


class VectorIndexRebuildRead(ApiModel):
    organization_id: UUID
    signature: str
    backend: str
    considered: int
    embedded: int
    skipped_unchanged: int


@router.get(
    "/organizations/{organization_id}/retrieval/vector-index",
    response_model=VectorIndexStatusRead,
)
async def get_vector_index_status(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*INDEX_READERS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> VectorIndexStatusRead:
    """Whether retrieval is serving from the persisted index, and if not, why.

    The reason is named rather than collapsed into a bare boolean because
    "why did my search fall back to the slow path" is the first question an
    operator asks, and `EMPTY`, `STALE` and `STALE_CATALOG_MOVED` have three
    different answers.
    """
    enforce_organization(context, organization_id)
    freshness = await index_freshness(session, organization_id, settings=settings)
    return VectorIndexStatusRead(
        organization_id=organization_id,
        usable=freshness.usable,
        reason=freshness.reason,
        entries=freshness.entries,
        signature=freshness.signature,
        built_at=freshness.built_at,
        age_minutes=freshness.age_minutes,
        backend=settings.vector_index_backend,
        max_age_minutes=settings.vector_index_max_age_minutes,
    )


@router.post(
    "/organizations/{organization_id}/retrieval/vector-index/rebuild",
    response_model=VectorIndexRebuildRead,
)
async def rebuild_vector_index_endpoint(
    organization_id: UUID,
    datasource_id: UUID | None = Query(default=None),
    context: SecurityContext = Depends(require_roles(*INDEX_OPERATORS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> VectorIndexRebuildRead:
    """Embed this organization's metadata into the persisted index.

    Idempotent: an object whose text has not changed is not re-embedded, so
    running this on a schedule over an unchanged estate costs one query and
    no model calls. Refuses (409) rather than half-building when no embedding
    provider is configured or the estate exceeds the rebuild bound -- a
    silently partial index would let retrieval serve confident answers from a
    fraction of the catalog.
    """
    enforce_organization(context, organization_id)
    if await session.get(Organization, organization_id) is None:
        raise HTTPException(status_code=404, detail="organization not found")
    try:
        result = await rebuild_vector_index(
            session, organization_id, settings=settings, datasource_id=datasource_id
        )
    except (EmbeddingUnavailable, VectorIndexUnavailable) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return VectorIndexRebuildRead(
        organization_id=organization_id,
        signature=result.signature,
        backend=result.backend,
        considered=result.considered,
        embedded=result.embedded,
        skipped_unchanged=result.skipped_unchanged,
    )
