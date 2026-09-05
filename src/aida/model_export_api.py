"""`GET /v1/datasources/{datasource_id}/model/export.xlsx` -- the whole model
for one datasource as a downloadable workbook.

Follows the attachment idiom `asset_evidence_api.export_asset_evidence` and
`context_compiler_api.download_context_compilation` already established: a
`Content-Disposition: attachment` header naming the file, and an
`X-Artifact-SHA256` header over the exact bytes returned so a steward can
verify that what they attached to a ticket is what the platform composed.

The hash identifies *these bytes*, not the model's state: the README sheet
records when and by whom the snapshot was taken, so two downloads of an
unchanged model hash differently. That is the right trade -- an exported
artifact that cannot say when it was taken is worth less than one whose hash
happens to be comparable across downloads, and the hash's actual job (proving
an attached file is the one the platform produced) is unaffected. What
`aida.xlsx`'s determinism buys is that the hash varies with the *content*
alone: no zip timestamps or part ordering leak into it, so two exports that
differ in their hash differ in something a reader can see.

Format is .xlsx rather than CSV because the deliverable is several related
sheets (tables, columns, relationships) plus a README that explains which
cells are editable -- a set of CSVs would lose both the relationship between
the sheets and the instructions, and the instructions are what keep a steward
from editing an id column and breaking their own re-upload.

Authorization is the datasource read gate, run once for the whole workbook.
This is the honest granularity: the export is datasource-scoped by
construction, and every sheet in it is derived from objects under that one
datasource.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import AuthorizationDenied, gate
from aida.config import Settings, get_settings
from aida.db import get_session
from aida.model_export import compose_model_workbook
from aida.models import DataSource
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.xlsx import write_workbook

router = APIRouter(prefix="/v1", tags=["model-export"])

_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The read population that can already see these objects individually through
# the catalog, semantics and relationship endpoints. An export is a delivery
# mode for facts a caller can read anyway, not a new disclosure, so it takes
# the same population rather than a narrower one it would then have to justify.
_EXPORT_READ_ROLES = (
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


def _safe_filename_stem(name: str) -> str:
    """A datasource name reduced to characters safe in a filename.

    A `Content-Disposition` filename carrying a quote, a newline or a path
    separator is a header-injection and path-traversal question nobody should
    have to think about at the point of download; the id in the name keeps the
    file unambiguous even when two datasources sanitize to the same stem.
    """
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return (stem or "datasource")[:60]


@router.get("/datasources/{datasource_id}/model/export.xlsx")
async def export_datasource_model(
    datasource_id: UUID,
    context: SecurityContext = Depends(require_roles(*_EXPORT_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        # The same gate `list_catalog_rows` and `list_tables` use to authorize
        # a catalog read of this datasource.
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

    generated_at = datetime.now(UTC)
    composition = await compose_model_workbook(
        session,
        datasource=datasource,
        generated_at=generated_at,
        generated_by=context.principal_id,
    )
    content = write_workbook(composition.sheets)
    filename = (
        f"{_safe_filename_stem(datasource.name)}-{datasource.id}-model-"
        f"{generated_at.date().isoformat()}.xlsx"
    )
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Artifact-SHA256": hashlib.sha256(content).hexdigest(),
        # Surfaced as headers as well as in the README sheet: a scripted
        # caller reads headers, a person reads the sheet, and a truncated
        # export must not look complete to either.
        "X-Export-Truncated": "true" if composition.any_truncated else "false",
        "X-Export-Row-Counts": ",".join(
            f"{sheet}={count}" for sheet, count in composition.row_counts.items()
        ),
    }
    return Response(content=content, media_type=_XLSX_MEDIA_TYPE, headers=headers)
