"""R11-FP08: routine description drafts -- generate, list, read, edit, submit.

The routine-level sibling of `asset_description_api` (GL-9) and
`column_description_api`, route for route. The drafting itself -- evidence,
composition, scoring, the body-state vocabulary -- is
`aida.routine_description_service`; this module is the tenant-scoped, audited
API around it.

There is no approve endpoint here, on purpose, and for exactly the reason the
two sibling modules have none: a draft reaches `RoutineDocumentationVersion`
only through an independent decision on its `GovernanceReview`
(`POST /v1/governance/reviews/{review_id}/decision`, which dispatches to
`semantic_api._decide_routine_description_draft`). A direct publish endpoint
would be a way around the one gate that makes the content trustworthy.

**Authorization is per datasource, not per table.** Every other description
endpoint gates `resource_type="table"`, because every other subject is a table
or hangs off one. A routine hangs off a schema and has no table, so this
follows the precedent that already answered that question for a routine
subject: `ontology_api._authorize_mapping_reads` gates a `ROUTINE` mapping on
`resource_type="datasource"`. See `_gate_routine` below.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.envelope_models import MetadataRoutine, RoutineDescriptionDraft
from aida.events import record_audit, record_outbox
from aida.models import GovernanceReview, MetadataCatalog, MetadataSchema
from aida.routine_description_service import (
    GENERATE_ROUTINE_LIMIT,
    OPEN_DRAFT_STATUSES,
    ORIGIN_METADATA,
    ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE,
    RoutineEvidence,
    compose_routine_draft_text,
    current_routine_descriptions,
    ensure_routines_are_describable,
    gather_routine_evidence,
    latest_withdrawn_routine_version,
    resolve_routine_description,
    routine_evidence_payload,
    routine_refusal_reason,
    score_routine_evidence,
)
from aida.schemas import (
    GovernanceReviewRead,
    Page,
    RoutineDescriptionDraftEdit,
    RoutineDescriptionDraftGenerate,
    RoutineDescriptionDraftGenerateResult,
    RoutineDescriptionDraftRead,
    RoutineDescriptionRead,
)
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["routine-description-drafting"])

#: Who may generate, edit and submit drafts: `asset_description_api`'s write
#: population, unchanged. The same people draft a table's description, its
#: columns' and its procedures'.
WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "SemanticAdmin", "DataSteward")

#: Who may read one routine's drafts. Every such read is also gated per
#: datasource through `gate_read`, so a role here is necessary, never sufficient.
READ_ROLES = (
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

#: Who may list drafts across a whole organization. Narrower than a per-routine
#: read on purpose, exactly as in `column_description_api`: an organization-wide
#: page spans every datasource, and the per-datasource read gate (ADR-0017) is a
#: check such a page cannot run row by row. Anyone else lists one datasource at
#: a time, gated.
ORGANIZATION_LIST_ROLES = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "SemanticAdmin",
    "DataSteward",
    "Reviewer",
    "Auditor",
)


async def _gate_routine(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    routine: MetadataRoutine,
) -> None:
    """The routine read gate, against the resource routine access is expressed on.

    `resource_type="datasource"`, following `ontology_api`'s `ROUTINE` branch. A
    caller who cannot read a datasource's routines through
    `procedure_lineage_api` must not be able to read -- or have drafted -- text
    composed from one here.
    """
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="datasource",
        resource_id=str(routine.datasource_id),
        datasource_id=routine.datasource_id,
    )


def _draft_read(
    draft: RoutineDescriptionDraft, qualified_name: str, routine_type: str
) -> RoutineDescriptionDraftRead:
    return RoutineDescriptionDraftRead(
        id=draft.id,
        organization_id=draft.organization_id,
        datasource_id=draft.datasource_id,
        routine_id=draft.routine_id,
        routine_qualified_name=qualified_name,
        routine_type=routine_type,
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

    `column_description_api._edited_origin`, verbatim in behaviour: the origin's
    first half survives, so a reviewer still learns that something other than a
    person composed the draft they are reading.
    """
    base = str(evidence.get("origin") or ORIGIN_METADATA).removesuffix("_WITH_HUMAN_EDITS")
    return f"{base}_WITH_HUMAN_EDITS"


async def _routine_or_404(
    session: AsyncSession, draft: RoutineDescriptionDraft
) -> MetadataRoutine:
    routine = await session.get(MetadataRoutine, draft.routine_id)
    if routine is None:
        raise HTTPException(
            status_code=404, detail="the routine this draft describes no longer exists"
        )
    return routine


