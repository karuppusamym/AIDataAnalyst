"""R11-C8: which answers relied on a disputed agent decision while it stood.

A correction undoes the catalog change a disputed reviewer-agent decision made.
It does not, by itself, find the answers produced while that change stood -- and
an answer grounded on a wrong business annotation, or generated against a wrongly
classified column, is the downstream harm the correction exists to limit. This
module finds those answers, so the owner can decide what to do about each one.

**Identity, not content.** `AgentRun` keeps no question and no answer text
(INV-6), so harm is found through what a run *touched*: its
`grounding_fragment_digests`, which name the exact business-annotation version a
run cited, and its `retrieval_evidence`, which names every object retrieved plus
the `table_id` / `source_table_id` the orchestrator hydrates into model context
(`agent_orchestrator._model_context` reads exactly those two keys). Every match
says which of two bases it rests on:

* `EXACT_VERSION` -- the run's grounding cites the very annotation version the
  decision published. The strongest evidence a value-free ledger can give: that
  content was in the model's context.
* `ASSET_IN_CONTEXT` -- the run retrieved, or hydrated into model context, an
  asset the decision changed. Over-inclusive by design: it proves the asset was
  consulted, not that the changed attribute shaped the answer, which nothing
  stored can prove. Missing a harmed answer is the failure that matters here;
  listing one that was not harmed costs a steward a look.

**The window is the time the change stood**: from the disputed decision
(`GovernanceReview.decided_at`) until its correction took effect, or with no end
while none has.

**Bounded, and it says so.** At most `MAX_SCANNED_RUNS` runs in the window are
read, oldest first; `truncated` reports that later runs were not.

**What cannot reach an answer is said rather than searched.** Ownership
operations change who owns an asset, which no answer's context carries, so
`reaches_answers` is false for them -- an empty list there would read as
"checked, and nothing found".

Nothing here re-issues, retracts or annotates an answer. It finds them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import RoutineDescriptionDraft
from aida.models import (
    AgentRun,
    AssetDescriptionDraft,
    BulkStewardshipOperation,
    ColumnDescriptionDraft,
    DescriptionWithdrawal,
    GovernanceReview,
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    MetadataColumn,
    ModelImportBatch,
    ModelImportChange,
    ReviewAuditSample,
)

#: The most runs one impact read scans. Bounded because every run's grounding
#: and retrieval evidence is read to match it.
MAX_SCANNED_RUNS = 5_000

EXACT_VERSION = "EXACT_VERSION"
ASSET_IN_CONTEXT = "ASSET_IN_CONTEXT"

#: Bulk operations that change only who owns an asset. Neither retrieval nor
#: model context carries ownership, so no answer can have relied on it.
_OWNERSHIP_OPERATIONS = frozenset({"ASSIGN_OWNERSHIP", "REASSIGN_LEAVER"})


@dataclass(frozen=True, slots=True)
class ImpactSubject:
    """One thing the disputed decision changed, named as runs name it."""

    object_type: str
    object_id: str
    #: Set for a business annotation: the exact version the decision published.
    annotation_version_id: str | None = None
    #: Set for a column: the table whose hydrated context carries the column.
    table_id: str | None = None


@dataclass(frozen=True, slots=True)
class AffectedRun:
    agent_run_id: UUID
    created_at: datetime
    datasource_id: UUID
    principal_id: str
    status: str
    bases: tuple[str, ...]
    matched_object_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorrectionState:
    """The correction raised from the sample, if one was."""

    kind: str
    correction_id: UUID
    #: PENDING, APPLIED or REJECTED.
    status: str
    #: When an applied correction took effect -- the end of the window.
    effective_at: datetime | None


@dataclass(frozen=True, slots=True)
class DownstreamImpact:
    window_start: datetime
    #: `None` while the change still stands.
    window_end: datetime | None
    correction: CorrectionState | None
    reaches_answers: bool
    subjects: tuple[ImpactSubject, ...]
    scanned_runs: int
    truncated: bool
    affected_runs: tuple[AffectedRun, ...]


def _aware(value: datetime) -> datetime:
    """SQLite hands timestamps back naive; every stored one is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _uuids(values: Iterable[Any]) -> list[UUID]:
    parsed: list[UUID] = []
    for value in values:
        try:
            parsed.append(UUID(str(value)))
        except ValueError:
            continue
    return parsed


