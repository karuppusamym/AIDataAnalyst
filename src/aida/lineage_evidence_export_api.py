"""AT-20: `GET /v1/datasources/{datasource_id}/unified-lineage/impact/{node_id}/export`.

The export sibling of `unified_lineage_api.get_unified_lineage_impact` --
same route shape (`datasource_id`/`node_id` path params, `depth`/`node_limit`
query params), same `UNIFIED_LINEAGE_READER_ROLES` population, and the exact
same `_load_datasource` gate (`enforce_organization`) the live graph and
impact routes already run through, so an export can never become a way to
see lineage its caller could not otherwise read through the live endpoints.
See `aida.lineage_evidence_export` for how the artifact itself is composed
and what "signed" honestly means here (a SHA-256 content hash, not a
cryptographic signature).

Format: JSON, following `context_compiler_api.download_context_compilation`
(EE.9) and UX-7's `asset_evidence_api.export_asset_evidence`'s established
attachment idiom -- `Content-Disposition: attachment` naming the file, and an
`X-Artifact-SHA256` header over the exact bytes returned. `pyproject.toml`
pins no PDF or diagram-rendering library (no reportlab/weasyprint/fpdf2/
xhtml2pdf/graphviz/pydot), and this row's hard constraints forbid adding one
-- JSON is the honest, dependency-free choice already established twice on
this branch today, not a new pattern invented for this row.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import get_session
from aida.lineage_evidence_export import compose_lineage_export_artifact
from aida.security import SecurityContext, require_roles
from aida.unified_lineage_api import (
    UNIFIED_LINEAGE_READER_ROLES,
    LineageNodeNotFoundError,
    _load_datasource,
)

router = APIRouter(prefix="/v1", tags=["unified-lineage"])


def _sanitize_for_filename(value: str) -> str:
    """Node ids can be a bare UUID, or a synthetic `dbt:<uuid>` /
    `openlineage:<namespace>:<name>` id whose namespace/name came from an
    external system and is not guaranteed filename-safe. Collapse anything
    that is not alphanumeric/`-`/`_`/`.` to `_` so the `Content-Disposition`
    quoted-string can never be broken out of by a crafted dataset name.
    """
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in value)


@router.get("/datasources/{datasource_id}/unified-lineage/impact/{node_id}/export")
async def export_unified_lineage_impact(
    datasource_id: UUID,
    node_id: str,
    depth: int = Query(default=5, ge=1, le=8),
    node_limit: int = Query(default=200, ge=5, le=2_000),
    context: SecurityContext = Depends(require_roles(*UNIFIED_LINEAGE_READER_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """A downloadable, hash-verifiable artifact of the same point-in-time
    lineage `GET .../unified-lineage/impact/{node_id}` would return for the
    chosen asset and depth -- the AT-20 "lineage evidence export as a signed
    artifact" deliverable. Same authorization as that live route (not a
    separate or weaker check), and `compose_lineage_export_artifact` reused
    for both, so the export can never disagree with what the live pane would
    show for the same asset/depth at the same instant.
    """
    datasource = await _load_datasource(session, context, datasource_id)
    try:
        artifact = await compose_lineage_export_artifact(
            session,
            datasource,
            node_id,
            depth=depth,
            node_limit=node_limit,
            settings=settings,
        )
    except LineageNodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    content = json.dumps(artifact, indent=2, sort_keys=True, default=str)
    artifact_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    filename = (
        f"lineage-{datasource_id}-{_sanitize_for_filename(node_id)}-depth{depth}-export.json"
    )
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Artifact-SHA256": artifact_hash,
        },
    )
