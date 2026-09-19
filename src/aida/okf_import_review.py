"""R11-OKF03: the full preview a reviewer reads before deciding an imported OKF edit.

An OKF import raises two kinds of review of its own: an `OKF_IMPORT_BATCH` (table purposes and
column descriptions, one `ModelImportBatch` per datasource) and an
`OKF_IMPORT_ROUTINE_DESCRIPTION` (one routine's purpose, a `RoutineDescriptionDraft`). The
generic review diff (`review_detail_snapshots`) shows a batch's recorded old and new values; it
cannot show what a reviewer most needs before approving an edit made in a file some time ago:

* **per document**, the object each change is about as Atlas holds it *now* -- a batch groups
  changes by table, because a table's document (and its column sets) is what the editor edited;
* **before and after text** -- what the proposal replaces, as the import recorded it, beside
  the proposed text;
* **conflicts** -- whether the approved description moved since the export, which approval
  then skips (a batch: `SKIPPED_STALE`) or refuses (a routine: the review stays pending), and
  whether the target is gone;
* **status** -- each change's own row status once decided.

So this module predicts what approval will do, change by change, from the same comparisons the
approval makes: `model_import._is_stale` (a current approved version different from the
expected one, including "none expected, one exists now"), a missing target, and for a routine
`routine_definition_moved` and the description-version check in
`apply_routine_description_draft`. It is a read: it writes nothing, decides nothing, and a
prediction it gets wrong is still decided by the approval's own re-check.

**Authorization.** The reviewer roles of the generic diff, the review's organization (INV-5),
and the workbook upload's `READ_METADATA` gate on the review's datasource: the preview shows
the current approved text of that source's objects, which a reviewer who may not read the
source's model must not be shown by way of a review.

**Value freedom (INV-6).** Nothing is persisted. Text is shown only where export screening
would release it -- the rule the import's own preview follows -- so a description screening
withholds from a bundle is withheld here too.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import DEFINITION_MOVED
from aida.authorization_gate import AuthorizationDenied, gate
from aida.config import Settings
from aida.envelope_models import (
    MetadataRoutine,
    RoutineDescriptionDraft,
    RoutineDocumentation,
    RoutineDocumentationVersion,
)
from aida.ingest_screening import CLEAN, screen_text
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    ColumnDocumentation,
    ColumnDocumentationVersion,
    GovernanceReview,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    ModelImportBatch,
    ModelImportChange,
)
from aida.okf_export import DESCRIPTION_APPROVED
from aida.okf_import import (
    OKF_IMPORT_REVIEW_TYPE,
    OKF_IMPORT_REVIEW_TYPES,
    OKF_IMPORT_ROUTINE_REVIEW_TYPE,
    current_routine_documentation,
)
from aida.okf_import_bundle import (
    DATASOURCE_NOT_AUTHORIZED,
    SOURCE_CHANGED_SINCE_EXPORT,
    TARGET_NOT_ACTIVE,
    TARGET_NOT_FOUND,
)
from aida.routine_description_service import routine_definition_moved
from aida.security_types import SecurityContext

#: What approval will do with one change, predicted from the approval's own comparisons.
STATE_APPLIES: Final = "APPLIES"
STATE_CONFLICT: Final = "CONFLICT"
STATE_TARGET_UNAVAILABLE: Final = "TARGET_UNAVAILABLE"
#: The change is no longer pending: its row status says what happened to it.
STATE_DECIDED: Final = "DECIDED"

#: Documents per page, so a 5,000-edit import never becomes one response.
DEFAULT_PAGE_SIZE: Final = 25
MAX_PAGE_SIZE: Final = 100
#: Subject ids read per query, so a large batch never becomes one oversized IN list.
_IDS_PER_READ: Final = 1_000

_SCREENING_ORIGIN: Final = "okf_import_review"


@dataclass(frozen=True, slots=True)
class OkfReviewChange:
    change_id: str
    subject_type: str
    subject_id: str
    field: str
    label: str
    before_value: str | None
    proposed_value: str | None
    expected_version: int | None
    current_version: int | None
    #: The approved text now, shown only when it is not the text the import replaces.
    current_value: str | None
    status: str
    skip_reason: str | None
    state: str
    reason_code: str | None
    #: `False` when the target still exists but is no longer active in the catalog.
    target_active: bool | None
    #: One sentence: what approving does with this change.
    approval_effect: str


@dataclass(frozen=True, slots=True)
class OkfReviewDocument:
    document_id: str
    label: str
    object_type: str
    changes: tuple[OkfReviewChange, ...]

    @property
    def conflicts(self) -> int:
        """Changes approval will not publish as proposed: in conflict, or target gone."""
        return sum(
            1
            for change in self.changes
            if change.state in (STATE_CONFLICT, STATE_TARGET_UNAVAILABLE)
        )


@dataclass(frozen=True, slots=True)
class OkfReviewPreview:
    review: GovernanceReview
    proposal_status: str
    datasource_id: UUID
    filename: str | None
    archive_sha256: str | None
    documents: tuple[OkfReviewDocument, ...]
    authority: str

    def counts(self) -> dict[str, int]:
        changes = [change for document in self.documents for change in document.changes]
        return {
            "documents": len(self.documents),
            "changes": len(changes),
            "applies": sum(1 for change in changes if change.state == STATE_APPLIES),
            "conflicts": sum(1 for change in changes if change.state == STATE_CONFLICT),
            "target_unavailable": sum(
                1 for change in changes if change.state == STATE_TARGET_UNAVAILABLE
            ),
            "decided": sum(1 for change in changes if change.state == STATE_DECIDED),
        }


_BATCH_AUTHORITY: Final = (
    "Approving publishes every change marked APPLIES through the description family's "
    "append-only publish, as one decision. A change whose approved description moved after the "
    "export is skipped (SKIPPED_STALE), never overwritten; a change whose table or column is "
    "gone is skipped (SKIPPED_MISSING). Rejecting keeps every change row for the importer to "
    "read. Verification or status values in the edited file granted nothing."
)
_ROUTINE_AUTHORITY: Final = (
    "Approving publishes this routine description through the routine description workflow. "
    "If the routine's description or captured body moved, or the routine is no longer active, "
    "approval is refused and the review stays pending for you to reject. Verification or "
    "status values in the edited file granted nothing."
)


def _released(text: str | None) -> str | None:
    """Text as export screening would release it, or nothing."""
    if text is None:
        return None
    return text if screen_text(text, content_origin=_SCREENING_ORIGIN).status == CLEAN else None


def _chunks(values: Sequence[UUID]) -> Iterable[list[UUID]]:
    for start in range(0, len(values), _IDS_PER_READ):
        yield list(values[start : start + _IDS_PER_READ])


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _version_label(version: int | None) -> str:
    return "no approved description" if version is None else f"v{version}"


async def _authorize(
    session: AsyncSession, context: SecurityContext, settings: Settings, datasource_id: UUID
) -> None:
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
    except AuthorizationDenied as denied:
        raise HTTPException(
            status_code=403,
            detail={
                "reason_code": DATASOURCE_NOT_AUTHORIZED,
                "detail": "you may not read the model of the source this import edits",
            },
        ) from denied


# --- a description batch ------------------------------------------------------------------


async def _tables(
    session: AsyncSession, organization_id: UUID, table_ids: Sequence[UUID]
) -> dict[UUID, tuple[MetadataTable, str]]:
    found: dict[UUID, tuple[MetadataTable, str]] = {}
    for chunk in _chunks(table_ids):
        rows = (
            await session.execute(
                select(MetadataTable, MetadataSchema, MetadataCatalog)
                .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
                .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
                .where(
                    MetadataTable.id.in_(chunk),
                    MetadataTable.organization_id == organization_id,
                    MetadataSchema.organization_id == organization_id,
                    MetadataCatalog.organization_id == organization_id,
                )
            )
        ).all()
        for table, schema, source_catalog in rows:
            found[table.id] = (table, f"{source_catalog.name}.{schema.name}.{table.name}")
    return found


async def _columns(
    session: AsyncSession, organization_id: UUID, column_ids: Sequence[UUID]
) -> dict[UUID, MetadataColumn]:
    found: dict[UUID, MetadataColumn] = {}
    for chunk in _chunks(column_ids):
        for column in (
            await session.scalars(
                select(MetadataColumn).where(
                    MetadataColumn.id.in_(chunk),
                    MetadataColumn.organization_id == organization_id,
                )
            )
        ).all():
            found[column.id] = column
    return found


async def _table_documentation(
    session: AsyncSession, organization_id: UUID, table_ids: Sequence[UUID]
) -> dict[UUID, AssetDocumentationVersion]:
    """The newest approved documentation per table: the version the batch apply's stale check
    compares against (`model_import._latest_approved_documentation`)."""
    found: dict[UUID, AssetDocumentationVersion] = {}
    for chunk in _chunks(table_ids):
        rows = (
            await session.execute(
                select(AssetDocumentationVersion, AssetDocumentation.table_id)
                .join(
                    AssetDocumentation,
                    AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
                )
                .where(
                    AssetDocumentation.table_id.in_(chunk),
                    AssetDocumentation.organization_id == organization_id,
                    AssetDocumentationVersion.organization_id == organization_id,
                    AssetDocumentationVersion.status == DESCRIPTION_APPROVED,
                )
                .order_by(AssetDocumentationVersion.version)
            )
        ).all()
        # Ascending, so the newest approved version per table wins.
        found.update({table_id: version for version, table_id in rows})
    return found


async def _column_descriptions(
    session: AsyncSession, organization_id: UUID, column_ids: Sequence[UUID]
) -> dict[UUID, ColumnDocumentationVersion]:
    """The newest approved description per column: what the column apply compares against."""
    found: dict[UUID, ColumnDocumentationVersion] = {}
    for chunk in _chunks(column_ids):
        rows = (
            await session.execute(
                select(ColumnDocumentationVersion, ColumnDocumentation.column_id)
                .join(
                    ColumnDocumentation,
                    ColumnDocumentation.id == ColumnDocumentationVersion.documentation_id,
                )
                .where(
                    ColumnDocumentation.column_id.in_(chunk),
                    ColumnDocumentation.organization_id == organization_id,
                    ColumnDocumentationVersion.organization_id == organization_id,
                    ColumnDocumentationVersion.status == DESCRIPTION_APPROVED,
                )
                .order_by(ColumnDocumentationVersion.version)
            )
        ).all()
        found.update({column_id: version for version, column_id in rows})
    return found


def _batch_change(
    row: ModelImportChange,
    *,
    label: str,
    field: str,
    present: bool,
    active: bool | None,
    current_version: int | None,
    current_text: str | None,
) -> OkfReviewChange:
    state: str
    reason: str | None = None
    if row.status != "PENDING":
        state = STATE_DECIDED
        effect = f"Already decided: {row.status.lower().replace('_', ' ')}."
    elif not present:
        state, reason = STATE_TARGET_UNAVAILABLE, TARGET_NOT_FOUND
        effect = "Approving skips this change: its target is no longer in the catalog."
    elif current_version != row.expected_version:
        state, reason = STATE_CONFLICT, SOURCE_CHANGED_SINCE_EXPORT
        effect = (
            "Approving skips this change (SKIPPED_STALE): the approved description moved from "
            f"{_version_label(row.expected_version)} to {_version_label(current_version)} "
            "after the export, and an import never overwrites what it did not see."
        )
    else:
        state = STATE_APPLIES
        effect = "Approving publishes the proposed text as the new approved description."
        if active is False:
            reason = TARGET_NOT_ACTIVE
            effect += " Note: its target is no longer active in the catalog."
    return OkfReviewChange(
        change_id=str(row.id),
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        field=field,
        label=label,
        before_value=_released(row.old_value),
        proposed_value=_released(row.new_value),
        expected_version=row.expected_version,
        current_version=current_version,
        current_value=(
            _released(current_text) if current_version != row.expected_version else None
        ),
        status=row.status,
        skip_reason=row.skip_reason,
        state=state,
        reason_code=reason,
        target_active=active,
        approval_effect=effect,
    )


async def _batch_preview(
    session: AsyncSession,
    review: GovernanceReview,
    context: SecurityContext,
    settings: Settings,
) -> OkfReviewPreview:
    object_id = _uuid(review.object_id)
    batch = await session.get(ModelImportBatch, object_id) if object_id else None
    if (
        batch is None
        or batch.organization_id != review.organization_id
        or batch.governance_review_id != review.id
    ):
        raise HTTPException(status_code=409, detail="review target is unavailable")
    await _authorize(session, context, settings, batch.datasource_id)
    organization_id = review.organization_id
    rows = list(
        (
            await session.scalars(
                select(ModelImportChange)
                .where(
                    ModelImportChange.batch_id == batch.id,
                    ModelImportChange.organization_id == organization_id,
                )
                .order_by(ModelImportChange.row_number, ModelImportChange.id)
            )
        ).all()
    )
    column_ids = [
        value
        for row in rows
        if row.subject_type == "COLUMN" and (value := _uuid(row.subject_id)) is not None
    ]
    columns = await _columns(session, organization_id, column_ids)
    table_ids = {
        value
        for row in rows
        if row.subject_type == "TABLE" and (value := _uuid(row.subject_id)) is not None
    }
    table_ids |= {column.table_id for column in columns.values()}
    tables = await _tables(session, organization_id, sorted(table_ids, key=str))
    documentation = await _table_documentation(session, organization_id, list(tables))
    descriptions = await _column_descriptions(session, organization_id, list(columns))

    grouped: dict[str, list[OkfReviewChange]] = {}
    headings: dict[str, tuple[str, str]] = {}
    for row in rows:
        subject = _uuid(row.subject_id)
        if row.subject_type == "COLUMN":
            column = columns.get(subject) if subject else None
            table_id = column.table_id if column is not None else None
            found = tables.get(table_id) if table_id is not None else None
            current = descriptions.get(column.id) if column is not None else None
            name = column.name if column is not None else row.subject_label.rsplit(".", 1)[-1]
            change = _batch_change(
                row,
                label=f"{found[1]}.{name}" if found else row.subject_label,
                field=f"column:{name}",
                present=column is not None,
                active=(
                    column.status == "ACTIVE" and (found is None or found[0].status == "ACTIVE")
                    if column is not None
                    else None
                ),
                current_version=current.version if current else None,
                current_text=current.description if current else None,
            )
        else:
            table_id = subject
            found = tables.get(subject) if subject else None
            documented = documentation.get(subject) if subject else None
            change = _batch_change(
                row,
                label=found[1] if found else row.subject_label,
                field="purpose",
                present=found is not None,
                active=found[0].status == "ACTIVE" if found else None,
                current_version=documented.version if documented else None,
                current_text=documented.readme if documented else None,
            )
        if found is not None and table_id is not None:
            key = str(table_id)
            headings.setdefault(key, (found[1], found[0].object_type))
        else:
            # The table is gone: group what the import recorded under its recorded label, so a
            # reviewer still sees which document the orphaned change came from.
            recorded = (
                row.subject_label.rsplit(".", 1)[0]
                if row.subject_type == "COLUMN"
                else row.subject_label
            )
            key = f"missing:{recorded}"
            headings.setdefault(key, (recorded, "UNAVAILABLE"))
        grouped.setdefault(key, []).append(change)
    documents = tuple(
        OkfReviewDocument(
            document_id=key,
            label=headings[key][0],
            object_type=headings[key][1],
            changes=tuple(changes),
        )
        for key, changes in sorted(grouped.items(), key=lambda entry: headings[entry[0]][0])
    )
    return OkfReviewPreview(
        review=review,
        proposal_status=batch.status,
        datasource_id=batch.datasource_id,
        filename=batch.filename,
        archive_sha256=batch.content_sha256,
        documents=documents,
        authority=_BATCH_AUTHORITY,
    )


# --- a routine purpose --------------------------------------------------------------------


async def _routine_label(
    session: AsyncSession, organization_id: UUID, routine: MetadataRoutine
) -> str:
    row = (
        await session.execute(
            select(MetadataSchema.name, MetadataCatalog.name)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataSchema.id == routine.schema_id,
                MetadataSchema.organization_id == organization_id,
                MetadataCatalog.organization_id == organization_id,
            )
        )
    ).first()
    parts = (
        (str(row[1]), str(row[0]), routine.package_name, routine.name)
        if row is not None
        else (routine.package_name, routine.name)
    )
    return ".".join(part for part in parts if part) + routine.signature


async def _described_as(
    session: AsyncSession, organization_id: UUID, routine_id: UUID, version: int | None
) -> str | None:
    """The approved text at `version` -- what the export showed and the draft replaces.
    Append-only, so a superseded version still reads back."""
    if version is None:
        return None
    text: str | None = await session.scalar(
        select(RoutineDocumentationVersion.description)
        .join(
            RoutineDocumentation,
            RoutineDocumentation.id == RoutineDocumentationVersion.documentation_id,
        )
        .where(
            RoutineDocumentation.routine_id == routine_id,
            RoutineDocumentation.organization_id == organization_id,
            RoutineDocumentationVersion.organization_id == organization_id,
            RoutineDocumentationVersion.version == version,
        )
    )
    return text


async def _routine_preview(
    session: AsyncSession,
    review: GovernanceReview,
    context: SecurityContext,
    settings: Settings,
) -> OkfReviewPreview:
    object_id = _uuid(review.object_id)
    draft = await session.get(RoutineDescriptionDraft, object_id) if object_id else None
    if (
        draft is None
        or draft.organization_id != review.organization_id
        or draft.governance_review_id != review.id
    ):
        raise HTTPException(status_code=409, detail="review target is unavailable")
    await _authorize(session, context, settings, draft.datasource_id)
    organization_id = review.organization_id
    routine = await session.get(MetadataRoutine, draft.routine_id)
    if routine is not None and routine.organization_id != organization_id:
        routine = None
    label = (
        await _routine_label(session, organization_id, routine)
        if routine is not None
        else str(draft.routine_id)
    )
    current = await current_routine_documentation(session, organization_id, draft.routine_id)
    current_version = current.version if current is not None else None
    reason: str | None = None
    if draft.status != "PENDING_APPROVAL":
        state = STATE_DECIDED
        effect = f"Already decided: {draft.status.lower().replace('_', ' ')}."
    elif routine is None or routine.status != "ACTIVE":
        state = STATE_TARGET_UNAVAILABLE
        reason = TARGET_NOT_FOUND if routine is None else TARGET_NOT_ACTIVE
        effect = (
            "Approval will be refused: the routine is no longer active in the catalog. Reject "
            "this review."
        )
    elif current_version != draft.base_description_version:
        state, reason = STATE_CONFLICT, SOURCE_CHANGED_SINCE_EXPORT
        effect = (
            "Approval will be refused: the routine's approved description moved from "
            f"{_version_label(draft.base_description_version)} to "
            f"{_version_label(current_version)} after the export. Reject this review."
        )
    elif (moved := await routine_definition_moved(session, draft)) is not None:
        state, reason = STATE_CONFLICT, DEFINITION_MOVED
        effect = f"Approval will be refused: {moved}"
    else:
        state = STATE_APPLIES
        effect = "Approving publishes the proposed text as the routine's approved description."
    change = OkfReviewChange(
        change_id=str(draft.id),
        subject_type="ROUTINE",
        subject_id=str(draft.routine_id),
        field="purpose",
        label=label,
        before_value=_released(
            await _described_as(
                session, organization_id, draft.routine_id, draft.base_description_version
            )
        ),
        proposed_value=_released(draft.drafted_text),
        expected_version=draft.base_description_version,
        current_version=current_version,
        current_value=(
            _released(current.description)
            if current is not None and current_version != draft.base_description_version
            else None
        ),
        status=draft.status,
        skip_reason=None,
        state=state,
        reason_code=reason,
        target_active=routine.status == "ACTIVE" if routine is not None else None,
        approval_effect=effect,
    )
    provenance = (draft.evidence or {}).get("okf_import", {})
    return OkfReviewPreview(
        review=review,
        proposal_status=draft.status,
        datasource_id=draft.datasource_id,
        filename=None,
        archive_sha256=provenance.get("archive_sha256") if isinstance(provenance, dict) else None,
        documents=(
            OkfReviewDocument(
                document_id=str(draft.routine_id),
                label=label,
                object_type=routine.routine_type if routine is not None else "UNAVAILABLE",
                changes=(change,),
            ),
        ),
        authority=_ROUTINE_AUTHORITY,
    )


async def read_okf_import_review(
    session: AsyncSession,
    review_id: UUID,
    context: SecurityContext,
    settings: Settings,
) -> OkfReviewPreview:
    """The full preview of one OKF import review. 404 for anything that is not one."""
    review = await session.get(GovernanceReview, review_id)
    if review is None or review.object_type not in OKF_IMPORT_REVIEW_TYPES:
        raise HTTPException(status_code=404, detail="OKF import review not found")
    if "PlatformAdmin" not in context.roles and context.organization_id != review.organization_id:
        # 404, not 403: whether another tenant has an import waiting is itself not disclosed.
        raise HTTPException(status_code=404, detail="OKF import review not found")
    if review.object_type == OKF_IMPORT_REVIEW_TYPE:
        return await _batch_preview(session, review, context, settings)
    assert review.object_type == OKF_IMPORT_ROUTINE_REVIEW_TYPE
    return await _routine_preview(session, review, context, settings)
