from uuid import UUID

from aida.main import app
from aida.relationship_intelligence import (
    ColumnMeta,
    composite_group_fingerprint,
    generate_composite_relationship_candidates,
    resolve_canonical_table_id,
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
# RL-2 -- canonical table resolution
#
# Table family detection (RL-1) is NOT in this module -- it shipped
# independently as ``aida.table_family_intelligence`` (see
# ``tests/test_table_family_intelligence.py``). ``resolve_canonical_table_id``
# is the one remaining piece: an explicit steward override always wins,
# otherwise fall back to the family's own ``TableFamilyCandidate.base_table_id``,
# which is itself ``None`` for an un-overridden SNAPSHOT family.
# --------------------------------------------------------------------------


def test_resolve_canonical_prefers_steward_override_over_base_table() -> None:
    result = resolve_canonical_table_id(
        base_table_id=uid(1), steward_override_table_id=uid(2)
    )

    assert result == uid(2)


def test_resolve_canonical_falls_back_to_base_table_when_no_override() -> None:
    result = resolve_canonical_table_id(base_table_id=uid(1), steward_override_table_id=None)

    assert result == uid(1)


def test_resolve_canonical_is_none_when_snapshot_has_no_base_and_no_override() -> None:
    # A SNAPSHOT TableFamilyCandidate never gets an algorithmic base_table_id
    # (see that model's docstring); with no steward override either, there is
    # nothing to resolve to.
    result = resolve_canonical_table_id(base_table_id=None, steward_override_table_id=None)

    assert result is None


def test_rl2_endpoints_are_exposed_on_the_app() -> None:
    # The RL-1 endpoints (table-family-candidates/*) live entirely in
    # aida.table_family_api -- see tests/test_table_family_api.py. These are
    # only the RL-2 (canonical resolution / steward override) additions.
    paths = app.openapi()["paths"]
    expected = {
        "/v1/datasources/{datasource_id}/canonical-table/resolve",
        "/v1/table-family-candidates/{family_candidate_id}/canonical",
        "/v1/table-family-candidates/{family_candidate_id}/canonical/override",
    }
    assert expected <= paths.keys()
    assert "get" in paths["/v1/datasources/{datasource_id}/canonical-table/resolve"]
    assert "get" in paths["/v1/table-family-candidates/{family_candidate_id}/canonical"]
    assert "post" in paths["/v1/table-family-candidates/{family_candidate_id}/canonical/override"]


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
