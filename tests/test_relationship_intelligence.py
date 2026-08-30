from datetime import UTC, datetime
from uuid import UUID

from aida.relationship_intelligence import (
    CanonicalCandidate,
    ColumnMeta,
    TableFamilyObservation,
    base_entity_key,
    composite_group_fingerprint,
    detect_table_families,
    generate_composite_relationship_candidates,
    resolve_canonical_member,
)


def uid(value: int) -> UUID:
    return UUID(int=value)


def column(
    table: int,
    column_id: int,
    name: str,
    physical_type: str = "text",
    *,
    nullable: bool = True,
    ordinal: int = 1,
    null_count: int | None = None,
    non_null_count: int | None = None,
    approximate_distinct_count: int | None = None,
) -> ColumnMeta:
    return ColumnMeta(
        id=uid(column_id),
        table_id=uid(table),
        name=name,
        physical_type=physical_type,
        nullable=nullable,
        ordinal_position=ordinal,
        null_count=null_count,
        non_null_count=non_null_count,
        approximate_distinct_count=approximate_distinct_count,
    )


# --------------------------------------------------------------------------
# RL-1 -- table family detection
# --------------------------------------------------------------------------


def test_base_entity_key_strips_date_and_history_suffixes() -> None:
    assert base_entity_key("customer_20240101")[1:] == ("20240101", "date")
    assert base_entity_key("customer_history")[0] == "customer"
    assert base_entity_key("customer_history")[2] == "history"
    assert base_entity_key("customer")[2] is None


def test_scd_table_detected_with_effective_expiry_pair_and_current_flag() -> None:
    observation = TableFamilyObservation(
        table_id=uid(1),
        table_name="dim_customer",
        schema_id=uid(100),
        primary_key_columns=("customer_key",),
        columns=(
            column(1, 1, "customer_key", "bigint", nullable=False, ordinal=1),
            column(1, 2, "effective_from", "date", ordinal=2),
            column(1, 3, "effective_to", "date", ordinal=3),
            column(1, 4, "is_current", "boolean", ordinal=4),
        ),
        row_count_estimate=500_000,
        inbound_reference_count=0,
    )

    groups = detect_table_families([observation])

    assert len(groups) == 1
    group = groups[0]
    assert group.family_type == "SCD"
    assert group.confidence >= 0.6
    assert group.members[0].table_id == uid(1)
    assert group.evidence["effective_expiry_pair"] is True
    assert group.evidence["current_flag_columns"] == ["is_current"]


def test_delta_cdc_table_detected_from_operation_column_and_low_cardinality_profile() -> None:
    observation = TableFamilyObservation(
        table_id=uid(2),
        table_name="orders_cdc",
        schema_id=uid(100),
        primary_key_columns=("order_id",),
        columns=(
            column(2, 1, "order_id", "bigint", nullable=False, ordinal=1),
            column(
                2,
                2,
                "op_type",
                "varchar",
                ordinal=2,
                null_count=0,
                non_null_count=1000,
                approximate_distinct_count=3,
            ),
            column(2, 3, "cdc_timestamp", "timestamp", ordinal=3),
        ),
    )

    groups = detect_table_families([observation])

    assert groups[0].family_type == "DELTA_CDC"
    assert groups[0].evidence["low_cardinality_operation_column"] is True


def test_history_table_detected_from_temporal_columns_and_near_duplicate_keys() -> None:
    observation = TableFamilyObservation(
        table_id=uid(3),
        table_name="account_history",
        schema_id=uid(100),
        primary_key_columns=("account_id",),
        columns=(
            column(
                3,
                1,
                "account_id",
                "bigint",
                nullable=False,
                ordinal=1,
                null_count=0,
                non_null_count=10_000,
                approximate_distinct_count=2_000,
            ),
            column(3, 2, "as_of_date", "date", ordinal=2),
            column(3, 3, "version_number", "integer", ordinal=3),
        ),
    )

    groups = detect_table_families([observation])

    assert groups[0].family_type == "HISTORY"
    assert groups[0].evidence["near_duplicate_keys"] is True
    assert groups[0].evidence["name_hint"] is True