async def _column_subjects(session: AsyncSession, column_ids: Iterable[Any]) -> list[ImpactSubject]:
    ids = _uuids(column_ids)
    if not ids:
        return []
    rows = (
        await session.execute(
            select(MetadataColumn.id, MetadataColumn.table_id).where(MetadataColumn.id.in_(ids))
        )
    ).all()
    return [
        ImpactSubject("COLUMN", str(column_id), table_id=str(table_id))
        for column_id, table_id in rows
    ]


async def _annotation_version_subjects(
    session: AsyncSession, table_id: UUID, version_numbers: set[int]
) -> list[ImpactSubject]:
    rows = (
        await session.execute(
            select(MetadataBusinessAnnotationVersion.id, MetadataBusinessAnnotation.id)
            .join(
                MetadataBusinessAnnotation,
                MetadataBusinessAnnotation.id == MetadataBusinessAnnotationVersion.annotation_id,
            )
            .where(
                MetadataBusinessAnnotation.table_id == table_id,
                MetadataBusinessAnnotationVersion.version.in_(version_numbers),
            )
        )
    ).all()
    return [
        ImpactSubject(
            "BUSINESS_ANNOTATION", str(annotation_id), annotation_version_id=str(version_id)
        )
        for version_id, annotation_id in rows
    ]


async def _bulk_operation_subjects(
    session: AsyncSession, sample: ReviewAuditSample
) -> tuple[bool, list[ImpactSubject]]:
    operation = await session.scalar(
        select(BulkStewardshipOperation).where(
            BulkStewardshipOperation.governance_review_id == sample.governance_review_id,
            BulkStewardshipOperation.organization_id == sample.organization_id,
        )
    )
    if operation is None:
        return True, []
    if operation.operation_type in _OWNERSHIP_OPERATIONS:
        return False, []
    applied = list(operation.applied_subject_ids or [])
    if operation.operation_type == "DEPRECATE_TERM":
        return True, [ImpactSubject("GLOSSARY_TERM", str(term_id)) for term_id in applied]
    if operation.operation_type == "CLASSIFY" or operation.subject_type == "COLUMN":
        return True, await _column_subjects(session, applied)
    subjects = [ImpactSubject("TABLE", str(table_id)) for table_id in applied]
    term_id = (operation.parameters or {}).get("term_id")
    if operation.operation_type == "LINK_TERM" and term_id:
        subjects.append(ImpactSubject("GLOSSARY_TERM", str(term_id)))
    return True, subjects


async def _enrichment_subjects(
    session: AsyncSession, sample: ReviewAuditSample, review: GovernanceReview
) -> list[ImpactSubject]:
    proposal_id = _uuid(review.object_id)
    if proposal_id is None:
        return []
    annotation = await session.scalar(
        select(MetadataBusinessAnnotation).where(
            MetadataBusinessAnnotation.organization_id == sample.organization_id,
            MetadataBusinessAnnotation.source_proposal_id == proposal_id,
        )
    )
    if annotation is None:
        return []
    versions = list(
        await session.scalars(
            select(MetadataBusinessAnnotationVersion).where(
                MetadataBusinessAnnotationVersion.annotation_id == annotation.id,
                MetadataBusinessAnnotationVersion.approved_by == sample.agent_principal_id,
            )
        )
    )
    if not versions:
        return []
    # The version this decision published is the agent's approval stamped
    # nearest the decision: the approval writes it inside the decision itself.
    # Nearest rather than equal, so two clock reads a microsecond apart in one
    # transaction still resolve, and a later approval by the same agent does not.
    anchor = _aware(review.decided_at) if review.decided_at is not None else None
    chosen = (
        min(versions, key=lambda version: abs(_aware(version.approved_at) - anchor))
        if anchor is not None
        else max(versions, key=lambda version: version.version)
    )
    return [
        ImpactSubject(
            "BUSINESS_ANNOTATION", str(annotation.id), annotation_version_id=str(chosen.id)
        )
    ]


