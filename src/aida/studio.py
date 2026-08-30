"""Studio authoring environment core logic.

Provides change set management, conflict detection, diff computation,
and impact preview for semantic model objects (metrics, tools, glossary
terms, context products).

Change sets follow the lifecycle: DRAFT -> TESTING -> SUBMITTED -> MERGED/REJECTED.
Only tested change sets can be submitted for governance review.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID, uuid4

ObjectType = Literal["METRIC", "TOOL", "TERM", "CONTEXT_PRODUCT"]
Operation = Literal["CREATE", "UPDATE", "DELETE"]
ChangeSetStatus = Literal["DRAFT", "TESTING", "SUBMITTED", "MERGED", "REJECTED"]
ConflictStatus = Literal["CLEAN", "CONFLICTED", "RESOLVED"]


@dataclass
class ChangeItem:
    """One proposed change within a change set."""

    id: UUID
    object_type: ObjectType
    object_id: str
    operation: Operation
    before_snapshot: dict[str, Any] | None = None
    after_snapshot: dict[str, Any] | None = None
    diff: dict[str, Any] | None = None
    test_status: str = "UNTESTED"


@dataclass
class ChangeSet:
    """A named collection of proposed changes to governed objects."""

    id: UUID
    name: str
    author: str
    base_version: str
    items: list[ChangeItem] = field(default_factory=list)
    status: ChangeSetStatus = "DRAFT"
    conflict_status: ConflictStatus = "CLEAN"


@dataclass
class Conflict:
    """A detected conflict between a change set item and published state."""

    object_type: str
    object_id: str
    field_name: str
    change_set_value: Any
    current_value: Any


@dataclass
class Diff:
    """Structured diff between two snapshots."""

    entries: list[DiffEntry] = field(default_factory=list)


@dataclass
class DiffEntry:
    """One field-level difference."""

    field: str
    before: Any
    after: Any


@dataclass
class ImpactPreview:
    """Preview of what would change if a change set merges."""

    change_set_id: UUID
    affected_object_count: int = 0
    affected_objects: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TestFixture:
    """Synthetic test data matching the schema of a governed object."""

    object_type: str
    object_id: str
    synthetic_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestResult:
    """Result of testing one change item against fixtures."""

    passed: bool
    failures: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _compute_version_hash(items: list[dict[str, Any]]) -> str:
    """Compute a deterministic hash over a set of published objects."""
    canonical = json.dumps(sorted(items, key=lambda x: str(x)), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_change_set(name: str, author: str) -> ChangeSet:
    """Create a new change set in DRAFT status."""
    return ChangeSet(
        id=uuid4(),
        name=name,
        author=author,
        base_version=_compute_version_hash([]),
        status="DRAFT",
        conflict_status="CLEAN",
    )


def add_item(change_set: ChangeSet, item: ChangeItem) -> ChangeSet:
    """Add a change item to a change set.

    Computes the diff if both before and after snapshots are present.
    """
    if change_set.status != "DRAFT":
        raise ValueError("items can only be added to DRAFT change sets")

    if item.before_snapshot and item.after_snapshot:
        item.diff = compute_diff(item.before_snapshot, item.after_snapshot)

    change_set.items.append(item)
    return change_set


def remove_item(change_set: ChangeSet, item_id: UUID) -> ChangeSet:
    """Remove a change item from a change set."""
    if change_set.status != "DRAFT":
        raise ValueError("items can only be removed from DRAFT change sets")

    change_set.items = [i for i in change_set.items if i.id != item_id]
    return change_set


def compute_diff(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    """Compute a structured diff between two snapshots.

    Returns a dict of {field: {"before": old_value, "after": new_value}}
    for all fields that differ between the two snapshots.
    """
    diff: dict[str, Any] = {}
    all_keys = set(before.keys()) | set(after.keys())

    for key in sorted(all_keys):
        before_val = before.get(key)
        after_val = after.get(key)
        if before_val != after_val:
            diff[key] = {"before": before_val, "after": after_val}

    return diff


def detect_conflicts(
    change_set: ChangeSet,
    current_state: dict[str, dict[str, Any]],
) -> list[Conflict]:
    """Detect conflicts between a change set and the current published state.

    `current_state` maps "object_type:object_id" -> current published snapshot.
    Conflicts arise when a field the change set modifies has also changed
    in the published state since the change set was created.
    """
    conflicts: list[Conflict] = []

    for item in change_set.items:
        key = f"{item.object_type}:{item.object_id}"
        current = current_state.get(key)

        if item.operation == "CREATE":
            if current is not None:
                conflicts.append(
                    Conflict(
                        object_type=item.object_type,
                        object_id=item.object_id,
                        field_name="<exists>",
                        change_set_value="CREATE",
                        current_value="ALREADY_EXISTS",
                    )
                )
            continue

        if item.operation == "DELETE":
            if current is None:
                conflicts.append(
                    Conflict(
                        object_type=item.object_type,
                        object_id=item.object_id,
                        field_name="<exists>",
                        change_set_value="DELETE",
                        current_value="ALREADY_DELETED",
                    )
                )
            continue

        # UPDATE: check field-level conflicts
        if current is None:
            conflicts.append(
                Conflict(
                    object_type=item.object_type,
                    object_id=item.object_id,
                    field_name="<exists>",
                    change_set_value="UPDATE",
                    current_value="NOT_FOUND",
                )
            )
            continue

        if item.before_snapshot is None:
            continue

        for field_name, before_val in item.before_snapshot.items():
            current_val = current.get(field_name)
            after_val = (item.after_snapshot or {}).get(field_name)
            # If the change set modifies this field AND the published
            # state has diverged from what the change set based on
            if before_val != after_val and current_val != before_val:
                conflicts.append(
                    Conflict(
                        object_type=item.object_type,
                        object_id=item.object_id,
                        field_name=field_name,
                        change_set_value=after_val,
                        current_value=current_val,
                    )
                )

    return conflicts


def compute_impact(change_set: ChangeSet) -> ImpactPreview:
    """Compute the impact preview of merging a change set.

    Lists all objects that would be created, updated, or deleted.
    """
    affected: list[dict[str, Any]] = []

    for item in change_set.items:
        entry: dict[str, Any] = {
            "object_type": item.object_type,
            "object_id": item.object_id,
            "operation": item.operation,
        }
        if item.diff:
            entry["changed_fields"] = list(item.diff.keys())
        affected.append(entry)

    return ImpactPreview(
        change_set_id=change_set.id,
        affected_object_count=len(affected),
        affected_objects=affected,
    )