@dataclass(frozen=True, slots=True)
class _PlannedDraft:
    routine: MetadataRoutine
    evidence: RoutineEvidence
    drafted_text: str
    fingerprint: str
    scores: ConfidenceBreakdown


@router.post(
    "/organizations/{organization_id}/routine-description-drafts/generate",
    response_model=RoutineDescriptionDraftGenerateResult,
)
async def generate_routine_description_drafts(
    organization_id: UUID,
    body: RoutineDescriptionDraftGenerate,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RoutineDescriptionDraftGenerateResult:
    """Draft descriptions for the requested routines.

    **A package in the request refuses the whole request, by name.** Refused
    rather than skipped, and refused rather than sliced: a steward who names a
    package and gets back a result missing it has learned nothing about why,
    and `column_description_api`'s own "refused, not sliced" rule says a
    silently reduced result looks complete. The refusal names the ids and the
    reason (`ensure_routine_is_describable`), so the client can drop them and
    ask again. Everything else that produces no draft has its own counter for
    the same reason -- a result that only says "0 created" is indistinguishable
    from a broken request.
    """
    enforce_organization(context, organization_id)
    routines = (
        await session.scalars(
            select(MetadataRoutine)
            .where(
                MetadataRoutine.organization_id == organization_id,
                MetadataRoutine.id.in_(body.routine_ids),
                MetadataRoutine.status == "ACTIVE",
            )
            .order_by(MetadataRoutine.name, MetadataRoutine.id)
        )
    ).all()
    ensure_routines_are_describable(routines)
    readable: list[MetadataRoutine] = []
    for routine in routines:
        try:
            await _gate_routine(session, context, settings, routine)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_403_FORBIDDEN:
                raise
            continue
        readable.append(routine)
    routines_skipped = len(body.routine_ids) - len(readable)

    routine_ids = [routine.id for routine in readable]
    open_drafts = set(
        await session.scalars(
            select(RoutineDescriptionDraft.routine_id).where(
                RoutineDescriptionDraft.organization_id == organization_id,
                RoutineDescriptionDraft.routine_id.in_(routine_ids),
                RoutineDescriptionDraft.status.in_(OPEN_DRAFT_STATUSES),
            )
        )
    )
    described = await current_routine_descriptions(session, routine_ids)

    planned: list[_PlannedDraft] = []
    skipped_open = 0
    skipped_described = 0
    skipped_duplicate = 0
    for routine in readable:
        if routine.id in open_drafts:
            skipped_open += 1
            continue
        if not body.include_described:
            # A retired description is a decision, not a gap: "we looked and
            # chose to say nothing" (`description_withdrawal`). Drafting over it
            # by default would quietly re-propose what a reviewer retired -- the
            # same default the column endpoint applies.
            if routine.id in described:
                skipped_described += 1
                continue
            if await latest_withdrawn_routine_version(session, routine.id) is not None:
                skipped_described += 1
                continue
        evidence = await gather_routine_evidence(session, routine)
        drafted_text = compose_routine_draft_text(evidence)
        payload = routine_evidence_payload(evidence)
        if (
            await routine_refusal_reason(
                session, routine.id, drafted_text=drafted_text, payload=payload
            )
            is not None
        ):
            # R11-FP10: this text, the machine text it was edited from, or the
            # evidence it stands on was already refused -- or the same words
            # were approved once and then withdrawn.
            skipped_duplicate += 1
            continue
        planned.append(
            _PlannedDraft(
                routine=routine,
                evidence=evidence,
                drafted_text=drafted_text,
                fingerprint=text_fingerprint(drafted_text),
                scores=score_routine_evidence(evidence),
            )
        )

    if len(planned) > GENERATE_ROUTINE_LIMIT:
        raise HTTPException(
            status_code=422,
            detail=(
                f"this request would draft {len(planned)} routines, over the "
                f"{GENERATE_ROUTINE_LIMIT} one request may create; request fewer at a time"
            ),
        )

    created: list[tuple[RoutineDescriptionDraft, str, str]] = []
    below_threshold = 0
    for item in planned:
        if item.scores.overall < MINIMUM_EVIDENCE_FOR_REVIEW:
            below_threshold += 1
        draft = RoutineDescriptionDraft(
            organization_id=organization_id,
            datasource_id=item.routine.datasource_id,
            routine_id=item.routine.id,
            drafted_text=item.drafted_text,
            text_fingerprint=item.fingerprint,
            accuracy_score=item.scores.accuracy,
            clarity_score=item.scores.clarity,
            style_score=item.scores.style,
            completeness_score=item.scores.completeness,
            overall_score=item.scores.overall,
            evidence={
                **routine_evidence_payload(item.evidence),
                "origin": ORIGIN_METADATA,
            },
            status="DRAFT",
            base_description_version=item.evidence.current_description_version,
            created_by=context.principal_id,
        )
        session.add(draft)
        created.append(
            (draft, item.evidence.qualified_name, item.evidence.routine_type)
        )
    try:
        await session.flush()
    except IntegrityError as exc:
        # `uq_routine_description_draft_open`: another request opened a draft for
        # one of these routines between the planning pass and this flush.
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "another request drafted some of these routines at the same time; "
                "reload and try again"
            ),
        ) from exc

    record_audit(
        session,
        replace(context, organization_id=organization_id),
        action="routine_description.draft.generate",
        resource_type="routine_description_draft",
        resource_id=str(organization_id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "requested": len(body.routine_ids),
            "drafts_created": len(created),
            "skipped_open": skipped_open,
            "skipped_described": skipped_described,
            "skipped_duplicate_rejected": skipped_duplicate,
            "below_review_threshold": below_threshold,
            "routines_skipped": routines_skipped,
            "include_described": body.include_described,
        },
    )
    await session.commit()
    return RoutineDescriptionDraftGenerateResult(
        drafts=[
            _draft_read(draft, qualified_name, routine_type)
            for draft, qualified_name, routine_type in created
        ],
        created=len(created),
        skipped_open=skipped_open,
        skipped_described=skipped_described,
        skipped_duplicate_rejected=skipped_duplicate,
        below_review_threshold=below_threshold,
        routines_skipped=routines_skipped,
    )


