from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_certification import asset_certification_is_active
from aida.certification_evidence import compute_certification_evidence
from aida.config import get_settings
from aida.catalog_bulk_actions import (
    CatalogBulkItemError,
    apply_classify_item,
    apply_tag_item,
)
from aida.models import (
    AssetCertification,
    AssetTag,
    AssetTermLink,
    BulkStewardshipOperation,
    GlossaryConflict,
    GlossaryLinkProposal,
    GlossaryTerm,
    GlossaryTermVersion,
    MetadataColumn,
    MetadataTable,
    OwnershipAssignment,
)
from aida.schemas import CoverageDimensionRead, StewardshipCoverageRead

COVERAGE_DIMENSIONS = (
    "documented",
    "owned",
    "classified",
    "certified",
    "quality_monitored",
    "semantically_mapped",
)


def build_stewardship_coverage(
    *,
    organization_id: UUID,
    datasource_id: UUID | None,
    domain_id: UUID | None,
    line_of_business_id: UUID | None,
    table_ids: set[UUID],
    evidence_sets: dict[str, set[UUID]],
    computed_at: datetime,
) -> StewardshipCoverageRead:
    """Compute the six-dimensional score from bounded, table-level evidence."""
    total = len(table_ids)
    dimensions: dict[str, CoverageDimensionRead] = {}
    for name in COVERAGE_DIMENSIONS:
        covered = len(evidence_sets.get(name, set()) & table_ids)
        dimensions[name] = CoverageDimensionRead(
            covered=covered,
            total=total,
            percentage=round(covered * 100 / total, 2) if total else 0.0,
        )
    overall = (
        round(sum(dimension.percentage for dimension in dimensions.values()) / len(dimensions), 2)
        if total
        else 0.0
    )
    owned = evidence_sets.get("owned", set())
    return StewardshipCoverageRead(
        organization_id=organization_id,
        datasource_id=datasource_id,
        domain_id=domain_id,
        line_of_business_id=line_of_business_id,
        table_count=total,
        overall_score=overall,
        dimensions=dimensions,
        unowned_table_ids=sorted(table_ids - owned, key=str)[:500],
        computed_at=computed_at,
    )


def active_certified_table_ids(
    certifications: list[AssetCertification], *, now: datetime
) -> set[UUID]:
    """Table IDs whose *table-level* certification is currently ACTIVE and not expired.

    Certification is a time-bound attestation, not a permanent one: a certification
    that has passed its ``expires_at`` must stop counting toward the "certified"
    stewardship-coverage dimension even though its ``status`` row hasn't separately
    been flipped -- expiry, not just status, gates whether it still counts. The
    active/expired projection itself is ``aida.asset_certification.asset_certification_is_active``,
    the single shared definition module 04 (CT-5) also uses for table *and* column
    certification, so this and the catalog HTTP endpoints can never drift apart on
    what "currently certified" means.

    CT-5 extended certification to also be column-scoped (``asset_type ==
    "COLUMN"``), with ``table_id`` still denormalized onto those rows for lookup
    convenience -- so a column certification is explicitly excluded here rather
    than silently making its parent table look certified too. Rows predating that
    column (``asset_type`` unset) are treated as table-level, matching the
    model's own default.
    """
    return {
        certification.table_id
        for certification in certifications
        if certification.asset_type != "COLUMN"
        and asset_certification_is_active(certification, at=now)
    }


