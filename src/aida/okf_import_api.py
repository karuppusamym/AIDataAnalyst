"""R11-OKF03: the two REST doors onto OKF import -- preview, then apply. Disabled by default.

    POST /v1/context-product-versions/{version_id}/okf-bundle/imports/preview
    POST /v1/context-product-versions/{version_id}/okf-bundle/imports?preview_digest=...

Both take the edited bundle archive as the raw request body, with the filename as a query
parameter -- the shape the workbook upload uses (`model_import_api`), for the same reason: no
multipart dependency, and a browser can send a `File` straight through.

**Two steps, deliberately.** A preview applies nothing: it says what each edit would propose, in
which existing family, and what it would not -- conflicts, refusals, unsupported and derived
changes, claims -- so a steward who uploaded the wrong file can walk away. An apply raises
proposals only when it reproduces the preview the caller accepted (`preview_digest`), and even
then only *proposals*: pending reviews a different principal decides in the ordinary queue.
There is no approve step here and no shortcut to one.

**Off until accepted.** `Settings.okf_import_enabled` ships `False`; both routes refuse with
`OKF_IMPORT_DISABLED` before reading the body or any row. Roles are the population that may
author every family import writes into -- workbook descriptions, ontology drafts and routine
description drafts -- and beyond the role, the caller must be able to read the bundle exactly as
the OKF read routes require (`aida.okf_store.read_published_bundle`).

**The reviewer's read.** What an import raised is decided in the ordinary review queue, and
`GET /v1/governance/reviews/{review_id}/okf-import-preview` is what that queue shows a reviewer
of an `OKF_IMPORT_BATCH` or `OKF_IMPORT_ROUTINE_DESCRIPTION` review (`aida.okf_import_review`).
It is not behind `okf_import_enabled`: a deployment that switches import off must still let a
reviewer read -- and so decide -- the imports already waiting in its queue.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings, get_settings
from aida.db import get_session
from aida.okf_import import (
    OkfImportApplied,
    OkfImportPreview,
    apply_okf_import,
    preview_audit_details,
    preview_okf_import,
    record_import_audit,
)
from aida.okf_import_bundle import (
    ARCHIVE_TOO_LARGE,
    MAX_ARCHIVE_BYTES,
    OKF_IMPORT_DISABLED,
    OkfImportRefused,
)
from aida.okf_import_review import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    OkfReviewChange,
    OkfReviewPreview,
    read_okf_import_review,
)
from aida.request_body import read_body_within
from aida.schemas import ApiModel
from aida.security import SecurityContext, require_roles

router = APIRouter(prefix="/v1", tags=["okf-import"])

#: Who may import: the roles that may author every family import writes into (a workbook's
#: descriptions, an ontology draft and a routine description draft). A role is necessary and
#: never sufficient -- the bundle read's own scope, consumer-role and per-datasource checks all
#: still apply.
OKF_IMPORT_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataSteward")
#: Who may read an import review's full preview: exactly the roles of the generic review diff
#: (`semantic_api.get_governance_review_diff`), so this view widens nobody's reach.
OKF_IMPORT_REVIEW_ROLES = ("PlatformAdmin", "SemanticAdmin", "DataSteward", "Reviewer")

_FILENAME = Query(min_length=1, max_length=255, description="The uploaded file's name.")


class OkfImportBaseRead(ApiModel):
    publication_id: UUID
    sequence: int
    is_current: bool
    bundle_content_digest: str
    content_snapshot_digest: str


class OkfImportItemRead(ApiModel):
    """One edit and its decision. `current_value`/`proposed_value` are for the importing user's
    review of the preview; nothing here is persisted except by an apply, as a pending proposal."""

    item_id: str
    path: str
    family: str
    kind: str
    field: str
    outcome: str
    reason_code: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    target_label: str | None = None
    datasource_id: UUID | None = None
    ontology_key: str | None = None
    concept_key: str | None = None
    expected_version: int | None = None
    current_version: int | None = None
    current_value: str | None = None
    proposed_value: str | None = None
    added_aliases: list[str]
    detail: str | None = None


class OkfImportNoteRead(ApiModel):
    """A change that is not proposed: refused, unsupported, a claim, tolerated or ignored."""

    path: str | None = None
    outcome: str
    reason_code: str
    field: str | None = None
    detail: str | None = None


class OkfImportPreviewRead(ApiModel):
    context_product_version_id: UUID
    import_enabled: bool
    archive_sha256: str
    #: Pass this to the apply route to raise exactly these proposals.
    preview_digest: str
    base_publication: OkfImportBaseRead
    counts: dict[str, int]
    items: list[OkfImportItemRead]
    notes: list[OkfImportNoteRead]
    #: What an apply does and does not do, stated with the preview it applies to.
    authority: str


class OkfImportBatchRead(ApiModel):
    batch_id: UUID
    datasource_id: UUID
    governance_review_id: UUID
    change_count: int


class OkfImportMeaningRead(ApiModel):
    ontology_version_id: UUID
    ontology_key: str
    version: int
    base_version: int
    governance_review_id: UUID
    concept_count: int


class OkfImportRoutineRead(ApiModel):
    draft_id: UUID
    routine_id: UUID
    datasource_id: UUID
    governance_review_id: UUID


class OkfImportRead(ApiModel):
    preview: OkfImportPreviewRead
    #: Pending description batches, one per datasource, each under an OKF_IMPORT_BATCH review.
    description_batches: list[OkfImportBatchRead]
    #: Pending ontology versions, each under its ONTOLOGY_VERSION review.
    meaning_versions: list[OkfImportMeaningRead]
    #: Pending routine description drafts, each under an OKF_IMPORT_ROUTINE_DESCRIPTION review.
    routine_drafts: list[OkfImportRoutineRead]


class OkfImportReviewChangeRead(ApiModel):
    """One proposed change and what approving it will do, predicted from the approval's own
    comparisons. `state` is APPLIES, CONFLICT, TARGET_UNAVAILABLE or DECIDED."""

    change_id: str
    subject_type: str
    subject_id: str
    field: str
    label: str
    #: What the proposal replaces, as the import recorded it. Released as export screening would.
    before_value: str | None = None
    proposed_value: str | None = None
    expected_version: int | None = None
    current_version: int | None = None
    #: The approved text now, only when it is not what the import replaces.
    current_value: str | None = None
    status: str
    skip_reason: str | None = None
    state: str
    reason_code: str | None = None
    target_active: bool | None = None
    approval_effect: str


class OkfImportReviewDocumentRead(ApiModel):
    """The changes about one object: a table (with its columns) or a routine."""

    document_id: str
    label: str
    object_type: str
    conflicts: int
    changes: list[OkfImportReviewChangeRead]


class OkfImportReviewRead(ApiModel):
    review_id: UUID
    object_type: str
    object_id: str
    review_status: str
    requested_by: str
    #: The batch's or draft's own status (PENDING_REVIEW / PENDING_APPROVAL until decided).
    proposal_status: str
    datasource_id: UUID
    filename: str | None = None
    archive_sha256: str | None = None
    #: Over every document, not only this page.
    counts: dict[str, int]
    offset: int
    limit: int
    total_documents: int
    documents: list[OkfImportReviewDocumentRead]
    authority: str


_AUTHORITY = (
    "Nothing in this bundle is applied by importing it. An apply raises pending proposals in "
    "the existing description and ontology families; each is published only when a principal "
    "other than the importer approves its review. Verification or status values in the file "
    "are claims and grant nothing."
)


def _require_enabled(settings: Settings) -> None:
    if not settings.okf_import_enabled:
        raise HTTPException(
            status_code=403,
            detail={
                "reason_code": OKF_IMPORT_DISABLED,
                "detail": "OKF import is disabled on this deployment (okf_import_enabled)",
            },
        )


async def _archive(request: Request) -> bytes:
    """The raw body, refused as soon as it is known to pass `MAX_ARCHIVE_BYTES` (R11-AUD11).

    On its declared length before any of it is read, and otherwise -- a chunked body declares none
    -- as soon as what has arrived passes the limit, so an over-limit upload is never buffered
    past the limit plus one chunk.
    """
    content = await read_body_within(
        request,
        MAX_ARCHIVE_BYTES,
        detail={"reason_code": ARCHIVE_TOO_LARGE, "detail": "the archive is too large"},
    )
    if not content:
        raise HTTPException(status_code=422, detail="the request body is empty")
    return content


def _preview_read(preview: OkfImportPreview) -> OkfImportPreviewRead:
    base = preview.base
    return OkfImportPreviewRead(
        context_product_version_id=preview.stored.version.id,
        import_enabled=True,
        archive_sha256=preview.archive_sha256,
        preview_digest=preview.digest,
        base_publication=OkfImportBaseRead(
            publication_id=base.id,
            sequence=base.sequence,
            is_current=base.id == preview.stored.head.publication_id,
            bundle_content_digest=base.bundle_content_digest,
            content_snapshot_digest=base.content_snapshot_digest,
        ),
        counts=preview.counts(),
        items=[
            OkfImportItemRead(
                item_id=item.item_id,
                path=item.path,
                family=item.family,
                kind=item.kind,
                field=item.field,
                outcome=item.outcome,
                reason_code=item.reason_code,
                target_type=item.target_type,
                target_id=item.target_id,
                target_label=item.target_label,
                datasource_id=item.datasource_id,
                ontology_key=item.ontology_key,
                concept_key=item.concept_key,
                expected_version=item.expected_version,
                current_version=item.current_version,
                current_value=item.current_value,
                proposed_value=item.proposed_value,
                added_aliases=list(item.added_aliases),
                detail=item.detail,
            )
            for item in preview.items
        ],
        notes=[
            OkfImportNoteRead(
                path=note.path,
                outcome=note.outcome,
                reason_code=note.reason_code,
                field=note.field,
                detail=note.detail,
            )
            for note in preview.notes
        ],
        authority=_AUTHORITY,
    )


def _applied_read(applied: OkfImportApplied) -> OkfImportRead:
    return OkfImportRead(
        preview=_preview_read(applied.preview),
        description_batches=[
            OkfImportBatchRead(
                batch_id=batch.batch_id,
                datasource_id=batch.datasource_id,
                governance_review_id=batch.governance_review_id,
                change_count=batch.change_count,
            )
            for batch in applied.batches
        ],
        meaning_versions=[
            OkfImportMeaningRead(
                ontology_version_id=meaning.ontology_version_id,
                ontology_key=meaning.ontology_key,
                version=meaning.version,
                base_version=meaning.base_version,
                governance_review_id=meaning.governance_review_id,
                concept_count=meaning.concept_count,
            )
            for meaning in applied.meaning
        ],
        routine_drafts=[
            OkfImportRoutineRead(
                draft_id=routine.draft_id,
                routine_id=routine.routine_id,
                datasource_id=routine.datasource_id,
                governance_review_id=routine.governance_review_id,
            )
            for routine in applied.routines
        ],
    )


def _change_read(change: OkfReviewChange) -> OkfImportReviewChangeRead:
    return OkfImportReviewChangeRead(
        change_id=change.change_id,
        subject_type=change.subject_type,
        subject_id=change.subject_id,
        field=change.field,
        label=change.label,
        before_value=change.before_value,
        proposed_value=change.proposed_value,
        expected_version=change.expected_version,
        current_version=change.current_version,
        current_value=change.current_value,
        status=change.status,
        skip_reason=change.skip_reason,
        state=change.state,
        reason_code=change.reason_code,
        target_active=change.target_active,
        approval_effect=change.approval_effect,
    )


def _review_read(preview: OkfReviewPreview, *, offset: int, limit: int) -> OkfImportReviewRead:
    review = preview.review
    return OkfImportReviewRead(
        review_id=review.id,
        object_type=review.object_type,
        object_id=review.object_id,
        review_status=review.status,
        requested_by=review.requested_by,
        proposal_status=preview.proposal_status,
        datasource_id=preview.datasource_id,
        filename=preview.filename,
        archive_sha256=preview.archive_sha256,
        counts=preview.counts(),
        offset=offset,
        limit=limit,
        total_documents=len(preview.documents),
        documents=[
            OkfImportReviewDocumentRead(
                document_id=document.document_id,
                label=document.label,
                object_type=document.object_type,
                conflicts=document.conflicts,
                changes=[_change_read(change) for change in document.changes],
            )
            for document in preview.documents[offset : offset + limit]
        ],
        authority=preview.authority,
    )


async def _refusal(
    session: AsyncSession,
    context: SecurityContext,
    version_id: UUID,
    refusal: OkfImportRefused,
    *,
    action: str,
) -> HTTPException:
    """Record a refused import by its reason code alone, then answer with it (INV-6: no member
    name, path or value from the upload reaches the audit record)."""
    await session.rollback()
    record_import_audit(
        session,
        context,
        version_id=version_id,
        action=action,
        outcome="DENIED",
        details={"reason_code": refusal.reason_code},
    )
    await session.commit()
    return HTTPException(
        status_code=refusal.status_code,
        detail={"reason_code": refusal.reason_code, "detail": refusal.message},
    )


@router.post(
    "/context-product-versions/{version_id}/okf-bundle/imports/preview",
    response_model=OkfImportPreviewRead,
)
async def preview_okf_bundle_import(
    version_id: UUID,
    request: Request,
    filename: Annotated[str, _FILENAME] = "okf-bundle.zip",
    context: SecurityContext = Depends(require_roles(*OKF_IMPORT_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OkfImportPreviewRead:
    """What importing this edited bundle would propose, and everything it would not. Applies
    nothing; records an audit of counts and reason codes."""
    _require_enabled(settings)
    content = await _archive(request)
    try:
        preview = await preview_okf_import(
            session, version_id, context, settings, content, filename=filename
        )
    except OkfImportRefused as refusal:
        raise await _refusal(
            session, context, version_id, refusal, action="context_product.okf_import_preview"
        ) from refusal
    record_import_audit(
        session,
        context,
        version_id=version_id,
        action="context_product.okf_import_preview",
        outcome="SUCCESS",
        details=preview_audit_details(preview),
        organization_id=preview.stored.version.organization_id,
    )
    read = _preview_read(preview)
    await session.commit()
    return read


@router.post(
    "/context-product-versions/{version_id}/okf-bundle/imports",
    response_model=OkfImportRead,
    status_code=status.HTTP_201_CREATED,
)
async def apply_okf_bundle_import(
    version_id: UUID,
    request: Request,
    preview_digest: Annotated[
        str,
        Query(
            pattern=r"^[0-9a-f]{64}$",
            description="The `preview_digest` of the preview being accepted.",
        ),
    ],
    filename: Annotated[str, _FILENAME] = "okf-bundle.zip",
    context: SecurityContext = Depends(require_roles(*OKF_IMPORT_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OkfImportRead:
    """Raise the proposals an accepted preview listed, pending review. Publishes nothing."""
    _require_enabled(settings)
    content = await _archive(request)
    try:
        applied = await apply_okf_import(
            session,
            version_id,
            context,
            settings,
            content,
            preview_digest=preview_digest,
            filename=filename,
        )
    except OkfImportRefused as refusal:
        raise await _refusal(
            session, context, version_id, refusal, action="context_product.okf_import_apply"
        ) from refusal
    details = preview_audit_details(applied.preview)
    details["description_batches"] = [str(batch.batch_id) for batch in applied.batches]
    details["meaning_versions"] = [str(item.ontology_version_id) for item in applied.meaning]
    details["routine_drafts"] = [str(item.draft_id) for item in applied.routines]
    record_import_audit(
        session,
        context,
        version_id=version_id,
        action="context_product.okf_import_apply",
        outcome="SUCCESS",
        details=details,
        organization_id=applied.preview.stored.version.organization_id,
    )
    read = _applied_read(applied)
    await session.commit()
    return read


@router.get(
    "/governance/reviews/{review_id}/okf-import-preview",
    response_model=OkfImportReviewRead,
)
async def get_okf_import_review(
    review_id: UUID,
    offset: Annotated[int, Query(ge=0, description="Documents to skip.")] = 0,
    limit: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Documents to return.")
    ] = DEFAULT_PAGE_SIZE,
    context: SecurityContext = Depends(require_roles(*OKF_IMPORT_REVIEW_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> OkfImportReviewRead:
    """What an OKF import review asks a reviewer to approve, document by document: each
    change's before and proposed text, whether its approved description moved since the export,
    and what approving will do with it. Counts cover every document; `documents` is one page.
    Reads only; 404 for a review that is not an OKF import's."""
    preview = await read_okf_import_review(session, review_id, context, settings)
    return _review_read(preview, offset=offset, limit=limit)