async def _qualified_names(
    session: AsyncSession, routine_ids: list[UUID]
) -> dict[UUID, tuple[str, str]]:
    """`catalog.schema.routine` and the routine kind, per id, in one read."""
    if not routine_ids:
        return {}
    rows = (
        await session.execute(
            select(
                MetadataRoutine.id,
                MetadataRoutine.name,
                MetadataRoutine.routine_type,
                MetadataSchema.name,
                MetadataCatalog.name,
            )
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(MetadataRoutine.id.in_(routine_ids))
        )
    ).all()
    return {
        row[0]: (f"{row[4]}.{row[3]}.{row[1]}", str(row[2])) for row in rows
    }


@router.get(
    "/organizations/{organization_id}/routine-description-drafts",
    response_model=Page,
)
async def list_routine_description_drafts(
    organization_id: UUID,
    draft_status: str | None = Query(default=None, alias="status", max_length=30),
    datasource_id: UUID | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page:
    """One datasource's drafts (gated), or -- for stewards and reviewers -- all of them.

    Highest-evidence first, which is how a reviewer triages; that ordering is
    the score's *only* effect, exactly as for table and column drafts.
    """
    enforce_organization(context, organization_id)
    filters = [RoutineDescriptionDraft.organization_id == organization_id]
    if datasource_id is None:
        if context.roles.isdisjoint(ORGANIZATION_LIST_ROLES):
            raise HTTPException(
                status_code=403,
                detail=(
                    "listing every datasource's drafts needs a steward or reviewer role; "
                    "pass datasource_id to list one datasource's drafts"
                ),
            )
    else:
        await gate_read(
            session,
            context,
            settings,
            action="READ_METADATA",
            resource_type="datasource",
            resource_id=str(datasource_id),
            datasource_id=datasource_id,
        )
        filters.append(RoutineDescriptionDraft.datasource_id == datasource_id)
    if draft_status:
        filters.append(RoutineDescriptionDraft.status == draft_status.upper())

    base = select(RoutineDescriptionDraft).where(*filters)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = list(
        (
            await session.scalars(
                base.order_by(
                    RoutineDescriptionDraft.overall_score.desc(),
                    RoutineDescriptionDraft.created_at.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    names = await _qualified_names(session, [draft.routine_id for draft in rows])
    return Page(
        items=[
            _draft_read(draft, *names.get(draft.routine_id, (str(draft.routine_id), "")))
            for draft in rows
        ],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


@router.get("/routines/{routine_id}/description", response_model=RoutineDescriptionRead)
async def get_routine_description(
    routine_id: UUID,
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RoutineDescriptionRead:
    """The one description a routine detail read should show, and its standing.

    R11-FP08: the catalog precedence chain, for a routine -- approved version,
    then a pending draft flagged as proposed, then the source system's own
    comment. Resolved in `routine_description_service` rather than here so the
    MCP and compilation surfaces answer from the same rungs in the same order
    (`atlas.modules.catalog.service._description` is the precedent, and the
    reason the rungs are in that order).
    """
    routine = await session.get(MetadataRoutine, routine_id)
    if routine is None:
        raise HTTPException(status_code=404, detail="routine not found")
    enforce_organization(context, routine.organization_id)
    await _gate_routine(session, context, settings, routine)
    resolved = await resolve_routine_description(session, routine)
    names = await _qualified_names(session, [routine.id])
    qualified_name, _kind = names.get(routine.id, (routine.name, routine.routine_type))
    return RoutineDescriptionRead(
        routine_id=routine.id,
        routine_qualified_name=qualified_name,
        description=resolved.text,
        description_is_proposed=resolved.is_proposed,
        documentation_withdrawn=resolved.is_withdrawn,
        description_is_source_comment=resolved.is_source_comment,
    )


@router.put(
    "/routine-description-drafts/{draft_id}",
    response_model=RoutineDescriptionDraftRead,
)
async def edit_routine_description_draft(
    draft_id: UUID,
    body: RoutineDescriptionDraftEdit,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RoutineDescriptionDraftRead:
    """Fix a draft's text before it is submitted.

    The editor is recorded as `edited_by` and appended to `editors`, and
    `semantic_api._decide_routine_description_draft` refuses anyone so recorded
    as the draft's approver: editing is authorship. The two halves are one
    mechanism -- the stamp here is what the guard there reads -- so neither is
    optional.

    The score is left alone: it measures the catalog evidence the draft rests
    on, and rewording the prose does not change that evidence.
    """
    draft = await session.scalar(
        select(RoutineDescriptionDraft)
        .where(RoutineDescriptionDraft.id == draft_id)
        .with_for_update()
    )
    if draft is None:
        raise HTTPException(status_code=404, detail="routine description draft not found")
    enforce_organization(context, draft.organization_id)
    routine = await _routine_or_404(session, draft)
    await _gate_routine(session, context, settings, routine)
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
        action="routine_description.draft.edit",
        resource_type="routine_description_draft",
        resource_id=str(draft.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"fingerprint": draft.text_fingerprint, "routine_id": str(draft.routine_id)},
    )
    names = await _qualified_names(session, [draft.routine_id])
    await session.commit()
    return _draft_read(
        draft, *names.get(draft.routine_id, (routine.name, routine.routine_type))
    )


@router.post(
    "/routine-description-drafts/{draft_id}/submit",
    response_model=GovernanceReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_routine_description_draft(
    draft_id: UUID,
    context: SecurityContext = Depends(require_roles(*WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> GovernanceReview:
    """Move one DRAFT into the shared review queue.

    The minimum-evidence gate is the same one table and column drafts pass
    (`asset_description_service.ensure_reviewable`, one threshold for all
    three), so a near-empty routine draft can never reach a state that could be
    mistaken for a published description.
    """
    draft = await session.get(RoutineDescriptionDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="routine description draft not found")
    enforce_organization(context, draft.organization_id)
    routine = await _routine_or_404(session, draft)
    await _gate_routine(session, context, settings, routine)
    if draft.status == "PENDING_APPROVAL":
        existing = await session.scalar(
            select(GovernanceReview).where(
                GovernanceReview.object_type == ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE,
                GovernanceReview.object_id == str(draft.id),
                GovernanceReview.status == "PENDING",
            )
        )
        if existing is not None:
            return existing
    if draft.status != "DRAFT":
        raise HTTPException(status_code=409, detail="only a draft can be submitted for review")
    ensure_reviewable(draft.overall_score)
    draft.status = "PENDING_APPROVAL"
    review = GovernanceReview(
        organization_id=draft.organization_id,
        object_type=ROUTINE_DESCRIPTION_DRAFT_OBJECT_TYPE,
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
        action="routine_description.draft.submit",
        resource_type="governance_review",
        resource_id=str(review.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "draft_id": str(draft.id),
            "routine_id": str(draft.routine_id),
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
    await session.commit()
    return review


__all__ = ["router"]
