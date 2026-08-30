"""Deterministic, metadata-only structural matching for catalog identity.

Two governance items share the same underlying question -- "is this table,
structurally, the same logical object as that one?" -- and answer it from the
same ingredients: column names, physical types, ordinal positions and column
counts already sitting in the catalog. Never sampled or live row values
(ADR-0014, "value-free control plane").

- CT-4 rename detection: is a table just created in this scan actually the
  table just tombstoned in the same scan, renamed?
- CT-6 cross-source object resolution: is a table in one datasource the same
  logical business asset as a table in another datasource?

Everything here is a pure function over plain column shapes -- no database
session, no ORM session state -- so it is exercised directly in tests without
a live database. The DB-facing orchestration (querying candidates, bounding
scan size, inserting proposals) lives in ``aida.workflows.activities``
(rename) and ``aida.intelligence_api`` (cross-source).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Protocol

RENAME_DETECTION_RULE = "STRUCTURAL_MATCH_V1"
CROSS_SOURCE_EXACT_NAME_RULE = "EXACT_NAME_SCHEMA_SHAPE_V1"
CROSS_SOURCE_STRUCTURAL_RULE = "STRUCTURAL_NAME_SIMILARITY_V1"


class ColumnShape(Protocol):
    """The minimal column surface these heuristics read. ``MetadataColumn`` satisfies it."""

    name: str
    physical_type: str
    ordinal_position: int


def _name_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, left.strip().lower(), right.strip().lower()).ratio()


@dataclass(frozen=True, slots=True)
class StructuralShapeMatch:
    column_count_left: int
    column_count_right: int
    column_count_matches: bool
    type_position_match_ratio: float
    column_name_jaccard: float


def compare_column_shapes(
    left_columns: Sequence[ColumnShape], right_columns: Sequence[ColumnShape]
) -> StructuralShapeMatch:
    """Bounded, O(n) structural comparison of two column lists.

    ``type_position_match_ratio`` rewards the common rename signature (same
    types in the same order); ``column_name_jaccard`` rewards the common
    replication signature (same column names, order irrelevant).
    """
    left_sorted = sorted(left_columns, key=lambda c: c.ordinal_position)
    right_sorted = sorted(right_columns, key=lambda c: c.ordinal_position)
    left_count = len(left_sorted)
    right_count = len(right_sorted)
    widest = max(left_count, right_count, 1)
    position_matches = sum(
        1
        for left, right in zip(left_sorted, right_sorted, strict=False)
        if left.physical_type.strip().lower() == right.physical_type.strip().lower()
    )
    left_names = {c.name.strip().lower() for c in left_sorted}
    right_names = {c.name.strip().lower() for c in right_sorted}
    union = left_names | right_names
    jaccard = (len(left_names & right_names) / len(union)) if union else 0.0
    tolerance = max(1, round(0.2 * widest))
    return StructuralShapeMatch(
        column_count_left=left_count,
        column_count_right=right_count,
        column_count_matches=abs(left_count - right_count) <= tolerance,
        type_position_match_ratio=position_matches / widest,
        column_name_jaccard=jaccard,
    )


@dataclass(frozen=True, slots=True)
class IdentityMatch:
    confidence: float
    detection_rule: str
    evidence: dict[str, Any]


def score_table_rename(
    *,
    old_table_name: str,
    old_columns: Sequence[ColumnShape],
    new_table_name: str,
    new_columns: Sequence[ColumnShape],
    min_confidence: float = 0.6,
) -> IdentityMatch | None:
    """Score a candidate rename: a just-tombstoned table vs. a just-created one.

    Requires a *strong* structural match as a hard gate -- matching column
    count plus either near-identical type/position signatures or near-identical
    column-name sets -- before name similarity is even consulted, so a renamed
    table with an unrelated new structure never qualifies. Returns ``None``
    when the pair does not clear the gate or the resulting confidence falls
    below ``min_confidence``.
    """
    shape = compare_column_shapes(old_columns, new_columns)
    if shape.column_count_left == 0 or shape.column_count_right == 0:
        return None
    if not shape.column_count_matches:
        return None
    if shape.type_position_match_ratio < 0.8 and shape.column_name_jaccard < 0.8:
        return None
    table_name_similarity = _name_similarity(old_table_name, new_table_name)
    confidence = round(
        min(
            1.0,
            0.45 * shape.type_position_match_ratio
            + 0.35 * shape.column_name_jaccard
            + 0.20 * table_name_similarity,
        ),
        4,
    )
    if confidence < min_confidence:
        return None
    return IdentityMatch(
        confidence=confidence,
        detection_rule=RENAME_DETECTION_RULE,
        evidence={
            "old_table_name": old_table_name,
            "new_table_name": new_table_name,
            "table_name_similarity": round(table_name_similarity, 4),
            "column_count_old": shape.column_count_left,
            "column_count_new": shape.column_count_right,
            "type_position_match_ratio": round(shape.type_position_match_ratio, 4),
            "column_name_jaccard": round(shape.column_name_jaccard, 4),
            "source_values_inspected": False,
        },
    )


def score_cross_source_match(
    *,
    source_schema_name: str,
    source_table_name: str,
    source_columns: Sequence[ColumnShape],
    target_schema_name: str,
    target_table_name: str,
    target_columns: Sequence[ColumnShape],
    min_confidence: float = 0.6,
) -> IdentityMatch | None:
    """Score whether two tables in different datasources are the same logical asset.

    Unlike rename detection this never requires the same schema (the two
    tables live in different estates entirely), so an exact case-insensitive
    table-name match is treated as strong, independent evidence and a small
    confidence bonus; a renamed/differently-shaped equivalent still needs
    corroborating column-shape evidence to qualify.
    """
    shape = compare_column_shapes(source_columns, target_columns)
    if shape.column_count_left == 0 or shape.column_count_right == 0:
        return None
    exact_name_match = source_table_name.strip().lower() == target_table_name.strip().lower()
    table_name_similarity = _name_similarity(source_table_name, target_table_name)
    schema_name_similarity = _name_similarity(source_schema_name, target_schema_name)
    structurally_plausible = shape.column_count_matches and (
        shape.type_position_match_ratio >= 0.6 or shape.column_name_jaccard >= 0.6
    )
    name_plausible = exact_name_match or table_name_similarity >= 0.5
    if not (structurally_plausible and name_plausible):
        return None
    confidence = (
        0.40 * shape.type_position_match_ratio
        + 0.25 * shape.column_name_jaccard
        + 0.20 * table_name_similarity
        + 0.15 * schema_name_similarity
    )
    if exact_name_match:
        confidence += 0.05
    confidence = round(min(1.0, confidence), 4)
    if confidence < min_confidence:
        return None
    return IdentityMatch(
        confidence=confidence,
        detection_rule=(
            CROSS_SOURCE_EXACT_NAME_RULE if exact_name_match else CROSS_SOURCE_STRUCTURAL_RULE
        ),
        evidence={
            "source_qualified_name": f"{source_schema_name}.{source_table_name}",
            "target_qualified_name": f"{target_schema_name}.{target_table_name}",
            "exact_name_match": exact_name_match,
            "table_name_similarity": round(table_name_similarity, 4),
            "schema_name_similarity": round(schema_name_similarity, 4),
            "column_count_source": shape.column_count_left,
            "column_count_target": shape.column_count_right,
            "type_position_match_ratio": round(shape.type_position_match_ratio, 4),
            "column_name_jaccard": round(shape.column_name_jaccard, 4),
            "source_values_inspected": False,
        },
    )