async def _import_subjects(
    session: AsyncSession, sample: ReviewAuditSample, review: GovernanceReview
) -> list[ImpactSubject]:
    batch = await session.scalar(
        select(ModelImportBatch).where(
            ModelImportBatch.governance_review_id == review.id,
            ModelImportBatch.organization_id == sample.organization_id,
        )
    )
    if batch is None:
        return []
    changes = list(
        await session.scalars(
            select(ModelImportChange).where(
                ModelImportChange.batch_id == batch.id,
                ModelImportChange.status == "APPLIED",
            )
        )
    )
    subjects: list[ImpactSubject] = []
    column_ids: list[str] = []
    annotation_versions: dict[str, set[int]] = {}
    for change in changes:
        if change.subject_type == "COLUMN":
            column_ids.append(change.subject_id)
        elif change.field == "readme":
            subjects.append(ImpactSubject("TABLE", change.subject_id))
        elif change.published_version is not None:
            annotation_versions.setdefault(change.subject_id, set()).add(change.published_version)
    subjects.extend(await _column_subjects(session, column_ids))
    for table_id, numbers in annotation_versions.items():
        for parsed in _uuids([table_id]):
            subjects.extend(await _annotation_version_subjects(session, parsed, numbers))
    return subjects


async def decision_subjects(
    session: AsyncSession, sample: ReviewAuditSample, review: GovernanceReview | None
) -> tuple[bool, list[ImpactSubject]]:
    """What the sampled decision changed, and whether any of it can reach an answer.

    Walked from the sample, never supplied: the sample names its review, and
    the review names the object whose approval made the change.
    """
    if sample.object_type == "BULK_STEWARDSHIP_OPERATION":
        return await _bulk_operation_subjects(session, sample)
    if review is None:
        return True, []
    if sample.object_type == "METADATA_ENRICHMENT_PROPOSAL":
        subjects = await _enrichment_subjects(session, sample, review)
    elif sample.object_type == "MODEL_IMPORT_BATCH":
        subjects = await _import_subjects(session, sample, review)
    elif sample.object_type in (
        "ASSET_DESCRIPTION_DRAFT",
        "COLUMN_DESCRIPTION_DRAFT",
        "ROUTINE_DESCRIPTION_DRAFT",
    ):
        draft_id = _uuid(review.object_id)
        subjects = []
        if draft_id is not None and sample.object_type == "COLUMN_DESCRIPTION_DRAFT":
            column_draft = await session.get(ColumnDescriptionDraft, draft_id)
            if column_draft is not None and column_draft.published_version_id is not None:
                subjects = [
                    ImpactSubject(
                        "COLUMN",
                        str(column_draft.column_id),
                        table_id=str(column_draft.table_id),
                    )
                ]
        elif draft_id is not None and sample.object_type == "ROUTINE_DESCRIPTION_DRAFT":
            # R11-FP08. The subject is the routine itself, and no `table_id` is
            # carried: a routine hangs off a schema, not a table, and the tables
            # it touches are *lineage*, not what the description changed.
            # Naming one of them here would report every answer that consulted
            # a table the procedure happens to write as having relied on the
            # procedure's description, which is over-inclusive past the point of
            # being useful -- `ASSET_IN_CONTEXT` is deliberately broad, not
            # unbounded. A routine's own identity does appear in retrieval
            # evidence, so `ASSET_IN_CONTEXT` still matches the runs that read
            # it.
            routine_draft = await session.get(RoutineDescriptionDraft, draft_id)
            if routine_draft is not None and routine_draft.published_version_id is not None:
                subjects = [ImpactSubject("ROUTINE", str(routine_draft.routine_id))]
        elif draft_id is not None:
            asset_draft = await session.get(AssetDescriptionDraft, draft_id)
            if asset_draft is not None and asset_draft.published_version_id is not None:
                subjects = [ImpactSubject("TABLE", str(asset_draft.table_id))]
    else:
        subjects = []
    # One subject per identity, in a stable order, however many changes named it.
    unique = {
        (s.object_type, s.object_id, s.annotation_version_id): s for s in subjects
    }
    return True, [unique[key] for key in sorted(unique, key=lambda key: tuple(map(str, key)))]