async def apply_bulk_operation(
    session: AsyncSession,
    operation: BulkStewardshipOperation,
    *,
    reviewer: str,
    now: datetime,
) -> tuple[str, int]:
    applied = 0
    parameters = operation.parameters
    subject_ids = [UUID(value) for value in operation.subject_ids]
    if operation.operation_type == "ASSIGN_OWNERSHIP":
        # P2-07: new/reactivated OwnershipAssignment rows carry an `expires_at`
        # sourced from `settings.ownership_reaffirm_days` (default 180). Legacy
        # rows without `expires_at` are unaffected until they're re-affirmed
        # or freshly assigned; the expiry-warning sweep skips them explicitly.
        reaffirm_days = get_settings().ownership_reaffirm_days
        expires_at = now + timedelta(days=reaffirm_days)
        for subject_id in subject_ids:
            existing = await session.scalar(
                select(OwnershipAssignment).where(
                    OwnershipAssignment.organization_id == operation.organization_id,
                    OwnershipAssignment.subject_type == operation.subject_type,
                    OwnershipAssignment.subject_id == str(subject_id),
                    OwnershipAssignment.owner_type == parameters["owner_type"],
                    OwnershipAssignment.owner_principal == parameters["owner_principal"],
                )
            )
            if existing is not None:
                if existing.status != "ACTIVE":
                    existing.status = "ACTIVE"
                    existing.assigned_by = reviewer
                    # A reactivation is treated as a fresh assertion of
                    # ownership -- extends the expiry and clears any prior
                    # warning stamp so the row can warn again in its next cycle.
                    existing.expires_at = expires_at
                    existing.expiry_warning_emitted_at = None
                    applied += 1
                continue
            session.add(
                OwnershipAssignment(
                    organization_id=operation.organization_id,
                    subject_type=operation.subject_type,
                    subject_id=str(subject_id),
                    owner_type=parameters["owner_type"],
                    owner_principal=parameters["owner_principal"],
                    assignment_kind="RULE" if parameters.get("source_rule_id") else "MANUAL",
                    source_rule_id=(
                        UUID(parameters["source_rule_id"])
                        if parameters.get("source_rule_id")
                        else None
                    ),
                    assigned_by=reviewer,
                    expires_at=expires_at,
                )
            )
            applied += 1
        event_type = "ownership.assigned.v1"
    elif operation.operation_type == "LINK_TERM":
        term_id = UUID(parameters["term_id"])
        for table_id in subject_ids:
            existing = await session.scalar(
                select(AssetTermLink).where(
                    AssetTermLink.table_id == table_id,
                    AssetTermLink.term_id == term_id,
                )
            )
            if existing is not None:
                continue
            session.add(
                AssetTermLink(
                    organization_id=operation.organization_id,
                    table_id=table_id,
                    term_id=term_id,
                    linked_by=reviewer,
                    link_type="BULK",
                    confidence=1.0,
                )
            )
            applied += 1
        event_type = "glossary.term_linked_bulk.v1"
    elif operation.operation_type == "DEPRECATE_TERM":
        terms = (
            await session.scalars(
                select(GlossaryTerm).where(
                    GlossaryTerm.organization_id == operation.organization_id,
                    GlossaryTerm.id.in_(subject_ids),
                )
            )
        ).all()
        for term in terms:
            if term.lifecycle_status == "DEPRECATED":
                continue
            term.lifecycle_status = "DEPRECATED"
            term.deprecated_by = reviewer
            term.deprecated_at = now
            term.deprecation_reason = parameters["rationale"]
            await session.execute(
                update(GlossaryTermVersion)
                .where(
                    GlossaryTermVersion.term_id == term.id,
                    GlossaryTermVersion.status == "APPROVED",
                )
                .values(status="DEPRECATED", updated_at=now)
            )
            applied += 1
        event_type = "glossary.term_deprecated.v1"
    elif operation.operation_type == "CERTIFY_ASSET":
        expires_at = datetime.fromisoformat(parameters["expires_at"])
        for table_id in subject_ids:
            await session.execute(
                update(AssetCertification)
                .where(
                    AssetCertification.table_id == table_id,
                    # CT-5: never supersede a column-scoped certification from a
                    # table-level bulk certify -- they are different assets that
                    # happen to share table_id as a denormalized lookup column.
                    AssetCertification.asset_type == "TABLE",
                    AssetCertification.status == "ACTIVE",
                )
                .values(status="SUPERSEDED", updated_at=now)
            )
            # P3-09: capture structured evidence per subject so the reviewed-
            # bulk (P0-02) certify path writes the same evidence shape as the
            # direct-write single/bulk endpoints and the playbook auto-apply.
            evidence_blob = await compute_certification_evidence(
                session,
                table_id,
                organization_id=operation.organization_id,
                now=now,
                certifier_notes=parameters["rationale"],
            )
            session.add(
                AssetCertification(
                    organization_id=operation.organization_id,
                    table_id=table_id,
                    asset_type="TABLE",
                    rationale=parameters["rationale"],
                    certified_by=reviewer,
                    expires_at=expires_at,
                    evidence=evidence_blob,
                )
            )
            applied += 1
        event_type = "certification.granted.v1"
    elif operation.operation_type == "TAG":
        # AT-1: a playbook's TAG action, routed through review because its
        # match count exceeded the playbook's own auto-apply threshold.
        # Reuses CT-1's own single-item core (`apply_tag_item`) rather than a
        # second tagging implementation -- the same function the synchronous
        # `/tables/bulk-tag` endpoint calls per item.
        table_rows = (
            await session.scalars(
                select(MetadataTable).where(MetadataTable.id.in_(subject_ids))
            )
        ).all()
        tables_by_id = {row.id: row for row in table_rows}
        existing_tag_rows = (
            await session.scalars(
                select(AssetTag).where(
                    AssetTag.table_id.in_(subject_ids),
                    AssetTag.tag_key == parameters["tag_key"],
                )
            )
        ).all()
        existing_tags = {row.table_id: row for row in existing_tag_rows}
        for subject_id in subject_ids:
            try:
                row, is_new = apply_tag_item(
                    subject_id,
                    tables=tables_by_id,
                    existing_tags=existing_tags,
                    organization_id=operation.organization_id,
                    tag_key=parameters["tag_key"],
                    tag_value=parameters.get("tag_value"),
                    applied_by=reviewer,
                )
            except CatalogBulkItemError:
                # A table this playbook run matched has since been deleted or
                # deprecated -- skipped, not a hard failure of the whole
                # governed decision, matching every other branch's idempotent
                # skip of a subject that went stale between request and
                # decision (see REASSIGN_LEAVER below).
                continue
            if is_new:
                session.add(row)
            applied += 1
        event_type = "catalog.asset_tag.applied.v1"
    elif operation.operation_type == "CLASSIFY":
        # AT-1: a playbook's CLASSIFY action, same reuse as TAG above but of
        # `apply_classify_item`, CT-1's single-item column-classification core.
        column_rows = (
            await session.execute(
                select(MetadataColumn, MetadataTable)
                .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
                .where(MetadataColumn.id.in_(subject_ids))
            )
        ).all()
        columns_by_id = {row[0].id: (row[0], row[1]) for row in column_rows}
        for subject_id in subject_ids:
            try:
                apply_classify_item(
                    subject_id,
                    columns=columns_by_id,
                    classification=parameters["classification"],
                )
            except CatalogBulkItemError:
                continue
            applied += 1
        event_type = "catalog.column.classified.v1"
    elif operation.operation_type == "REASSIGN_LEAVER":
        # GL-7: `subject_ids` here are `OwnershipAssignment.id` values (not
        # bare catalog/glossary ids like every other operation type) --
        # already-typed rows discovered/validated by
        # `stewardship_api.request_leaver_reassignment`, which is what lets
        # one operation span every asset kind the leaver owned (table *and*
        # term) in a single governed decision.
        leaving_principal = parameters["leaving_principal"]
        successor_principal = parameters["successor_principal"]
        owner_type = parameters["owner_type"]
        assignment_rows = (
            await session.scalars(
                select(OwnershipAssignment).where(OwnershipAssignment.id.in_(subject_ids))
            )
        ).all()
        assignments_by_id = {row.id: row for row in assignment_rows}
        # Successor rows already active for the same (subject_type,
        # subject_id, owner_type) are looked up so a reassignment reactivates
        # an existing co-owner row (GL-2's own idempotency rule) instead of
        # violating the unique constraint on
        # (organization_id, subject_type, subject_id, owner_type, owner_principal).
        successor_lookup = {
            (row.subject_type, row.subject_id): row
            for row in (
                await session.scalars(
                    select(OwnershipAssignment).where(
                        OwnershipAssignment.organization_id == operation.organization_id,
                        OwnershipAssignment.owner_type == owner_type,
                        OwnershipAssignment.owner_principal == successor_principal,
                    )
                )
            ).all()
        }
        for subject_id in subject_ids:
            assignment = assignments_by_id.get(subject_id)
            if (
                assignment is None
                or assignment.status != "ACTIVE"
                or assignment.owner_principal != leaving_principal
            ):
                # Stale by the time this was decided (already reassigned,
                # revoked, or vacated between request and decision) --
                # skipped, not counted, never a hard failure of the whole
                # governed decision (mirrors the idempotent-skip convention
                # every other branch above already follows).
                continue
            assignment.status = "REASSIGNED"
            successor_row = successor_lookup.get((assignment.subject_type, assignment.subject_id))
            if successor_row is not None:
                successor_row.status = "ACTIVE"
                successor_row.assigned_by = reviewer
            else:
                successor_row = OwnershipAssignment(
                    organization_id=operation.organization_id,
                    subject_type=assignment.subject_type,
                    subject_id=assignment.subject_id,
                    owner_type=owner_type,
                    owner_principal=successor_principal,
                    assignment_kind="REASSIGNED",
                    assigned_by=reviewer,
                )
                session.add(successor_row)
                successor_lookup[(assignment.subject_type, assignment.subject_id)] = successor_row
            applied += 1
        event_type = "ownership.leaver_reassigned.v1"
    else:
        raise HTTPException(status_code=422, detail="unsupported stewardship operation")
    operation.status = "APPLIED"
    operation.applied_by = reviewer
    operation.applied_at = now
    operation.applied_count = applied
    return event_type, applied


