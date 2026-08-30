"""RL-1: pure-function coverage for the four table-family/temporal detectors.

Follows this repo's established convention (see `catalog_bulk_actions` and
its test file) of exercising the detection heuristics directly on plain
tuple fixtures, with no database and no FastAPI app involved.
"""

from uuid import uuid4

from aida.table_family_intelligence import (
    SCD_CORROBORATING_COLUMNS,
    detect_delta_families,
    detect_history_families,
    detect_scd_tables,
    detect_snapshot_families,
    detect_table_families,
)


def _table(name: str, columns: list[tuple[str, str, bool]] | None = None):
    return (uuid4(), name, columns or [])


# ---------------------------------------------------------------------------
# Snapshot family detection
# ---------------------------------------------------------------------------


def test_snapshot_family_detected_with_three_date_suffixed_siblings() -> None:
    tables = [
        _table("sales_20240101"),
        _table("sales_20240201"),
        _table("sales_20240301"),
    ]
    candidates = detect_snapshot_families(tables)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.family_type == "SNAPSHOT"
    assert candidate.base_table_id is None
    assert {member_id for member_id, _, _ in tables} == set(candidate.member_table_ids)
    assert candidate.evidence["member_count"] == 3
    assert candidate.evidence["suffix_kinds"] == ["YYYYMMDD"]
    assert 0.0 < candidate.confidence <= 0.9


def test_snapshot_family_detected_with_underscore_separated_date_parts() -> None:
    tables = [
        _table("daily_export_2024_01_15"),
        _table("daily_export_2024_02_15"),
        _table("daily_export_2024_03_15"),
        _table("daily_export_2024_04_15"),
    ]
    candidates = detect_snapshot_families(tables)
    assert len(candidates) == 1
    assert candidates[0].evidence["member_count"] == 4
    assert candidates[0].evidence["suffix_kinds"] == ["YYYY_MM_DD"]


def test_two_unrelated_tables_sharing_a_short_prefix_do_not_fire() -> None:
    tables = [_table("user_config"), _table("user_profile")]
    assert detect_snapshot_families(tables) == []


def test_exactly_two_date_suffixed_siblings_are_too_weak_to_fire() -> None:
    # Below MIN_SNAPSHOT_FAMILY_SIZE: a pair sharing a plausible date suffix
    # alone is treated as coincidental, not a family.
    tables = [_table("sales_20240101"), _table("sales_20240201")]
    assert detect_snapshot_families(tables) == []


def test_snapshot_sequence_suffix_scores_lower_than_date_suffix() -> None:
    date_tables = [
        _table("sales_20240101"),
        _table("sales_20240201"),
        _table("sales_20240301"),
    ]
    sequence_tables = [
        _table("part_001"),
        _table("part_002"),
        _table("part_003"),
    ]
    date_confidence = detect_snapshot_families(date_tables)[0].confidence
    sequence_confidence = detect_snapshot_families(sequence_tables)[0].confidence
    assert sequence_confidence < date_confidence


# ---------------------------------------------------------------------------
# History/audit family detection
# ---------------------------------------------------------------------------


def test_history_family_detected_when_live_sibling_exists() -> None:
    orders = _table("orders")
    history = _table("orders_history")
    candidates = detect_history_families([orders, history])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.family_type == "HISTORY"
    assert candidate.base_table_id == orders[0]
    assert set(candidate.member_table_ids) == {orders[0], history[0]}
    assert candidate.evidence["matched_suffix"] == "_history"


def test_history_suffixed_table_with_no_sibling_does_not_fire() -> None:
    candidates = detect_history_families([_table("orders_history")])
    assert candidates == []


def test_history_family_also_recognizes_hist_audit_archive_suffixes() -> None:
    for suffix in ("_hist", "_audit", "_archive"):
        candidates = detect_history_families([_table("orders"), _table(f"orders{suffix}")])
        assert len(candidates) == 1
        assert candidates[0].evidence["matched_suffix"] == suffix


# ---------------------------------------------------------------------------
# Delta family detection
# ---------------------------------------------------------------------------


