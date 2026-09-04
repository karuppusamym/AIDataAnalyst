"""RT-1 / NT-1: operator control over the persisted vector index and governance notifications.

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

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import get_session
from aida.embedding_provider import EmbeddingUnavailable
from aida.governance_notifications import notify_governance_event
from aida.models import NotificationEventRecord, Organization
from aida.schemas import ApiModel, Page
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


# ---------------------------------------------------------------------------
# NT-1: governance notifications
# ---------------------------------------------------------------------------


class NotificationRecordRead(ApiModel):
    id: UUID
    channel: str
    status: str
    dedup_key: str
    sent_at: datetime | None
    created_at: datetime


class NotificationTestResult(ApiModel):
    organization_id: UUID
    enabled: bool
    outcomes: dict[str, str]


@router.get("/organizations/{organization_id}/notifications/governance", response_model=Page)
async def list_governance_notifications(
    organization_id: UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*INDEX_READERS)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """The delivery ledger.

    Every attempt is a row, including the ones that were skipped, so an
    operator can tell "not configured" from "delivered" without reading logs.
    """
    enforce_organization(context, organization_id)
    base = select(NotificationEventRecord).where(
        NotificationEventRecord.organization_id == organization_id
    )
    counter = (
        select(func.count())
        .select_from(NotificationEventRecord)
        .where(NotificationEventRecord.organization_id == organization_id)
    )
    if status_filter:
        base = base.where(NotificationEventRecord.status == status_filter)
        counter = counter.where(NotificationEventRecord.status == status_filter)
    total = int(await session.scalar(counter) or 0)
    rows = (
        await session.scalars(
            base.order_by(NotificationEventRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[
            NotificationRecordRead(
                id=row.id,
                channel=row.channel,
                status=row.status,
                dedup_key=row.dedup_key,
                sent_at=row.sent_at,
                created_at=row.created_at,
            ).model_dump(mode="json")
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/organizations/{organization_id}/notifications/governance/test",
    response_model=NotificationTestResult,
)
async def send_test_governance_notification(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*INDEX_OPERATORS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> NotificationTestResult:
    """Send a synthetic REVIEW_REQUESTED to every configured channel.

    The only way to find out that a webhook URL is wrong before a real
    governance event needs it.
    """
    enforce_organization(context, organization_id)
    outcomes = await notify_governance_event(
        session,
        organization_id,
        "REVIEW_REQUESTED",
        {
            "object_type": "TEST",
            "object_id": str(organization_id),
            "object_name": "connectivity test",
            "principal_id": context.principal_id,
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        settings=settings,
    )
    await session.commit()
    return NotificationTestResult(
        organization_id=organization_id,
        enabled=settings.governance_notifications_enabled,
        outcomes={outcome.channel: outcome.status for outcome in outcomes},
    )
