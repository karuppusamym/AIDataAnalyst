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

from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import (
    MINIMUM_EVIDENCE_FOR_REVIEW,
    ConfidenceBreakdown,
    ensure_reviewable,
    text_fingerprint,
)
from aida.authorization_gate import gate_read
from aida.column_description_model import (
    MODEL_DRAFT_TABLE_LIMIT,
    ColumnDraftModelUnavailable,
    ModelDraftResult,
    approved_drafting_route,
    draft_thin_columns,
    model_evidence_payload,
    table_context_for,
)
from aida.column_description_service import (
    COLUMN_DESCRIPTION_DRAFT_OBJECT_TYPE,
    GENERATE_COLUMN_LIMIT,
    OPEN_DRAFT_STATUSES,
    ORIGIN_METADATA,
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
from aida.model_gateway import ApprovedModelRoute, ProviderNeutralModelGateway
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


def _edited_origin(evidence: dict[str, Any]) -> str:
    """Where the text came from, kept through a person's edit.

    A model draft a steward rewords becomes MODEL_INFERRED_WITH_HUMAN_EDITS,
    not a human original: the reviewer still needs to know a model proposed it.
    """
    base = str(evidence.get("origin") or ORIGIN_METADATA).removesuffix("_WITH_HUMAN_EDITS")
    return f"{base}_WITH_HUMAN_EDITS"


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


def get_column_draft_gateway(
    settings: Settings = Depends(get_settings),
) -> ProviderNeutralModelGateway:
    """The governed model gateway, as a dependency.

    Constructed per request like every other gateway user, used only when a
    request asks for `model_assist`, and injectable so tests can hand in a
    provider that answers deterministically.
    """
    return ProviderNeutralModelGateway(settings)


def _replaceable(draft: ColumnDescriptionDraft) -> bool:
    """A thin evidence draft nobody has touched -- the only kind the model may
    replace. An edited draft is a person's words, a submitted one is in
    someone's queue, and a model draft is already the model's answer."""
    evidence = draft.evidence or {}
    return (
        draft.status == "DRAFT"
        and evidence.get("origin", ORIGIN_METADATA) == ORIGIN_METADATA
        and not evidence.get("editors")
        and draft.overall_score < MINIMUM_EVIDENCE_FOR_REVIEW
    )


@dataclass
class _TablePlan:
    table: MetadataTable
    columns: list[MetadataColumn]
    chosen: list[MetadataColumn]
    descriptions: dict[UUID, ColumnDocumentationVersion]
    replaceable: dict[UUID, ColumnDescriptionDraft] = field(default_factory=dict)


@dataclass
class _PlannedDraft:
    column: MetadataColumn
    drafted_text: str
    fingerprint: str
    scores: ConfidenceBreakdown
    overall: float
    evidence: dict[str, Any]
    base_version: int | None
    by_model: bool


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
    gateway: ProviderNeutralModelGateway = Depends(get_column_draft_gateway),
) -> ColumnDescriptionDraftGenerateResult:
    """Draft descriptions for the columns of the requested tables.

    Two passes, on purpose. The first decides *which* columns would be drafted
    and refuses the whole request when that is over `GENERATE_COLUMN_LIMIT`,
    before anything is written. Slicing silently to the limit -- which the
    table-draft endpoint does, and which its client has to guard against --
    would hand back a subset that looks complete. The second pass composes and
    writes.

    With `model_assist`, a column whose catalog evidence is too thin to clear
    the review bar is drafted by the governed model gateway instead
    (`aida.column_description_model`), and a thin evidence draft nobody has
    touched is replaced by the model's. Columns with enough evidence are still
    drafted from it; the model is never asked about them. Whether the model may
    be used at all is settled before anything is read or written, so a refusal
    names its reason and leaves nothing behind. A call that fails part way
    falls back to evidence drafts for the columns it would have covered.
    """
    enforce_organization(context, organization_id)
    route: ApprovedModelRoute | None = None
    if body.model_assist:
        if len(body.table_ids) > MODEL_DRAFT_TABLE_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"model-assisted drafting takes at most {MODEL_DRAFT_TABLE_LIMIT} tables "
                    "per request, because each model call runs inside the request; send fewer"
                ),
            )
        try:
            route = await approved_drafting_route(session, organization_id, settings)
        except ColumnDraftModelUnavailable as exc:
            raise HTTPException(
                status_code=409, detail=f"model drafting is not available: {exc.reason}"
            ) from exc

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
    plans: list[_TablePlan] = []
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
        open_drafts = {
            draft.column_id: draft
            for draft in (
                await session.scalars(
                    select(ColumnDescriptionDraft).where(
                        ColumnDescriptionDraft.column_id.in_(column_ids),
                        ColumnDescriptionDraft.status.in_(OPEN_DRAFT_STATUSES),
                    )
                )
            ).all()
        }
        descriptions = await current_descriptions_by_column_id(session, column_ids)
        # A retired description is a decision, not a gap: "we looked and chose
        # to say nothing" (see `description_withdrawal`). Drafting over it by
        # default would quietly re-propose what a reviewer retired.
        retired = await withdrawn_column_versions(
            session, [column_id for column_id in column_ids if column_id not in descriptions]
        )
        plan = _TablePlan(table=table, columns=columns, chosen=[], descriptions=descriptions)
        for column in columns:
            open_draft = open_drafts.get(column.id)
            if open_draft is not None:
                if route is not None and _replaceable(open_draft):
                    plan.replaceable[column.id] = open_draft
                    plan.chosen.append(column)
                else:
                    skipped_open += 1
                continue
            if not body.include_described and (column.id in descriptions or column.id in retired):
                skipped_described += 1
                continue
            plan.chosen.append(column)
        if plan.chosen:
            plans.append(plan)

    planned = sum(len(plan.chosen) for plan in plans)
    if planned > GENERATE_COLUMN_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(
                f"these tables have {planned} columns to draft, over the "
                f"{GENERATE_COLUMN_LIMIT} one request may create; request fewer tables at a time"
            ),
        )

    # Pass 2: compose -- from evidence, or for thin columns from the model --
    # then write.
    created: list[tuple[ColumnDescriptionDraft, str, str]] = []
    skipped_duplicate = 0
    below_threshold = 0
    model_drafted = 0
    model_fallbacks = 0
    model_withheld = 0
    replaced_thin = 0
    model_note: str | None = None
    model_stopped = False
    for plan in plans:
        table = plan.table
        evidence_by_column = await gather_table_column_evidence(
            session, table, plan.chosen, plan.descriptions
        )
        scored = {
            column.id: score_column_evidence(evidence_by_column[column.id])
            for column in plan.chosen
        }
        model_results: dict[UUID, ModelDraftResult] = {}
        asked_model: set[UUID] = set()
        thin = [
            evidence_by_column[column.id]
            for column in plan.chosen
            if scored[column.id].overall < MINIMUM_EVIDENCE_FOR_REVIEW
        ]
        if route is not None and not model_stopped and thin:
            outcome = await draft_thin_columns(
                session,
                organization_id=organization_id,
                gateway=gateway,
                route=route,
                thin=thin,
                sibling_names=[column.name for column in plan.columns],
                table_context=await table_context_for(session, table.id),
            )
            asked_model = {evidence.column_id for evidence in thin}
            model_results = {result.column_id: result for result in outcome.results}
            model_withheld += outcome.withheld
            model_note = model_note or outcome.note
            model_stopped = outcome.stop

        rejected = {
            (row[0], row[1])
            for row in (
                await session.execute(
                    select(
                        ColumnDescriptionDraft.column_id,
                        ColumnDescriptionDraft.text_fingerprint,
                    ).where(
                        ColumnDescriptionDraft.column_id.in_([column.id for column in plan.chosen]),
                        ColumnDescriptionDraft.status == "REJECTED",
                    )
                )
            ).all()
        }

        planned_drafts: list[_PlannedDraft] = []
        for column in plan.chosen:
            evidence = evidence_by_column[column.id]
            scores = scored[column.id]
            model_result = model_results.get(column.id)
            if model_result is None and column.id in plan.replaceable:
                # The model did not draft it, so the thin draft already open stays.
                if column.id in asked_model:
                    model_fallbacks += 1
                continue
            if model_result is not None:
                drafted_text = model_result.text
                overall = model_result.confidence
                payload = model_evidence_payload(
                    column_evidence_payload(evidence),
                    result=model_result,
                    evidence_score=scores.overall,
                )
            else:
                if column.id in asked_model:
                    model_fallbacks += 1
                drafted_text = compose_column_draft_text(evidence)
                overall = scores.overall
                payload = column_evidence_payload(evidence)
            fingerprint = text_fingerprint(drafted_text)
            if (column.id, fingerprint) in rejected:
                # Negative knowledge: a reviewer already turned down exactly
                # this text for this column.
                skipped_duplicate += 1
                continue
            planned_drafts.append(
                _PlannedDraft(
                    column=column,
                    drafted_text=drafted_text,
                    fingerprint=fingerprint,
                    scores=scores,
                    overall=overall,
                    evidence=payload,
                    base_version=evidence.current_description_version,
                    by_model=model_result is not None,
                )
            )

        # Close the thin drafts being replaced before opening their successors:
        # one open draft per column is a database constraint, not a convention.
        superseding = [
            plan.replaceable[item.column.id]
            for item in planned_drafts
            if item.column.id in plan.replaceable
        ]
        for old in superseding:
            old.status = "SUPERSEDED"
            old.evidence = {
                **(old.evidence or {}),
                "superseded_reason": "replaced by a model-assisted draft",
                "superseded_by": context.principal_id,
            }
        if superseding:
            await session.flush()
            replaced_thin += len(superseding)

        for item in planned_drafts:
            if item.overall < MINIMUM_EVIDENCE_FOR_REVIEW:
                below_threshold += 1
            if item.by_model:
                model_drafted += 1
            draft = ColumnDescriptionDraft(
                organization_id=organization_id,
                table_id=table.id,
                column_id=item.column.id,
                drafted_text=item.drafted_text,
                text_fingerprint=item.fingerprint,
                accuracy_score=item.scores.accuracy,
                clarity_score=item.scores.clarity,
                style_score=item.scores.style,
                completeness_score=item.scores.completeness,
                overall_score=item.overall,
                evidence=item.evidence,
                status="DRAFT",
                base_description_version=item.base_version,
                created_by=context.principal_id,
            )
            session.add(draft)
            created.append((draft, table.name, item.column.name))
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
            "model_assist": body.model_assist,
            "model_route": route.route_key if route is not None else None,
            "model_drafted": model_drafted,
            "model_fallbacks": model_fallbacks,
            "model_withheld": model_withheld,
            "replaced_thin_drafts": replaced_thin,
            "model_note": model_note,
        },
    )
    await session.commit()
    return ColumnDescriptionDraftGenerateResult(
        drafts=[
            _draft_read(draft, table_name, column_name)
            for draft, table_name, column_name in created
        ],
        created=len(created),
        skipped_open=skipped_open,
        skipped_described=skipped_described,
        skipped_duplicate_rejected=skipped_duplicate,
        below_review_threshold=below_threshold,
        tables_skipped=tables_skipped,
        model_drafted=model_drafted,
        model_fallbacks=model_fallbacks,
        model_withheld=model_withheld,
        replaced_thin_drafts=replaced_thin,
        model_note=model_note,
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
        items=[
            _draft_read(draft, table_name, column_name) for draft, table_name, column_name in rows
        ],
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
        select(ColumnDescriptionDraft)
        .where(ColumnDescriptionDraft.id == draft_id)
        .with_for_update()
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
        "origin": _edited_origin(evidence),
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