async def apply_conflict_resolution(
    conflict: GlossaryConflict,
    *,
    reviewer: str,
    now: datetime,
) -> str:
    if conflict.status != "REVIEW_REQUIRED":
        raise HTTPException(status_code=409, detail="conflict is no longer pending review")
    conflict.status = "RESOLVED"
    conflict.resolved_by = reviewer
    conflict.resolved_at = now
    return "glossary.conflict_resolved.v1"


async def reject_conflict_resolution(conflict: GlossaryConflict) -> str:
    if conflict.status != "REVIEW_REQUIRED":
        raise HTTPException(status_code=409, detail="conflict is no longer pending review")
    conflict.status = "OPEN"
    conflict.proposed_resolution = None
    conflict.proposed_definition = None
    conflict.resolution_rationale = None
    return "glossary.conflict_resolution_rejected.v1"


async def apply_link_proposal(
    session: AsyncSession,
    proposal: GlossaryLinkProposal,
    *,
    reviewer: str,
    now: datetime,
) -> str:
    if proposal.status != "REVIEW_REQUIRED":
        raise HTTPException(status_code=409, detail="link proposal is no longer pending review")
    existing = await session.scalar(
        select(AssetTermLink).where(
            AssetTermLink.table_id == proposal.table_id,
            AssetTermLink.term_id == proposal.term_id,
        )
    )
    if existing is None:
        session.add(
            AssetTermLink(
                organization_id=proposal.organization_id,
                table_id=proposal.table_id,
                term_id=proposal.term_id,
                linked_by=reviewer,
                link_type="INFERRED",
                confidence=proposal.confidence,
                source_annotation_id=proposal.source_annotation_id,
            )
        )
    proposal.status = "APPROVED"
    proposal.reviewed_by = reviewer
    proposal.reviewed_at = now
    return "glossary.link_proposal_approved.v1"


async def reject_link_proposal(
    proposal: GlossaryLinkProposal,
    *,
    reviewer: str,
    now: datetime,
) -> str:
    if proposal.status != "REVIEW_REQUIRED":
        raise HTTPException(status_code=409, detail="link proposal is no longer pending review")
    proposal.status = "REJECTED"
    proposal.reviewed_by = reviewer
    proposal.reviewed_at = now
    return "glossary.link_proposal_rejected.v1"


def utc_now() -> datetime:
    return datetime.now(UTC)
