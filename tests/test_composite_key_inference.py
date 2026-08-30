"""Pure-function coverage for PR-1 (composite key inference).

Follows the fixture style established by `tests/test_catalog_bulk_actions.py`
(CT-1): no database, plain dataclasses in, dataclasses out.
"""

from uuid import uuid4

from aida.composite_key_inference import (
    MAX_CANDIDATE_MEMBERS,
    MAX_CANDIDATES_RETURNED,
    MAX_CONFIDENCE,
    MAX_KEY_SIZE,
    ColumnKeyEvidence,
    infer_composite_key_candidates,
)

SAMPLED_ROWS = 1000


def clean_column(name: str, *, distinct: int = SAMPLED_ROWS, nulls: int = 0) -> ColumnKeyEvidence:
    return ColumnKeyEvidence(
        column_id=uuid4(),
        column_name=name,
        null_count=nulls,
        non_null_count=SAMPLED_ROWS - nulls,
        approximate_distinct_count=distinct,
    )


# ---------------------------------------------------------------------------
# Clean composite key: surfaced, bounded confidence, full evidence
# ---------------------------------------------------------------------------


def test_clean_two_column_composite_key_is_surfaced_with_bounded_confidence() -> None:
    region = clean_column("region_code", distinct=950)
    sequence = clean_column("sequence_no", distinct=1000)
    noise = clean_column("status_flag", distinct=2)  # near-constant, not a key member

    proposals = infer_composite_key_candidates(
        columns=[region, sequence, noise],
        sampled_row_count=SAMPLED_ROWS,
        row_count_estimate=SAMPLED_ROWS * 10,
    )

    assert proposals, "expected at least one candidate"
    pair = next(p for p in proposals if len(p.column_ids) == 2)
    assert set(pair.column_ids) == {region.column_id, sequence.column_id}
    assert noise.column_id not in pair.column_ids

    # Bounded, conservative confidence -- never a certified key.
    assert 0.0 < pair.confidence <= MAX_CONFIDENCE

    # Evidence is self-contained: a reviewer needs nothing else to see why.
    assert pair.evidence["sampled_row_count"] == SAMPLED_ROWS
    assert pair.evidence["row_count_estimate"] == SAMPLED_ROWS * 10
    evidenced_ids = {col["column_id"] for col in pair.evidence["columns"]}
    assert evidenced_ids == {str(region.column_id), str(sequence.column_id)}
    for col in pair.evidence["columns"]:
        assert "null_count" in col
        assert "non_null_count" in col
        assert "approximate_distinct_count" in col
        assert "distinct_ratio" in col
    assert pair.detection_rule.startswith("composite_key_min_distinct_ratio_v1")


def test_single_column_key_is_the_size_one_case_of_the_same_search() -> None:
    unique_column = clean_column("account_uuid", distinct=1000)
    proposals = infer_composite_key_candidates(
        columns=[unique_column],
        sampled_row_count=SAMPLED_ROWS,
        row_count_estimate=None,
    )
    assert len(proposals) == 1
    assert proposals[0].column_ids == (unique_column.column_id,)
    assert 0.0 < proposals[0].confidence <= MAX_CONFIDENCE


def test_narrower_keys_score_at_least_as_high_as_wider_ones_from_the_same_evidence() -> None:
    a = clean_column("a", distinct=1000)
    b = clean_column("b", distinct=1000)
    proposals = infer_composite_key_candidates(
        columns=[a, b],
        sampled_row_count=SAMPLED_ROWS,
        row_count_estimate=None,
    )
    size_one = [p for p in proposals if len(p.column_ids) == 1]
    size_two = [p for p in proposals if len(p.column_ids) == 2]
    assert size_one and size_two
    assert max(p.confidence for p in size_one) > size_two[0].confidence


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


def test_column_with_nulls_above_threshold_is_excluded() -> None:
    nully = clean_column("optional_code", distinct=990, nulls=50)  # 5% nulls
    other = clean_column("sequence_no", distinct=1000)

    proposals = infer_composite_key_candidates(
        columns=[nully, other],
        sampled_row_count=SAMPLED_ROWS,
        row_count_estimate=None,
    )

    touched_columns = {cid for p in proposals for cid in p.column_ids}
    assert nully.column_id not in touched_columns
    # The remaining clean column can still form its own size-1 candidate.
    assert other.column_id in touched_columns


def test_column_already_declared_as_a_key_is_excluded() -> None:
    declared_pk = clean_column("id", distinct=1000)
    plain = clean_column("external_ref", distinct=980)

    proposals = infer_composite_key_candidates(
        columns=[declared_pk, plain],
        sampled_row_count=SAMPLED_ROWS,
        row_count_estimate=None,
        declared_key_column_ids={declared_pk.column_id},
    )

    touched_columns = {cid for p in proposals for cid in p.column_ids}
    assert declared_pk.column_id not in touched_columns
    assert plain.column_id in touched_columns


def test_low_distinct_ratio_column_never_becomes_a_candidate_member() -> None:
    low_cardinality = clean_column("country", distinct=5)  # far below the floor
    proposals = infer_composite_key_candidates(
        columns=[low_cardinality],
        sampled_row_count=SAMPLED_ROWS,
        row_count_estimate=None,
    )
    assert proposals == []


# ---------------------------------------------------------------------------
# No evidence -> no fabricated candidates
# ---------------------------------------------------------------------------


def test_no_columns_produces_nothing() -> None:
    proposals = infer_composite_key_candidates(
        columns=[], sampled_row_count=0, row_count_estimate=None
    )
    assert proposals == []


def test_zero_sampled_rows_produces_nothing_even_with_columns() -> None:
    column = clean_column("id")
    proposals = infer_composite_key_candidates(
        columns=[column], sampled_row_count=0, row_count_estimate=None
    )
    assert proposals == []


# ---------------------------------------------------------------------------
# Bounded search on a wide table
# ---------------------------------------------------------------------------


def test_search_stays_bounded_on_a_wide_synthetic_table() -> None:
    # Many more eligible columns than MAX_CANDIDATE_MEMBERS, with strictly
    # increasing distinct ratios so ranking is unambiguous.
    wide_columns = [clean_column(f"col_{i}", distinct=900 + i) for i in range(40)]

    proposals = infer_composite_key_candidates(
        columns=wide_columns,
        sampled_row_count=SAMPLED_ROWS,
        row_count_estimate=None,
    )

    assert len(proposals) <= MAX_CANDIDATES_RETURNED
    assert all(len(p.column_ids) <= MAX_KEY_SIZE for p in proposals)

    touched_columns = {cid for p in proposals for cid in p.column_ids}
    assert len(touched_columns) <= MAX_CANDIDATE_MEMBERS

    # Only the top-N (by distinct ratio) columns were ever eligible members --
    # the lowest-ratio columns in the wide table never appear anywhere.
    by_distinct_count = sorted(wide_columns, key=lambda c: c.approximate_distinct_count)
    top_ids = {c.column_id for c in by_distinct_count[-MAX_CANDIDATE_MEMBERS:]}
    assert touched_columns <= top_ids
