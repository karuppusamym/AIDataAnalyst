"""CT-4 (rename detection) and CT-6 (cross-source resolution) heuristic tests.

These exercise aida.identity_resolution directly: pure functions over plain
column shapes, no database session required.
"""

from dataclasses import dataclass

import pytest

from aida.identity_resolution import (
    CROSS_SOURCE_EXACT_NAME_RULE,
    CROSS_SOURCE_STRUCTURAL_RULE,
    RENAME_DETECTION_RULE,
    compare_column_shapes,
    score_cross_source_match,
    score_table_rename,
)


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    physical_type: str
    ordinal_position: int


def _columns(*specs: tuple[str, str]) -> list[Column]:
    return [
        Column(name=name, physical_type=physical_type, ordinal_position=index)
        for index, (name, physical_type) in enumerate(specs, start=1)
    ]


ACCOUNT_COLUMNS = _columns(
    ("account_id", "bigint"),
    ("customer_id", "bigint"),
    ("balance", "numeric"),
    ("opened_at", "timestamp"),
)


# --- compare_column_shapes ------------------------------------------------------------------


def test_compare_column_shapes_identical_columns() -> None:
    shape = compare_column_shapes(ACCOUNT_COLUMNS, ACCOUNT_COLUMNS)

    assert shape.column_count_matches
    assert shape.type_position_match_ratio == 1.0
    assert shape.column_name_jaccard == 1.0


def test_compare_column_shapes_penalizes_reordered_types() -> None:
    reordered = _columns(
        ("account_id", "bigint"),
        ("balance", "numeric"),
        ("customer_id", "bigint"),
        ("opened_at", "timestamp"),
    )

    shape = compare_column_shapes(ACCOUNT_COLUMNS, reordered)

    assert shape.column_count_matches
    assert shape.column_name_jaccard == 1.0
    assert shape.type_position_match_ratio == 0.5  # positions 1 and 4 still match by type


def test_compare_column_shapes_tolerates_a_couple_of_added_columns() -> None:
    wider = _columns(
        ("account_id", "bigint"),
        ("customer_id", "bigint"),
        ("balance", "numeric"),
        ("opened_at", "timestamp"),
        ("branch_code", "varchar"),
    )

    shape = compare_column_shapes(ACCOUNT_COLUMNS, wider)

    assert shape.column_count_matches  # tolerance is max(1, 20% of 5) == 1


def test_compare_column_shapes_empty_columns_produce_zero_union() -> None:
    shape = compare_column_shapes([], [])

    assert shape.column_name_jaccard == 0.0
    assert shape.type_position_match_ratio == 0.0


# --- score_table_rename (CT-4) ---------------------------------------------------------------


def test_score_table_rename_detects_same_columns_different_name() -> None:
    match = score_table_rename(
        old_table_name="CUSTOMER_ACCOUNT",
        old_columns=ACCOUNT_COLUMNS,
        new_table_name="CUST_ACCOUNT",
        new_columns=ACCOUNT_COLUMNS,
    )

    assert match is not None
    assert match.detection_rule == RENAME_DETECTION_RULE
    assert match.confidence > 0.8
    assert match.evidence["column_count_old"] == 4
    assert match.evidence["column_count_new"] == 4
    assert match.evidence["source_values_inspected"] is False


def test_score_table_rename_case_insensitive_pure_rename_is_near_certain() -> None:
    match = score_table_rename(
        old_table_name="Account",
        old_columns=ACCOUNT_COLUMNS,
        new_table_name="account",
        new_columns=ACCOUNT_COLUMNS,
    )

    assert match is not None
    assert match.confidence >= 0.95


def test_score_table_rename_rejects_weak_structural_match() -> None:
    unrelated_columns = _columns(
        ("product_sku", "varchar"),
        ("price", "numeric"),
    )

    match = score_table_rename(
        old_table_name="ACCOUNT",
        old_columns=ACCOUNT_COLUMNS,
        new_table_name="ACCOUNTS_V2",
        new_columns=unrelated_columns,
    )

    assert match is None


def test_score_table_rename_rejects_when_column_count_differs_beyond_tolerance() -> None:
    much_wider = _columns(*[(f"col_{i}", "varchar") for i in range(20)])

    match = score_table_rename(
        old_table_name="ACCOUNT",
        old_columns=ACCOUNT_COLUMNS,
        new_table_name="ACCOUNT",
        new_columns=much_wider,
    )

    assert match is None


