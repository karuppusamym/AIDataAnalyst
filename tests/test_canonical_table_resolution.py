"""Pure-function coverage for RL-2 (canonical table resolution).

Follows this repo's established convention (see
`tests/test_table_family_intelligence.py` / `tests/test_composite_key_inference.py`)
of exercising the detection heuristic directly on plain tuple fixtures, with
no database and no FastAPI app involved.
"""

from uuid import uuid4

from aida.canonical_table_resolution import (
    MAX_CANDIDATE_PAIRS_EVALUATED,
    MAX_CONFIDENCE,
    MAX_GROUP_SIZE,
    MAX_NAME_BUCKET_SIZE,
    MIN_COLUMN_SIGNATURE_SIMILARITY,
    TableInput,
    _TableRecord,
    detect_canonical_table_groups,
    pick_default_canonical,
)

BASE_COLUMNS = [
    ("id", "int"),
    ("name", "varchar(50)"),
    ("created_at", "timestamp"),
    ("status", "varchar"),
]


def _table(
    name: str,
    *,
    schema_name: str = "public",
    catalog_name: str = "analytics",
    datasource_id=None,
    fingerprint: str = "",
    row_count_estimate: int | None = None,
    columns: list[tuple[str, str]] | None = None,
) -> TableInput:
    return (
        uuid4(),
        name,
        schema_name,
        catalog_name,
        datasource_id or uuid4(),
        fingerprint or f"fp-{uuid4()}",
        row_count_estimate,
        columns if columns is not None else BASE_COLUMNS,
    )


# ---------------------------------------------------------------------------
# Cross-schema/cross-datasource duplicate group forms with a sensible default
# ---------------------------------------------------------------------------


def test_cross_datasource_duplicate_forms_group_with_higher_row_count_as_default() -> None:
    prod = _table(
        "orders",
        schema_name="public",
        catalog_name="analytics",
        row_count_estimate=500_000,
        columns=BASE_COLUMNS,
    )
    reporting_mirror = _table(
        "orders",
        schema_name="public",
        catalog_name="reporting",
        row_count_estimate=480_000,
        columns=BASE_COLUMNS,
    )

    groups = detect_canonical_table_groups([prod, reporting_mirror])

    assert len(groups) == 1
    group = groups[0]
    assert set(group.member_table_ids) == {prod[0], reporting_mirror[0]}
    # Identical column signatures -> Jaccard similarity of 1.0 -> confidence
    # saturates at the module's ceiling.
    assert group.confidence == MAX_CONFIDENCE
    assert group.default_canonical_table_id == prod[0]
    assert group.evidence["spans_multiple_datasources"] is True
    assert group.evidence["default_canonical_table_id"] == str(prod[0])


def test_dev_schema_member_never_wins_default_pick_even_with_higher_row_count() -> None:
    dev_copy = _table(
        "orders",
        schema_name="dev",
        catalog_name="analytics",
        row_count_estimate=999_999_999,  # deliberately implausible/huge
        columns=BASE_COLUMNS,
    )
    prod_copy = _table(
        "orders",
        schema_name="public",
        catalog_name="analytics",
        row_count_estimate=10,
        columns=BASE_COLUMNS,
    )

    groups = detect_canonical_table_groups([dev_copy, prod_copy])

    assert len(groups) == 1
    assert groups[0].default_canonical_table_id == prod_copy[0]
    reason = groups[0].evidence["default_canonical_pick_reason"]
    assert "preferred_non_dev_staging_sandbox_schema_or_catalog" in reason["steps_applied"]


def test_minor_column_drift_still_groups_above_similarity_threshold() -> None:
    # 4 shared columns + 1 extra on one side => Jaccard 4/5 = 0.8, exactly the
    # documented floor.
    drifted_columns = [*BASE_COLUMNS, ("legacy_flag", "boolean")]
    left = _table("customers", row_count_estimate=100, columns=BASE_COLUMNS)
    right = _table("customers", row_count_estimate=90, columns=drifted_columns)

    groups = detect_canonical_table_groups([left, right])

    assert len(groups) == 1
    assert groups[0].evidence["min_pairwise_column_signature_similarity"] == 0.8


def test_identical_fingerprint_is_treated_as_maximal_shape_match() -> None:
    # Deliberately dissimilar column payloads (so Jaccard alone would not
    # clear the threshold) but a byte-identical fingerprint, simulating a
    # verbatim replicated discovery snapshot.
    left = _table("accounts", fingerprint="same-hash", columns=BASE_COLUMNS)
    right = _table(
        "accounts",
        fingerprint="same-hash",
        columns=[("id", "int"), ("balance", "number"), ("owner", "varchar")],
    )

    groups = detect_canonical_table_groups([left, right])

    assert len(groups) == 1
    assert groups[0].evidence["min_pairwise_column_signature_similarity"] == 1.0


