"""R11-FP05/FP17: the footprint gaps read routes. See `aida.footprint_gaps`.

The summary is per organization; the drill-down is per source and per kind, because that is
the unit someone acts on. Both answer only for a datasource the caller may read metadata of,
through the same gate: the summary leaves a denied source out, and the drill-down answers as
it does for a source that does not exist, so neither route says a source is there.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import AuthorizationDenied, gate
from aida.config import Settings, get_settings
from aida.db import get_session
from aida.footprint_gap_detail import (
    FootprintGapDetailRead,
    UnknownGapKind,
    footprint_gap_objects,
)
from aida.footprint_gaps import FootprintGapsRead, footprint_gaps
from aida.models import DataSource
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


@router.get(
    "/datasources/{datasource_id}/footprint-gaps/{kind}",
    response_model=FootprintGapDetailRead,
)
async def get_footprint_gap_objects(
    datasource_id: UUID,
    kind: str,
    context: SecurityContext = Depends(require_roles(*FOOTPRINT_GAP_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> FootprintGapDetailRead:
    """R11-FP05: the objects behind one source's count of one gap kind.

    A caller who may not read this source's metadata gets the same answer as one asking about a
    source that does not exist, because "you may not read it" and "it is not there" must not be
    distinguishable from outside: the summary route drops such a source silently for the same
    reason. An unrecognised kind is a 404 rather than an empty list, which would read as a
    source with none of that gap.
    """
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        await gate(
            session,
            context,
            settings=settings,
            action="READ_METADATA",
            resource_type="datasource",
            resource_id=str(datasource_id),
            datasource_id=datasource_id,
        )
    except AuthorizationDenied as denied:
        raise HTTPException(status_code=404, detail="datasource not found") from denied
    try:
        return await footprint_gap_objects(
            session,
            organization_id=datasource.organization_id,
            datasource_id=datasource_id,
            kind=kind,
        )
    except UnknownGapKind as unknown:
        raise HTTPException(status_code=404, detail="unknown footprint gap kind") from unknown
