"""RL-2: canonical table resolution -- API layer.

This module owns the database fetch, persistence, de-duplication, and
maker-checker review flow around the pure detector in
``aida.canonical_table_resolution``. It follows the same shape as
``aida.table_family_api`` (RL-1) and ``aida.composite_key_api`` (PR-1):
``discover`` -> ``list`` -> ``decision``, with the same maker-checker rules
(a group starts PENDING, its maker cannot also review it, and an
already-decided group cannot be decided again).

Kept as its own router/module (rather than added to ``api.py`` /
``intelligence_api.py`` / ``semantic_api.py``) and wired into the app with a
single ``app.include_router(canonical_table_router)`` line in ``main.py``.

**Trigger scope**: unlike RL-1 (one schema at a time) or PR-1 (one table at a
time), canonical-duplicate groups can legitimately span schemas, catalogs,
and datasources within a single organization -- so `discover` is
organization-scoped: ``POST /v1/organizations/{organization_id}/canonical-
table-groups/discover``. The request body's optional ``datasource_ids``
narrows the scan to specific datasource(s) (e.g. a known production +
reporting-mirror pair) without needing a second endpoint shape for that
narrower case. This deliberately does *not* reuse
``intelligence_api.discover_cross_source_relationship_candidates``'s
data_domain / cross-boundary-grant machinery: that gate protects surfacing
an actual data *relationship* across an organizational boundary, whereas a
canonical-duplicate grouping only ever names tables the requesting
organization already owns and that the calling principal already has
organization-level read access to via ``require_roles`` +
``enforce_organization`` -- there is no second party's boundary being
crossed here.
"""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.canonical_table_resolution import (
    CanonicalGroupDraft,
    TableInput,
    detect_canonical_table_groups,
)
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    CanonicalTableGroup,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    TableProfile,
)
from aida.schemas import (
    CanonicalTableGroupDecision,
    CanonicalTableGroupDiscoveryRequest,
    CanonicalTableGroupRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["canonical-table-resolution"])

# A single discover call fetches at most this many ACTIVE tables from one
# organization -- the pure detector (`canonical_table_resolution`) has its
# own independent bound (`MAX_TABLES_EVALUATED`) as defense-in-depth, but
# this is the primary cap: an organization with more tables than this is
# triaged over repeated calls (optionally narrowed by `datasource_ids`)
# rather than scanned all at once, matching every other discovery path in
# this platform (see `intelligence_api.discover_cross_source_relationship_
# candidates`'s docstring on the same ADR-0017 SS8 posture).
CANONICAL_TABLE_SCAN_MAX_TABLES = 5_000
CANONICAL_TABLE_SCAN_MAX_COLUMNS = 100_000
# TableProfile rows are fetched to resolve each table's most recent
# row_count_estimate (used only by the detector's default-canonical pick,
# never to gate grouping itself) -- capped independently since a table can
# accumulate many historical profile runs.
CANONICAL_TABLE_SCAN_MAX_PROFILES = 20_000

_DISCOVER_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin")
_LIST_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "Auditor", "Viewer")
_DECISION_ROLES = ("PlatformAdmin", "MetadataReviewer", "DataSteward")


def _existing_member_keys(rows: list[CanonicalTableGroup]) -> set[frozenset[UUID]]:
    """Identity of already-persisted, non-REJECTED groups.

    Mirrors `CanonicalGroupDraft.member_key()` so a re-run of discovery can
    skip anything already recorded (PENDING or APPROVED) for the same member
    set, without spamming duplicates. A REJECTED group does *not* block
    re-detection -- new evidence (or simply re-running after a false
    rejection) should be able to raise it again, same as
    `table_family_api._existing_member_keys`.
    """
    return {
        frozenset(UUID(member) for member in row.member_table_ids)
        for row in rows
        if row.status != "REJECTED"
    }


