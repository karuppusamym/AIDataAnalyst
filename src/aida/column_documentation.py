"""Column-level business description of record: the single write path.

`MetadataColumn.source_description` is the source system's own comment,
re-derived (and overwritten) by every rediscovery pass. This module owns the
*authored* counterpart -- content a steward approved, which rediscovery must
never touch -- as the column-level mirror of the table-level
`AssetDocumentation`/`AssetDocumentationVersion` pair.

Until this module existed there was no such store at all: `DocumentClaim`'s
docstring records that an APPROVED column `DESCRIBES` claim's terminal state
was the claim row itself, so a steward could approve a column description and
no reader anywhere could resolve it afterwards. `publish_column_description`
below is what an approval now calls, and it is the only write path -- there is
deliberately no ungoverned direct-write endpoint, because on this platform
authored content reaches an APPROVED version only through
`semantic_api.decide_governance_review`'s maker-checker guard.

Append-only, following `business_annotation_versions.write_annotation_version`
exactly: the prior `APPROVED` row's `status` moves to `SUPERSEDED` in the same
transaction that inserts the new `APPROVED` row and is never edited for
content, so an `AgentRun` grounded on a column description stays replayable
against the content it actually saw.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.selectable import Subquery

from aida.models import ColumnDocumentation, ColumnDocumentationVersion


async def publish_column_description(
    session: AsyncSession,
    *,
    organization_id: UUID,
    table_id: UUID,
    column_id: UUID,
    description: str,
    created_by: str,
    approved_by: str,
    approved_at: datetime,
    source_claim_id: UUID | None = None,
) -> ColumnDocumentationVersion:
    """Publish `description` as the column's new current version.

    Creates the parent `ColumnDocumentation` on first publish -- the same
    get-or-create shape `asset_description_service.apply_asset_description_draft`
    uses for `AssetDocumentation`, and safe under the parent's
    `uq_column_documentation_column_id` unique constraint because every caller
    reaches here inside one already-serialized review-decision transaction.
    """
    documentation = await session.scalar(
        select(ColumnDocumentation).where(ColumnDocumentation.column_id == column_id)
    )
    if documentation is None:
        documentation = ColumnDocumentation(
            organization_id=organization_id,
            table_id=table_id,
            column_id=column_id,
        )
        session.add(documentation)
        await session.flush()
    latest_version_number = await session.scalar(
        select(func.max(ColumnDocumentationVersion.version)).where(
            ColumnDocumentationVersion.documentation_id == documentation.id
        )
    )
    await session.execute(
        update(ColumnDocumentationVersion)
        .where(
            ColumnDocumentationVersion.documentation_id == documentation.id,
            ColumnDocumentationVersion.status == "APPROVED",
        )
        .values(status="SUPERSEDED", updated_at=approved_at)
    )
    version = ColumnDocumentationVersion(
        organization_id=organization_id,
        documentation_id=documentation.id,
        version=(latest_version_number or 0) + 1,
        status="APPROVED",
        description=description,
        source_claim_id=source_claim_id,
        created_by=created_by,
        approved_by=approved_by,
        approved_at=approved_at,
    )
    session.add(version)
    await session.flush()
    return version


def current_version_ranked_subquery() -> Subquery:
    """Every `APPROVED` `ColumnDocumentationVersion`, ranked latest-first per
    `documentation_id` -- the same window-function shape
    `business_annotation_versions.current_version_ranked_subquery` and
    `catalog_read_model._latest_approved_documentation` already use, so the
    three resolve "current version" identically.
    """
    return (
        select(
            ColumnDocumentationVersion,
            func.row_number()
            .over(
                partition_by=ColumnDocumentationVersion.documentation_id,
                order_by=ColumnDocumentationVersion.version.desc(),
            )
            .label("rn"),
        )
        .where(ColumnDocumentationVersion.status == "APPROVED")
        .subquery()
    )


def current_version_alias() -> tuple[type[ColumnDocumentationVersion], Subquery]:
    """`(alias, ranked)` pair for joining the current version onto its parent:

    alias, ranked = current_version_alias()
    select(ColumnDocumentation, alias)
        .join(alias, ranked.c.documentation_id == ColumnDocumentation.id)
        .where(ranked.c.rn == 1, ...)
    """
    ranked = current_version_ranked_subquery()
    alias = aliased(ColumnDocumentationVersion, ranked)
    return alias, ranked


async def current_descriptions_by_column_id(
    session: AsyncSession, column_ids: list[UUID]
) -> dict[UUID, ColumnDocumentationVersion]:
    """The current `APPROVED` version for each of `column_ids`, keyed by
    `column_id`. Missing keys mean no approved description exists yet -- a
    caller renders the source comment (or nothing) in that case rather than
    treating absence as an error.

    One query for the whole batch: the column pane and the workbook export
    both resolve a page of columns at once, and a per-column round trip would
    put this on the wrong side of the catalog's scale budget (CT-2).
    """
    if not column_ids:
        return {}
    alias, ranked = current_version_alias()
    rows = (
        await session.execute(
            select(alias, ColumnDocumentation.column_id)
            .join(ColumnDocumentation, ColumnDocumentation.id == ranked.c.documentation_id)
            .where(ranked.c.rn == 1, ColumnDocumentation.column_id.in_(column_ids))
        )
    ).all()
    return {column_id: version for version, column_id in rows}


async def current_descriptions_for_table(
    session: AsyncSession, table_id: UUID
) -> dict[UUID, ColumnDocumentationVersion]:
    """Every current column description under one table, keyed by `column_id`.

    Filters on the parent's denormalized `table_id` rather than joining back
    through `metadata_column`, which is why that column exists.
    """
    alias, ranked = current_version_alias()
    rows = (
        await session.execute(
            select(alias, ColumnDocumentation.column_id)
            .join(ColumnDocumentation, ColumnDocumentation.id == ranked.c.documentation_id)
            .where(ranked.c.rn == 1, ColumnDocumentation.table_id == table_id)
        )
    ).all()
    return {column_id: version for version, column_id in rows}


async def resolve_column_description_version(
    session: AsyncSession, version_id: UUID
) -> ColumnDocumentationVersion | None:
    """Resolve one version by id regardless of `status` -- a `SUPERSEDED` row
    is still the exact content some past run or citation referenced, and must
    stay resolvable.
    """
    return await session.get(ColumnDocumentationVersion, version_id)
