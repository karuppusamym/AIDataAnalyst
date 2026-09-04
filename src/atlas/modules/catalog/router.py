"""catalog -- HTTP routes, mounted by the app entrypoint (`aida.main`).

Status: real content, populated 2026-09-03 under tracker ST-07 Commit C
(Phase 5 of `Docs/40-engineering/06-refactor-plan.md`). Endpoints move here
verbatim from `aida.api`; each preserves its path, method, response model,
tag placement (none) and required roles, so `openapi.json` is byte-identical
after the move. Only the source file for each handler changes.

The router deliberately keeps `APIRouter(prefix="/v1")` with NO `tags=`
argument -- the `aida.api` router it inherits from also carries no tags,
and adding a "catalog" tag here would give the moved endpoints an OpenAPI
tag they didn't have before. Grouping in Swagger UI stays exactly as it
was.

Import-linter: this module is in the `catalog module privacy` contract's
`protected_modules` list; it can be imported by `atlas.modules.catalog.api`
(and by the app-assembly file `aida.main`, which needs to `include_router`
it). New sibling modules that need to reach handlers from outside should
call them via HTTP or via a service function, not via a router import.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dataclasses import replace
from datetime import UTC, datetime

from fastapi import status

from aida.asset_certification import asset_certification_is_active, current_asset_certification
from aida.certification_evidence import compute_certification_evidence
from aida.authorization_gate import AuthorizationDenied, gate, gate_read
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.models import (
    AssetCertification,
    AssetTag,
    BulkStewardshipOperation,
    CatalogBulkActionRun,
    DataSource,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    OwnershipAssignment,
)
from aida.pagination import InvalidCursor, apply_keyset, decode_cursor, encode_cursor
from aida.schemas import (
    AssetCertificationRead,
    BulkStewardshipOperationCreate,
    BulkStewardshipOperationRead,
    CatalogBulkActionRunRead,
    CatalogBulkCertifyRequest,
    CatalogBulkClassifyRequest,
    CatalogBulkOwnRequest,
    CatalogBulkSelectionFilter,
    CatalogBulkTagRequest,
    CatalogRowRead,
    CertificationDecisionRequest,
    CertificationRevokeRequest,
    CursorPage,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles
from atlas.modules.catalog.service import (
    ALLOWED_CLASSIFICATIONS,
    CATALOG_BULK_ACTION_MAX_ITEMS,
    CATALOG_BULK_FILTER_SCAN_CAP,
    BulkItemResult,
    BulkPlan,
    CatalogBulkItemError,
    apply_certify_item,
    apply_classify_item,
    apply_own_item,
    apply_tag_item,
    compose_catalog_rows,
    dedupe_preserving_order,
    match_columns_by_pattern,
    match_tables_by_filter,
)

router = APIRouter(prefix="/v1")

_CURSOR_DESCRIPTION = (
    "Opaque keyset cursor from a previous page's next_cursor. When supplied, "
    "offset is ignored, no total is computed, and the response cost stays "
    "bounded by limit no matter how many pages precede it."
)

# 2026-09-03 (ST-07 Commit C Slice 2): the two role sets every catalog-owned
# mutating endpoint uses. Duplicated from `aida.api` on purpose during the
# strangle: doing so instead of `from aida.api import ...` keeps the
# dependency direction correct (atlas ← aida, not atlas ↔ aida), and the
# constants are 4-item tuples of role names -- trivial to keep in sync until
# Slice 3 removes the aida-side copy along with the last bulk endpoint that
# uses them there.
CATALOG_BULK_ACTION_WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")
CATALOG_BULK_ACTION_READ_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "DataSteward",
    "Analyst",
    "Viewer",
)


def _asset_certification_read(
    certification: AssetCertification, *, is_active: bool
) -> AssetCertificationRead:
    return AssetCertificationRead(
        id=certification.id,
        organization_id=certification.organization_id,
        table_id=certification.table_id,
        column_id=certification.column_id,
        asset_type=certification.asset_type,
        status=certification.status,
        rationale=certification.rationale,
        certified_by=certification.certified_by,
        expires_at=certification.expires_at,
        is_active=is_active,
        revoked_at=certification.revoked_at,
        revoked_by=certification.revoked_by,
        revocation_reason=certification.revocation_reason,
        # P3-09: pass the raw dict through; pydantic parses it into the
        # `CertificationEvidence` model on the read side. `None` on legacy
        # rows (evidence IS NULL) still projects correctly -- the field on
        # the schema is `CertificationEvidence | None`.
        evidence=certification.evidence,
        created_at=certification.created_at,
        updated_at=certification.updated_at,
    )


@router.get("/organizations/{organization_id}/catalog/rows", response_model=CursorPage)
async def list_catalog_rows(
    organization_id: UUID,
    q: str | None = Query(default=None, min_length=2, max_length=200),
    object_type: str | None = Query(default=None, max_length=30),
    table_status: str = Query(default="ACTIVE", alias="status", max_length=30),
    certification: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, description=_CURSOR_DESCRIPTION),
    context: SecurityContext = Depends(
        require_roles("PlatformAdmin", "MetadataAdmin", "Analyst", "Viewer")
    ),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CursorPage:
    """UX-12: the composed catalog-table-list read model.

    One request returns everything the catalog table UI needs per row
    (description, proposal state, owner, certification, quality, glossary
    terms, row estimate) instead of the five separate calls per table this
    replaces (`Docs/20-modules/21-experience-shell.md`). Read-only: this
    module never accepts or forwards a caller-supplied statement, so ADR-0004
    (the execution gateway is the only path to a source query) is untouched.

    Reuses `list_tables`' own keyset contract (`aida.pagination`) and its own
    permission gate (`aida.authorization_gate.gate`, action `READ_METADATA`,
    resource_type `datasource`) rather than introducing either anew -- the
    gate is called once per DISTINCT datasource on the page (bounded by how
    many datasources this org has, not by page size), and a row from a
    datasource the caller cannot read is dropped from the page rather than
    failing the whole request, which is why a page can come back shorter than
    `limit` for a caller with partial datasource access; `next_cursor` still
    walks every remaining row exactly once.
    """
    enforce_organization(context, organization_id)

    order_columns: tuple[Any, ...] = (MetadataTable.name, MetadataTable.id)
    filters: list[Any] = [MetadataTable.organization_id == organization_id]
    if table_status != "ALL":
        filters.append(MetadataTable.status == table_status)
    if object_type and object_type != "ALL":
        filters.append(MetadataTable.object_type == object_type)
    if q:
        normalized_query = q.strip().lower()
        filters.append(
            or_(
                func.lower(MetadataTable.name).contains(normalized_query),
                func.lower(func.coalesce(MetadataTable.source_description, "")).contains(
                    normalized_query
                ),
            )
        )

    base_query = (
        select(MetadataTable, MetadataSchema.name, DataSource.name)
        .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
        .join(DataSource, DataSource.id == MetadataTable.datasource_id)
        .where(*filters)
    )

    total: int | None = None
    if cursor is not None:
        try:
            raw_values = decode_cursor(cursor, arity=len(order_columns))
            last_values = tuple(
                coerce(value) for coerce, value in zip((str, UUID), raw_values, strict=True)
            )
        except (InvalidCursor, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid cursor") from exc
        statement = apply_keyset(
            base_query.order_by(*order_columns), order_columns, last_values
        ).limit(limit)
    else:
        total = (
            await session.scalar(select(func.count()).select_from(MetadataTable).where(*filters))
            or 0
        )
        statement = base_query.order_by(*order_columns).limit(limit).offset(offset)

    db_rows = (await session.execute(statement)).all()
    page_rows: list[tuple[MetadataTable, str, str]] = [
        (table, schema_name, datasource_name) for table, schema_name, datasource_name in db_rows
    ]
    next_cursor = (
        encode_cursor(page_rows[-1][0].name, str(page_rows[-1][0].id))
        if len(page_rows) == limit
        else None
    )

    # One gate() call per distinct datasource on the page -- not per row --
    # cached so a page dominated by one denied datasource still costs one
    # call for it, not one per row from it.
    datasource_allowed: dict[UUID, bool] = {}
    for table, _, _ in page_rows:
        datasource_id = table.datasource_id
        if datasource_id in datasource_allowed:
            continue
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
        except AuthorizationDenied:
            datasource_allowed[datasource_id] = False
        else:
            datasource_allowed[datasource_id] = True
    permitted_rows = [row for row in page_rows if datasource_allowed.get(row[0].datasource_id)]

    items: list[CatalogRowRead] = await compose_catalog_rows(session, permitted_rows)

    if certification and certification != "ALL":
        # Certification is a derived, not stored, value (the latest
        # AssetCertification row projected through expiry), so it is filtered
        # here rather than in SQL -- the same reason the permission filter
        # above can leave a page short of `limit`: walk further with
        # `next_cursor` to keep collecting matches.
        items = [item for item in items if item.certification == certification]

    return CursorPage(
        items=items,
        limit=limit,
        offset=offset,
        total=total,
        next_cursor=next_cursor,
    )



@router.post(
    "/tables/{table_id}/certification",
    response_model=AssetCertificationRead,
    status_code=status.HTTP_201_CREATED,
)
async def certify_table_asset(
    table_id: UUID,
    body: CertificationDecisionRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> AssetCertificationRead:
    """Module 04's public interface: ``certify_asset(scope, table_id, decision)``.

    Certifies the table itself, or -- module 04's scale note names column as
    the dominant catalog entity -- one specific column of it. Immediate and
    role-gated, the same as CT-1's bulk certify action on this same table:
    a single deliberate certification by an authorized steward, not a batch,
    so there is no maker-checker review to wait on.
    """
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    if table.status != "ACTIVE":
        raise HTTPException(status_code=409, detail=f"table status is {table.status}, not ACTIVE")
    now = datetime.now(UTC)
    if body.expires_at <= now:
        raise HTTPException(status_code=422, detail="certification expiry must be in the future")
    column: MetadataColumn | None = None
    if body.asset_type == "COLUMN":
        column = await session.get(MetadataColumn, body.column_id)
        if (
            column is None
            or column.organization_id != table.organization_id
            or column.table_id != table.id
        ):
            raise HTTPException(status_code=404, detail="column not found on this table")
        if column.status != "ACTIVE":
            raise HTTPException(
                status_code=409, detail=f"column status is {column.status}, not ACTIVE"
            )
    prior_rows = (
        await session.scalars(
            select(AssetCertification).where(
                AssetCertification.table_id == table.id,
                AssetCertification.asset_type == body.asset_type,
                AssetCertification.column_id == (column.id if column else None),
                AssetCertification.status == "ACTIVE",
            )
        )
    ).all()
    for prior in prior_rows:
        prior.status = "SUPERSEDED"
    # P3-09: capture the structured evidence blob (description version /
    # active owners / quality snapshot / glossary term ids) alongside the
    # free-text rationale, so a future revoke-on-evidence-change job has
    # something machine-consumable to key off. Column-level certs pass the
    # parent table id -- the composition is table-scoped either way (the
    # column certification implicitly asserts the same context).
    evidence_blob = await compute_certification_evidence(
        session,
        table.id,
        organization_id=table.organization_id,
        now=now,
        certifier_notes=body.rationale,
    )
    certification = AssetCertification(
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=column.id if column else None,
        asset_type=body.asset_type,
        rationale=body.rationale,
        certified_by=context.principal_id,
        expires_at=body.expires_at,
        evidence=evidence_blob,
    )
    session.add(certification)
    try:
        await session.flush()
    except IntegrityError as exc:
        # P2-08: partial unique index `ix_asset_certification_active_tuple`
        # is the atomicity backstop the app-side supersede loop cannot give.
        # A second concurrent certify call for the same tuple that got past
        # the SUPERSEDE loop (because it read no prior ACTIVE row before this
        # request had committed its own insert) is refused here rather than
        # allowed to leave two ACTIVE rows behind.
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="certification_already_active_on_this_tuple",
        ) from exc
    record_audit(
        session,
        replace(context, organization_id=table.organization_id),
        action="catalog.asset.certify",
        resource_type="asset_certification",
        resource_id=str(certification.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "table_id": str(table.id),
            "column_id": str(column.id) if column else None,
            "asset_type": body.asset_type,
            "expires_at": body.expires_at.isoformat(),
            "superseded_count": len(prior_rows),
        },
    )
    record_outbox(
        session,
        organization_id=table.organization_id,
        aggregate_type="asset_certification",
        aggregate_id=str(certification.id),
        event_type="catalog.asset.certified.v1",
        payload={
            "certification_id": str(certification.id),
            "table_id": str(table.id),
            "column_id": str(column.id) if column else None,
            "asset_type": body.asset_type,
            "expires_at": body.expires_at.isoformat(),
        },
    )
    await session.commit()
    return _asset_certification_read(
        certification, is_active=asset_certification_is_active(certification, at=now)
    )


@router.post(
    "/tables/{table_id}/certification/revoke",
    response_model=AssetCertificationRead,
)
async def revoke_table_certification(
    table_id: UUID,
    body: CertificationRevokeRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AssetCertificationRead:
    """P2-08: manual revocation of an ACTIVE certification.

    Flips the currently ACTIVE certification row for the ``(table, asset_type,
    column?)`` tuple to ``status = "REVOKED"``, stamping
    ``revoked_at`` / ``revoked_by`` / ``revocation_reason`` atomically. This is
    the ONLY writer of the ``REVOKED`` status; before P2-08 the value existed
    in the state machine (readers in
    ``catalog/service.py::_certification_state`` and
    ``asset_usage_decision.py`` already gated on it) but no code ever produced
    it, so a revoked-by-policy certification could only be worked around by
    letting it expire.

    Maker-checker: by default, the principal who granted the certification
    cannot revoke it -- the same two-person rule module 08 applies to reviewed
    stewardship operations, applied here at the certification itself.
    Off-switchable via ``certification_revoke_enforce_maker_checker`` for
    single-steward deployments where the rule would deadlock every revoke.

    Emits a ``catalog.asset.certification_revoked.v1`` outbox event so
    downstream projections (retrieval demotion, policy-decision cache,
    ``asset_usage_decision``'s ``REVOKED -> BLOCKED``) invalidate promptly.
    """
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    asset_type = "TABLE"
    column_id: UUID | None = None
    if body.column_id is not None:
        column = await session.get(MetadataColumn, body.column_id)
        if (
            column is None
            or column.organization_id != table.organization_id
            or column.table_id != table.id
        ):
            raise HTTPException(status_code=404, detail="column not found on this table")
        asset_type = "COLUMN"
        column_id = column.id
    active = (
        await session.scalars(
            select(AssetCertification)
            .where(
                AssetCertification.table_id == table.id,
                AssetCertification.asset_type == asset_type,
                AssetCertification.column_id == column_id,
                AssetCertification.status == "ACTIVE",
            )
            .order_by(AssetCertification.created_at.desc())
            .limit(1)
        )
    ).first()
    if active is None:
        raise HTTPException(
            status_code=404, detail="no active certification to revoke"
        )
    if (
        settings.certification_revoke_enforce_maker_checker
        and active.certified_by == context.principal_id
    ):
        raise HTTPException(
            status_code=409,
            detail="same_principal_cannot_revoke_own_certification",
        )
    now = datetime.now(UTC)
    active.status = "REVOKED"
    active.revoked_at = now
    active.revoked_by = context.principal_id
    active.revocation_reason = body.reason
    await session.flush()
    record_audit(
        session,
        replace(context, organization_id=table.organization_id),
        action="CERTIFICATION_REVOKED",
        resource_type="asset_certification",
        resource_id=str(active.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "table_id": str(table.id),
            "column_id": str(column_id) if column_id else None,
            "asset_type": asset_type,
            "revoked_by": context.principal_id,
            "certified_by": active.certified_by,
            "reason": body.reason,
        },
    )
    record_outbox(
        session,
        organization_id=table.organization_id,
        aggregate_type="asset_certification",
        aggregate_id=str(active.id),
        event_type="catalog.asset.certification_revoked.v1",
        payload={
            "certification_id": str(active.id),
            "table_id": str(table.id),
            "column_id": str(column_id) if column_id else None,
            "asset_type": asset_type,
            "revoked_by": context.principal_id,
            "revoked_at": now.isoformat(),
            "reason": body.reason,
        },
    )
    await session.commit()
    return _asset_certification_read(active, is_active=False)


@router.get("/tables/{table_id}/certification", response_model=AssetCertificationRead)
async def get_table_certification(
    table_id: UUID,
    column_id: UUID | None = Query(default=None),
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AssetCertificationRead:
    """The currently *active* certification for a table, or one of its columns.

    Expiry is enforced here rather than trusted from ``status``: a certification
    row keeps reading back ``status == "ACTIVE"`` after ``expires_at`` passes
    (see ``aida.asset_certification``), so this 404s once the active one has
    expired, even though the row itself is still sitting there as evidence.
    """
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="table",
        resource_id=str(table.id),
        datasource_id=table.datasource_id,
    )
    asset_type = "TABLE"
    if column_id is not None:
        column = await session.get(MetadataColumn, column_id)
        if (
            column is None
            or column.organization_id != table.organization_id
            or column.table_id != table.id
        ):
            raise HTTPException(status_code=404, detail="column not found on this table")
        asset_type = "COLUMN"
    rows = (
        await session.scalars(
            select(AssetCertification)
            .where(
                AssetCertification.table_id == table.id,
                AssetCertification.asset_type == asset_type,
                AssetCertification.column_id == column_id,
            )
            .order_by(AssetCertification.created_at.desc())
            .limit(20)
        )
    ).all()
    active = current_asset_certification(list(rows), at=datetime.now(UTC))
    if active is None:
        raise HTTPException(status_code=404, detail="no active certification found")
    return _asset_certification_read(active, is_active=True)



# 2026-09-03 (ST-07 Commit C Slice 3): moved from `aida.api` alongside the
# bulk endpoints. Only the "catalog bulk action" endpoints below reference
# this map, so it lives here rather than in a shared module.
_CATALOG_BULK_ACTION_EVENT_TYPES = {
    "TAG": "catalog.asset_tag.applied.v1",
    "CLASSIFY": "catalog.column.classified.v1",
    "OWN": "ownership.assigned.v1",
    "CERTIFY": "certification.granted.v1",
}

# ---------------------------------------------------------------------------
# CT-1: Catalog bulk actions (tag, classify, own, certify)
# ---------------------------------------------------------------------------


async def _resolve_bulk_table_subjects(
    session: AsyncSession,
    *,
    organization_id: UUID,
    table_ids: list[UUID] | None,
    selection_filter: CatalogBulkSelectionFilter | None,
) -> tuple[list[UUID], str, bool]:
    if table_ids is not None:
        return dedupe_preserving_order(table_ids), "EXPLICIT", False
    assert selection_filter is not None
    datasource = await session.get(DataSource, selection_filter.datasource_id)
    if datasource is None or datasource.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="data source not found")
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.datasource_id == selection_filter.datasource_id,
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataTable.id)
            .limit(CATALOG_BULK_FILTER_SCAN_CAP)
        )
    ).all()
    candidates = [(row[0], row[1]) for row in rows]
    matched, truncated = match_tables_by_filter(
        candidates,
        match_field=selection_filter.match_field,
        match_pattern=selection_filter.match_pattern,
        cap=CATALOG_BULK_ACTION_MAX_ITEMS,
    )
    if not matched:
        raise HTTPException(status_code=409, detail="filter matched no active tables")
    return matched, "FILTER", truncated


async def _fetch_bulk_tables(
    session: AsyncSession, *, organization_id: UUID, table_ids: list[UUID]
) -> dict[UUID, MetadataTable]:
    rows = (
        await session.scalars(
            select(MetadataTable).where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.id.in_(table_ids),
            )
        )
    ).all()
    return {row.id: row for row in rows}


async def _persist_catalog_bulk_action_run(
    session: AsyncSession,
    *,
    context: SecurityContext,
    organization_id: UUID,
    action: str,
    selection_mode: str,
    parameters: dict[str, Any],
    plan: BulkPlan,
) -> CatalogBulkActionRun:
    run = CatalogBulkActionRun(
        organization_id=organization_id,
        action=action,
        selection_mode=selection_mode,
        parameters=parameters,
        requested_count=len(plan.results),
        succeeded_count=plan.succeeded_count,
        failed_count=plan.failed_count,
        results=[item.as_dict() for item in plan.results],
        requested_by=context.principal_id,
    )
    session.add(run)
    await session.flush()
    if plan.succeeded_count and plan.failed_count:
        outcome = "PARTIAL_SUCCESS"
    elif plan.succeeded_count:
        outcome = "SUCCESS"
    else:
        outcome = "FAILURE"
    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action=f"catalog.bulk_{action.lower()}",
        resource_type="catalog_bulk_action_run",
        resource_id=str(run.id),
        outcome=outcome,
        correlation_id=get_correlation_id(),
        details={
            "requested_count": run.requested_count,
            "succeeded_count": run.succeeded_count,
            "failed_count": run.failed_count,
            "selection_mode": selection_mode,
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="catalog_bulk_action_run",
        aggregate_id=str(run.id),
        event_type=_CATALOG_BULK_ACTION_EVENT_TYPES[action],
        payload={
            "run_id": str(run.id),
            "action": action,
            "succeeded_count": run.succeeded_count,
            "failed_count": run.failed_count,
        },
    )
    return run


@router.post(
    "/organizations/{organization_id}/tables/bulk-tag",
    response_model=CatalogBulkActionRunRead,
)
async def bulk_tag_tables(
    organization_id: UUID,
    body: CatalogBulkTagRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CatalogBulkActionRun:
    enforce_organization(context, organization_id)
    subject_ids, selection_mode, truncated = await _resolve_bulk_table_subjects(
        session,
        organization_id=organization_id,
        table_ids=body.table_ids,
        selection_filter=body.filter,
    )
    tables = await _fetch_bulk_tables(
        session, organization_id=organization_id, table_ids=subject_ids
    )
    existing_tag_rows = (
        await session.scalars(
            select(AssetTag).where(
                AssetTag.table_id.in_(subject_ids),
                AssetTag.tag_key == body.tag_key,
            )
        )
    ).all()
    existing_tags = {row.table_id: row for row in existing_tag_rows}
    results: list[BulkItemResult] = []
    for subject_id in subject_ids:
        try:
            async with session.begin_nested():
                row, is_new = apply_tag_item(
                    subject_id,
                    tables=tables,
                    existing_tags=existing_tags,
                    organization_id=organization_id,
                    tag_key=body.tag_key,
                    tag_value=body.tag_value,
                    applied_by=context.principal_id,
                )
                if is_new:
                    session.add(row)
                await session.flush([row])
        except CatalogBulkItemError as exc:
            results.append(BulkItemResult(str(subject_id), "FAILED", str(exc)))
            continue
        except IntegrityError:
            results.append(
                BulkItemResult(str(subject_id), "FAILED", "database constraint violation")
            )
            continue
        results.append(BulkItemResult(str(subject_id), "SUCCEEDED", None))
    plan = BulkPlan(results=results)
    run = await _persist_catalog_bulk_action_run(
        session,
        context=context,
        organization_id=organization_id,
        action="TAG",
        selection_mode=selection_mode,
        parameters={
            "tag_key": body.tag_key,
            "tag_value": body.tag_value,
            "selection_truncated": truncated,
        },
        plan=plan,
    )
    await session.commit()
    return run


@router.post(
    "/organizations/{organization_id}/tables/bulk-classify",
    response_model=CatalogBulkActionRunRead,
)
async def bulk_classify_columns(
    organization_id: UUID,
    body: CatalogBulkClassifyRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CatalogBulkActionRun:
    enforce_organization(context, organization_id)
    truncated = False
    if body.column_ids is not None:
        subject_ids = dedupe_preserving_order(body.column_ids)
        selection_mode = "EXPLICIT"
    else:
        table_ids, _, table_truncated = await _resolve_bulk_table_subjects(
            session,
            organization_id=organization_id,
            table_ids=body.table_ids,
            selection_filter=body.filter,
        )
        selection_mode = "EXPLICIT" if body.table_ids is not None else "FILTER"
        truncated = truncated or table_truncated
        column_rows = (
            await session.scalars(
                select(MetadataColumn)
                .where(
                    MetadataColumn.organization_id == organization_id,
                    MetadataColumn.table_id.in_(table_ids),
                    MetadataColumn.status == "ACTIVE",
                )
                .order_by(MetadataColumn.id)
                .limit(CATALOG_BULK_FILTER_SCAN_CAP)
            )
        ).all()
        subject_ids, column_truncated = match_columns_by_pattern(
            column_rows,
            name_pattern=body.column_name_pattern,
            cap=CATALOG_BULK_ACTION_MAX_ITEMS,
        )
        truncated = truncated or column_truncated
        if not subject_ids:
            raise HTTPException(status_code=409, detail="selection matched no active columns")
    rows = (
        await session.execute(
            select(MetadataColumn, MetadataTable)
            .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
            .where(
                MetadataColumn.organization_id == organization_id,
                MetadataColumn.id.in_(subject_ids),
            )
        )
    ).all()
    columns = {row[0].id: (row[0], row[1]) for row in rows}
    results: list[BulkItemResult] = []
    for subject_id in subject_ids:
        try:
            async with session.begin_nested():
                column = apply_classify_item(
                    subject_id, columns=columns, classification=body.classification
                )
                await session.flush([column])
        except CatalogBulkItemError as exc:
            results.append(BulkItemResult(str(subject_id), "FAILED", str(exc)))
            continue
        except IntegrityError:
            results.append(
                BulkItemResult(str(subject_id), "FAILED", "database constraint violation")
            )
            continue
        results.append(BulkItemResult(str(subject_id), "SUCCEEDED", None))
    plan = BulkPlan(results=results)
    run = await _persist_catalog_bulk_action_run(
        session,
        context=context,
        organization_id=organization_id,
        action="CLASSIFY",
        selection_mode=selection_mode,
        parameters={
            "classification": body.classification,
            "column_name_pattern": body.column_name_pattern,
            "selection_truncated": truncated,
        },
        plan=plan,
    )
    await session.commit()
    return run


# ---------------------------------------------------------------------------
# GV-2 / P0-02: bulk-action governance router.
#
# The 2026-08-30 audit flagged the two "bulk" endpoints below
# (`bulk_assign_ownership`, `bulk_certify_tables`) as a maker-checker
# bypass: they wrote straight to ACTIVE under only RBAC, while every
# same-shape operation reached through `aida.stewardship_api._create_bulk_operation`
# routed through `BulkStewardshipOperation` + `GovernanceReview` with
# maker != checker enforced. A `DataSteward` could therefore act as both
# maker and applier by picking the catalog endpoint instead of the
# governed one. `_should_route_bulk_through_governance` closes that gap
# without changing the RBAC allowed-writers list at all: two settings
# knobs (see `atlas.platform.config.Settings.bulk_governance_threshold`
# and `.bulk_governance_roles_requiring_review`) decide, per call,
# whether the request may direct-write or MUST go through review.
#
# `_route_bulk_through_governance` is the one bridge into the governed
# path, and it deliberately calls into `stewardship_api._create_bulk_operation`
# rather than duplicating the review-object creation, so any future change
# to how a `BulkStewardshipOperation` is minted lands here for free.
# ---------------------------------------------------------------------------


def _should_route_bulk_through_governance(
    context: SecurityContext,
    count: int,
    settings: Settings,
) -> tuple[bool, str | None]:
    """Return (route_through_review, reason). ``reason`` is a short string
    naming why the request must be reviewed, suitable for the direct-write
    audit event's ``details`` and for the returned 202 body's ``reason``
    field. ``None`` on the direct-write path.
    """
    require_review = frozenset(settings.bulk_governance_roles_requiring_review)
    if not context.roles.isdisjoint(require_review):
        # Any of the caller's roles is in the review-required list -- e.g. the
        # default `DataSteward`, which is authorized to *request* a bulk
        # action but not to unilaterally apply one.
        return True, "role_requires_review"
    if count > settings.bulk_governance_threshold:
        return True, "count_above_threshold"
    return False, None


async def _route_bulk_through_governance(
    session: AsyncSession,
    *,
    organization_id: UUID,
    context: SecurityContext,
    operation_type: str,
    subject_ids: list[UUID],
    owner_type: str | None = None,
    owner_principal: str | None = None,
    rationale: str | None = None,
    expires_at: datetime | None = None,
    reason: str,
) -> Response:
    """Delegate a catalog-router bulk action into the governed path.

    Reuses `aida.stewardship_api._create_bulk_operation` -- the SAME helper
    the governed endpoint uses -- so the router now shares one code path
    for creating a `BulkStewardshipOperation` + its paired
    `GovernanceReview`; the maker != checker enforcement lives entirely on
    that side and does not need re-implementing here. Returns HTTP 202 with
    the operation, matching the governed endpoint's response shape.
    """
    # Import inside the function to keep `atlas.modules.catalog.router` from
    # importing `aida.stewardship_api` at module load (avoids any circular
    # import surprise; also lets the reachability gate treat the two as
    # separately-reachable routers rather than one composite module).
    from aida.stewardship_api import _create_bulk_operation

    body = BulkStewardshipOperationCreate(
        operation_type=operation_type,  # type: ignore[arg-type]
        subject_type="TABLE",
        subject_ids=subject_ids,
        owner_type=owner_type,  # type: ignore[arg-type]
        owner_principal=owner_principal,
        rationale=rationale,
        expires_at=expires_at,
    )
    operation = await _create_bulk_operation(
        session,
        organization_id=organization_id,
        body=body,
        context=context,
    )
    # Additional audit signal naming *why* the router chose to route this
    # particular request through review, so an operator inspecting one
    # `BulkStewardshipOperation` row can see whether the trigger was the
    # count threshold, a role in the review-required list, or both.
    record_audit(
        session,
        context,
        action="catalog.bulk_action.review_routed.v1",
        resource_type="bulk_stewardship_operation",
        resource_id=str(operation.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "operation_type": operation_type,
            "subject_count": len(subject_ids),
            "reason": reason,
            "roles": sorted(context.roles),
        },
    )
    await session.commit()
    review_url = (
        f"/v1/organizations/{organization_id}/governance-reviews/"
        f"{operation.governance_review_id}"
    )
    # Response body keeps `BulkStewardshipOperationRead`\'s existing shape --
    # so the openapi.json addition matches an existing declared model -- and
    # exposes `review_url`/`route_reason` via headers rather than
    # extending the schema; the client already has `governance_review_id`
    # on the body and can reconstruct the URL if it prefers.
    return Response(
        content=BulkStewardshipOperationRead.model_validate(operation).model_dump_json(),
        media_type="application/json",
        status_code=status.HTTP_202_ACCEPTED,
        headers={
            "Location": review_url,
            "X-Bulk-Route-Reason": reason,
        },
    )


def _resolve_settings(settings: Settings | None) -> Settings:
    """FastAPI wires `settings` in via `Depends(get_settings)`; the router
    handlers are also called directly by the CT-1 test suite, which passes
    only `context=` and `session=`. In that path `settings` arrives as the
    `Depends` sentinel, not a `Settings` instance -- fall back to
    `get_settings()` (LRU-cached) so a directly-called handler still gets
    real settings without every test having to construct and thread one
    through.
    """
    return settings if isinstance(settings, Settings) else get_settings()



@router.post(
    "/organizations/{organization_id}/tables/bulk-own",
    response_model=CatalogBulkActionRunRead,
    # GV-2: the review-routed path returns 202 with a
    # `BulkStewardshipOperationRead` body; declare it in `responses` so the
    # generated OpenAPI keeps typed shapes for both 200 (direct-write, the
    # existing shape -- backward-compatible) and 202 (review-routed).
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": BulkStewardshipOperationRead,
            "description": (
                "Request exceeded the bulk-governance threshold or the "
                "caller\'s role requires review; a "
                "`BulkStewardshipOperation` has been created and awaits "
                "an independent approver (maker != checker)."
            ),
        }
    },
)
async def bulk_assign_ownership(
    organization_id: UUID,
    body: CatalogBulkOwnRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CatalogBulkActionRun | Response:
    settings = _resolve_settings(settings)
    enforce_organization(context, organization_id)
    subject_ids, selection_mode, truncated = await _resolve_bulk_table_subjects(
        session,
        organization_id=organization_id,
        table_ids=body.table_ids,
        selection_filter=body.filter,
    )
    # GV-2 / P0-02: decide whether this call may direct-write to ACTIVE or
    # must route through `BulkStewardshipOperation` + `GovernanceReview`
    # (maker != checker). See `_should_route_bulk_through_governance` for
    # the two knobs (`AIDA_BULK_GOVERNANCE_THRESHOLD`,
    # `AIDA_BULK_GOVERNANCE_ROLES_REQUIRING_REVIEW`) that decide.
    should_route, reason = _should_route_bulk_through_governance(
        context, len(subject_ids), settings
    )
    if should_route:
        return await _route_bulk_through_governance(
            session,
            organization_id=organization_id,
            context=context,
            operation_type="ASSIGN_OWNERSHIP",
            subject_ids=subject_ids,
            owner_type=body.owner_type,
            owner_principal=body.owner_principal,
            reason=reason or "policy",
        )
    tables = await _fetch_bulk_tables(
        session, organization_id=organization_id, table_ids=subject_ids
    )
    existing_assignment_rows = (
        await session.scalars(
            select(OwnershipAssignment).where(
                OwnershipAssignment.organization_id == organization_id,
                OwnershipAssignment.subject_type == "TABLE",
                OwnershipAssignment.subject_id.in_([str(value) for value in subject_ids]),
                OwnershipAssignment.owner_type == body.owner_type,
                OwnershipAssignment.owner_principal == body.owner_principal,
            )
        )
    ).all()
    existing_assignments = {UUID(row.subject_id): row for row in existing_assignment_rows}
    results: list[BulkItemResult] = []
    for subject_id in subject_ids:
        try:
            async with session.begin_nested():
                row, is_new = apply_own_item(
                    subject_id,
                    tables=tables,
                    existing_assignments=existing_assignments,
                    organization_id=organization_id,
                    owner_type=body.owner_type,
                    owner_principal=body.owner_principal,
                    assigned_by=context.principal_id,
                )
                if is_new:
                    session.add(row)
                await session.flush([row])
        except CatalogBulkItemError as exc:
            results.append(BulkItemResult(str(subject_id), "FAILED", str(exc)))
            continue
        except IntegrityError:
            results.append(
                BulkItemResult(str(subject_id), "FAILED", "database constraint violation")
            )
            continue
        results.append(BulkItemResult(str(subject_id), "SUCCEEDED", None))
    plan = BulkPlan(results=results)
    run = await _persist_catalog_bulk_action_run(
        session,
        context=context,
        organization_id=organization_id,
        action="OWN",
        selection_mode=selection_mode,
        parameters={
            "owner_type": body.owner_type,
            "owner_principal": body.owner_principal,
            "selection_truncated": truncated,
        },
        plan=plan,
    )
    # GV-2 / P0-02: audit signal on every direct-write path, naming
    # operator, count, resolved subject ids and the reason the request was
    # allowed to skip review -- so a compliance query can surface every
    # bypass at grep-time instead of reconstructing it after the fact from
    # `OwnershipAssignment` rows alone.
    record_audit(
        session,
        context,
        action="catalog.bulk_action.direct_write.v1",
        resource_type="catalog_bulk_action_run",
        resource_id=str(run.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "operation_type": "ASSIGN_OWNERSHIP",
            "subject_count": len(subject_ids),
            "subject_ids": [str(value) for value in subject_ids],
            "owner_type": body.owner_type,
            "owner_principal": body.owner_principal,
            "reason": "within_threshold_and_role_not_in_review_list",
            "roles": sorted(context.roles),
        },
    )
    await session.commit()
    return run


@router.post(
    "/organizations/{organization_id}/tables/bulk-certify",
    response_model=CatalogBulkActionRunRead,
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": BulkStewardshipOperationRead,
            "description": (
                "Request exceeded the bulk-governance threshold or the "
                "caller\'s role requires review; a "
                "`BulkStewardshipOperation` has been created and awaits "
                "an independent approver (maker != checker)."
            ),
        }
    },
)
async def bulk_certify_tables(
    organization_id: UUID,
    body: CatalogBulkCertifyRequest,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> CatalogBulkActionRun | Response:
    settings = _resolve_settings(settings)
    enforce_organization(context, organization_id)
    if body.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="certification expiry must be in the future")
    subject_ids, selection_mode, truncated = await _resolve_bulk_table_subjects(
        session,
        organization_id=organization_id,
        table_ids=body.table_ids,
        selection_filter=body.filter,
    )
    # GV-2 / P0-02: same governance gate as `bulk_assign_ownership`. A
    # `DataSteward` who could formerly bulk-certify an entire quarter\'s
    # tables in one direct write now routes through the SAME
    # `BulkStewardshipOperation` + `GovernanceReview` flow the governed
    # `_create_bulk_operation` endpoint uses.
    should_route, reason = _should_route_bulk_through_governance(
        context, len(subject_ids), settings
    )
    if should_route:
        return await _route_bulk_through_governance(
            session,
            organization_id=organization_id,
            context=context,
            operation_type="CERTIFY_ASSET",
            subject_ids=subject_ids,
            rationale=body.rationale,
            expires_at=body.expires_at,
            reason=reason or "policy",
        )
    tables = await _fetch_bulk_tables(
        session, organization_id=organization_id, table_ids=subject_ids
    )
    active_certifications = (
        await session.scalars(
            select(AssetCertification).where(
                AssetCertification.table_id.in_(subject_ids),
                # CT-5: certification is now also column-scoped (`asset_type ==
                # "COLUMN"`), with `table_id` still denormalized onto those rows
                # for lookup. Table-level bulk certify must only ever supersede a
                # prior *table*-level certification, never a column's.
                AssetCertification.asset_type == "TABLE",
                AssetCertification.status == "ACTIVE",
            )
        )
    ).all()
    grouped_certifications: dict[UUID, list[AssetCertification]] = {}
    for row in active_certifications:
        grouped_certifications.setdefault(row.table_id, []).append(row)
    results: list[BulkItemResult] = []
    now = datetime.now(UTC)
    for subject_id in subject_ids:
        try:
            async with session.begin_nested():
                # P3-09: compute the structured evidence blob per subject
                # before the SAVEPOINT commits so every catalog-bulk-certify
                # row carries the same shape the single-certify path writes.
                evidence_blob = await compute_certification_evidence(
                    session,
                    subject_id,
                    organization_id=organization_id,
                    now=now,
                    certifier_notes=body.rationale,
                )
                new_certification, superseded_priors = apply_certify_item(
                    subject_id,
                    tables=tables,
                    active_certifications=grouped_certifications,
                    organization_id=organization_id,
                    rationale=body.rationale,
                    expires_at=body.expires_at,
                    certified_by=context.principal_id,
                    evidence=evidence_blob,
                )
                session.add(new_certification)
                await session.flush([new_certification, *superseded_priors])
        except CatalogBulkItemError as exc:
            results.append(BulkItemResult(str(subject_id), "FAILED", str(exc)))
            continue
        except IntegrityError:
            results.append(
                BulkItemResult(str(subject_id), "FAILED", "database constraint violation")
            )
            continue
        results.append(BulkItemResult(str(subject_id), "SUCCEEDED", None))
    plan = BulkPlan(results=results)
    run = await _persist_catalog_bulk_action_run(
        session,
        context=context,
        organization_id=organization_id,
        action="CERTIFY",
        selection_mode=selection_mode,
        parameters={
            "rationale": body.rationale,
            "expires_at": body.expires_at.isoformat(),
            "selection_truncated": truncated,
        },
        plan=plan,
    )
    record_audit(
        session,
        context,
        action="catalog.bulk_action.direct_write.v1",
        resource_type="catalog_bulk_action_run",
        resource_id=str(run.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "operation_type": "CERTIFY_ASSET",
            "subject_count": len(subject_ids),
            "subject_ids": [str(value) for value in subject_ids],
            "rationale": body.rationale,
            "expires_at": body.expires_at.isoformat(),
            "reason": "within_threshold_and_role_not_in_review_list",
            "roles": sorted(context.roles),
        },
    )
    await session.commit()
    return run


@router.get(
    "/organizations/{organization_id}/catalog-bulk-actions",
    response_model=Page,
)
async def list_catalog_bulk_action_runs(
    organization_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    enforce_organization(context, organization_id)
    filters = (CatalogBulkActionRun.organization_id == organization_id,)
    total = await session.scalar(
        select(func.count()).select_from(CatalogBulkActionRun).where(*filters)
    )
    rows = (
        await session.scalars(
            select(CatalogBulkActionRun)
            .where(*filters)
            .order_by(CatalogBulkActionRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[CatalogBulkActionRunRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get(
    "/organizations/{organization_id}/catalog-bulk-actions/{run_id}",
    response_model=CatalogBulkActionRunRead,
)
async def get_catalog_bulk_action_run(
    organization_id: UUID,
    run_id: UUID,
    context: SecurityContext = Depends(require_roles(*CATALOG_BULK_ACTION_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> CatalogBulkActionRun:
    enforce_organization(context, organization_id)
    run = await session.get(CatalogBulkActionRun, run_id)
    if run is None or run.organization_id != organization_id:
        raise HTTPException(status_code=404, detail="catalog bulk action run not found")
    return run