@router.post(
    "/organizations/{organization_id}/canonical-table-groups/discover",
    response_model=Page,
    status_code=status.HTTP_202_ACCEPTED,
)
async def discover_canonical_table_groups(
    organization_id: UUID,
    body: CanonicalTableGroupDiscoveryRequest,
    context: SecurityContext = Depends(require_roles(*_DISCOVER_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    organization = await session.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")

    table_filters = [
        MetadataTable.organization_id == organization.id,
        MetadataTable.status == "ACTIVE",
    ]
    if body.datasource_ids:
        table_filters.append(MetadataTable.datasource_id.in_(body.datasource_ids))

    tables = (
        await session.scalars(
            select(MetadataTable)
            .where(*table_filters)
            .order_by(MetadataTable.name)
            .limit(CANONICAL_TABLE_SCAN_MAX_TABLES)
        )
    ).all()
    if len(tables) < 2:
        return Page(items=[], limit=body.max_candidates, offset=0, total=0)

    table_ids = [table.id for table in tables]
    schema_ids = {table.schema_id for table in tables}
    schemas = (
        await session.scalars(select(MetadataSchema).where(MetadataSchema.id.in_(schema_ids)))
    ).all()
    schemas_by_id = {schema.id: schema for schema in schemas}
    catalog_ids = {schema.catalog_id for schema in schemas}
    catalogs = (
        await session.scalars(select(MetadataCatalog).where(MetadataCatalog.id.in_(catalog_ids)))
    ).all()
    catalogs_by_id = {catalog.id: catalog for catalog in catalogs}

    columns = (
        await session.scalars(
            select(MetadataColumn)
            .where(
                MetadataColumn.table_id.in_(table_ids),
                MetadataColumn.status == "ACTIVE",
            )
            .order_by(MetadataColumn.table_id, MetadataColumn.ordinal_position)
            .limit(CANONICAL_TABLE_SCAN_MAX_COLUMNS)
        )
    ).all()
    columns_by_table: dict[UUID, list[MetadataColumn]] = {}
    for column in columns:
        columns_by_table.setdefault(column.table_id, []).append(column)

    profiles = (
        await session.scalars(
            select(TableProfile)
            .where(
                TableProfile.table_id.in_(table_ids),
                TableProfile.status == "COMPLETED",
            )
            .order_by(TableProfile.table_id, TableProfile.created_at.desc())
            .limit(CANONICAL_TABLE_SCAN_MAX_PROFILES)
        )
    ).all()
    latest_row_count_by_table: dict[UUID, int | None] = {}
    for profile in profiles:
        # Ordered by created_at desc per table above -- first hit per
        # table_id is the latest COMPLETED profile.
        latest_row_count_by_table.setdefault(profile.table_id, profile.row_count_estimate)

    detector_input: list[TableInput] = []
    for table in tables:
        schema = schemas_by_id.get(table.schema_id)
        catalog = catalogs_by_id.get(schema.catalog_id) if schema else None
        detector_input.append(
            (
                table.id,
                table.name,
                schema.name if schema else "",
                catalog.name if catalog else "",
                table.datasource_id,
                table.fingerprint,
                latest_row_count_by_table.get(table.id),
                [
                    (column.name, column.physical_type)
                    for column in columns_by_table.get(table.id, [])
                ],
            )
        )
    drafts: list[CanonicalGroupDraft] = detect_canonical_table_groups(detector_input)

    existing_rows = (
        await session.scalars(
            select(CanonicalTableGroup).where(
                CanonicalTableGroup.organization_id == organization.id
            )
        )
    ).all()
    existing_keys = _existing_member_keys(list(existing_rows))

    created: list[CanonicalTableGroup] = []
    for draft in drafts:
        if draft.member_key() in existing_keys:
            continue
        group = CanonicalTableGroup(
            organization_id=organization.id,
            member_table_ids=[str(member_id) for member_id in draft.member_table_ids],
            canonical_table_id=None,
            detection_rule=draft.detection_rule,
            confidence=draft.confidence,
            evidence=draft.evidence,
            status="PENDING",
            created_by=context.principal_id,
        )
        session.add(group)
        created.append(group)
        existing_keys.add(draft.member_key())
        if len(created) >= body.max_candidates:
            break

    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=organization.id),
        action="canonical_table_groups.discover",
        resource_type="organization",
        resource_id=str(organization.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "created_groups": len(created),
            "tables_scanned": len(tables),
            "columns_scanned": len(columns),
        },
    )
    await session.commit()
    return Page(
        items=[CanonicalTableGroupRead.model_validate(item) for item in created],
        limit=body.max_candidates,
        offset=0,
        total=len(created),
    )


@router.get("/organizations/{organization_id}/canonical-table-groups", response_model=Page)
async def list_canonical_table_groups(
    organization_id: UUID,
    candidate_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_LIST_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    organization = await session.get(Organization, organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail="organization not found")

    filters = [CanonicalTableGroup.organization_id == organization.id]
    if candidate_status:
        filters.append(CanonicalTableGroup.status == candidate_status.upper())
    total = await session.scalar(
        select(func.count()).select_from(CanonicalTableGroup).where(*filters)
    )
    rows = (
        await session.scalars(
            select(CanonicalTableGroup)
            .where(*filters)
            .order_by(CanonicalTableGroup.confidence.desc(), CanonicalTableGroup.created_at)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[CanonicalTableGroupRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.post(
    "/canonical-table-groups/{group_id}/decision",
    response_model=CanonicalTableGroupRead,
)
async def decide_canonical_table_group(
    group_id: UUID,
    body: CanonicalTableGroupDecision,
    context: SecurityContext = Depends(require_roles(*_DECISION_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CanonicalTableGroup:
    group = await session.get(CanonicalTableGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="canonical table group not found")
    enforce_organization(context, group.organization_id)
    if group.created_by == context.principal_id:
        raise HTTPException(status_code=409, detail="maker cannot review their own group")
    if group.status != "PENDING":
        raise HTTPException(status_code=409, detail="canonical table group is already decided")

    if body.decision == "APPROVE":
        member_ids = {UUID(member) for member in group.member_table_ids}
        if body.canonical_table_id not in member_ids:
            raise HTTPException(
                status_code=422,
                detail="canonical_table_id must be one of the group's member_table_ids",
            )
        group.canonical_table_id = body.canonical_table_id

    group.status = "APPROVED" if body.decision == "APPROVE" else "REJECTED"
    group.reviewed_by = context.principal_id
    group.review_reason = body.reason
    group.reviewed_at = datetime.now(UTC)
    record_audit(
        session,
        replace(context, organization_id=group.organization_id),
        action="canonical_table_group.decide",
        resource_type="canonical_table_group",
        resource_id=str(group.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"decision": body.decision},
    )
    record_outbox(
        session,
        organization_id=group.organization_id,
        aggregate_type="canonical_table_group",
        aggregate_id=str(group.id),
        event_type="canonical_table_group.decided.v1",
        payload={
            "group_id": str(group.id),
            "status": group.status,
            "canonical_table_id": (
                str(group.canonical_table_id) if group.canonical_table_id else None
            ),
        },
    )
    await session.commit()
    return group