def test_append_only_table_detected_from_insert_audit_without_update_audit() -> None:
    observation = TableFamilyObservation(
        table_id=uid(4),
        table_name="event_log",
        schema_id=uid(100),
        primary_key_columns=("event_id",),
        columns=(
            column(4, 1, "event_id", "bigint", nullable=False, ordinal=1),
            column(4, 2, "created_at", "timestamp", ordinal=2),
            column(4, 3, "payload", "jsonb", ordinal=3),
        ),
    )

    groups = detect_table_families([observation])

    assert groups[0].family_type == "APPEND_ONLY"
    assert groups[0].evidence["single_monotonic_primary_key"] is True


def test_reference_table_detected_when_small_and_widely_referenced() -> None:
    observation = TableFamilyObservation(
        table_id=uid(5),
        table_name="currency",
        schema_id=uid(100),
        primary_key_columns=("currency_code",),
        columns=(column(5, 1, "currency_code", "varchar", nullable=False, ordinal=1),),
        row_count_estimate=180,
        inbound_reference_count=6,
    )

    groups = detect_table_families([observation])

    assert groups[0].family_type == "REFERENCE"
    assert groups[0].evidence["widely_referenced"] is True


def test_snapshot_cluster_detected_across_date_suffixed_siblings() -> None:
    schema_id = uid(100)
    observations = [
        TableFamilyObservation(
            table_id=uid(10 + i),
            table_name=f"customer_2024010{i}",
            schema_id=schema_id,
            primary_key_columns=("customer_id",),
            columns=(
                column(10 + i, 100 + i, "customer_id", "bigint", nullable=False, ordinal=1),
                column(10 + i, 200 + i, "customer_name", "varchar", ordinal=2),
            ),
        )
        for i in range(1, 6)
    ]

    groups = detect_table_families(observations)

    assert len(groups) == 1
    group = groups[0]
    assert group.family_type == "SNAPSHOT"
    assert len(group.members) == 5
    assert group.evidence["member_count"] == 5
    assert {member.table_id for member in group.members} == {uid(10 + i) for i in range(1, 6)}


def test_table_with_no_signal_is_not_assigned_a_family() -> None:
    observation = TableFamilyObservation(
        table_id=uid(20),
        table_name="widgets",
        schema_id=uid(100),
        primary_key_columns=("widget_id",),
        columns=(
            column(20, 1, "widget_id", "bigint", nullable=False, ordinal=1),
            column(20, 2, "widget_name", "varchar", ordinal=2),
            column(20, 3, "updated_at", "timestamp", ordinal=3),
        ),
        row_count_estimate=50_000_000,
        inbound_reference_count=0,
    )

    groups = detect_table_families([observation])

    assert groups == []


def test_detection_is_deterministic_regardless_of_input_order() -> None:
    schema_id = uid(100)
    observations = [
        TableFamilyObservation(
            table_id=uid(30 + i),
            table_name=f"account_2024010{i}",
            schema_id=schema_id,
            primary_key_columns=("account_id",),
            columns=(
                column(30 + i, 300 + i, "account_id", "bigint", nullable=False, ordinal=1),
                column(30 + i, 400 + i, "balance", "numeric", ordinal=2),
            ),
        )
        for i in range(1, 4)
    ]

    forward = detect_table_families(observations)
    backward = detect_table_families(list(reversed(observations)))

    assert forward == backward


# --------------------------------------------------------------------------
# RL-2 -- canonical table resolution
# --------------------------------------------------------------------------


def test_single_member_family_is_trivially_canonical() -> None:
    result = resolve_canonical_member(
        [CanonicalCandidate(table_id=uid(1), table_name="customer")], family_type="REFERENCE"
    )

    assert result.table_id == uid(1)
    assert result.confidence == 1.0
    assert result.evidence["reason"] == "ONLY_FAMILY_MEMBER"


