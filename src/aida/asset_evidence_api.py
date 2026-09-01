"""UX-13: `GET /v1/metadata/tables/{table_id}/evidence`.

See `aida.asset_evidence` for how the response is composed.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_evidence import compose_asset_evidence
from aida.authorization_gate import AuthorizationDenied, gate
from aida.config import Settings, get_settings
from aida.db import get_session
from aida.models import DataSource, MetadataTable
from aida.schemas import AssetEvidenceRead
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["asset-evidence"])

# Same read population as `glossary_api.GLOSSARY_READ_ROLES`: evidence
# composes catalog, quality and lineage facts already readable individually
# through those modules' own endpoints, so the read population matches
# rather than introduces a narrower one.
_EVIDENCE_READ_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "SemanticAdmin",
    "DataSteward",
    "Reviewer",
    "Analyst",
    "Viewer",
    "Auditor",
)


@router.get("/metadata/tables/{table_id}/evidence", response_model=AssetEvidenceRead)
async def get_asset_evidence(
    table_id: UUID,
    context: SecurityContext = Depends(require_roles(*_EVIDENCE_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AssetEvidenceRead:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="metadata table not found")
    enforce_organization(context, table.organization_id)

    datasource = await session.get(DataSource, table.datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    try:
        # Same gate `list_catalog_rows` (UX-12) and `list_tables` use to
        # authorize a catalog read of this table.
        await gate(
            session,
            context,
            settings=settings,
            action="READ_METADATA",
            resource_type="datasource",
            resource_id=str(datasource.id),
            datasource_id=datasource.id,
        )
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403, detail=exc.reason_code) from exc

    return await compose_asset_evidence(session, table)
