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

Neither is persisted: a dry-run is a read. A later run is not bound to a dry-run's versions
(see `Remaining` in the R11-REV01 report); the preview is honest about the state it read and
the time it read it.

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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import (
    AssetCertification,
    AssetTag,
    MetadataColumn,
    MetadataPlaybook,
    MetadataSchema,
    MetadataTable,
    OwnershipAssignment,
)
from aida.playbooks import resolve_playbook_matches_detailed


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
    )