# ---------------------------------------------------------------------------
# Negative: same name, different shape -> never groups
# ---------------------------------------------------------------------------


def test_same_name_but_structurally_different_tables_do_not_group() -> None:
    orders_a = _table("orders", columns=BASE_COLUMNS)
    orders_b = _table(
        "orders",
        columns=[("order_total", "number"), ("region", "varchar"), ("flag", "boolean")],
    )

    groups = detect_canonical_table_groups([orders_a, orders_b])

    assert groups == []


def test_tables_below_minimum_column_count_never_group_even_if_identical() -> None:
    tiny_a = _table("lookup", columns=[("id", "int"), ("code", "varchar")])
    tiny_b = _table("lookup", columns=[("id", "int"), ("code", "varchar")])

    groups = detect_canonical_table_groups([tiny_a, tiny_b])

    assert groups == []


def test_single_table_never_groups() -> None:
    assert detect_canonical_table_groups([_table("orders")]) == []


# ---------------------------------------------------------------------------
# Bounds: group size and candidate-pair caps
# ---------------------------------------------------------------------------


def test_group_size_is_capped_and_oversized_component_is_dropped() -> None:
    # One more member than MAX_GROUP_SIZE, all mutually identical -- forms a
    # single connected component exceeding the cap, so no group should be
    # emitted for it at all (never silently truncated to the cap).
    members = [
        _table("widgets", schema_name=f"env{i}", columns=BASE_COLUMNS)
        for i in range(MAX_GROUP_SIZE + 1)
    ]

    groups = detect_canonical_table_groups(members)

    assert groups == []


def test_name_bucket_larger_than_cap_is_skipped_entirely() -> None:
    members = [
        _table("generic_lookup", schema_name=f"env{i}", columns=BASE_COLUMNS)
        for i in range(MAX_NAME_BUCKET_SIZE + 1)
    ]

    groups = detect_canonical_table_groups(members)

    assert groups == []


def test_candidate_pair_budget_holds_on_a_synthetic_wide_input() -> None:
    # Many distinct bare names, each with only 2 members -- exercises the
    # pair-evaluation path broadly without tripping the per-bucket cap, and
    # confirms the detector completes and returns sane, bounded output
    # rather than evaluating an unbounded number of pairs.
    members: list[TableInput] = []
    for i in range(500):
        base_name = f"table_{i}"
        members.append(_table(base_name, columns=BASE_COLUMNS))
        members.append(_table(base_name, columns=BASE_COLUMNS))

    groups = detect_canonical_table_groups(members)

    assert len(groups) <= 500
    assert all(len(g.member_table_ids) == 2 for g in groups)
    # Every group's confidence must be sane regardless of how many pairs the
    # bound allowed to be evaluated.
    assert all(0.0 < g.confidence <= MAX_CONFIDENCE for g in groups)


def test_max_candidate_pairs_constant_is_a_positive_bound() -> None:
    # Guards the constant itself against an accidental regression to 0 or
    # negative, which would silently disable detection entirely.
    assert MAX_CANDIDATE_PAIRS_EVALUATED > 0
    assert MIN_COLUMN_SIGNATURE_SIMILARITY > 0.0


# ---------------------------------------------------------------------------
# pick_default_canonical, directly
# ---------------------------------------------------------------------------


def _record(
    *, schema_name: str, catalog_name: str, row_count_estimate: int | None, table_id=None
) -> _TableRecord:
    return _TableRecord(
        table_id=table_id or uuid4(),
        name="orders",
        schema_name=schema_name,
        catalog_name=catalog_name,
        datasource_id=uuid4(),
        fingerprint="fp",
        row_count_estimate=row_count_estimate,
        signature=frozenset({("id", "int")}),
    )


def test_pick_default_canonical_falls_back_to_row_count_when_all_flagged_or_all_clean() -> None:
    # All members share the same "cleanliness" -- the environment filter is a
    # no-op, row_count decides.
    low = _record(schema_name="public", catalog_name="analytics", row_count_estimate=10)
    high = _record(schema_name="public", catalog_name="analytics", row_count_estimate=99)

    winner, reason = pick_default_canonical([low, high])

    assert winner == high.table_id
    assert reason["steps_applied"] == ["preferred_highest_row_count_estimate"]


def test_pick_default_canonical_is_fully_deterministic_on_a_true_tie() -> None:
    a = _record(schema_name="public", catalog_name="analytics", row_count_estimate=50)
    b = _record(schema_name="public", catalog_name="analytics", row_count_estimate=50)

    winner, reason = pick_default_canonical([a, b])

    assert winner == min(a.table_id, b.table_id, key=str)
    assert "tie_broken_by_lowest_table_id" in reason["steps_applied"]
