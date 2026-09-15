"""R11-FP05/FP17: the footprint gaps read route. See `aida.footprint_gaps`."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import get_session
from aida.footprint_gaps import FootprintGapsRead, footprint_gaps
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["operations"])

#: The fleet summary's readers, plus the people most gaps route to.
FOOTPRINT_GAP_READ_ROLES = (
    "PlatformAdmin",
    "OrganizationAdmin",
    "Operations",
    "Auditor",
    "MetadataAdmin",
    "DataSteward",
)


@router.get(
    "/organizations/{organization_id}/footprint-gaps", response_model=FootprintGapsRead
)
async def get_footprint_gaps(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*FOOTPRINT_GAP_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FootprintGapsRead:
    enforce_organization(context, organization_id)
    return await footprint_gaps(
        session, context=context, settings=settings, organization_id=organization_id
    )
