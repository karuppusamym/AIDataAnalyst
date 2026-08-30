"""Deterministic, metadata-only relationship intelligence (module 06).

Everything in this file operates on names, types, declared constraints, and
value-free profile statistics only (ADR-0014) -- never on sampled source
values. It is intentionally free of any database session so that it can be
exhaustively unit tested; ``aida.intelligence_api`` fetches rows and calls
into these functions.

Covers two open items from ``Docs/20-modules/06-relationship-intelligence.md``:

* RL-2 -- canonical table resolution (``resolve_canonical_table_id``)
* RL-3 -- composite relationship candidates (``generate_composite_relationship_candidates``)

RL-1 (table family / temporal intelligence) is NOT here: it shipped
independently as ``aida.table_family_intelligence`` / ``aida.table_family_api``,
backed by ``TableFamilyCandidate`` in ``aida.models``. A duplicate detector
used to live in this file; it has been removed in favor of that shipped
implementation, and RL-2 is now wired onto ``TableFamilyCandidate`` instead
of a competing table-family model.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

# --------------------------------------------------------------------------
# Shared primitives
# --------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """Lowercase, separator-normalized token form of an identifier."""
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def _has_token(normalized: str, pattern: str) -> bool:
    """True if ``pattern`` occurs as an underscore-delimited run inside ``normalized``."""
    return f"_{pattern}_" in f"_{normalized}_"


def _find_matches(names: dict[str, str], patterns: frozenset[str]) -> list[str]:
    """Return original column names whose normalized form matches any pattern."""
    return [
        original
        for original, normalized in names.items()
        if any(_has_token(normalized, pattern) for pattern in patterns)
    ]


@dataclass(frozen=True, slots=True)
class ColumnMeta:
    """Metadata-only view of one column: names, types, and (optional) profile stats."""

    id: UUID
    table_id: UUID
    name: str
    physical_type: str
    nullable: bool
    ordinal_position: int
    null_count: int | None = None
    non_null_count: int | None = None
    approximate_distinct_count: int | None = None

    @property
    def null_rate(self) -> float | None:
        if self.null_count is None or self.non_null_count is None:
            return None
        total = self.null_count + self.non_null_count
        return (self.null_count / total) if total else 0.0


# --------------------------------------------------------------------------
# RL-2 -- canonical table resolution
# --------------------------------------------------------------------------
#
# Table-family detection itself (RL-1) shipped upstream as
# ``aida.table_family_intelligence`` / ``aida.table_family_api``, backed by
# ``TableFamilyCandidate`` in ``aida.models`` -- this module no longer
# detects families (see git history for the removed, duplicate detector that
# used to live here). What remains is the one real gap ``TableFamilyCandidate``
# leaves open: it carries an algorithmic ``base_table_id`` pick but that is
# explicitly never set for a SNAPSHOT family, and there is no steward
# override at all upstream. ``resolve_canonical_table_id`` below is that
# resolution, as a pure function; ``aida.intelligence_api`` does the fetching
# (the family lookup and any ``CanonicalTableMapping`` row) and calls it.

CANONICAL_RESOLUTION_ALGORITHM_VERSION = "canonical-resolution-v1"


def resolve_canonical_table_id(
    *, base_table_id: UUID | None, steward_override_table_id: UUID | None
) -> UUID | None:
    """RL-2 -- the effective canonical table id for one table family.

    Deterministic three-way fallback, no database access:

    1. An explicit steward override (``CanonicalTableMapping.canonical_table_id``)
       always wins when present.
    2. Otherwise, the family's own algorithmic pick
       (``TableFamilyCandidate.base_table_id``), if one was resolved.
    3. Otherwise ``None`` -- e.g. an un-overridden SNAPSHOT family, where the
       algorithm never names a "current" member (see that model's docstring)
       and no steward has named one either.
    """
    if steward_override_table_id is not None:
        return steward_override_table_id
    return base_table_id


# --------------------------------------------------------------------------
# RL-3 -- composite (multi-column) relationship candidates
# --------------------------------------------------------------------------

COMPOSITE_ALGORITHM_VERSION = "composite-relationship-v1"
COMPOSITE_DETECTION_RULE = "COMPOSITE_EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1"
COMPOSITE_MAX_CONFIDENCE = 0.93
COMPOSITE_HIGH_NULL_RATE = 0.98


@dataclass(frozen=True, slots=True)
class CompositeMemberPair:
    ordinal: int
    source_column_id: UUID
    target_column_id: UUID
    source_column_name: str
    target_column_name: str


@dataclass(frozen=True, slots=True)
class CompositeCandidateResult:
    source_table_id: UUID
    target_table_id: UUID
    members: tuple[CompositeMemberPair, ...]
    confidence: float
    evidence: dict[str, Any]
    detection_rule: str = COMPOSITE_DETECTION_RULE

    @property
    def fingerprint(self) -> str:
        return composite_group_fingerprint(
            self.source_table_id,
            self.target_table_id,
            [(pair.source_column_id, pair.target_column_id) for pair in self.members],
        )


def composite_group_fingerprint(
    source_table_id: UUID,
    target_table_id: UUID,
    ordered_column_pairs: list[tuple[UUID, UUID]],
) -> str:
    """Stable identity for a composite candidate: order matters, dedupe is exact."""
    material = "|".join(
        [str(source_table_id), str(target_table_id)]
        + [f"{source}:{target}" for source, target in ordered_column_pairs]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def generate_composite_relationship_candidates(
    *,
    columns_by_table: dict[UUID, tuple[ColumnMeta, ...]],
    composite_primary_keys: dict[UUID, tuple[str, ...]],
    declared_composite_foreign_keys: frozenset[
        tuple[UUID, tuple[str, ...], UUID, tuple[str, ...]]
    ] = frozenset(),
    existing_fingerprints: frozenset[str] = frozenset(),
    max_group_columns: int = 4,
    max_candidates_per_table: int = 25,
) -> list[CompositeCandidateResult]:
    """Propose bounded, evidence-backed composite (multi-column) FK-like candidates.

    Follows the module's pruning order: declared constraints are skipped as
    facts, name/type compatibility prunes first, profile-based nullability
    prunes further, ordinal alignment only refines the score, and survivors
    are capped per target table.
    """
    results: list[CompositeCandidateResult] = []

    for target_table_id in sorted(composite_primary_keys, key=str):
        pk_columns = composite_primary_keys[target_table_id]
        if not (2 <= len(pk_columns) <= max_group_columns):
            continue
        target_columns = columns_by_table.get(target_table_id, ())
        target_by_lower = {column.name.lower(): column for column in target_columns}
        pk_column_metas = [target_by_lower.get(name.lower()) for name in pk_columns]
        if any(meta is None for meta in pk_column_metas):
            continue

        per_table_count = 0
        for source_table_id in sorted(columns_by_table, key=str):
            if source_table_id == target_table_id or per_table_count >= max_candidates_per_table:
                continue
            source_columns = columns_by_table[source_table_id]
            source_by_lower = {column.name.lower(): column for column in source_columns}

            # Rule 2: name and type compatibility prunes first -- every PK column
            # must exist on the source side with a compatible physical type.
            matched: list[tuple[ColumnMeta, ColumnMeta]] = []
            type_mismatch = False
            for target_meta in pk_column_metas:
                assert target_meta is not None
                source_meta = source_by_lower.get(target_meta.name.lower())
                if source_meta is None:
                    matched = []
                    break
                if source_meta.physical_type.lower() != target_meta.physical_type.lower():
                    type_mismatch = True
                    break
                matched.append((source_meta, target_meta))
            if type_mismatch or len(matched) != len(pk_column_metas):
                continue

            ordered_pairs = [(source.id, target.id) for source, target in matched]
            source_names = tuple(source.name.lower() for source, _ in matched)
            target_names = tuple(target.name.lower() for _, target in matched)
            declared_key = (source_table_id, source_names, target_table_id, target_names)
            if declared_key in declared_composite_foreign_keys:
                continue  # Rule 1: declared constraints are facts, not candidates.

            fingerprint = composite_group_fingerprint(
                source_table_id, target_table_id, ordered_pairs
            )
            if fingerprint in existing_fingerprints:
                continue

            # Rule 3: cardinality/nullability profile pruning, best-effort.
            null_rates = [source.null_rate for source, _ in matched]
            known_null_rates = [rate for rate in null_rates if rate is not None]
            all_source_null = bool(known_null_rates) and all(
                rate is not None and rate >= COMPOSITE_HIGH_NULL_RATE for rate in known_null_rates
            )
            if all_source_null:
                continue

            # Rule 4: ordinal position / parent-table context refines (not a prune).
            source_ordinals = [source.ordinal_position for source, _ in matched]
            ordinal_alignment = source_ordinals == sorted(source_ordinals)

            confidence = 0.65 + 0.05 * (len(matched) - 2)
            if known_null_rates and max(known_null_rates) < 0.05:
                confidence += 0.05
            if ordinal_alignment:
                confidence += 0.05
            confidence = min(confidence, COMPOSITE_MAX_CONFIDENCE)

            members = tuple(
                CompositeMemberPair(
                    ordinal=index,
                    source_column_id=source.id,
                    target_column_id=target.id,
                    source_column_name=source.name,
                    target_column_name=target.name,
                )
                for index, (source, target) in enumerate(matched)
            )
            evidence = {
                "algorithm_version": COMPOSITE_ALGORITHM_VERSION,
                "member_count": len(members),
                "column_name_match": "EXACT_CASE_INSENSITIVE",
                "physical_type_match": "EXACT",
                "target_is_primary_key": True,
                "ordinal_alignment": ordinal_alignment,
                "known_null_rates": [round(rate, 4) for rate in known_null_rates] or None,
                "source_values_inspected": False,
            }
            results.append(
                CompositeCandidateResult(
                    source_table_id=source_table_id,
                    target_table_id=target_table_id,
                    members=members,
                    confidence=confidence,
                    evidence=evidence,
                )
            )
            per_table_count += 1

    return results