def _status(raw: str, *, pending: str, applied: str) -> str:
    if raw == pending:
        return "PENDING"
    if raw == applied:
        return "APPLIED"
    return "REJECTED"


async def correction_state(
    session: AsyncSession, sample: ReviewAuditSample
) -> CorrectionState | None:
    """The newest correction raised from this sample, through any of the three paths."""
    found: list[tuple[datetime, CorrectionState]] = []
    reversal = await session.scalar(
        select(BulkStewardshipOperation)
        .where(
            BulkStewardshipOperation.review_audit_sample_id == sample.id,
            BulkStewardshipOperation.reverses_operation_id.is_not(None),
        )
        .order_by(BulkStewardshipOperation.created_at.desc())
        .limit(1)
    )
    if reversal is not None:
        status = _status(reversal.status, pending="REVIEW_REQUIRED", applied="APPLIED")
        found.append(
            (
                _aware(reversal.created_at),
                CorrectionState(
                    "BULK_REVERSAL",
                    reversal.id,
                    status,
                    reversal.applied_at if status == "APPLIED" else None,
                ),
            )
        )
    withdrawal = await session.scalar(
        select(DescriptionWithdrawal)
        .where(DescriptionWithdrawal.review_audit_sample_id == sample.id)
        .order_by(DescriptionWithdrawal.created_at.desc())
        .limit(1)
    )
    if withdrawal is not None:
        status = _status(withdrawal.status, pending="PENDING_REVIEW", applied="APPROVED")
        found.append(
            (
                _aware(withdrawal.created_at),
                CorrectionState(
                    "DESCRIPTION_WITHDRAWAL",
                    withdrawal.id,
                    status,
                    withdrawal.reviewed_at if status == "APPLIED" else None,
                ),
            )
        )
    import_reversal = await session.scalar(
        select(ModelImportBatch)
        .where(
            ModelImportBatch.review_audit_sample_id == sample.id,
            ModelImportBatch.reverses_batch_id.is_not(None),
        )
        .order_by(ModelImportBatch.created_at.desc())
        .limit(1)
    )
    if import_reversal is not None:
        status = _status(import_reversal.status, pending="PENDING_REVIEW", applied="APPLIED")
        found.append(
            (
                _aware(import_reversal.created_at),
                CorrectionState(
                    "IMPORT_REVERSAL",
                    import_reversal.id,
                    status,
                    import_reversal.reviewed_at if status == "APPLIED" else None,
                ),
            )
        )
    if not found:
        return None
    return max(found, key=lambda item: item[0])[1]