def test_canonical_resolution_prefers_latest_snapshot_date() -> None:
    candidates = [
        CanonicalCandidate(table_id=uid(1), table_name="customer_20240101"),
        CanonicalCandidate(table_id=uid(2), table_name="customer_20240301"),
        CanonicalCandidate(table_id=uid(3), table_name="customer_20240201"),
    ]

    result = resolve_canonical_member(candidates, family_type="SNAPSHOT")

    assert result.table_id == uid(2)
    assert result.evidence["selected"]["snapshot_recency_token"] == "20240301"  # noqa: S105 -- date token, not a credential
    assert len(result.evidence["candidates_considered"]) == 3


def test_canonical_resolution_penalizes_history_suffixed_variant() -> None:
    candidates = [
        CanonicalCandidate(
            table_id=uid(1),
            table_name="customer",
            inbound_reference_count=5,
            updated_at=datetime(2024, 6, 1, tzinfo=UTC),
        ),
        CanonicalCandidate(
            table_id=uid(2),
            table_name="customer_history",
            inbound_reference_count=1,
            updated_at=datetime(2024, 6, 1, tzinfo=UTC),
        ),
    ]

    result = resolve_canonical_member(candidates, family_type="HISTORY")

    assert result.table_id == uid(1)
    assert result.evidence["selected"]["history_or_variant_suffixed"] is False


def test_canonical_resolution_is_deterministic_on_full_ties() -> None:
    candidates = [
        CanonicalCandidate(table_id=uid(9), table_name="alpha"),
        CanonicalCandidate(table_id=uid(2), table_name="alpha"),
    ]

    result = resolve_canonical_member(candidates, family_type="REFERENCE")

    # Every signal ties, so the lowest table_id wins as the final tie-break.
    assert result.table_id == uid(2)


def test_resolve_canonical_member_rejects_empty_input() -> None:
    import pytest

    with pytest.raises(ValueError, match="at least one candidate"):
        resolve_canonical_member([], family_type="REFERENCE")


# --------------------------------------------------------------------------
# RL-3 -- composite relationship candidates
# --------------------------------------------------------------------------


def test_composite_candidate_proposed_for_matching_two_column_key() -> None:
    target_table = uid(1)
    source_table = uid(2)
    columns_by_table = {
        target_table: (
            column(1, 1, "region_id", "integer", nullable=False, ordinal=1),
            column(1, 2, "customer_id", "integer", nullable=False, ordinal=2),
        ),
        source_table: (
            column(2, 10, "region_id", "integer", ordinal=1),
            column(2, 11, "customer_id", "integer", ordinal=2),
            column(2, 12, "order_id", "integer", ordinal=3),
        ),
    }
    composite_primary_keys = {target_table: ("region_id", "customer_id")}

    results = generate_composite_relationship_candidates(
        columns_by_table=columns_by_table,
        composite_primary_keys=composite_primary_keys,
    )

    assert len(results) == 1
    candidate = results[0]
    assert candidate.source_table_id == source_table
    assert candidate.target_table_id == target_table
    assert [pair.source_column_name for pair in candidate.members] == ["region_id", "customer_id"]
    assert candidate.evidence["member_count"] == 2
    assert candidate.evidence["ordinal_alignment"] is True
    assert 0.0 < candidate.confidence <= 0.93


def test_composite_candidate_skipped_when_declared_as_foreign_key() -> None:
    target_table = uid(1)
    source_table = uid(2)
    columns_by_table = {
        target_table: (
            column(1, 1, "region_id", "integer", nullable=False, ordinal=1),
            column(1, 2, "customer_id", "integer", nullable=False, ordinal=2),
        ),
        source_table: (
            column(2, 10, "region_id", "integer", ordinal=1),
            column(2, 11, "customer_id", "integer", ordinal=2),
        ),
    }
    composite_primary_keys = {target_table: ("region_id", "customer_id")}
    declared = frozenset(
        {(source_table, ("region_id", "customer_id"), target_table, ("region_id", "customer_id"))}
    )

    results = generate_composite_relationship_candidates(
        columns_by_table=columns_by_table,
        composite_primary_keys=composite_primary_keys,
        declared_composite_foreign_keys=declared,
    )

    assert results == []


