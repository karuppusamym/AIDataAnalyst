"""R11-REV01: a playbook rule dry-run with versioned evidence, and each action's real
automation path stated per action.

**What a dry-run is.** `dry_run_playbook` resolves a playbook's filter with the very matcher
its scheduled run uses (`aida.playbooks.resolve_playbook_matches_detailed`), reads each
matched subject's current state, and reports -- without writing anything, not even
`last_run_at` -- which subjects the rule would act on, the before/after of each, the
evidence version each was evaluated at, and what the run would do with the set: apply it
without a human, queue it for review, or nothing.

**Versioned evidence.** Two fingerprints, both SHA-256:

* `rule_version` -- the rule itself: action, datasource, match field and patterns, action
  parameters and the auto-apply bound. Editing any of them changes it.
* each item's `evidence_version` -- the subject as the rule saw it: its id, catalog
  fingerprint, status, qualified name and the current value of the attribute the action
  writes (the tag value, the classification, the owner assignment, the active
  certifications). Re-running the dry-run after the catalog moves shows which subjects moved.

`GET .../dry-run` persists neither: that dry-run is a read. **Storing and binding** is the
other half. `store_dry_run` records a preview as a `PlaybookDryRunRecord` -- the rule version,
a digest of *which* subjects matched (`match_digest`), a digest of the state each was in
(`evidence_digest`), and the per-subject `[id, evidence version]` pairs -- ids and hashes only.
`run_bound_to_dry_run` then runs the playbook *as previewed*: it re-evaluates the rule in the
run's own transaction, compares that to the record (`compare_to_preview`: the rule, the match
set, each subject's version), and by default refuses to run at all when anything moved,
naming how many subjects were added, removed or changed. When it does run, it checks the
subjects the run actually acted on -- read back from the run's own record, the bulk action
run's per-subject results or the queued operation's subject list -- against that same
evaluation, so a catalog change landing between the check and the run cannot slip through:
the caller rolls the run back. A record binds at most one run, and says which.

**Automation, per action, not globally.** Every one of the four playbook actions *has* an
automatic branch: `evaluate_and_run_playbook` applies the action without any human decision,
as the `fleet-scheduler` worker principal, when `0 < matched <= auto_apply_max_items`
(`aida.playbooks._auto_apply`). The default bound is 0, which disables it; a steward can
raise it per playbook. It is deterministic rule application -- no model is consulted, so it
is not unattended LLM approval -- but it *is* unattended application, and the dry-run says so
for the specific action and bound rather than describing playbooks as human-only. Above the
bound the action is queued as a `BULK_STEWARDSHIP_OPERATION` for maker-checker review, whose
approval has a governed compensating operation. An auto-applied run records per-subject
results in `catalog_bulk_action_run` but no before-image, so it has no governed reversal;
that is reported as such.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.events import record_audit
from aida.models import (
    AssetCertification,
    AssetTag,
    BulkStewardshipOperation,
    CatalogBulkActionRun,
    MetadataColumn,
    MetadataPlaybook,
    MetadataSchema,
    MetadataTable,
    OwnershipAssignment,
)
from aida.playbooks import (
    PlaybookRunOutcome,
    evaluate_and_run_playbook,
    resolve_playbook_matches_detailed,
)
from aida.review_batch_models import PlaybookDryRunRecord
from aida.security_types import SecurityContext


@dataclass(frozen=True, slots=True)
class ActionAutomation:
    """How one playbook action is actually applied, stated for that action."""

    action: str
    subject_type: str
    has_automatic_branch: bool
    automatic_path: str
    automatic_principal: str
    involves_model: bool
    reviewed_operation_type: str
    compensating_operation_when_reviewed: str
    compensating_operation_when_automatic: str | None
    automatic_correction_reason: str | None


#: One entry per action `aida.playbooks` supports. `tests/test_playbook_dry_run.py` keeps the
#: reviewed/compensating pairs in step with `stewardship_service._REVERSAL_OF` and the
#: operation types with `playbooks._OPERATION_TYPE_FOR_ACTION`.
PLAYBOOK_ACTION_AUTOMATION: Final[dict[str, ActionAutomation]] = {
    action: ActionAutomation(
        action=action,
        subject_type="COLUMN" if action == "CLASSIFY" else "TABLE",
        has_automatic_branch=True,
        automatic_path="aida.playbooks._auto_apply",
        automatic_principal="fleet-scheduler",
        involves_model=False,
        reviewed_operation_type=operation_type,
        compensating_operation_when_reviewed=compensating,
        compensating_operation_when_automatic=None,
        automatic_correction_reason="NO_BEFORE_IMAGE_RECORDED",
    )
    for action, operation_type, compensating in (
        ("TAG", "TAG", "RESTORE_TAG"),
        ("CLASSIFY", "CLASSIFY", "RESTORE_CLASSIFICATION"),
        ("OWN", "ASSIGN_OWNERSHIP", "WITHDRAW_OWNERSHIP"),
        ("CERTIFY", "CERTIFY_ASSET", "WITHDRAW_CERTIFICATION"),
    )
}


@dataclass(frozen=True, slots=True)
class DryRunItem:
    subject_type: str
    subject_id: UUID
    qualified_name: str
    current_value: str | None
    proposed_value: str | None
    change: str  # CREATE | UPDATE | NO_CHANGE | SUPERSEDE
    evidence_version: str


@dataclass(slots=True)
class PlaybookDryRun:
    playbook_id: UUID
    action: str
    enabled: bool
    rule_version: str
    evaluated_at: datetime
    matched_count: int
    tables_truncated: bool
    columns_truncated: bool
    auto_apply_max_items: int
    #: NO_MATCHES | AUTOMATIC | HUMAN_REVIEW -- what `evaluate_and_run_playbook` would do now.
    predicted_disposition: str
    automation: ActionAutomation
    items: list[DryRunItem] = field(default_factory=list)
    #: The matcher's own subject list, in its order -- what `match_digest` is taken over.
    subject_ids: list[UUID] = field(default_factory=list)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def rule_version(playbook: MetadataPlaybook) -> str:
    return _digest(
        {
            "action": playbook.action,
            "datasource_id": str(playbook.datasource_id),
            "match_field": playbook.match_field,
            "match_pattern": playbook.match_pattern,
            "column_name_pattern": playbook.column_name_pattern,
            "action_parameters": playbook.action_parameters,
            "auto_apply_max_items": playbook.auto_apply_max_items,
        }
    )


def predicted_disposition(matched_count: int, auto_apply_max_items: int) -> str:
    """The same comparison `evaluate_and_run_playbook` makes, named."""
    if matched_count == 0:
        return "NO_MATCHES"
    if matched_count <= auto_apply_max_items:
        return "AUTOMATIC"
    return "HUMAN_REVIEW"


async def _table_items(
    session: AsyncSession, playbook: MetadataPlaybook, subject_ids: list[UUID]
) -> list[DryRunItem]:
    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.organization_id == playbook.organization_id,
                MetadataTable.id.in_(subject_ids),
            )
        )
    ).all()
    tables = {row[0].id: (row[0], row[1]) for row in rows}
    params = playbook.action_parameters
    current: dict[UUID, str | None] = {}
    proposed: str | None
    if playbook.action == "TAG":
        tags = (
            await session.scalars(
                select(AssetTag).where(
                    AssetTag.organization_id == playbook.organization_id,
                    AssetTag.table_id.in_(subject_ids),
                    AssetTag.tag_key == params["tag_key"],
                )
            )
        ).all()
        present = {row.table_id: row.tag_value for row in tags}
        proposed = f"{params['tag_key']}={params.get('tag_value')}"
        current = {
            key: f"{params['tag_key']}={value}" for key, value in present.items()
        }
    elif playbook.action == "OWN":
        assignments = (
            await session.scalars(
                select(OwnershipAssignment).where(
                    OwnershipAssignment.organization_id == playbook.organization_id,
                    OwnershipAssignment.subject_type == "TABLE",
                    OwnershipAssignment.subject_id.in_([str(value) for value in subject_ids]),
                    OwnershipAssignment.owner_type == params["owner_type"],
                    OwnershipAssignment.owner_principal == params["owner_principal"],
                )
            )
        ).all()
        current = {
            UUID(row.subject_id): f"{row.owner_type}:{row.owner_principal}:{row.status}"
            for row in assignments
        }
        proposed = f"{params['owner_type']}:{params['owner_principal']}:ACTIVE"
    else:
        certifications = (
            await session.scalars(
                select(AssetCertification).where(
                    AssetCertification.organization_id == playbook.organization_id,
                    AssetCertification.table_id.in_(subject_ids),
                    AssetCertification.asset_type == "TABLE",
                    AssetCertification.status == "ACTIVE",
                )
            )
        ).all()
        active: dict[UUID, int] = {}
        for row in certifications:
            active[row.table_id] = active.get(row.table_id, 0) + 1
        current = {key: f"ACTIVE_CERTIFICATIONS:{count}" for key, count in active.items()}
        proposed = f"CERTIFIED:expires_after_days={params.get('expires_after_days')}"

    items: list[DryRunItem] = []
    for subject_id in subject_ids:
        found = tables.get(subject_id)
        if found is None:
            continue
        table, schema_name = found
        before = current.get(subject_id)
        if playbook.action == "CERTIFY":
            change = "SUPERSEDE" if before is not None else "CREATE"
        elif before is None:
            change = "CREATE"
        elif before == proposed:
            change = "NO_CHANGE"
        else:
            change = "UPDATE"
        items.append(
            DryRunItem(
                subject_type="TABLE",
                subject_id=subject_id,
                qualified_name=f"{schema_name}.{table.name}",
                current_value=before,
                proposed_value=proposed,
                change=change,
                evidence_version=_digest(
                    [
                        str(table.id),
                        table.fingerprint,
                        table.status,
                        schema_name,
                        table.name,
                        before,
                    ]
                ),
            )
        )
    return items


async def _column_items(
    session: AsyncSession, playbook: MetadataPlaybook, subject_ids: list[UUID]
) -> list[DryRunItem]:
    rows = (
        await session.execute(
            select(MetadataColumn, MetadataTable.name, MetadataSchema.name)
            .join(MetadataTable, MetadataTable.id == MetadataColumn.table_id)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .where(
                MetadataTable.organization_id == playbook.organization_id,
                MetadataColumn.id.in_(subject_ids),
            )
        )
    ).all()
    columns = {row[0].id: (row[0], row[1], row[2]) for row in rows}
    proposed = str(playbook.action_parameters["classification"])
    items: list[DryRunItem] = []
    for subject_id in subject_ids:
        found = columns.get(subject_id)
        if found is None:
            continue
        column, table_name, schema_name = found
        before = column.classification
        items.append(
            DryRunItem(
                subject_type="COLUMN",
                subject_id=subject_id,
                qualified_name=f"{schema_name}.{table_name}.{column.name}",
                current_value=before,
                proposed_value=proposed,
                change="NO_CHANGE" if before == proposed else "UPDATE",
                evidence_version=_digest(
                    [
                        str(column.id),
                        column.fingerprint,
                        column.status,
                        schema_name,
                        table_name,
                        column.name,
                        before,
                    ]
                ),
            )
        )
    return items


async def dry_run_playbook(
    session: AsyncSession, playbook: MetadataPlaybook, *, now: datetime
) -> PlaybookDryRun:
    """Evaluate a playbook's rule without applying it. Writes nothing.

    Statements: the matcher's own (one table scan bounded by `CATALOG_BULK_FILTER_SCAN_CAP`,
    plus one column load for CLASSIFY) and at most two more for current state. None grows
    with the number of matched subjects beyond the matcher's own bounded read.
    """
    matches = await resolve_playbook_matches_detailed(session, playbook)
    subject_ids = matches.subject_ids
    if not subject_ids:
        items: list[DryRunItem] = []
    elif playbook.action == "CLASSIFY":
        items = await _column_items(session, playbook, subject_ids)
    else:
        items = await _table_items(session, playbook, subject_ids)
    return PlaybookDryRun(
        playbook_id=playbook.id,
        action=playbook.action,
        enabled=playbook.enabled,
        rule_version=rule_version(playbook),
        evaluated_at=now,
        matched_count=len(subject_ids),
        tables_truncated=matches.tables_truncated,
        columns_truncated=matches.columns_truncated,
        auto_apply_max_items=playbook.auto_apply_max_items,
        predicted_disposition=predicted_disposition(
            len(subject_ids), playbook.auto_apply_max_items
        ),
        automation=PLAYBOOK_ACTION_AUTOMATION[playbook.action],
        items=items,
        subject_ids=list(subject_ids),
    )


# ---------------------------------------------------------------------------
# Stored dry-runs, and runs bound to them
# ---------------------------------------------------------------------------

#: How many moved subject ids a binding names; the counts are always complete.
BINDING_SAMPLE_MAX: Final = 50


def match_digest(subject_ids: Sequence[UUID]) -> str:
    """Which subjects matched, independent of order: SHA-256 over the sorted ids."""
    return _digest(sorted(str(value) for value in subject_ids))


def evidence_digest(pairs: Sequence[tuple[str, str]]) -> str:
    """The state the matched subjects were in: SHA-256 over sorted (id, evidence version)."""
    return _digest(sorted([subject_id, version] for subject_id, version in pairs))


def _pairs(preview: PlaybookDryRun) -> list[tuple[str, str]]:
    return [(str(item.subject_id), item.evidence_version) for item in preview.items]


async def store_dry_run(
    session: AsyncSession,
    playbook: MetadataPlaybook,
    preview: PlaybookDryRun,
    *,
    context: SecurityContext,
) -> PlaybookDryRunRecord:
    """Record a preview so a later run can be bound to it. The caller commits."""
    pairs = _pairs(preview)
    record = PlaybookDryRunRecord(
        organization_id=playbook.organization_id,
        playbook_id=playbook.id,
        action=preview.action,
        rule_version=preview.rule_version,
        match_digest=match_digest(preview.subject_ids),
        evidence_digest=evidence_digest(pairs),
        matched_count=preview.matched_count,
        tables_truncated=preview.tables_truncated,
        columns_truncated=preview.columns_truncated,
        auto_apply_max_items=preview.auto_apply_max_items,
        predicted_disposition=preview.predicted_disposition,
        change_counts=dict(Counter(item.change for item in preview.items)),
        subject_versions=[list(pair) for pair in pairs],
        evaluated_by=context.principal_id,
        evaluated_at=preview.evaluated_at,
    )
    session.add(record)
    await session.flush()
    record_audit(
        session,
        context,
        action="playbook.dry_run_store",
        resource_type="metadata_playbook",
        resource_id=str(playbook.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "dry_run_id": str(record.id),
            "rule_version": record.rule_version,
            "match_digest": record.match_digest,
            "evidence_digest": record.evidence_digest,
            "matched_count": record.matched_count,
            "predicted_disposition": record.predicted_disposition,
        },
    )
    return record


@dataclass(frozen=True, slots=True)
class PreviewBinding:
    """How a run's own evaluation compares to the preview it is bound to."""

    #: MATCHES (same rule, same subjects, each at the same version) or DIFFERS.
    status: str
    rule_version_matches: bool
    match_set_matches: bool
    evidence_matches: bool
    #: Matched now but not previewed / previewed but not matched now / in both, but moved.
    added_count: int
    removed_count: int
    changed_count: int
    #: Up to BINDING_SAMPLE_MAX of those subjects, added first, then removed, then changed.
    moved_subject_ids: tuple[UUID, ...]
    #: RULE_CHANGED, MATCH_SET_CHANGED, EVIDENCE_CHANGED, RUN_DIVERGED_FROM_PREVIEW.
    reasons: tuple[str, ...]


