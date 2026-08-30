"""Value-free single-column and composite key inference (module 05, PR-1).

Every candidate produced here is derived exclusively from already-computed,
value-free profile statistics (null counts, non-null counts, approximate
distinct counts) per ADR-0014 — no source values are inspected or required.

Single-column candidates are a deterministic check: a NOT NULL column whose
approximate distinct count is close enough to the table's row count is a
plausible key. Composite candidates are a genuine heuristic: without a joint
``SELECT COUNT(DISTINCT a, b, ...)`` query (which would require an additional
live query this module does not run), the best value-free estimate of a
column combination's joint distinctness is the mathematical upper bound
``min(row_count, product(distinct_count_i))`` — true joint distinctness can
never exceed that product (pigeonhole). When that upper bound already falls
short of the row count, the combination is definitively ruled out. When it
clears the bar, the combination is only a *candidate*: the true joint
distinctness could still be lower than the upper bound if the columns
correlate, so composite proposals carry a materially lower confidence than
single-column ones and both are exit-gated on human review (see
``KeyInferenceCandidate`` in models.py / the discover+decision endpoints in
intelligence_api.py), mirroring how ``RelationshipCandidate`` decisions work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any
from uuid import UUID

SINGLE_COLUMN_RULE = "SINGLE_COLUMN_NOT_NULL_DISTINCT_V1"
COMPOSITE_RULE = "COMPOSITE_INDEPENDENCE_UPPER_BOUND_V1"

DEFAULT_MIN_RATIO = 0.95
DEFAULT_MIN_COLUMN_RATIO = 0.1
DEFAULT_MAX_POOL = 8
MIN_COMPOSITE_COLUMNS = 2
MAX_COMPOSITE_COLUMNS = 4

_COMPOSITE_BASE_CONFIDENCE = {2: 0.55, 3: 0.45, 4: 0.35}


@dataclass(frozen=True, slots=True)
class ColumnStat:
    """Value-free per-column profile statistics used as key-inference input."""

    column_id: UUID
    column_name: str
    null_count: int
    non_null_count: int
    approximate_distinct_count: int


@dataclass(frozen=True, slots=True)
class KeyCandidate:
    column_ids: tuple[UUID, ...]
    column_names: tuple[str, ...]
    column_count: int
    detection_rule: str
    confidence: float
    estimated_distinctness_ratio: float
    evidence: dict[str, Any] = field(default_factory=dict)


def _distinctness_ratio(stat: ColumnStat, row_count: int) -> float:
    if row_count <= 0:
        return 0.0
    return min(1.0, stat.approximate_distinct_count / row_count)


def infer_single_column_keys(
    stats: list[ColumnStat],
    row_count: int,
    *,
    min_ratio: float = DEFAULT_MIN_RATIO,
) -> list[KeyCandidate]:
    """Propose NOT NULL columns whose distinct count nearly covers every row."""
    if row_count <= 0:
        return []
    candidates: list[KeyCandidate] = []
    for stat in stats:
        if stat.null_count > 0:
            continue
        ratio = _distinctness_ratio(stat, row_count)
        if ratio < min_ratio:
            continue
        confidence = round(min(0.97, 0.5 + 0.5 * ratio), 4)
        candidates.append(
            KeyCandidate(
                column_ids=(stat.column_id,),
                column_names=(stat.column_name,),
                column_count=1,
                detection_rule=SINGLE_COLUMN_RULE,
                confidence=confidence,
                estimated_distinctness_ratio=ratio,
                evidence={
                    "method": "NULL_FREE_DISTINCT_RATIO",
                    "value_scope": "METADATA_ONLY",
                    "actual_values_inspected": False,
                    "row_count_basis": row_count,
                    "null_count": stat.null_count,
                    "approximate_distinct_count": stat.approximate_distinct_count,
                    "distinctness_ratio": ratio,
                    "requires_review": True,
                },
            )
        )
    candidates.sort(key=lambda item: item.estimated_distinctness_ratio, reverse=True)
    return candidates


def infer_composite_keys(
    stats: list[ColumnStat],
    row_count: int,
    *,
    min_ratio: float = DEFAULT_MIN_RATIO,
    min_column_ratio: float = DEFAULT_MIN_COLUMN_RATIO,
    max_pool: int = DEFAULT_MAX_POOL,
    min_columns: int = MIN_COMPOSITE_COLUMNS,
    max_columns: int = MAX_COMPOSITE_COLUMNS,
) -> list[KeyCandidate]:
    """Propose bounded 2-4 column combinations whose combined distinctness upper
    bound (product of individual distinct counts, capped at row_count — a valid
    upper bound on joint distinctness) approaches the row count.

    The candidate pool is pruned to NOT NULL columns clearing ``min_column_ratio``
    individually, and capped at ``max_pool`` columns (highest distinctness
    first), which bounds the combinatorial search exactly as the module spec
    requires ("bounded sets of 2-4 columns").
    """
    if row_count <= 0:
        return []
    pool = sorted(
        (
            (stat, _distinctness_ratio(stat, row_count))
            for stat in stats
            if stat.null_count == 0 and _distinctness_ratio(stat, row_count) >= min_column_ratio
        ),
        key=lambda item: item[1],
        reverse=True,
    )[:max_pool]

    candidates: list[KeyCandidate] = []
    for size in range(min_columns, max_columns + 1):
        for combo in combinations(pool, size):
            combo_stats = [item[0] for item in combo]
            product = 1
            for combo_stat in combo_stats:
                product *= max(1, combo_stat.approximate_distinct_count)
            upper_bound = min(row_count, product)
            ratio = upper_bound / row_count
            if ratio < min_ratio:
                continue
            base_confidence = _COMPOSITE_BASE_CONFIDENCE.get(size, 0.3)
            confidence = round(min(0.7, base_confidence * (0.5 + 0.5 * ratio)), 4)
            candidates.append(
                KeyCandidate(
                    column_ids=tuple(stat.column_id for stat in combo_stats),
                    column_names=tuple(stat.column_name for stat in combo_stats),
                    column_count=size,
                    detection_rule=COMPOSITE_RULE,
                    confidence=confidence,
                    estimated_distinctness_ratio=ratio,
                    evidence={
                        "method": "INDEPENDENCE_UPPER_BOUND_PRODUCT",
                        "value_scope": "METADATA_ONLY",
                        "actual_values_inspected": False,
                        "row_count_basis": row_count,
                        "column_distinct_counts": {
                            stat.column_name: stat.approximate_distinct_count
                            for stat in combo_stats
                        },
                        "combined_upper_bound_distinct": upper_bound,
                        "combined_upper_bound_ratio": ratio,
                        "assumption": (
                            "Upper bound assumes no correlation between columns; true joint "
                            "distinctness can only be lower, never higher, than this bound. "
                            "Unconfirmed without an additional joint-distinct query."
                        ),
                        "requires_review": True,
                    },
                )
            )
    candidates.sort(key=lambda item: (item.column_count, -item.estimated_distinctness_ratio))
    return candidates


def infer_key_candidates(
    stats: list[ColumnStat],
    row_count: int,
    *,
    min_ratio: float = DEFAULT_MIN_RATIO,
    min_column_ratio: float = DEFAULT_MIN_COLUMN_RATIO,
    max_pool: int = DEFAULT_MAX_POOL,
    max_composite_columns: int = MAX_COMPOSITE_COLUMNS,
    declared_primary_key_column_ids: frozenset[frozenset[UUID]] | None = None,
    max_candidates: int | None = None,
) -> list[KeyCandidate]:
    """Single-column plus minimal composite key candidates for one table.

    A composite is only proposed when no already-accepted smaller candidate
    (single-column or smaller composite) is a subset of its columns — a
    candidate key must be minimal, so a composite that is only a superset of
    an already-sufficient smaller key is redundant noise and is skipped.
    Column sets matching an already-declared PRIMARY_KEY constraint are
    skipped entirely since those are ground truth, not proposals.
    """
    declared = declared_primary_key_column_ids or frozenset()
    singles = infer_single_column_keys(stats, row_count, min_ratio=min_ratio)
    accepted_sets: list[frozenset[UUID]] = [
        frozenset(candidate.column_ids) for candidate in singles
    ]
    results = [
        candidate for candidate in singles if frozenset(candidate.column_ids) not in declared
    ]

    composites = infer_composite_keys(
        stats,
        row_count,
        min_ratio=min_ratio,
        min_column_ratio=min_column_ratio,
        max_pool=max_pool,
        min_columns=MIN_COMPOSITE_COLUMNS,
        max_columns=max_composite_columns,
    )
    for candidate in composites:
        column_set = frozenset(candidate.column_ids)
        if column_set in declared:
            continue
        if any(existing <= column_set for existing in accepted_sets):
            continue
        results.append(candidate)
        accepted_sets.append(column_set)

    if max_candidates is not None:
        results = results[:max_candidates]
    return results
