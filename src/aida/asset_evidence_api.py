"""UX-13: `GET /v1/metadata/tables/{table_id}/evidence`.
UX-7: the same endpoint's export sibling, `.../evidence/export`.

See `aida.asset_evidence` for how the response is composed.

UX-7 -- permalinks and export
------------------------------
The `GET .../evidence` route above is already the permalink: a durable,
server-resolvable URL with no request body and no session-only state -- two
callers hitting the same URL with their own independently-authorized
credentials get the same (re-derived, not cached) evidence back. What UX-13
did not yet have was a downloadable artifact of that same composition, so
this module adds one delivery mode alongside it:

``GET /v1/metadata/tables/{table_id}/evidence/export``
    Same authorization (`_authorize_table_read` below, shared by both
    routes -- not a separate or weaker check), same `compose_asset_evidence`
    call, serialized verbatim as the response body. Follows
    `context_compiler_api.download_context_compilation`'s (EE.9) established
    attachment idiom: a `Content-Disposition: attachment` header naming the
    file, and an `X-Artifact-SHA256` header over the exact bytes returned, so
    a steward or auditor can verify what they attached to a ticket is what
    the platform actually composed.

    Format: JSON, not PDF. `pyproject.toml` pins no PDF-generation library
    (no reportlab/weasyprint/fpdf2/xhtml2pdf) and this row's hard constraint
    is not to add a new dependency when an existing, dependency-free format
    is honest -- JSON is: it is `AssetEvidenceRead.model_dump_json()`
    verbatim (the same wire shape `GET .../evidence` already returns), so
    the export can never drift from the live composition by construction,
    which a hand-formatted Markdown/PDF rendering step would risk.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
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


async def _authorize_table_read(
    table_id: UUID,
    context: SecurityContext,
    session: AsyncSession,
    settings: Settings,
) -> MetadataTable:
    """Load `table_id` and authorize a read of it -- the one gate both the
    live evidence route and its `/export` sibling run through, so an export
    can never become a way to see evidence its caller could not otherwise
    read. Raises the same `HTTPException`s (404 / 403) either route would.
    """
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
    return table


@router.get("/metadata/tables/{table_id}/evidence", response_model=AssetEvidenceRead)
async def get_asset_evidence(
    table_id: UUID,
    context: SecurityContext = Depends(require_roles(*_EVIDENCE_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AssetEvidenceRead:
    table = await _authorize_table_read(table_id, context, session, settings)
    return await compose_asset_evidence(session, table)


@router.get("/metadata/tables/{table_id}/evidence/export")
async def export_asset_evidence(
    table_id: UUID,
    context: SecurityContext = Depends(require_roles(*_EVIDENCE_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """UX-7: a downloadable artifact of the same evidence `GET .../evidence`
    would return -- same `require_roles` population, same
    `_authorize_table_read` gate (not a separate or weaker check), and
    `compose_asset_evidence` reused verbatim rather than re-derived, so this
    can never disagree with the live pane. See the module docstring for why
    JSON.
    """
    table = await _authorize_table_read(table_id, context, session, settings)
    evidence = await compose_asset_evidence(session, table)
    content = evidence.model_dump_json(indent=2)
    artifact_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="table-{table.id}-evidence.json"',
            "X-Artifact-SHA256": artifact_hash,
        },
    )
