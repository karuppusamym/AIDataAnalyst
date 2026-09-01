"""CT-1: bulk catalog stewardship actions (tag, classify, own, certify).

This module holds one ``apply_<action>_item`` function per action -- the
*single-item* core each bulk endpoint in ``aida.api`` dispatches to, one
subject at a time, inside that item's own SAVEPOINT (``session.begin_nested``).
This mirrors PG-3's ``_apply_governance_review_decision`` pattern exactly: a
single code path decides whether one subject succeeds or fails, so a
single-item bulk call and a batched one can never drift, and a failure
partway through one item's dispatch (an unmet precondition, or a database
constraint discovered only at flush time) is contained to that item's own
SAVEPOINT rather than corrupting the batch or leaking into sibling items that
already committed within the same transaction.

Each ``apply_*_item`` function either mutates an already-fetched, session-
attached ORM row in place (for an update) and returns it, or returns a
brand-new, not-yet-``session.add``-ed row for the caller to add and flush; a
precondition failure (subject not found, not ACTIVE, already in a terminal
state, ...) raises ``CatalogBulkItemError``, which the API layer catches per
item to record a FAILED result and move on to the next subject.

Ownership and certification reuse the exact fields and idempotency rules
already established by GL-2 (``OwnershipAssignment`` subject_type/subject_id
keying) and GL-5/CT-5 (``AssetCertification`` supersede-then-create,
``asset_certification.py``'s active/expiry projection) -- this module adds an
immediate, per-item partial-success execution path next to those workflows,
plus the previously-missing tag and classify actions described by module 04
(catalog).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any
from uuid import UUID

from aida.classification import SENSITIVE_CLASSES
from aida.models import (
    AssetCertification,
    AssetTag,
    MetadataColumn,
    MetadataTable,
    OwnershipAssignment,
)

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
    """Accumulates the per-item results of a bulk run as the API layer works
    through ``subject_ids`` one SAVEPOINT at a time. ``new_rows`` is unused by
    the per-item-SAVEPOINT endpoints (each row is added and flushed inside its
    own SAVEPOINT as it is decided) and stays only so any caller that still
    wants a whole-plan-at-once view has somewhere to put one.
    """

    results: list[BulkItemResult] = field(default_factory=list)
    new_rows: list[Any] = field(default_factory=list)

    @property
    def succeeded_count(self) -> int:
        return sum(1 for item in self.results if item.status == "SUCCEEDED")

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.results if item.status == "FAILED")


class CatalogBulkItemError(Exception):
    """Raised by an ``apply_*_item`` function when one subject fails a
    business-rule precondition (not found, wrong status, ...).

    The API layer catches this per item, inside that item's own SAVEPOINT, to
    record a FAILED result with ``str(exc)`` as the reason and continue to the
    next subject -- exactly PG-3's ``HTTPException``-per-item convention,
    adapted to a plain exception since these functions are ORM-only (no
    ``HTTPException`` semantics belong this far from the transport layer).
    """


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


def _require_active_table(table: MetadataTable | None) -> MetadataTable:
    if table is None:
        raise CatalogBulkItemError("table not found in this organization")
    if table.status != "ACTIVE":
        raise CatalogBulkItemError(f"table status is {table.status}, not ACTIVE")
    return table


def apply_tag_item(
    subject_id: UUID,
    *,
    tables: Mapping[UUID, MetadataTable],
    existing_tags: Mapping[UUID, AssetTag],
    organization_id: UUID,
    tag_key: str,
    tag_value: str | None,
    applied_by: str,
) -> tuple[AssetTag, bool]:
    """Apply one tag to one table. Returns ``(row, is_new)``: for an existing
    tag, ``row`` is that same object mutated in place (already session-
    attached, just needs a flush); for a new tag, ``row`` is a fresh instance
    the caller must ``session.add`` before flushing. Raises
    ``CatalogBulkItemError`` if the table is missing or not ACTIVE.
    """
    _require_active_table(tables.get(subject_id))
    existing = existing_tags.get(subject_id)
    if existing is not None:
        existing.tag_value = tag_value
        existing.applied_by = applied_by
        return existing, False
    return (
        AssetTag(
            organization_id=organization_id,
            table_id=subject_id,
            tag_key=tag_key,
            tag_value=tag_value,
            applied_by=applied_by,
        ),
        True,
    )


def apply_classify_item(
    subject_id: UUID,
    *,
    columns: Mapping[UUID, tuple[MetadataColumn, MetadataTable]],
    classification: str,
) -> MetadataColumn:
    """Apply one classification to one column, mutating it in place (already
    session-attached). Raises ``CatalogBulkItemError`` if the column is
    missing, or if the column or its parent table is not ACTIVE.
    """
    found = columns.get(subject_id)
    if found is None:
        raise CatalogBulkItemError("column not found in this organization")
    column, table = found
    if column.status != "ACTIVE":
        raise CatalogBulkItemError(f"column status is {column.status}, not ACTIVE")
    if table.status != "ACTIVE":
        raise CatalogBulkItemError(f"parent table status is {table.status}, not ACTIVE")
    column.classification = classification
    return column


def apply_own_item(
    subject_id: UUID,
    *,
    tables: Mapping[UUID, MetadataTable],
    existing_assignments: Mapping[UUID, OwnershipAssignment],
    organization_id: UUID,
    owner_type: str,
    owner_principal: str,
    assigned_by: str,
) -> tuple[OwnershipAssignment, bool]:
    """Assign ownership of one table. Returns ``(row, is_new)`` with the same
    convention as ``apply_tag_item``: an existing (subject, owner) assignment
    is reactivated in place (GL-2's idempotency rule), otherwise a fresh
    ``OwnershipAssignment`` is returned for the caller to add. Raises
    ``CatalogBulkItemError`` if the table is missing or not ACTIVE.
    """
    _require_active_table(tables.get(subject_id))
    existing = existing_assignments.get(subject_id)
    if existing is not None:
        existing.status = "ACTIVE"
        existing.assigned_by = assigned_by
        return existing, False
    return (
        OwnershipAssignment(
            organization_id=organization_id,
            subject_type="TABLE",
            subject_id=str(subject_id),
            owner_type=owner_type,
            owner_principal=owner_principal,
            assignment_kind="BULK",
            assigned_by=assigned_by,
        ),
        True,
    )


def apply_certify_item(
    subject_id: UUID,
    *,
    tables: Mapping[UUID, MetadataTable],
    active_certifications: Mapping[UUID, Sequence[AssetCertification]],
    organization_id: UUID,
    rationale: str,
    expires_at: datetime,
    certified_by: str,
) -> tuple[AssetCertification, list[AssetCertification]]:
    """Certify one table. Returns ``(new_certification, superseded_priors)``:
    ``superseded_priors`` are the table's prior ACTIVE table-level
    certifications, already mutated to ``status="SUPERSEDED"`` in place (GL-5's
    supersede-then-create rule) -- the caller must flush both the new row (once
    added) and every superseded prior together, inside the same SAVEPOINT, so
    a failure can never leave a table with two simultaneously-ACTIVE table
    certifications. Raises ``CatalogBulkItemError`` if the table is missing or
    not ACTIVE.
    """
    _require_active_table(tables.get(subject_id))
    priors = list(active_certifications.get(subject_id, ()))
    for prior in priors:
        prior.status = "SUPERSEDED"
    new_certification = AssetCertification(
        organization_id=organization_id,
        table_id=subject_id,
        asset_type="TABLE",
        rationale=rationale,
        certified_by=certified_by,
        expires_at=expires_at,
    )
    return new_certification, priors