def _cited(
    grounding: list[dict[str, Any]] | None, retrieval: list[dict[str, Any]] | None
) -> tuple[set[str], set[str]]:
    """What one run touched: annotation versions it cited, and `TYPE:id` assets."""
    versions: set[str] = set()
    assets: set[str] = set()
    for entry in grounding or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("annotation_version_id"):
            versions.add(str(entry["annotation_version_id"]))
        if entry.get("object_type") and entry.get("object_id"):
            assets.add(f"{entry['object_type']}:{entry['object_id']}")
    for hit in retrieval or []:
        if not isinstance(hit, dict):
            continue
        if hit.get("object_type") and hit.get("object_id"):
            assets.add(f"{hit['object_type']}:{hit['object_id']}")
        metadata = hit.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue
        # The two keys `_model_context` reads to decide which tables it hydrates.
        for key in ("table_id", "source_table_id"):
            if metadata.get(key):
                assets.add(f"TABLE:{metadata[key]}")
        if metadata.get("term_id"):
            assets.add(f"GLOSSARY_TERM:{metadata['term_id']}")
        for term_id in metadata.get("bound_term_ids") or []:
            assets.add(f"GLOSSARY_TERM:{term_id}")
    return versions, assets


def _matches(
    subjects: Iterable[ImpactSubject], versions: set[str], assets: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    bases: set[str] = set()
    matched: set[str] = set()
    for subject in subjects:
        if subject.annotation_version_id is not None:
            # An annotation's content reaches a run only through its own hit, so
            # only the exact version counts -- not the table it annotates.
            if subject.annotation_version_id in versions:
                bases.add(EXACT_VERSION)
                matched.add(subject.object_id)
            continue
        keys = {f"{subject.object_type}:{subject.object_id}"}
        if subject.table_id is not None:
            # A column's classification is hydrated with every column of its table.
            keys.add(f"TABLE:{subject.table_id}")
        if keys & assets:
            bases.add(ASSET_IN_CONTEXT)
            matched.add(subject.object_id)
    return tuple(sorted(bases)), tuple(sorted(matched))


async def downstream_impact(session: AsyncSession, sample: ReviewAuditSample) -> DownstreamImpact:
    """The answers that relied on `sample`'s decision while it stood."""
    review = await session.get(GovernanceReview, sample.governance_review_id)
    window_start = _aware(
        review.decided_at if review is not None and review.decided_at else sample.sampled_at
    )
    correction = await correction_state(session, sample)
    window_end = (
        _aware(correction.effective_at)
        if correction is not None
        and correction.status == "APPLIED"
        and correction.effective_at is not None
        else None
    )
    reaches_answers, subjects = await decision_subjects(session, sample, review)
    if not reaches_answers or not subjects:
        return DownstreamImpact(
            window_start, window_end, correction, reaches_answers, tuple(subjects), 0, False, ()
        )

    query = select(
        AgentRun.id,
        AgentRun.created_at,
        AgentRun.datasource_id,
        AgentRun.principal_id,
        AgentRun.status,
        AgentRun.grounding_fragment_digests,
        AgentRun.retrieval_evidence,
    ).where(
        AgentRun.organization_id == sample.organization_id,
        AgentRun.created_at >= window_start,
    )
    if window_end is not None:
        query = query.where(AgentRun.created_at <= window_end)
    rows = (
        await session.execute(
            query.order_by(AgentRun.created_at, AgentRun.id).limit(MAX_SCANNED_RUNS + 1)
        )
    ).all()
    truncated = len(rows) > MAX_SCANNED_RUNS
    rows = rows[:MAX_SCANNED_RUNS]

    affected: list[AffectedRun] = []
    for row in rows:
        versions, assets = _cited(row.grounding_fragment_digests, row.retrieval_evidence)
        bases, matched = _matches(subjects, versions, assets)
        if bases:
            affected.append(
                AffectedRun(
                    agent_run_id=row.id,
                    created_at=_aware(row.created_at),
                    datasource_id=row.datasource_id,
                    principal_id=row.principal_id,
                    status=row.status,
                    bases=bases,
                    matched_object_ids=matched,
                )
            )
    return DownstreamImpact(
        window_start,
        window_end,
        correction,
        reaches_answers,
        tuple(subjects),
        len(rows),
        truncated,
        tuple(affected),
    )