def compare_to_preview(record: PlaybookDryRunRecord, current: PlaybookDryRun) -> PreviewBinding:
    previewed = {str(subject_id): version for subject_id, version in record.subject_versions}
    now = dict(_pairs(current))
    added = [key for key in now if key not in previewed]
    removed = [key for key in previewed if key not in now]
    changed = [key for key in now if key in previewed and previewed[key] != now[key]]
    rule_matches = record.rule_version == current.rule_version
    set_matches = record.match_digest == match_digest(current.subject_ids)
    evidence_matches = record.evidence_digest == evidence_digest(list(now.items()))
    reasons = tuple(
        reason
        for reason, failed in (
            ("RULE_CHANGED", not rule_matches),
            ("MATCH_SET_CHANGED", not set_matches),
            ("EVIDENCE_CHANGED", not evidence_matches),
        )
        if failed
    )
    return PreviewBinding(
        status="DIFFERS" if reasons else "MATCHES",
        rule_version_matches=rule_matches,
        match_set_matches=set_matches,
        evidence_matches=evidence_matches,
        added_count=len(added),
        removed_count=len(removed),
        changed_count=len(changed),
        moved_subject_ids=tuple(UUID(key) for key in (*added, *removed, *changed))[
            :BINDING_SAMPLE_MAX
        ],
        reasons=reasons,
    )


