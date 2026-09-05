"""Upload an edited model workbook, look at what it would change, submit it.

Three steps, deliberately separate:

    POST /v1/datasources/{id}/model/import   -> parse + diff, writes a DRAFT batch
    GET  /v1/model-imports/{batch_id}/changes -> what it would change, row by row
    POST /v1/model-imports/{batch_id}/submit  -> enters the shared review queue

Upload does not submit. A steward who exports a workbook, edits it, and
uploads the wrong file should be able to see that the diff is nonsense and
abandon it without ever having put 400 spurious changes in front of a
reviewer. Nothing publishes until someone *other than the submitter* approves
the batch's single `GovernanceReview` -- `semantic_api.decide_governance_review`
enforces that, the same way it does for every other governed object.

**Why the raw request body rather than a multipart form.** `python-multipart`
is not a pinned dependency, and this repo's standing constraint (stated in
`asset_evidence_api`'s own export docstring) is not to add one where an
existing, dependency-free shape is honest. The workbook is sent as the raw
request body with the filename as a query parameter -- which is also what
lets a browser send a `File` object straight through without re-encoding it,
where a JSON body would have forced base64 and a third more bytes.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import AuthorizationDenied, gate
from aida.config import Settings, get_settings
from aida.context import get_correlation_id
from aida.db import get_session
from aida.events import record_audit, record_outbox
from aida.model_import import (
    MAX_UPLOAD_BYTES,
    parse_and_diff_workbook,
    set_change_exclusion,
    submit_batch_for_review,
)
from aida.models import DataSource, ModelImportBatch, ModelImportChange
from aida.schemas import ApiModel, Page
from aida.security import SecurityContext, enforce_organization, require_roles

router = APIRouter(prefix="/v1", tags=["model-import"])

# Same population that may upload a data dictionary
# (`document_ingestion_api.DOCUMENT_WRITE_ROLES`): both propose metadata
# changes that a reviewer then decides, and neither publishes anything on its
# own, so they take the same population rather than inventing a second one.
_IMPORT_WRITE_ROLES = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")
_IMPORT_READ_ROLES = (*_IMPORT_WRITE_ROLES, "Reviewer", "Analyst", "Viewer", "Auditor")


class ModelImportBatchRead(ApiModel):
    id: UUID
    organization_id: UUID
    datasource_id: UUID
    filename: str
    content_sha256: str
    status: str
    governance_review_id: UUID | None = None
    change_count: int
    applied_count: int
    skipped_count: int
    rejected_row_count: int
    uploaded_by: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class ModelImportChangeRead(ApiModel):
    id: UUID
    batch_id: UUID
    sheet_name: str
    row_number: int
    subject_type: str
    subject_id: str
    subject_label: str
    field: str
    old_value: str | None = None
    new_value: str
    expected_version: int | None = None
    status: str
    skip_reason: str | None = None


async def _authorized_datasource(
    datasource_id: UUID,
    context: SecurityContext,
    session: AsyncSession,
    settings: Settings,
) -> DataSource:
    datasource = await session.get(DataSource, datasource_id)
    if datasource is None:
        raise HTTPException(status_code=404, detail="datasource not found")
    enforce_organization(context, datasource.organization_id)
    try:
        # The same gate the export runs. A caller who cannot read this
        # datasource's model must not be able to propose edits to it either.
        await gate(
            session,
            context,
            settings=settings,
            action="READ_METADATA",
            resource_type="datasource",
            resource_id=str(datasource.id),
            datasource_id=datasource.id,
        )
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=403, detail=exc.reason_code) from exc
    return datasource


async def _authorized_batch(
    batch_id: UUID,
    context: SecurityContext,
    session: AsyncSession,
) -> ModelImportBatch:
    batch = await session.get(ModelImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="model import not found")
    enforce_organization(context, batch.organization_id)
    return batch


@router.post(
    "/datasources/{datasource_id}/model/import",
    response_model=ModelImportBatchRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_model_workbook(
    datasource_id: UUID,
    request: Request,
    filename: str = Query(default="workbook.xlsx", min_length=1, max_length=255),
    context: SecurityContext = Depends(require_roles(*_IMPORT_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ModelImportBatch:
    """Parse and diff an uploaded workbook. Publishes nothing.

    The response's counts are the whole point of this being a separate step
    from submit: `change_count` is what would be published,
    `rejected_row_count` is what could not be turned into a change at all, and
    a steward who sees the wrong numbers can stop here.
    """
    datasource = await _authorized_datasource(datasource_id, context, session, settings)

    declared_length = request.headers.get("content-length")
    if declared_length is not None and declared_length.isdigit():
        # Refuse on the declared size before reading the body into memory --
        # `parse_and_diff_workbook` checks the real length too, but only after
        # it already holds the bytes.
        if int(declared_length) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"workbook exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB upload limit",
            )
    content = await request.body()
    if not content:
        raise HTTPException(status_code=422, detail="the request body is empty")

    batch = await parse_and_diff_workbook(
        session,
        datasource=datasource,
        content=content,
        filename=filename,
        uploaded_by=context.principal_id,
    )
    record_audit(
        session,
        context,
        action="model_import.upload",
        resource_type="model_import_batch",
        resource_id=str(batch.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "datasource_id": str(datasource.id),
            "filename": batch.filename,
            "content_sha256": batch.content_sha256,
            "change_count": batch.change_count,
            "rejected_row_count": batch.rejected_row_count,
        },
    )
    await session.commit()
    return batch


@router.get("/model-imports/{batch_id}", response_model=ModelImportBatchRead)
async def get_model_import(
    batch_id: UUID,
    context: SecurityContext = Depends(require_roles(*_IMPORT_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ModelImportBatch:
    return await _authorized_batch(batch_id, context, session)


@router.get("/model-imports/{batch_id}/changes", response_model=Page)
async def list_model_import_changes(
    batch_id: UUID,
    change_status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_IMPORT_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> Page:
    """Every change the batch would make, or made.

    Rejected rows are returned alongside real changes rather than filtered
    out: an upload that quietly dropped the rows it could not understand would
    look cleaner than it was, and the rows a steward most needs to see are
    exactly the ones that did not work.
    """
    batch = await _authorized_batch(batch_id, context, session)
    filters = [ModelImportChange.batch_id == batch.id]
    if change_status:
        filters.append(ModelImportChange.status == change_status)
    total = await session.scalar(
        select(func.count()).select_from(ModelImportChange).where(*filters)
    )
    rows = (
        (
            await session.execute(
                select(ModelImportChange)
                .where(*filters)
                .order_by(
                    ModelImportChange.sheet_name,
                    ModelImportChange.row_number,
                    ModelImportChange.field,
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return Page(
        items=[ModelImportChangeRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )


class ModelImportExclusionRequest(ApiModel):
    change_ids: list[UUID] = Field(min_length=1, max_length=5000)
    excluded: bool = True


@router.post("/model-imports/{batch_id}/changes/exclusion", response_model=ModelImportBatchRead)
async def set_model_import_exclusion(
    batch_id: UUID,
    body: ModelImportExclusionRequest,
    context: SecurityContext = Depends(require_roles(*_IMPORT_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ModelImportBatch:
    """Drop individual rows from a batch before submitting it.

    Deliberately restricted to a DRAFT batch: what a reviewer is shown must be
    fixed the moment it is submitted, so this is an uploader-side edit, not a
    partial approval. See `model_import.set_change_exclusion`.
    """
    batch = await _authorized_batch(batch_id, context, session)
    remaining = await set_change_exclusion(
        session, batch, change_ids=body.change_ids, excluded=body.excluded
    )
    record_audit(
        session,
        context,
        action="model_import.exclusion",
        resource_type="model_import_batch",
        resource_id=str(batch.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "excluded": body.excluded,
            "change_count": len(body.change_ids),
            "remaining_change_count": remaining,
        },
    )
    await session.commit()
    return batch


@router.post("/model-imports/{batch_id}/submit", response_model=ModelImportBatchRead)
async def submit_model_import(
    batch_id: UUID,
    context: SecurityContext = Depends(require_roles(*_IMPORT_WRITE_ROLES)),
    session: AsyncSession = Depends(get_session),
) -> ModelImportBatch:
    """Put the batch in the review queue. Still publishes nothing.

    `requested_by` is recorded as the submitter, so
    `decide_governance_review`'s maker-checker guard refuses to let the same
    principal approve their own upload.
    """
    batch = await _authorized_batch(batch_id, context, session)
    review = await submit_batch_for_review(session, batch, requested_by=context.principal_id)
    record_audit(
        session,
        context,
        action="model_import.submit",
        resource_type="model_import_batch",
        resource_id=str(batch.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"review_id": str(review.id), "change_count": batch.change_count},
    )
    record_outbox(
        session,
        organization_id=batch.organization_id,
        aggregate_type="model_import_batch",
        aggregate_id=str(batch.id),
        event_type="model_import.submitted.v1",
        payload={
            "batch_id": str(batch.id),
            "datasource_id": str(batch.datasource_id),
            "review_id": str(review.id),
            "change_count": batch.change_count,
        },
    )
    await session.commit()
    return batch


@router.get("/datasources/{datasource_id}/model-imports", response_model=Page)
async def list_model_imports(
    datasource_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*_IMPORT_READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Page:
    datasource = await _authorized_datasource(datasource_id, context, session, settings)
    filters = [ModelImportBatch.datasource_id == datasource.id]
    total = await session.scalar(select(func.count()).select_from(ModelImportBatch).where(*filters))
    rows = (
        (
            await session.execute(
                select(ModelImportBatch)
                .where(*filters)
                .order_by(ModelImportBatch.created_at.desc(), ModelImportBatch.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return Page(
        items=[ModelImportBatchRead.model_validate(row) for row in rows],
        limit=limit,
        offset=offset,
        total=total or 0,
    )