def test_score_table_rename_rejects_empty_column_lists() -> None:
    assert (
        score_table_rename(
            old_table_name="ACCOUNT",
            old_columns=[],
            new_table_name="ACCOUNT",
            new_columns=[],
        )
        is None
    )


def test_score_table_rename_honors_min_confidence_floor() -> None:
    # Strong structural match (same columns) but a very dissimilar table name pulls
    # confidence below a strict floor.
    match = score_table_rename(
        old_table_name="ACCOUNT",
        old_columns=ACCOUNT_COLUMNS,
        new_table_name="ZZZZZZZZZZZZ",
        new_columns=ACCOUNT_COLUMNS,
        min_confidence=0.95,
    )

    assert match is None


@pytest.mark.parametrize("min_confidence", [0.0, 0.5, 0.8])
def test_score_table_rename_confidence_never_exceeds_one(min_confidence: float) -> None:
    match = score_table_rename(
        old_table_name="ACCOUNT",
        old_columns=ACCOUNT_COLUMNS,
        new_table_name="ACCOUNT",
        new_columns=ACCOUNT_COLUMNS,
        min_confidence=min_confidence,
    )

    assert match is not None
    assert match.confidence <= 1.0


# --- score_cross_source_match (CT-6) ---------------------------------------------------------


def test_score_cross_source_match_exact_name_and_shape_is_strong_evidence() -> None:
    match = score_cross_source_match(
        source_schema_name="SALES",
        source_table_name="CUSTOMER",
        source_columns=ACCOUNT_COLUMNS,
        target_schema_name="SALES",
        target_table_name="customer",
        target_columns=ACCOUNT_COLUMNS,
    )

    assert match is not None
    assert match.detection_rule == CROSS_SOURCE_EXACT_NAME_RULE
    assert match.evidence["exact_name_match"] is True
    assert match.confidence >= 0.95


def test_score_cross_source_match_renamed_equivalent_needs_shape_confirmation() -> None:
    match = score_cross_source_match(
        source_schema_name="SALES",
        source_table_name="CUSTOMER",
        source_columns=ACCOUNT_COLUMNS,
        target_schema_name="SALES",
        target_table_name="CUSTOMERS",
        target_columns=ACCOUNT_COLUMNS,
    )

    assert match is not None
    assert match.detection_rule == CROSS_SOURCE_STRUCTURAL_RULE
    assert match.evidence["exact_name_match"] is False


def test_score_cross_source_match_rejects_unrelated_tables() -> None:
    unrelated_columns = _columns(
        ("product_sku", "varchar"),
        ("price", "numeric"),
    )

    match = score_cross_source_match(
        source_schema_name="SALES",
        source_table_name="CUSTOMER",
        source_columns=ACCOUNT_COLUMNS,
        target_schema_name="INVENTORY",
        target_table_name="PRODUCT",
        target_columns=unrelated_columns,
    )

    assert match is None


def test_score_cross_source_match_rejects_same_name_but_incompatible_shape() -> None:
    # Same table name across sources is not enough on its own if the shapes disagree
    # sharply -- this guards against treating two unrelated same-named tables (a common
    # occurrence, e.g. "TEMP" or "STAGING") as the same logical asset.
    incompatible_columns = _columns(("x", "varchar"))

    match = score_cross_source_match(
        source_schema_name="SALES",
        source_table_name="STAGING",
        source_columns=ACCOUNT_COLUMNS,
        target_schema_name="SALES",
        target_table_name="STAGING",
        target_columns=incompatible_columns,
    )

    assert match is None


def test_score_cross_source_match_never_inspects_row_values() -> None:
    match = score_cross_source_match(
        source_schema_name="SALES",
        source_table_name="CUSTOMER",
        source_columns=ACCOUNT_COLUMNS,
        target_schema_name="SALES",
        target_table_name="CUSTOMER",
        target_columns=ACCOUNT_COLUMNS,
    )

    assert match is not None
    assert match.evidence["source_values_inspected"] is False


def test_score_cross_source_match_honors_min_confidence_floor() -> None:
    match = score_cross_source_match(
        source_schema_name="SALES",
        source_table_name="CUSTOMER",
        source_columns=ACCOUNT_COLUMNS,
        target_schema_name="ARCHIVE",
        target_table_name="CUSTOMERS_OLD",
        target_columns=ACCOUNT_COLUMNS,
        min_confidence=0.99,
    )

    assert match is None
