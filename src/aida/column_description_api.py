"""Column description drafts: generate, list, edit, submit.

The column-level sibling of `asset_description_api` (GL-9). The drafting
itself -- evidence, composition, scoring -- is `aida.column_description_service`;
this module is the tenant-scoped, audited API around it.

There is no approve endpoint here, on purpose. A draft reaches
`ColumnDocumentationVersion` only through an independent decision on its
`GovernanceReview` (`POST /v1/governance/reviews/{review_id}/decision`, which
dispatches to `semantic_api._decide_column_description_draft`), exactly like the
two other ways a column gets a description -- a workbook import batch and an
approved document claim. A direct publish endpoint would be a way around the one
gate that makes the content trustworthy.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import (
    MINIMUM_EVIDENCE_FOR_REVIEW,
    ensure_reviewable,
    text_fingerprint,
)
from aida.authorization_gate import gate_read
from aida.column_description_service import (
    COLUMN_DESCRIPTION_DRAFT_OBJECT_TYPE,
    GENERATE_COLUMN_LIMIT,
    OPEN_DRAFT_STATUSES,
    column_evidence_payload,
    compose_column_draft_text,
    gather_table_column_evidence,
    score_column_evidence,
)
from aida.column_documentation import current_descriptions_by_column_id
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.description_withdrawal import withdrawn_column_versions
from aida.events import record_audit, record_outbox
from aida.models import (
    ColumnDescriptionDraft,
    ColumnDocumentationVersion,
    GovernanceReview,
    MetadataColumn,
    MetadataTable,
)
from aida.schemas import (
    ColumnDescriptionDraftBulkSubmitResult,
    ColumnDescriptionDraftEdit,
    ColumnDescriptionDraftGenerate,
    ColumnDescriptionDraftGenerateResult,
    ColumnDescriptionDraftRead,
    GovernanceReviewRead,
    Page,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["column-description-drafting"])

#: Who may generate, edit and submit drafts: `asset_description_api`'s write
#: population, unchanged. The same people draft a table's description and its
#: columns'.
WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "SemanticAdmin", "DataSteward")

#: Who may read one table's drafts. Every such read is also gated per table
#: through `gate_read`, so a role here is necessary, never sufficient.
TABLE_READ_ROLES = (
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

#: Who may list drafts across a whole organization. Narrower than a per-table
#: read on purpose: an organization-wide page spans every datasource, and the
#: per-datasource read gate (ADR-0017) is a per-table check such a page cannot
#: run row by row. Anyone else lists one table at a time, gated.
ORGANIZATION_LIST_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "SemanticAdmin",
    "DataSteward",
    "Reviewer",
    "Auditor",
)


def _draft_read(
    draft: ColumnDescriptionDraft, table_name: str, column_name: str
) -> ColumnDescriptionDraftRead:
    return ColumnDescriptionDraftRead(
        id=draft.id,
        organization_id=draft.organization_id,
        table_id=draft.table_id,
        table_name=table_name,
        column_id=draft.column_id,
        column_name=column_name,
        drafted_text=draft.drafted_text,
        accuracy_score=draft.accuracy_score,
        clarity_score=draft.clarity_score,
        style_score=draft.style_score,
        completeness_score=draft.completeness_score,
        overall_score=draft.overall_score,
        reviewable=draft.overall_score >= MINIMUM_EVIDENCE_FOR_REVIEW,
        evidence=draft.evidence,
        status=draft.status,
        base_description_version=draft.base_description_version,
        governance_review_id=draft.governance_review_id,
        published_version_id=draft.published_version_id,
        created_by=draft.created_by,
        reviewed_by=draft.reviewed_by,
        reviewed_at=draft.reviewed_at,
        created_at=draft.created_at,
        updated_at=draft.updated_at,
    )


async def _gate_table(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    table: MetadataTable,
) -> None:
    """The column-documentation read gate, against the same resource.

    A caller who cannot read a table's columns through
    `column_documentation_api` must not be able to read -- or have drafted --
    text composed from them here.
    """
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="table",
        resource_id=str(table.id),
        datasource_id=table.datasource_id,
    )


async def _table_and_column(
    session: AsyncSession, draft: ColumnDescriptionDraft
) -> tuple[MetadataTable, MetadataColumn]:
    table = await session.get(MetadataTable, draft.table_id)
    column = await session.get(MetadataColumn, draft.column_id)
    if table is None or column is None:
        raise HTTPException(
            status_code=404, detail="the column this draft describes no longer exists"
        )
    return table, column


@router.post(
    "/organizations/{organization_id}/column-description-drafts/generate",
    response_model=ColumnDescriptionDraftGenerateResult,
)
async def generate_column_description_drafts(
    organization_id: UUID,
    body: ColumnDescriptionDraftGenerate,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ColumnDescriptionDraftGenerateResult:
    """Draft descriptions for the columns of the requested tables.

    Two passes, on purpose. The first decides *which* columns would be drafted
    and refuses the whole request when that is over `GENERATE_COLUMN_LIMIT`,
    before anything is written. Slicing silently to the limit -- which the
    table-draft endpoint does, and which its client has to guard against --
    would hand back a subset that looks complete. The second pass composes and
    writes.
    """
    enforce_organization(context, organization_id)
    tables = (
        await session.scalars(
            select(MetadataTable)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.id.in_(body.table_ids),
                MetadataTable.status == "ACTIVE",
            )
            .order_by(MetadataTable.name, MetadataTable.id)
        )
    ).all()
    readable: list[MetadataTable] = []
    for table in tables:
        try:
            await _gate_table(session, context, settings, table)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_403_FORBIDDEN:
                raise
            continue
        readable.append(table)
    tables_skipped = len(body.table_ids) - len(readable)

    # Pass 1: decide which columns, and refuse before writing if too many.
    plan: list[
        tuple[MetadataTable, list[MetadataColumn], dict[UUID, ColumnDocumentationVersion]]
    ] = []
    skipped_open = 0
    skipped_described = 0
    for table in readable:
        columns = list(
            (
                await session.scalars(
                    select(MetadataColumn)
                    .where(MetadataColumn.table_id == table.id, MetadataColumn.status == "ACTIVE")
                    .order_by(MetadataColumn.ordinal_position, MetadataColumn.id)
                )
            ).all()
        )
        if not columns:
            continue
        column_ids = [column.id for column in columns]
        open_ids = set(
            await session.scalars(
                select(ColumnDescriptionDraft.column_id).where(
                    ColumnDescriptionDraft.column_id.in_(column_ids),
                    ColumnDescriptionDraft.status.in_(OPEN_DRAFT_STATUSES),
                )
            )
        )
        descriptions = await current_descriptions_by_column_id(session, column_ids)
        # A retired description is a decision, not a gap: "we looked and chose
        # to say nothing" (see `description_withdrawal`). Drafting over it by
        # default would quietly re-propose what a reviewer retired.
        retired = await withdrawn_column_versions(
            session, [column_id for column_id in column_ids if column_id not in descriptions]
        )
        chosen: list[MetadataColumn] = []
        for column in columns:
            if column.id in open_ids:
                skipped_open += 1
                continue
            if not body.include_described and (column.id in descriptions or column.id in retired):
                skipped_described += 1
                continue
            chosen.append(column)
        if chosen:
            plan.append((table, chosen, descriptions))

    planned = sum(len(chosen) for _, chosen, _ in plan)
    if planned > GENERATE_COLUMN_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(
                f"these tables have {planned} columns to draft, over the "
                f"{GENERATE_COLUMN_LIMIT} one request may create; request fewer tables at a time"
            ),
        )

    # Pass 2: compose and write.
    created: list[tuple[ColumnDescriptionDraft, str, str]] = []
    skipped_duplicate = 0
    below_threshold = 0
    for table, chosen, descriptions in plan:
        evidence_by_column = await gather_table_column_evidence(
            session, table, chosen, descriptions
        )
        rejected = {
            (row[0], row[1])
            for row in (
                await session.execute(
                    select(
                        ColumnDescriptionDraft.column_id,
                        ColumnDescriptionDraft.text_fingerprint,
                    ).where(
                        ColumnDescriptionDraft.column_id.in_([column.id for column in chosen]),
                        ColumnDescriptionDraft.status == "REJECTED",
                    )
                )
            ).all()
        }
        for column in chosen:
            evidence = evidence_by_column[column.id]
            drafted_text = compose_column_draft_text(evidence)
            fingerprint = text_fingerprint(drafted_text)
            if (column.id, fingerprint) in rejected:
                # Negative knowledge: a reviewer already turned down exactly
                # this text for this column.
                skipped_duplicate += 1
                continue
            scores = score_column_evidence(evidence)
            if scores.overall < MINIMUM_EVIDENCE_FOR_REVIEW:
                below_threshold += 1
            draft = ColumnDescriptionDraft(
                organization_id=organization_id,
                table_id=table.id,
                column_id=column.id,
                drafted_text=drafted_text,
                text_fingerprint=fingerprint,
                accuracy_score=scores.accuracy,
                clarity_score=scores.clarity,
                style_score=scores.style,
                completeness_score=scores.completeness,
                overall_score=scores.overall,
                evidence=column_evidence_payload(evidence),
                status="DRAFT",
                base_description_version=evidence.current_description_version,
                created_by=context.principal_id,
            )
            session.add(draft)
            created.append((draft, table.name, column.name))
    try:
        await session.flush()
    except IntegrityError as exc:
        # `uq_column_description_draft_open`: another request opened a draft
        # for one of these columns between pass 1 and this flush.
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "another request drafted some of these columns at the same time; "
                "reload and try again"
            ),
        ) from exc

    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="column_description.draft.generate",
        resource_type="column_description_draft",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "requested_tables": len(body.table_ids),
            "tables_skipped": tables_skipped,
            "drafts_created": len(created),
            "skipped_open": skipped_open,
            "skipped_described": skipped_described,
            "skipped_duplicate_rejected": skipped_duplicate,
            "below_review_threshold": below_threshold,
            "include_described": body.include_described,
        },
    )
    await session.commit()
    return ColumnDescriptionDraftGenerateResult(
        drafts=[_draft_read(draft, table_name, column_name) for draft, table_name, column_name in created],
        created=len(created),
        skipped_open=skipped_open,
        skipped_described=skipped_described,
        skipped_duplicate_rejected=skipped_duplicate,
        below_review_threshold=below_threshold,
        tables_skipped=tables_skipped,
    )


@router.get(
    "/organizations/{organization_id}/column-description-drafts",
    response_model=Page,
)
async def list_column_description_drafts(
    organization_id: UUID,
    draft_status: str | None = Query(default=None, alias="status", max_length=30),
    table_id: UUID | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*TABLE_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page:
    """One table's drafts (gated), or -- for stewards and reviewers -- all of them.

    One table's drafts come back in column order, which is how a steward reads
    a table. The organization-wide list comes back highest-evidence first,
    which is how a reviewer triages; that ordering is the score's *only*
    effect, exactly as for table drafts.
    """
    enforce_organization(context, organization_id)
    filters = [ColumnDescriptionDraft.organization_id == organization_id]
    if table_id is None:
        if context.roles.isdisjoint(ORGANIZATION_LIST_ROLES):
            raise HTTPException(
                status_code=403,
                detail=(
                    "listing every table's drafts needs a steward or reviewer role; "
                    "pass table_id to list one table's drafts"
                ),
            )
    else:
        table = await session.get(MetadataTable, table_id)
        if table is None or table.organization_id != organization_id:
            raise HTTPException(status_code=404, detail="table not found")
        await _gate_table(session, context, settings, table)
        filters.append(ColumnDescriptionDraft.table_id == table_id)
    if draft_status:
        filters.append(ColumnDescriptionDraft.status == draft_status.upper())

    base = (
        select(
            ColumnDescriptionDraft,
            MetadataTable.name.label("table_name"),
            MetadataColumn.name.label("column_name"),
        )
        .join(MetadataTable, MetadataTable.id == ColumnDescriptionDraft.table_id)
        .join(MetadataColumn, MetadataColumn.id == ColumnDescriptionDraft.column_id)
        .where(*filters)
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    ordering = (
        (MetadataColumn.ordinal_position, ColumnDescriptionDraft.created_at.desc())
        if table_id is not None
        else (ColumnDescriptionDraft.overall_score.desc(), ColumnDescriptionDraft.created_at.desc())
    )
    rows = (await session.execute(base.order_by(*ordering).limit(limit).offset(offset))).all()
    return Page(
        items=[_draft_read(draft, table_name, column_name) for draft, table_name, column_name in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.put(
    "/column-description-drafts/{draft_id}",
    response_model=ColumnDescriptionDraftRead,
)
async def edit_column_description_draft(
    draft_id: UUID,
    body: ColumnDescriptionDraftEdit,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ColumnDescriptionDraftRead:
    """Fix a draft's text before it is submitted.

    The editor is recorded, and `semantic_api._decide_column_description_draft`
    refuses anyone so recorded as the draft's approver: editing is authorship.
    The score is left alone -- it measures the catalog evidence the draft rests
    on, and rewording the prose does not change that evidence.
    """
    draft = await session.scalar(
        select(ColumnDescriptionDraft).where(ColumnDescriptionDraft.id == draft_id).with_for_update()
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="column description draft not found")
    enforce_organization(context, draft.organization_id)
    table, column = await _table_and_column(session, draft)
    await _gate_table(session, context, settings, table)
    if draft.status != "DRAFT" or draft.drafted_text != body.expected_text:
        raise HTTPException(
            status_code=409, detail="Draft changed or is already in review; reload before editing"
        )
    evidence = dict(draft.evidence or {})
    draft.evidence = {
        **evidence,
        "origin": "METADATA_WITH_HUMAN_EDITS",
        "original_fingerprint": evidence.get("original_fingerprint", draft.text_fingerprint),
        "edited_by": context.principal_id,
        "editors": sorted(set(evidence.get("editors", [])) | {context.principal_id}),
    }
    draft.drafted_text = body.drafted_text
    draft.text_fingerprint = text_fingerprint(body.drafted_text)
    record_audit(
        session,
        replace(context, organization_id=draft.organization_id),
        action="column_description.draft.edit",
        resource_type="column_description_draft",
        resource_id=str(draft.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"fingerprint": draft.text_fingerprint, "column_id": str(draft.column_id)},
    )
    await session.commit()
    return _draft_read(draft, table.name, column.name)


async def _submit_draft(
    session: AsyncSession, context: SecurityContext, draft: ColumnDescriptionDraft
) -> GovernanceReview:
    """Move one DRAFT into the shared review queue.

    The minimum-evidence gate is the same one table drafts pass
    (`asset_description_service.ensure_reviewable`, one threshold for both), so
    a near-empty draft can never reach a state that could be mistaken for a
    published description.
    """
    ensure_reviewable(draft.overall_score)
    draft.status = "PENDING_APPROVAL"
    review = GovernanceReview(
        organization_id=draft.organization_id,
        object_type=COLUMN_DESCRIPTION_DRAFT_OBJECT_TYPE,
        object_id=str(draft.id),
        requested_action="PUBLISH",
        requested_by=context.principal_id,
    )
    session.add(review)
    await session.flush()
    draft.governance_review_id = review.id
    record_audit(
        session,
        replace(context, organization_id=draft.organization_id),
        action="column_description.draft.submit",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "draft_id": str(draft.id),
            "column_id": str(draft.column_id),
            "overall_score": draft.overall_score,
        },
    )
    record_outbox(
        session,
        organization_id=draft.organization_id,
        aggregate_type="governance_review",
        aggregate_id=str(review.id),
        event_type="governance.review_requested.v1",
        payload={
            "review_id": str(review.id),
            "object_type": review.object_type,
            "object_id": str(draft.id),
            "overall_score": draft.overall_score,
        },
    )
    return review


@router.post(
    "/column-description-drafts/{draft_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_column_description_draft(
    draft_id: UUID,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GovernanceReview:
    draft = await session.get(ColumnDescriptionDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="column description draft not found")
    enforce_organization(context, draft.organization_id)
    table, _ = await _table_and_column(session, draft)
    await _gate_table(session, context, settings, table)
    if draft.status == "PENDING_APPROVAL":
        existing = await session.scalar(
            select(GovernanceReview).where(
                GovernanceReview.object_type == COLUMN_DESCRIPTION_DRAFT_OBJECT_TYPE,
                GovernanceReview.object_id == str(draft.id),
                GovernanceReview.status == "PENDING",
            )
        )
        if existing is not None:
            return existing
    if draft.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only a draft can be submitted for review")
    review = await _submit_draft(session, context, draft)
    await session.commit()
    return review


@router.post(
    "/tables/{table_id}/column-description-drafts/submit",
    response_model=ColumnDescriptionDraftBulkSubmitResult,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_table_column_description_drafts(
    table_id: UUID,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ColumnDescriptionDraftBulkSubmitResult:
    """Submit every reviewable DRAFT for one table.

    Still one review per draft, not one for the table: a reviewer may approve
    `customer_id` and reject `amt_ccy`, and a single decision for both would
    force them to accept or refuse text they had judged separately. Volume is
    handled where it already is -- the queue's bulk decision and sampling
    review -- not by merging decisions here.
    """
    table = await session.get(MetadataTable, table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table not found")
    enforce_organization(context, table.organization_id)
    await _gate_table(session, context, settings, table)
    drafts = (
        await session.scalars(
            select(ColumnDescriptionDraft)
            .where(
                ColumnDescriptionDraft.organization_id == table.organization_id,
                ColumnDescriptionDraft.table_id == table.id,
                ColumnDescriptionDraft.status == "DRAFT",
            )
            .order_by(ColumnDescriptionDraft.created_at, ColumnDescriptionDraft.id)
        )
    ).all()
    submitted: list[UUID] = []
    skipped = 0
    for draft in drafts:
        if draft.overall_score < MINIMUM_EVIDENCE_FOR_REVIEW:
            skipped += 1
            continue
        review = await _submit_draft(session, context, draft)
        submitted.append(review.id)
    await session.commit()
    return ColumnDescriptionDraftBulkSubmitResult(
        submitted_review_ids=submitted, skipped_below_threshold=skipped
    )
