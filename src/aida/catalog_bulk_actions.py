"""CT-1: bulk catalog stewardship actions (tag, classify, own, certify).

This module implements the *planning* half of each bulk action as small, pure
functions that operate on already-fetched ORM rows (no session, no I/O). The
API layer (see ``aida.api``) is responsible for the bounded database fetches
and for persisting whatever a plan decides to write. Keeping the two apart
means the partial-success behaviour that CT-1 requires -- "which items
succeeded, which failed and why" -- is exercised directly in tests without a
database.

Ownership and certification reuse the exact fields and idempotency rules
already established by ``aida.stewardship_service.apply_bulk_operation``
(subject_type/subject_id keying, supersede-then-create for certification);
this module adds an immediate, per-item partial-success execution path next
to that review-gated workflow, plus the previously-missing tag and classify
actions described by module 04 (catalog).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any
from uuid import UUID

from aida.models import (
    AssetCertification,
    AssetTag,
    MetadataColumn,
    MetadataTable,
    OwnershipAssignment,
)
from aida.query_gateway import SENSITIVE_CLASSES

# A single bulk request may touch at most this many subjects, whether the
# caller supplied an explicit ID list (rejected outright above this size) or
# a filter (silently truncated -- see `match_tables_by_filter`). This keeps
# every bulk action bounded in spirit of CT-2 (no unbounded catalog scans).
CATALOG_BULK_ACTION_MAX_ITEMS = 500

# Filter-mode selection scans at most this many candidate rows before giving
# up on finding more matches, mirroring the existing bound used by
# `apply_ownership_rule` in stewardship_api.py.
CATALOG_BULK_FILTER_SCAN_CAP = 10_000

ALLOWED_CLASSIFICATIONS = frozenset({"UNCLASSIFIED", "PUBLIC", "INTERNAL"} | SENSITIVE_CLASSES)

_TABLE_MATCH_FIELDS = ("TABLE_NAME", "SCHEMA_NAME", "QUALIFIED_NAME")


@dataclass(frozen=True)
class BulkItemResult:
    subject_id: str
    status: str
    reason: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"subject_id": self.subject_id, "status": self.status, "reason": self.reason}


@dataclass
class BulkPlan:
    results: list[BulkItemResult] = field(default_factory=list)
    new_rows: list[Any] = field(default_factory=list)

    @property
    def succeeded_count(self) -> int:
        return sum(1 for item in self.results if item.status == "SUCCEEDED")

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.results if item.status == "FAILED")


def dedupe_preserving_order(values: Sequence[UUID]) -> list[UUID]:
    return list(dict.fromkeys(values))


def match_tables_by_filter(
    candidates: Sequence[tuple[MetadataTable, str]],
    *,
    match_field: str,
    match_pattern: str,
    cap: int = CATALOG_BULK_ACTION_MAX_ITEMS,
) -> tuple[list[UUID], bool]:
    """Select active table ids whose name/schema/qualified-name matches a pattern.

    Returns the matched ids (capped at ``cap``) and whether the match set was
    truncated by the cap. Case-insensitive shell-style matching, the same
    matcher already used by ``apply_ownership_rule``.
    """
    if match_field not in _TABLE_MATCH_FIELDS:
        raise ValueError(f"unsupported match_field: {match_field}")
    matched: list[UUID] = []
    truncated = False
    normalized_pattern = match_pattern.casefold()
    for table, schema_name in candidates:
        value = {
            "TABLE_NAME": table.name,
            "SCHEMA_NAME": schema_name,
            "QUALIFIED_NAME": f"{schema_name}.{table.name}",
        }[match_field]
        if not fnmatchcase(value.casefold(), normalized_pattern):
            continue
        if len(matched) >= cap:
            truncated = True
            break
        matched.append(table.id)
    return matched, truncated


def match_columns_by_pattern(
    candidates: Sequence[MetadataColumn],
    *,
    name_pattern: str,
    cap: int = CATALOG_BULK_ACTION_MAX_ITEMS,
) -> tuple[list[UUID], bool]:
    matched: list[UUID] = []
    truncated = False
    normalized_pattern = name_pattern.casefold()
    for column in candidates:
        if not fnmatchcase(column.name.casefold(), normalized_pattern):
            continue
        if len(matched) >= cap:
            truncated = True
            break
        matched.append(column.id)
    return matched, truncated


def plan_tag(
    subject_ids: Sequence[UUID],
    *,
    tables: Mapping[UUID, MetadataTable],
    existing_tags: Mapping[UUID, AssetTag],
    organization_id: UUID,
    tag_key: str,
    tag_value: str | None,
    applied_by: str,
) -> BulkPlan:
    results: list[BulkItemResult] = []
    new_rows: list[AssetTag] = []
    for subject_id in subject_ids:
        table = tables.get(subject_id)
        if table is None:
            results.append(
                BulkItemResult(str(subject_id), "FAILED", "table not found in this organization")
            )
            continue
        if table.status != "ACTIVE":
            results.append(
                BulkItemResult(
                    str(subject_id), "FAILED", f"table status is {table.status}, not ACTIVE"
                )
            )
            continue
        existing = existing_tags.get(subject_id)
        if existing is not None:
            existing.tag_value = tag_value
            existing.applied_by = applied_by
        else:
            new_rows.append(
                AssetTag(
                    organization_id=organization_id,
                    table_id=subject_id,
                    tag_key=tag_key,
                    tag_value=tag_value,
                    applied_by=applied_by,
                )
            )
        results.append(BulkItemResult(str(subject_id), "SUCCEEDED", None))
    return BulkPlan(results=results, new_rows=new_rows)


def plan_classify(
    subject_ids: Sequence[UUID],
    *,
    columns: Mapping[UUID, tuple[MetadataColumn, MetadataTable]],
    classification: str,
) -> BulkPlan:
    results: list[BulkItemResult] = []
    for subject_id in subject_ids:
        found = columns.get(subject_id)
        if found is None:
            results.append(
                BulkItemResult(str(subject_id), "FAILED", "column not found in this organization")
            )
            continue
        column, table = found
        if column.status != "ACTIVE":
            results.append(
                BulkItemResult(
                    str(subject_id), "FAILED", f"column status is {column.status}, not ACTIVE"
                )
            )
            continue
        if table.status != "ACTIVE":
            results.append(
                BulkItemResult(
                    str(subject_id),
                    "FAILED",
                    f"parent table status is {table.status}, not ACTIVE",
                )
            )
            continue
        column.classification = classification
        results.append(BulkItemResult(str(subject_id), "SUCCEEDED", None))
    return BulkPlan(results=results, new_rows=[])


def plan_own(
    subject_ids: Sequence[UUID],
    *,
    tables: Mapping[UUID, MetadataTable],
    existing_assignments: Mapping[UUID, OwnershipAssignment],
    organization_id: UUID,
    owner_type: str,
    owner_principal: str,
    assigned_by: str,
) -> BulkPlan:
    results: list[BulkItemResult] = []
    new_rows: list[OwnershipAssignment] = []
    for subject_id in subject_ids:
        table = tables.get(subject_id)
        if table is None:
            results.append(
                BulkItemResult(str(subject_id), "FAILED", "table not found in this organization")
            )
            continue
        if table.status != "ACTIVE":
            results.append(
                BulkItemResult(
                    str(subject_id), "FAILED", f"table status is {table.status}, not ACTIVE"
                )
            )
            continue
        existing = existing_assignments.get(subject_id)
        if existing is not None:
            existing.status = "ACTIVE"
            existing.assigned_by = assigned_by
        else:
            new_rows.append(
                OwnershipAssignment(
                    organization_id=organization_id,
                    subject_type="TABLE",
                    subject_id=str(subject_id),
                    owner_type=owner_type,
                    owner_principal=owner_principal,
                    assignment_kind="BULK",
                    assigned_by=assigned_by,
                )
            )
        results.append(BulkItemResult(str(subject_id), "SUCCEEDED", None))
    return BulkPlan(results=results, new_rows=new_rows)


def plan_certify(
    subject_ids: Sequence[UUID],
    *,
    tables: Mapping[UUID, MetadataTable],
    active_certifications: Mapping[UUID, Sequence[AssetCertification]],
    organization_id: UUID,
    rationale: str,
    expires_at: datetime,
    certified_by: str,
) -> BulkPlan:
    results: list[BulkItemResult] = []
    new_rows: list[AssetCertification] = []
    for subject_id in subject_ids:
        table = tables.get(subject_id)
        if table is None:
            results.append(
                BulkItemResult(str(subject_id), "FAILED", "table not found in this organization")
            )
            continue
        if table.status != "ACTIVE":
            results.append(
                BulkItemResult(
                    str(subject_id), "FAILED", f"table status is {table.status}, not ACTIVE"
                )
            )
            continue
        for prior in active_certifications.get(subject_id, ()):
            prior.status = "SUPERSEDED"
        new_rows.append(
            AssetCertification(
                organization_id=organization_id,
                table_id=subject_id,
                rationale=rationale,
                certified_by=certified_by,
                expires_at=expires_at,
            )
        )
        results.append(BulkItemResult(str(subject_id), "SUCCEEDED", None))
    return BulkPlan(results=results, new_rows=new_rows)