class DryRunBindingError(Exception):
    """A whole-request refusal, carried as a code the router returns verbatim."""

    def __init__(self, code: str, http_status: int) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass(slots=True)
class BoundRunResult:
    record: PlaybookDryRunRecord
    binding: PreviewBinding
    ran: bool
    outcome: PlaybookRunOutcome | None = None
    #: PREVIEW_MISMATCH (refused before running) or RUN_DIVERGED_FROM_PREVIEW (the run acted
    #: on other subjects than were just checked -- the caller must roll the run back).
    refusal_code: str | None = None


async def _subjects_the_run_acted_on(
    session: AsyncSession, playbook: MetadataPlaybook, outcome: PlaybookRunOutcome
) -> list[UUID]:
    """Read back from the run's own record, not re-derived: what it actually acted on."""
    if outcome.bulk_action_run_id is not None:
        run = await session.scalar(
            select(CatalogBulkActionRun).where(
                CatalogBulkActionRun.id == outcome.bulk_action_run_id,
                CatalogBulkActionRun.organization_id == playbook.organization_id,
            )
        )
        return [UUID(str(item["subject_id"])) for item in (run.results if run else [])]
    if outcome.bulk_stewardship_operation_id is not None:
        operation = await session.scalar(
            select(BulkStewardshipOperation).where(
                BulkStewardshipOperation.id == outcome.bulk_stewardship_operation_id,
                BulkStewardshipOperation.organization_id == playbook.organization_id,
            )
        )
        return [UUID(str(value)) for value in (operation.subject_ids if operation else [])]
    return []


