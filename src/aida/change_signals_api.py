"""R11-FP15: read a datasource's change signals -- value-free, newest first.

A signal says no more than the catalog row it points at (an id, a kind of change), so it is read
under the catalog's own rule: the caller's organization owns the datasource, and the
authorization gate allows reading its metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import gate_read
from aida.change_signal_models import MetadataChangeSignal
from aida.config import Settings, get_settings
from aida.db import get_session
from aida.resource_scope import load_datasource_in_scope
from aida.schemas import ApiModel
from aida.security import SecurityContext, require_roles

router = APIRouter(prefix="/v1", tags=["change-signals"])

#: The catalog's read roles (`atlas.modules.catalog.router.CATALOG_BULK_ACTION_READ_ROLES`).
CHANGE_SIGNAL_READ_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "DataSteward",
    "Analyst",
    "Viewer",
)


class ChangeSignalRead(ApiModel):
    id: UUID
    analysis_run_id: UUID | None
    subject_kind: str
    subject_id: UUID
    signal_type: str
    change_class: str | None
    related_subject_id: UUID | None
    status: str
    detected_at: datetime
    processed_at: datetime | None


@router.get(
    "/datasources/{datasource_id}/change-signals", response_model=list[ChangeSignalRead]
)
async def list_change_signals(
    datasource_id: UUID,
    status: Literal["PENDING", "PROCESSED"] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    context: SecurityContext = Depends(require_roles(*CHANGE_SIGNAL_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> list[ChangeSignalRead]:
    datasource = await load_datasource_in_scope(session, context, datasource_id)
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="datasource",
        resource_id=str(datasource.id),
        datasource_id=datasource.id,
    )
    statement = select(MetadataChangeSignal).where(
        MetadataChangeSignal.datasource_id == datasource.id,
        MetadataChangeSignal.organization_id == datasource.organization_id,
    )
    if status is not None:
        statement = statement.where(MetadataChangeSignal.status == status)
    rows = await session.scalars(
        statement.order_by(MetadataChangeSignal.detected_at.desc(), MetadataChangeSignal.id)
        .limit(limit)
    )
    return [ChangeSignalRead.model_validate(row) for row in rows]
