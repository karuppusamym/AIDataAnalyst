"""Value-free single-column and composite key inference (module 05, PR-1).

Every test here builds candidates purely from `ColumnStat` -- null/non-null/
approximate-distinct counts -- and never touches a source value, mirroring
ADR-0014. Composite inference in particular is a genuine heuristic (an
independence upper bound, not a measured joint distinctness), so several
tests pin down exactly how conservative that bound is: it can rule a
combination out, but never promises one in with full confidence.
"""

from uuid import uuid4

from aida.key_inference import (
    COMPOSITE_RULE,
    MAX_COMPOSITE_COLUMNS,
    SINGLE_COLUMN_RULE,
    ColumnStat,
    infer_composite_keys,
    infer_key_candidates,
    infer_single_column_keys,
    key_fingerprint,
)


def _stat(name: str, *, null_count: int, non_null_count: int, distinct: int) -> ColumnStat:
    return ColumnStat(
        column_id=uuid4(),
        column_name=name,
        null_count=null_count,
        non_null_count=non_null_count,
        approximate_distinct_count=distinct,
    )


def test_not_null_near_unique_column_is_a_single_column_candidate() -> None:
    stat = _stat("account_id", null_count=0, non_null_count=1000, distinct=998)

    candidates = infer_single_column_keys([stat], row_count=1000)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.detection_rule == SINGLE_COLUMN_RULE
    assert candidate.column_names == ("account_id",)
    assert candidate.evidence["actual_values_inspected"] is False
    assert candidate.evidence["value_scope"] == "METADATA_ONLY"
    assert 0.0 < candidate.confidence <= 0.97


def test_column_with_nulls_is_never_a_key_candidate() -> None:
    stat = _stat("ssn", null_count=1, non_null_count=999, distinct=999)

    assert infer_single_column_keys([stat], row_count=1000) == []


def test_low_distinctness_single_column_is_rejected() -> None:
    stat = _stat("status", null_count=0, non_null_count=1000, distinct=3)

    assert infer_single_column_keys([stat], row_count=1000) == []


def test_composite_candidate_from_two_partial_columns() -> None:
    # Neither column alone clears the single-column bar (each 10% distinct,
    # right at the pool's minimum individual ratio), but their
    # independence-upper-bound product covers the full row count.
    a = _stat("region", null_count=0, non_null_count=1000, distinct=100)
    b = _stat("branch", null_count=0, non_null_count=1000, distinct=100)

    singles = infer_single_column_keys([a, b], row_count=1000)
    composites = infer_composite_keys([a, b], row_count=1000)

    assert singles == []
    assert len(composites) == 1
    candidate = composites[0]
    assert candidate.detection_rule == COMPOSITE_RULE
    assert candidate.column_count == 2
    assert set(candidate.column_names) == {"region", "branch"}
    assert candidate.evidence["actual_values_inspected"] is False
    assert candidate.evidence["combined_upper_bound_distinct"] == 1000
    # Composite confidence must never overstate a single-column candidate's,
    # because the upper bound can only ever be an overestimate of the truth.
    assert candidate.confidence < 0.97


def test_composite_search_is_bounded_to_four_columns() -> None:
    stats = [
        _stat(f"col_{i}", null_count=0, non_null_count=1000, distinct=150) for i in range(6)
    ]

    composites = infer_composite_keys(stats, row_count=1000)

    assert all(2 <= c.column_count <= MAX_COMPOSITE_COLUMNS for c in composites)
    # 6 columns bounded to combination sizes 2-4 is C(6,2)+C(6,3)+C(6,4) = 50,
    # never the unbounded 2^6 - 7 powerset (57 non-trivial subsets).
    assert len(composites) <= 15 + 20 + 15


def test_low_distinctness_column_is_pruned_from_the_pool() -> None:
    good = [_stat(f"good_{i}", null_count=0, non_null_count=1000, distinct=200) for i in range(3)]
    noise = _stat("flag", null_count=0, non_null_count=1000, distinct=2)

    composites = infer_composite_keys([*good, noise], row_count=1000, min_column_ratio=0.1)

    assert composites, "expected at least one composite candidate among the good columns"
    assert all("flag" not in c.column_names for c in composites)


def test_minimal_composite_is_skipped_when_a_subset_already_qualifies() -> None:
    key_col = _stat("id", null_count=0, non_null_count=1000, distinct=1000)
    extra = _stat("region", null_count=0, non_null_count=1000, distinct=200)

    candidates = infer_key_candidates([key_col, extra], row_count=1000)

    # `id` alone is already a sufficient single-column key; a composite that is
    # only a superset of it (id+region) is redundant noise and must be dropped.
    assert len(candidates) == 1
    assert candidates[0].column_names == ("id",)


def test_declared_primary_key_is_not_reproposed() -> None:
    stat = _stat("account_id", null_count=0, non_null_count=1000, distinct=1000)

    candidates = infer_key_candidates(
        [stat],
        row_count=1000,
        declared_primary_key_column_ids=frozenset({frozenset({stat.column_id})}),
    )

    assert candidates == []


def test_max_candidates_caps_the_result() -> None:
    stats = [_stat(f"col_{i}", null_count=0, non_null_count=1000, distinct=999) for i in range(5)]

    candidates = infer_key_candidates(stats, row_count=1000, max_candidates=2)

    assert len(candidates) == 2


def test_zero_row_count_produces_no_candidates() -> None:
    stat = _stat("id", null_count=0, non_null_count=0, distinct=0)

    assert infer_single_column_keys([stat], row_count=0) == []
    assert infer_composite_keys([stat], row_count=0) == []
    assert infer_key_candidates([stat], row_count=0) == []


def test_key_fingerprint_is_order_independent_and_stable() -> None:
    a, b = uuid4(), uuid4()

    first = key_fingerprint((a, b))
    second = key_fingerprint((b, a))

    assert first == second
    assert first == key_fingerprint((a, b))


def test_key_fingerprint_differs_for_different_column_sets() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()

    assert key_fingerprint((a, b)) != key_fingerprint((a, c))
