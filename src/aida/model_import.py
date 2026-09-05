"""Re-import an edited model workbook: parse, diff, review, apply.

The write half of the round trip `aida.model_export` opens. Three properties
carry most of the design:

**An upload applies nothing.** It parses the file, diffs it against current
state, and writes `ModelImportChange` rows for a steward to look at. Only an
APPROVE decision on the batch's `GovernanceReview` publishes, through exactly
the same `publish_*` helpers a single-asset approval uses -- there is no bulk
write path that bypasses them, because a bulk edit is not a lesser kind of
change than a single one.

**One review for the batch, not one per change.** `DocumentClaim` takes a
review per claim because deciding one means reading its own source paragraph.
A workbook's changes share one provenance -- this file, this uploader -- and
reviewing 400 of them as 400 queue entries would make the queue useless, which
is the reason this path exists next to the claim path rather than reusing it.

**Stale edits are skipped, not applied.** Every editable field is exported
alongside a read-only `*_version` column. That version is what the editor was
looking at; at apply time it is compared against the current version, and a
change whose expectation no longer holds is SKIPPED_STALE. Without that check,
a workbook exported on Monday and uploaded on Friday would silently discard
everything published in between -- the classic lost update, and the failure
mode most likely to go unnoticed in a bulk tool.

What the workbook deliberately cannot do:

* **Clear a description.** A blank cell is indistinguishable from a cell
  nobody filled in, and blanking one by accident is the easiest mistake to
  make in a spreadsheet. Blank means "no edit"; removing an approved
  description is done deliberately elsewhere.
* **Create a business annotation.** Editing `business_name` /
  `business_description` / `grain_statement` re-publishes an existing
  annotation. Creating one needs a domain and entity classification, which is
  not something a spreadsheet cell can supply.
* **Change anything ingestion owns.** Names, types, nullability,
  classification and source comments are read-only context. An edit to one is
  reported as a rejected row rather than ignored, so the steward learns it did
  not apply.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import publish_asset_documentation_version
from aida.business_annotation_versions import (
    AnnotationVersionContent,
    write_annotation_version,
)
from aida.catalog_read_model import _business_annotations, _latest_approved_documentation
from aida.column_documentation import (
    current_descriptions_by_column_id,
    publish_column_description,
)
from aida.model_export import COLUMN_SHEET, TABLE_SHEET
from aida.models import (
    DataSource,
    GovernanceReview,
    MetadataColumn,
    MetadataTable,
    ModelImportBatch,
    ModelImportChange,
)
from aida.xlsx_reader import ParsedSheet, WorkbookParseError, read_workbook

#: A reviewer has to be able to actually look at what they are approving. Well
#: past this, a single decision stops being a review and becomes a rubber
#: stamp, so an upload that would produce more changes is refused with a
#: message telling the uploader to narrow it rather than truncated into
#: something that looks complete.
MAX_CHANGES_PER_BATCH = 5_000

#: Ceiling on the uploaded file itself, checked before parsing.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

#: Editable fields per sheet, and the read-only version column each one is
#: checked against. The single source of truth for "what does a re-import
#: read back" -- `model_export.EDITABLE_COLUMNS` describes the same set to a
#: human in the README sheet, and `test_model_import.py` asserts the two agree
#: so the file's own instructions cannot drift from the code that reads it.
COLUMN_FIELDS: dict[str, str] = {"business_description": "description_version"}
TABLE_FIELDS: dict[str, str] = {
    "business_name": "annotation_version",
    "business_description": "annotation_version",
    "grain_statement": "annotation_version",
    "readme": "readme_version",
}

#: Table fields that live on the business annotation rather than on the
#: documentation. They publish together as one new annotation version.
_ANNOTATION_FIELDS = frozenset({"business_name", "business_description", "grain_statement"})


@dataclass(frozen=True, slots=True)
class _PendingChange:
    sheet_name: str
    row_number: int
    subject_type: str
    subject_id: str
    subject_label: str
    field: str
    old_value: str | None
    new_value: str
    expected_version: int | None
    status: str = "PENDING"
    skip_reason: str | None = None


def _parse_version(raw: str) -> int | None:
    """A `*_version` cell as an int, or `None` when it is blank or mangled.

    `None` means "no version to check against", which is the correct reading
    for a field that had no approved value at export time. A mangled value
    (someone typed over it) also lands here rather than raising: the change
    then applies only if the field still has no current version, which is the
    conservative outcome.
    """
    try:
        # Excel may hand back "2" or "2.0" depending on how the cell was
        # touched; both mean version 2.
        return int(float(raw)) if raw else None
    except ValueError:
        return None


def _sheet_or_none(sheets: dict[str, ParsedSheet], name: str) -> ParsedSheet | None:
    """Case-insensitive sheet lookup.

    Excel preserves sheet names, but a steward who rebuilds a sheet by hand
    may capitalise differently, and failing the whole upload over "columns"
    versus "Columns" would be a pointless obstacle.
    """
    for sheet_name, sheet in sheets.items():
        if sheet_name.strip().casefold() == name.casefold():
            return sheet
    return None


async def _diff_columns(
    session: AsyncSession,
    sheet: ParsedSheet,
    *,
    datasource_id: UUID,
) -> list[_PendingChange]:
    if "column_id" not in sheet.headers:
        return [
            _PendingChange(
                sheet_name=sheet.name,
                row_number=1,
                subject_type="COLUMN",
                subject_id="-",
                subject_label=sheet.name,
                field="column_id",
                old_value=None,
                new_value="",
                expected_version=None,
                status="REJECTED",
                skip_reason=(
                    "the Columns sheet has no column_id header; rows cannot be matched "
                    "without it. Re-export and edit that file rather than rebuilding the sheet."
                ),
            )
        ]

    raw_ids = [row.get("column_id", "") for row in sheet.rows]
    resolvable: dict[str, UUID] = {}
    for raw in raw_ids:
        try:
            resolvable[raw] = UUID(raw)
        except ValueError:
            continue

    columns: dict[UUID, tuple[MetadataColumn, str, str]] = {}
    if resolvable:
        rows = (
            await session.execute(
                select(MetadataColumn, MetadataTable.name)
                .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
                .where(
                    MetadataColumn.id.in_(list(resolvable.values())),
                    MetadataTable.datasource_id == datasource_id,
                )
            )
        ).all()
        columns = {
            column.id: (column, table_name, f"{table_name}.{column.name}")
            for column, table_name in rows
        }
    descriptions = await current_descriptions_by_column_id(session, list(columns))

    changes: list[_PendingChange] = []
    for offset, row in enumerate(sheet.rows):
        # +2: one for the header row, one because spreadsheet rows are 1-based.
        row_number = offset + 2
        raw_id = row.get("column_id", "")
        column_id = resolvable.get(raw_id)
        found = columns.get(column_id) if column_id else None
        if found is None:
            changes.append(
                _PendingChange(
                    sheet_name=sheet.name,
                    row_number=row_number,
                    subject_type="COLUMN",
                    subject_id=raw_id or "-",
                    subject_label=row.get("column", "") or raw_id or "(blank)",
                    field="column_id",
                    old_value=None,
                    new_value=raw_id,
                    expected_version=None,
                    status="REJECTED",
                    skip_reason=(
                        "no active column with this id in this datasource -- the id was "
                        "edited, or the column was dropped since the export"
                    ),
                )
            )
            continue

        column, _table_name, label = found
        documented = descriptions.get(column.id)
        for field, version_header in COLUMN_FIELDS.items():
            new_value = (row.get(field) or "").strip()
            if not new_value:
                continue  # blank means "no edit"; see the module docstring.
            current = documented.description if documented else None
            if new_value == (current or ""):
                continue
            changes.append(
                _PendingChange(
                    sheet_name=sheet.name,
                    row_number=row_number,
                    subject_type="COLUMN",
                    subject_id=str(column.id),
                    subject_label=label,
                    field=field,
                    old_value=current,
                    new_value=new_value,
                    expected_version=_parse_version(row.get(version_header, "")),
                )
            )
    return changes


async def _diff_tables(
    session: AsyncSession,
    sheet: ParsedSheet,
    *,
    datasource_id: UUID,
) -> list[_PendingChange]:
    if "table_id" not in sheet.headers:
        return [
            _PendingChange(
                sheet_name=sheet.name,
                row_number=1,
                subject_type="TABLE",
                subject_id="-",
                subject_label=sheet.name,
                field="table_id",
                old_value=None,
                new_value="",
                expected_version=None,
                status="REJECTED",
                skip_reason=(
                    "the Tables sheet has no table_id header; rows cannot be matched "
                    "without it. Re-export and edit that file rather than rebuilding the sheet."
                ),
            )
        ]

    resolvable: dict[str, UUID] = {}
    for row in sheet.rows:
        raw = row.get("table_id", "")
        try:
            resolvable[raw] = UUID(raw)
        except ValueError:
            continue

    tables: dict[UUID, MetadataTable] = {}
    if resolvable:
        found_rows = (
            await session.execute(
                select(MetadataTable).where(
                    MetadataTable.id.in_(list(resolvable.values())),
                    MetadataTable.datasource_id == datasource_id,
                )
            )
        ).scalars()
        tables = {table.id: table for table in found_rows}

    table_ids = list(tables)
    annotations = await _business_annotations(session, table_ids)
    documentation = await _latest_approved_documentation(session, table_ids)

    changes: list[_PendingChange] = []
    for offset, row in enumerate(sheet.rows):
        row_number = offset + 2
        raw_id = row.get("table_id", "")
        table_id = resolvable.get(raw_id)
        table = tables.get(table_id) if table_id else None
        if table is None:
            changes.append(
                _PendingChange(
                    sheet_name=sheet.name,
                    row_number=row_number,
                    subject_type="TABLE",
                    subject_id=raw_id or "-",
                    subject_label=row.get("table", "") or raw_id or "(blank)",
                    field="table_id",
                    old_value=None,
                    new_value=raw_id,
                    expected_version=None,
                    status="REJECTED",
                    skip_reason=(
                        "no active table with this id in this datasource -- the id was "
                        "edited, or the table was dropped since the export"
                    ),
                )
            )
            continue

        annotation = annotations.get(table.id)
        document = documentation.get(table.id)
        for field, version_header in TABLE_FIELDS.items():
            new_value = (row.get(field) or "").strip()
            if not new_value:
                continue
            if field in _ANNOTATION_FIELDS:
                if annotation is None:
                    changes.append(
                        _PendingChange(
                            sheet_name=sheet.name,
                            row_number=row_number,
                            subject_type="TABLE",
                            subject_id=str(table.id),
                            subject_label=table.name,
                            field=field,
                            old_value=None,
                            new_value=new_value,
                            expected_version=None,
                            status="REJECTED",
                            skip_reason=(
                                "this table has no approved business annotation yet. A "
                                "spreadsheet cell cannot create one -- an annotation needs a "
                                "domain and entity classification. Approve an annotation for "
                                "this table first, then this field becomes editable here."
                            ),
                        )
                    )
                    continue
                current = getattr(annotation, field)
            else:
                current = document.readme if document else None
            if new_value == (current or ""):
                continue
            changes.append(
                _PendingChange(
                    sheet_name=sheet.name,
                    row_number=row_number,
                    subject_type="TABLE",
                    subject_id=str(table.id),
                    subject_label=table.name,
                    field=field,
                    old_value=current,
                    new_value=new_value,
                    expected_version=_parse_version(row.get(version_header, "")),
                )
            )
    return changes


async def parse_and_diff_workbook(
    session: AsyncSession,
    *,
    datasource: DataSource,
    content: bytes,
    filename: str,
    uploaded_by: str,
) -> ModelImportBatch:
    """Parse an uploaded workbook and record what it would change.

    Writes a `DRAFT` batch and its `ModelImportChange` rows. Nothing is
    published here, and the batch does not enter the review queue until
    `submit_batch_for_review` is called -- so an uploader can look at the diff
    and walk away from a file they did not mean to upload.
    """
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"workbook exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB upload limit",
        )
    try:
        sheets = read_workbook(content)
    except WorkbookParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    columns_sheet = _sheet_or_none(sheets, COLUMN_SHEET)
    tables_sheet = _sheet_or_none(sheets, TABLE_SHEET)
    if columns_sheet is None and tables_sheet is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"the workbook has no {TABLE_SHEET!r} or {COLUMN_SHEET!r} sheet. "
                "Upload a file exported from this datasource, with its sheets intact."
            ),
        )

    pending: list[_PendingChange] = []
    if tables_sheet is not None:
        pending.extend(await _diff_tables(session, tables_sheet, datasource_id=datasource.id))
    if columns_sheet is not None:
        pending.extend(await _diff_columns(session, columns_sheet, datasource_id=datasource.id))

    real_changes = [change for change in pending if change.status == "PENDING"]
    if len(real_changes) > MAX_CHANGES_PER_BATCH:
        raise HTTPException(
            status_code=422,
            detail=(
                f"this workbook would change {len(real_changes)} fields, over the "
                f"{MAX_CHANGES_PER_BATCH} a single review can meaningfully cover. "
                "Split it into smaller uploads."
            ),
        )

    batch = ModelImportBatch(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        filename=filename[:255],
        content_sha256=hashlib.sha256(content).hexdigest(),
        status="DRAFT",
        change_count=len(real_changes),
        rejected_row_count=len(pending) - len(real_changes),
        uploaded_by=uploaded_by,
    )
    session.add(batch)
    await session.flush()
    for change in pending:
        session.add(
            ModelImportChange(
                organization_id=datasource.organization_id,
                batch_id=batch.id,
                sheet_name=change.sheet_name,
                row_number=change.row_number,
                subject_type=change.subject_type,
                subject_id=change.subject_id,
                subject_label=change.subject_label[:600],
                field=change.field,
                old_value=change.old_value,
                new_value=change.new_value,
                expected_version=change.expected_version,
                status=change.status,
                skip_reason=change.skip_reason,
            )
        )
    await session.flush()
    return batch


async def set_change_exclusion(
    session: AsyncSession,
    batch: ModelImportBatch,
    *,
    change_ids: list[UUID],
    excluded: bool,
) -> int:
    """Exclude (or restore) individual changes before the batch is submitted.

    A reviewer decides a batch as one thing -- that is the whole point of
    batching, and per-change decisions would put back exactly the per-row
    review load this path exists to avoid. But that left one wrong row forcing
    a steward to reject the file and re-upload a corrected one, which is a
    harsh price for a typo in a 400-row workbook.

    This is the release valve, and it sits on the *uploader's* side of the
    review boundary: only a DRAFT batch can be edited, so what a reviewer sees
    is fixed the moment it is submitted. Excluding is not deciding -- an
    excluded change is never applied and never counted as skipped; it is a row
    the uploader withdrew before anyone was asked to look at it.

    Returns the batch's updated `change_count`.
    """
    if batch.status != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail=(
                "this import has already been submitted; a reviewer decides it as one batch. "
                "Reject it and upload a corrected workbook to change what it contains."
            ),
        )
    if not change_ids:
        raise HTTPException(status_code=422, detail="no changes named")

    rows = list(
        (
            await session.execute(
                select(ModelImportChange).where(
                    ModelImportChange.batch_id == batch.id,
                    ModelImportChange.id.in_(change_ids),
                    # REJECTED rows are the diff's own findings, not proposals
                    # anyone can toggle -- they were never going to apply.
                    ModelImportChange.status.in_(("PENDING", "EXCLUDED")),
                )
            )
        )
        .scalars()
        .all()
    )
    for change in rows:
        change.status = "EXCLUDED" if excluded else "PENDING"
        change.skip_reason = (
            "excluded by the uploader before this batch was submitted" if excluded else None
        )
    await session.flush()

    remaining = await session.scalar(
        select(func.count())
        .select_from(ModelImportChange)
        .where(
            ModelImportChange.batch_id == batch.id,
            ModelImportChange.status == "PENDING",
        )
    )
    batch.change_count = remaining or 0
    await session.flush()
    return batch.change_count


async def submit_batch_for_review(
    session: AsyncSession,
    batch: ModelImportBatch,
    *,
    requested_by: str,
) -> GovernanceReview:
    """Put the batch in the shared review queue.

    One `GovernanceReview` for the whole batch -- see the module docstring.
    `requested_by` is the submitter, which
    `semantic_api.decide_governance_review`'s maker-checker guard then refuses
    to let approve their own batch.
    """
    if batch.status != "DRAFT":
        raise HTTPException(status_code=409, detail="this import has already been submitted")
    if batch.change_count == 0:
        raise HTTPException(
            status_code=409,
            detail=(
                "this workbook changes nothing that is still included; there is nothing "
                "to review"
            ),
        )
    review = GovernanceReview(
        organization_id=batch.organization_id,
        object_type="MODEL_IMPORT_BATCH",
        object_id=str(batch.id),
        requested_action="APPLY_WORKBOOK_EDITS",
        requested_by=requested_by,
    )
    session.add(review)
    await session.flush()
    batch.governance_review_id = review.id
    batch.status = "PENDING_REVIEW"
    await session.flush()
    return review


def _is_stale(current_version: int | None, expected_version: int | None) -> bool:
    """Has the field moved since the workbook was exported?

    Treats "no version either side" as fresh, and any disagreement as stale.
    `None` expected against a real current version means the field was empty
    at export time and someone has published one since -- which is exactly the
    case an unconditional apply would quietly destroy.
    """
    return current_version != expected_version


async def _apply_column_changes(
    session: AsyncSession,
    changes: list[ModelImportChange],
    *,
    batch: ModelImportBatch,
    reviewer: str,
    now: datetime,
) -> None:
    column_ids: list[UUID] = []
    for change in changes:
        try:
            column_ids.append(UUID(change.subject_id))
        except ValueError:
            change.status = "SKIPPED_MISSING"
            change.skip_reason = "the recorded column id is not a valid identifier"
    columns = {
        column.id: column
        for column in (
            await session.execute(select(MetadataColumn).where(MetadataColumn.id.in_(column_ids)))
        ).scalars()
    }
    current = await current_descriptions_by_column_id(session, list(columns))

    for change in changes:
        if change.status != "PENDING":
            continue
        column = columns.get(UUID(change.subject_id))
        if column is None:
            change.status = "SKIPPED_MISSING"
            change.skip_reason = "the column was dropped between upload and approval"
            continue
        documented = current.get(column.id)
        if _is_stale(documented.version if documented else None, change.expected_version):
            change.status = "SKIPPED_STALE"
            change.skip_reason = (
                "someone published a newer description for this column after the workbook "
                "was exported; this edit was not applied so their change is not lost"
            )
            continue
        await publish_column_description(
            session,
            organization_id=batch.organization_id,
            table_id=column.table_id,
            column_id=column.id,
            description=change.new_value,
            created_by=batch.uploaded_by,
            approved_by=reviewer,
            approved_at=now,
        )
        change.status = "APPLIED"


async def _apply_table_changes(
    session: AsyncSession,
    changes: list[ModelImportChange],
    *,
    batch: ModelImportBatch,
    reviewer: str,
    now: datetime,
) -> None:
    """Apply one table's changes, publishing at most one new version per store.

    Three edited annotation fields on the same table are one new
    `MetadataBusinessAnnotationVersion`, not three -- the version chain should
    record what the steward did (one edit), not how many cells it touched.
    """
    by_table: dict[str, list[ModelImportChange]] = {}
    for change in changes:
        by_table.setdefault(change.subject_id, []).append(change)

    table_ids: list[UUID] = []
    for raw_id in by_table:
        try:
            table_ids.append(UUID(raw_id))
        except ValueError:
            for change in by_table[raw_id]:
                change.status = "SKIPPED_MISSING"
                change.skip_reason = "the recorded table id is not a valid identifier"
    tables = {
        table.id: table
        for table in (
            await session.execute(select(MetadataTable).where(MetadataTable.id.in_(table_ids)))
        ).scalars()
    }
    annotations = await _business_annotations(session, list(tables))
    documentation = await _latest_approved_documentation(session, list(tables))

    for raw_id, table_changes in by_table.items():
        pending = [change for change in table_changes if change.status == "PENDING"]
        if not pending:
            continue
        try:
            table_id = UUID(raw_id)
        except ValueError:
            continue
        table = tables.get(table_id)
        if table is None:
            for change in pending:
                change.status = "SKIPPED_MISSING"
                change.skip_reason = "the table was dropped between upload and approval"
            continue

        readme_changes = [change for change in pending if change.field == "readme"]
        annotation_changes = [change for change in pending if change.field in _ANNOTATION_FIELDS]

        if readme_changes:
            document = documentation.get(table.id)
            current_version = document.version if document else None
            fresh = [
                change
                for change in readme_changes
                if not _is_stale(current_version, change.expected_version)
            ]
            for change in readme_changes:
                if change not in fresh:
                    change.status = "SKIPPED_STALE"
                    change.skip_reason = (
                        "someone published newer documentation for this table after the "
                        "workbook was exported; this edit was not applied"
                    )
            if fresh:
                # One readme field, so at most one fresh change here; take the
                # last if a hand-built sheet somehow repeated the row.
                await publish_asset_documentation_version(
                    session,
                    organization_id=batch.organization_id,
                    table_id=table.id,
                    readme=fresh[-1].new_value,
                    created_by=batch.uploaded_by,
                    approved_by=reviewer,
                    approved_at=now,
                )
                for change in fresh:
                    change.status = "APPLIED"

        if annotation_changes:
            annotation = annotations.get(table.id)
            if annotation is None:
                for change in annotation_changes:
                    change.status = "SKIPPED_MISSING"
                    change.skip_reason = (
                        "this table's business annotation was withdrawn between upload and approval"
                    )
                continue
            fresh = [
                change
                for change in annotation_changes
                if not _is_stale(annotation.version, change.expected_version)
            ]
            for change in annotation_changes:
                if change not in fresh:
                    change.status = "SKIPPED_STALE"
                    change.skip_reason = (
                        "someone published a newer annotation for this table after the "
                        "workbook was exported; this edit was not applied"
                    )
            if fresh:
                edited = {change.field: change.new_value for change in fresh}
                # Every field the workbook did not edit is carried forward from
                # the current version verbatim: this publishes a new version of
                # the whole annotation, so an unedited field must survive it.
                await write_annotation_version(
                    session,
                    organization_id=batch.organization_id,
                    annotation_id=annotation.annotation_id,
                    content=AnnotationVersionContent(
                        business_name=edited.get("business_name", annotation.business_name),
                        business_description=edited.get(
                            "business_description", annotation.business_description
                        ),
                        table_role=annotation.table_role,
                        grain_statement=edited.get("grain_statement", annotation.grain_statement),
                        synonyms=list(annotation.synonyms),
                        suggested_questions=list(annotation.suggested_questions),
                        tags=list(annotation.tags),
                        confidence=annotation.confidence,
                    ),
                    approved_by=reviewer,
                    approved_at=now,
                )
                for change in fresh:
                    change.status = "APPLIED"


async def apply_model_import_batch(
    session: AsyncSession,
    batch: ModelImportBatch,
    *,
    reviewer: str,
    now: datetime,
) -> tuple[str, int]:
    """Publish an approved batch's changes.

    Called only from `semantic_api.decide_governance_review`, after its shared
    maker-checker guard. Returns the event type and how many changes actually
    applied -- which can be fewer than the batch proposed, because a change
    superseded since export is skipped rather than applied.
    """
    if batch.status != "PENDING_REVIEW":
        raise HTTPException(status_code=409, detail="this import is no longer pending review")

    changes = list(
        (
            await session.execute(
                select(ModelImportChange)
                .where(
                    ModelImportChange.batch_id == batch.id,
                    ModelImportChange.status == "PENDING",
                )
                .order_by(ModelImportChange.sheet_name, ModelImportChange.row_number)
            )
        ).scalars()
    )
    table_changes = [change for change in changes if change.subject_type == "TABLE"]
    column_changes = [change for change in changes if change.subject_type == "COLUMN"]

    if table_changes:
        await _apply_table_changes(session, table_changes, batch=batch, reviewer=reviewer, now=now)
    if column_changes:
        await _apply_column_changes(
            session, column_changes, batch=batch, reviewer=reviewer, now=now
        )

    applied = sum(1 for change in changes if change.status == "APPLIED")
    batch.status = "APPLIED"
    batch.applied_count = applied
    batch.skipped_count = len(changes) - applied
    batch.reviewed_by = reviewer
    batch.reviewed_at = now
    await session.flush()
    return "model_import.applied.v1", applied


async def reject_model_import_batch(
    session: AsyncSession,
    batch: ModelImportBatch,
    *,
    reviewer: str,
    now: datetime,
) -> str:
    """Reject a batch. Its change rows are retained, not deleted, so the
    uploader can see exactly what was turned down rather than being told only
    that something was.
    """
    if batch.status != "PENDING_REVIEW":
        raise HTTPException(status_code=409, detail="this import is no longer pending review")
    for change in (
        (
            await session.execute(
                select(ModelImportChange).where(
                    ModelImportChange.batch_id == batch.id,
                    ModelImportChange.status == "PENDING",
                )
            )
        )
        .scalars()
        .all()
    ):
        change.status = "REJECTED"
        change.skip_reason = "the batch this change belonged to was rejected"
    batch.status = "REJECTED"
    batch.reviewed_by = reviewer
    batch.reviewed_at = now
    await session.flush()
    return "model_import.rejected.v1"
