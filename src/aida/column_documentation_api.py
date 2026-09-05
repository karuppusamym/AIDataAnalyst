"""`GET /v1/tables/{table_id}/column-documentation` -- one table's columns with
both descriptions resolved.

Why a second endpoint next to `api.list_columns` rather than a wider
`MetadataColumnRead`: `list_columns` is one of the five CT-2 keyset-paginated
catalog endpoints, whose whole point is a response cost that stays flat at
30M-column scale. Joining an authored-description lookup into it would put a
window function on the hot catalog list path for the benefit of a UI pane that
reads one table at a time. This endpoint serves that pane instead -- plain
offset paging over a single table's columns, which are bounded in the
hundreds -- and leaves `list_columns` exactly as it was.

Read-only by design. There is no sibling POST here: an authored column
description reaches `ColumnDocumentationVersion` only through
`semantic_api.decide_governance_review`'s maker-checker guard (today via an
approved `DocumentClaim`), and adding a direct-write endpoint would be a way
around the one gate that makes the content trustworthy. See
`aida.column_documentation`'s module docstring.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import gate_read
from aida.column_documentation import current_descriptions_for_table
from aida.config import Settings, get_settings
from aida.db import get_session
from aida.models import MetadataColumn, MetadataTable
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["column-documentation"])

# Identical to `api.list_columns`' population, deliberately: this endpoint
# returns the same columns that endpoint does, plus authored descriptions.
# A caller who cannot list a table's columns must not be able to read them
# here, and a narrower or wider population would make one of those two true.
_COLUMN_READ_ROLES = ("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")


class ColumnDocumentationRead(ApiModel):
    """One column, with the source comment and the authored description kept
    as separate fields.

    `source_description` is the source system's own comment on
    `MetadataColumn`, re-derived by every rediscovery pass.
    `business_description` is the current `APPROVED`
    `ColumnDocumentationVersion`. A UI that collapsed them into one field
    would be showing a steward content that rediscovery can overwrite
    alongside content it cannot, with no way to tell which is which.
    """

    column_id: UUID
    table_id: UUID
    name: str
    ordinal_position: int
    physical_type: str
    nullable: bool
    classification: str
    classification_source: str
    source_description: str | None = None
    business_description: str | None = None
    description_version: int | None = None
    description_approved_by: str | None = None
    description_approved_at: datetime | None = None
    source_claim_id: UUID | None = None


@router.get("/tables/{table_id}/column-documentation", response_model=Page)
async def list_column_documentation(
    table_id: UUID,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_COLUMN_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page:
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    # The same gate `api.list_columns` runs, against the same resource, so
    # this cannot become a way to read columns that endpoint would refuse.
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="table",
        resource_id=str(table.id),
        datasource_id=table.datasource_id,
    )

    filters = (
        MetadataColumn.organization_id == table.organization_id,
        MetadataColumn.table_id == table.id,
        MetadataColumn.status == "ACTIVE",
    )
    total = await session.scalar(select(func.count()).select_from(MetadataColumn).where(*filters))
    columns = (
        (
            await session.execute(
                select(MetadataColumn)
                .where(*filters)
                .order_by(MetadataColumn.ordinal_position, MetadataColumn.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    # One lookup for the whole table rather than per page: a table's column
    # count is bounded, and the pane's next page hits a warm result either way.
    descriptions = await current_descriptions_for_table(session, table.id)

    items = []
    for column in columns:
        documented = descriptions.get(column.id)
        items.append(
            ColumnDocumentationRead(
                column_id=column.id,
                table_id=column.table_id,
                name=column.name,
                ordinal_position=column.ordinal_position,
                physical_type=column.physical_type,
                nullable=column.nullable,
                classification=column.classification,
                classification_source=column.classification_source,
                source_description=column.source_description,
                business_description=documented.description if documented else None,
                description_version=documented.version if documented else None,
                description_approved_by=documented.approved_by if documented else None,
                description_approved_at=documented.approved_at if documented else None,
                source_claim_id=documented.source_claim_id if documented else None,
            )
        )
    return Page(items=items, limit=limit, offset=offset, total=total or 0)