def test_composite_candidate_skipped_on_type_mismatch() -> None:
    target_table = uid(1)
    source_table = uid(2)
    columns_by_table = {
        target_table: (
            column(1, 1, "region_id", "integer", nullable=False, ordinal=1),
            column(1, 2, "customer_id", "integer", nullable=False, ordinal=2),
        ),
        source_table: (
            column(2, 10, "region_id", "varchar", ordinal=1),  # type mismatch
            column(2, 11, "customer_id", "integer", ordinal=2),
        ),
    }
    composite_primary_keys = {target_table: ("region_id", "customer_id")}

    results = generate_composite_relationship_candidates(
        columns_by_table=columns_by_table,
        composite_primary_keys=composite_primary_keys,
    )

    assert results == []


def test_composite_candidate_skipped_when_source_columns_are_entirely_null() -> None:
    target_table = uid(1)
    source_table = uid(2)
    columns_by_table = {
        target_table: (
            column(1, 1, "region_id", "integer", nullable=False, ordinal=1),
            column(1, 2, "customer_id", "integer", nullable=False, ordinal=2),
        ),
        source_table: (
            column(
                2, 10, "region_id", "integer", ordinal=1, null_count=1000, non_null_count=0
            ),
            column(
                2, 11, "customer_id", "integer", ordinal=2, null_count=1000, non_null_count=0
            ),
        ),
    }
    composite_primary_keys = {target_table: ("region_id", "customer_id")}

    results = generate_composite_relationship_candidates(
        columns_by_table=columns_by_table,
        composite_primary_keys=composite_primary_keys,
    )

    assert results == []


def test_composite_candidates_are_capped_per_target_table() -> None:
    target_table = uid(1)
    columns_by_table: dict[UUID, tuple[ColumnMeta, ...]] = {
        target_table: (
            column(1, 1, "region_id", "integer", nullable=False, ordinal=1),
            column(1, 2, "customer_id", "integer", nullable=False, ordinal=2),
        ),
    }
    for i in range(5):
        source_table = uid(100 + i)
        columns_by_table[source_table] = (
            column(100 + i, 1000 + i * 2, "region_id", "integer", ordinal=1),
            column(100 + i, 1001 + i * 2, "customer_id", "integer", ordinal=2),
        )
    composite_primary_keys = {target_table: ("region_id", "customer_id")}

    results = generate_composite_relationship_candidates(
        columns_by_table=columns_by_table,
        composite_primary_keys=composite_primary_keys,
        max_candidates_per_table=2,
    )

    assert len(results) == 2


def test_composite_candidate_requires_all_key_columns_present_by_name() -> None:
    target_table = uid(1)
    source_table = uid(2)
    columns_by_table = {
        target_table: (
            column(1, 1, "region_id", "integer", nullable=False, ordinal=1),
            column(1, 2, "customer_id", "integer", nullable=False, ordinal=2),
        ),
        source_table: (
            column(2, 10, "region_id", "integer", ordinal=1),
            # customer_id is missing on the source table.
        ),
    }
    composite_primary_keys = {target_table: ("region_id", "customer_id")}

    results = generate_composite_relationship_candidates(
        columns_by_table=columns_by_table,
        composite_primary_keys=composite_primary_keys,
    )

    assert results == []


def test_single_column_primary_keys_are_not_treated_as_composite() -> None:
    target_table = uid(1)
    columns_by_table = {
        target_table: (column(1, 1, "id", "integer", nullable=False, ordinal=1),),
    }
    composite_primary_keys = {target_table: ("id",)}

    results = generate_composite_relationship_candidates(
        columns_by_table=columns_by_table,
        composite_primary_keys=composite_primary_keys,
    )

    assert results == []


def test_composite_group_fingerprint_is_order_sensitive_and_stable() -> None:
    pairs = [(uid(1), uid(2)), (uid(3), uid(4))]
    reversed_pairs = list(reversed(pairs))

    first = composite_group_fingerprint(uid(10), uid(20), pairs)
    again = composite_group_fingerprint(uid(10), uid(20), pairs)
    swapped = composite_group_fingerprint(uid(10), uid(20), reversed_pairs)

    assert first == again
    assert first != swapped