async def run_bound_to_dry_run(
    session: AsyncSession,
    *,
    playbook: MetadataPlaybook,
    dry_run_id: UUID,
    context: SecurityContext,
    require_match: bool,
    now: datetime,
) -> BoundRunResult:
    """Run a playbook as previewed by a stored dry-run, through the same
    `evaluate_and_run_playbook` the scheduler and `POST .../run` call. The caller commits --
    or, for `RUN_DIVERGED_FROM_PREVIEW`, rolls back.

    `require_match` (the default at the route) refuses to run unless the rule, the matched
    subjects and every subject's evidence version are exactly the previewed ones. Without it
    the run proceeds and the record says how it differed.
    """
    record = await session.scalar(
        select(PlaybookDryRunRecord)
        .where(
            PlaybookDryRunRecord.id == dry_run_id,
            PlaybookDryRunRecord.organization_id == playbook.organization_id,
            PlaybookDryRunRecord.playbook_id == playbook.id,
        )
        # Two bound runs of one preview at once: the second waits here, then sees `bound_at`.
        .with_for_update()
    )
    if record is None:
        raise DryRunBindingError("PLAYBOOK_DRY_RUN_NOT_FOUND", 404)
    if record.bound_at is not None:
        raise DryRunBindingError("PLAYBOOK_DRY_RUN_ALREADY_BOUND", 409)
    current = await dry_run_playbook(session, playbook, now=now)
    binding = compare_to_preview(record, current)
    if binding.status != "MATCHES" and require_match:
        record_audit(
            session,
            context,
            action="playbook.bound_run",
            resource_type="metadata_playbook",
            resource_id=str(playbook.id),
            outcome="REFUSED",
            correlation_id=get_correlation_id(),
            details={
                "dry_run_id": str(record.id),
                "refusal_code": "PREVIEW_MISMATCH",
                "reasons": list(binding.reasons),
                "added_count": binding.added_count,
                "removed_count": binding.removed_count,
                "changed_count": binding.changed_count,
            },
        )
        return BoundRunResult(record, binding, ran=False, refusal_code="PREVIEW_MISMATCH")

    outcome = await evaluate_and_run_playbook(session, playbook, now=now)
    acted_on = await _subjects_the_run_acted_on(session, playbook, outcome)
    if match_digest(acted_on) != match_digest(current.subject_ids):
        # Something committed between this function's evaluation and the run's own.
        diverged = PreviewBinding(
            status="DIFFERS",
            rule_version_matches=binding.rule_version_matches,
            match_set_matches=False,
            evidence_matches=binding.evidence_matches,
            added_count=binding.added_count,
            removed_count=binding.removed_count,
            changed_count=binding.changed_count,
            moved_subject_ids=binding.moved_subject_ids,
            reasons=(*binding.reasons, "RUN_DIVERGED_FROM_PREVIEW"),
        )
        if require_match:
            return BoundRunResult(
                record,
                diverged,
                ran=False,
                outcome=outcome,
                refusal_code="RUN_DIVERGED_FROM_PREVIEW",
            )
        binding = diverged

    record.bound_at = now
    record.bound_by = context.principal_id
    record.bound_binding_status = binding.status
    record.bound_run_outcome = outcome.outcome
    record.bound_bulk_action_run_id = outcome.bulk_action_run_id
    record.bound_bulk_stewardship_operation_id = outcome.bulk_stewardship_operation_id
    await session.flush()
    record_audit(
        session,
        context,
        action="playbook.bound_run",
        resource_type="metadata_playbook",
        resource_id=str(playbook.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "dry_run_id": str(record.id),
            "binding_status": binding.status,
            "reasons": list(binding.reasons),
            "run_outcome": outcome.outcome,
            "matched_count": outcome.matched_count,
            "bulk_action_run_id": (
                str(outcome.bulk_action_run_id) if outcome.bulk_action_run_id else None
            ),
            "bulk_stewardship_operation_id": (
                str(outcome.bulk_stewardship_operation_id)
                if outcome.bulk_stewardship_operation_id
                else None
            ),
        },
    )
    return BoundRunResult(record, binding, ran=True, outcome=outcome)
