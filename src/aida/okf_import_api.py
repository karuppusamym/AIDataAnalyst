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
author every family import writes into -- workbook descriptions and ontology drafts -- and
beyond the role, the caller must be able to read the bundle exactly as the OKF read routes
require (`aida.okf_store.read_published_bundle`).
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
from aida.schemas import ApiModel
from aida.security import SecurityContext, require_roles

router = APIRouter(prefix="/v1", tags=["okf-import"])

#: Who may import: the roles that may author both families import writes into (a workbook's
#: descriptions and an ontology draft). A role is necessary and never sufficient -- the bundle
#: read's own scope, consumer-role and per-datasource checks all still apply.
OKF_IMPORT_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataSteward")

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


class OkfImportRead(ApiModel):
    preview: OkfImportPreviewRead
    #: Pending description batches, one per datasource, each under an OKF_IMPORT_BATCH review.
    description_batches: list[OkfImportBatchRead]
    #: Pending ontology versions, each under its ONTOLOGY_VERSION review.
    meaning_versions: list[OkfImportMeaningRead]


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
    """The raw body, refused on its declared size before it is read into memory."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_ARCHIVE_BYTES:
        raise HTTPException(
            status_code=413,
            detail={"reason_code": ARCHIVE_TOO_LARGE, "detail": "the archive is too large"},
        )
    content = await request.body()
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