def test_delta_family_detected_when_base_table_exists() -> None:
    customers = _table("customers")
    delta = _table("customers_delta")
    candidates = detect_delta_families([customers, delta])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.family_type == "DELTA"
    assert candidate.base_table_id == customers[0]
    assert set(candidate.member_table_ids) == {customers[0], delta[0]}


def test_delta_suffixed_table_with_no_base_does_not_fire() -> None:
    assert detect_delta_families([_table("customers_delta")]) == []


def test_delta_family_also_recognizes_cdc_changes_diff_suffixes() -> None:
    for suffix in ("_cdc", "_changes", "_diff"):
        candidates = detect_delta_families([_table("customers"), _table(f"customers{suffix}")])
        assert len(candidates) == 1


# ---------------------------------------------------------------------------
# SCD Type 2 detection
# ---------------------------------------------------------------------------


def test_scd_detected_with_effective_and_expiration_date_columns() -> None:
    scd_table = _table(
        "dim_customer",
        [
            ("customer_key", "BIGINT", False),
            ("effective_date", "DATE", False),
            ("expiration_date", "DATE", True),
        ],
    )
    candidates = detect_scd_tables([scd_table])
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.family_type == "SCD"
    assert candidate.member_table_ids == [scd_table[0]]
    assert candidate.base_table_id == scd_table[0]
    assert candidate.evidence["strength"] == "STRONG_FROM_AND_TO"
    assert candidate.confidence >= 0.8


def test_scd_not_detected_from_ambiguous_version_column_alone() -> None:
    table = _table(
        "widget",
        [("id", "BIGINT", False), ("version", "INTEGER", False)],
    )
    assert detect_scd_tables([table]) == []
    assert "version" not in SCD_CORROBORATING_COLUMNS


def test_scd_not_detected_from_lone_from_side_column() -> None:
    table = _table(
        "widget",
        [("id", "BIGINT", False), ("valid_from", "DATE", False)],
    )
    assert detect_scd_tables([table]) == []


def test_scd_moderate_confidence_with_single_side_plus_corroborating_column() -> None:
    table = _table(
        "dim_product",
        [
            ("product_key", "BIGINT", False),
            ("valid_from", "DATE", False),
            ("is_current", "BOOLEAN", False),
        ],
    )
    candidates = detect_scd_tables([table])
    assert len(candidates) == 1
    assert candidates[0].evidence["strength"] == "MODERATE_SINGLE_SIDE_PLUS_CORROBORATING"
    strong_candidate = detect_scd_tables(
        [
            _table(
                "dim_customer",
                [("effective_date", "DATE", False), ("expiration_date", "DATE", True)],
            )
        ]
    )[0]
    assert candidates[0].confidence < strong_candidate.confidence


def test_scd_column_matching_is_case_insensitive() -> None:
    table = _table(
        "dim_customer",
        [("EFFECTIVE_DATE", "DATE", False), ("Expiration_Date", "DATE", True)],
    )
    assert len(detect_scd_tables([table])) == 1


# ---------------------------------------------------------------------------
# Combined detector + idempotency
# ---------------------------------------------------------------------------


def test_detect_table_families_combines_all_four_detectors() -> None:
    tables = [
        _table("sales_20240101"),
        _table("sales_20240201"),
        _table("sales_20240301"),
        _table("orders"),
        _table("orders_history"),
        _table("customers"),
        _table("customers_delta"),
        _table(
            "dim_customer",
            [("effective_date", "DATE", False), ("expiration_date", "DATE", True)],
        ),
    ]
    candidates = detect_table_families(tables)
    family_types = {candidate.family_type for candidate in candidates}
    assert family_types == {"SNAPSHOT", "HISTORY", "DELTA", "SCD"}


def test_detectors_are_deterministic_across_repeated_calls() -> None:
    tables = [
        _table("orders"),
        _table("orders_history"),
        _table("customers"),
        _table("customers_delta"),
    ]
    first = detect_table_families(tables)
    second = detect_table_families(tables)
    assert [candidate.member_key() for candidate in first] == [
        candidate.member_key() for candidate in second
    ]
