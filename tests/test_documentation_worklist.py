"""AT-5 -- pure ranking logic (`aida.documentation_worklist`).

No database: `rank_documentation_worklist` is a deterministic function of
plain `TableQuerySignal` dataclasses, mirroring `test_connector_health.py`'s
own "pure logic tested without a database" convention (CN-7/TL-6's "every
factor inspectable" shape). The DB-facing aggregation
(`stewardship_api._documentation_worklist_signals` and the
`documentation-worklist` endpoint) has its own integration test in
`tests/test_glossary_stewardship.py`.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from aida.documentation_worklist import (
    TableQuerySignal,
    rank_documentation_worklist,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _signal(
    *,
    table_id: UUID | None = None,
    table_name: str = "table",
    schema_name: str = "public",
    datasource_name: str = "warehouse",
    query_execution_count: int = 0,
    consumption_read_count: int = 0,
    last_queried_at: datetime | None = None,
    last_consumed_at: datetime | None = None,
    is_documented: bool = False,
    description_is_proposed: bool = False,
) -> TableQuerySignal:
    return TableQuerySignal(
        table_id=table_id or uuid4(),
        table_name=table_name,
        schema_name=schema_name,
        datasource_name=datasource_name,
        query_execution_count=query_execution_count,
        consumption_read_count=consumption_read_count,
        last_queried_at=last_queried_at,
        last_consumed_at=last_consumed_at,
        is_documented=is_documented,
        description_is_proposed=description_is_proposed,
    )


# --- documented tables excluded --------------------------------------------


def test_documented_table_is_excluded_regardless_of_volume() -> None:
    documented = _signal(
        table_name="documented_hot_table",
        query_execution_count=500,
        is_documented=True,
    )
    undocumented = _signal(table_name="undocumented_cold_table", query_execution_count=1)

    entries, total = rank_documentation_worklist([documented, undocumented], limit=10)

    assert total == 1
    assert [entry.table_name for entry in entries] == ["undocumented_cold_table"]


def test_pending_proposal_alone_still_counts_as_under_described() -> None:
    # is_documented is exactly UX-12's own determination: a PENDING_APPROVAL
    # draft alone (description_is_proposed=True) does not make is_documented
    # True -- this test asserts the worklist honours that, not re-derives it.
    proposed_only = _signal(
        table_name="has_a_pending_draft",
        query_execution_count=3,
        is_documented=False,
        description_is_proposed=True,
    )

    entries, total = rank_documentation_worklist([proposed_only], limit=10)

    assert total == 1
    assert entries[0].description_is_proposed is True


# --- ranked by real query volume, descending -------------------------------


def test_ranked_by_combined_query_and_consumption_volume_descending() -> None:
    low = _signal(table_name="low", query_execution_count=2, consumption_read_count=1)
    high = _signal(table_name="high", query_execution_count=10, consumption_read_count=5)
    mid = _signal(table_name="mid", query_execution_count=4, consumption_read_count=4)

    entries, total = rank_documentation_worklist([low, high, mid], limit=10)

    assert total == 3
    assert [entry.table_name for entry in entries] == ["high", "mid", "low"]
    assert entries[0].query_volume == 15
    assert entries[0].rank == 1
    assert entries[1].rank == 2
    assert entries[2].rank == 3


def test_every_ranking_factor_is_inspectable_on_the_entry() -> None:
    # TL-6/CN-7 convention: the inputs that drove the order, not just the
    # final position, are visible in the response.
    signal = _signal(
        table_name="accounts",
        query_execution_count=7,
        consumption_read_count=3,
        last_queried_at=NOW,
        last_consumed_at=NOW,
    )

    entries, _ = rank_documentation_worklist([signal], limit=10)

    entry = entries[0]
    assert entry.query_execution_count == 7
    assert entry.consumption_read_count == 3
    assert entry.query_volume == 10
    assert entry.last_queried_at == NOW
    assert entry.last_consumed_at == NOW


# --- ties broken deterministically ------------------------------------------


def test_ties_break_by_table_name_then_table_id() -> None:
    id_a = UUID("00000000-0000-0000-0000-000000000001")
    id_b = UUID("00000000-0000-0000-0000-000000000002")
    zebra = _signal(table_id=id_a, table_name="zebra", query_execution_count=5)
    alpha = _signal(table_id=id_b, table_name="alpha", query_execution_count=5)

    entries, _ = rank_documentation_worklist([zebra, alpha], limit=10)

    assert [entry.table_name for entry in entries] == ["alpha", "zebra"]


def test_tie_break_is_stable_across_repeated_calls() -> None:
    signals = [
        _signal(table_name="same_volume_a", query_execution_count=3),
        _signal(table_name="same_volume_b", query_execution_count=3),
        _signal(table_name="same_volume_c", query_execution_count=3),
    ]

    first_call, _ = rank_documentation_worklist(list(signals), limit=10)
    second_call, _ = rank_documentation_worklist(list(reversed(signals)), limit=10)

    assert [entry.table_name for entry in first_call] == [
        entry.table_name for entry in second_call
    ]


# --- zero-query-volume design choice: excluded by default ------------------


def test_zero_volume_tables_excluded_by_default() -> None:
    zero = _signal(table_name="never_queried")
    real = _signal(table_name="queried_once", query_execution_count=1)

    entries, total = rank_documentation_worklist([zero, real], limit=10)

    assert total == 1
    assert [entry.table_name for entry in entries] == ["queried_once"]


def test_zero_volume_tables_included_and_ranked_last_when_opted_in() -> None:
    zero = _signal(table_name="never_queried")
    real = _signal(table_name="queried_once", query_execution_count=1)

    entries, total = rank_documentation_worklist(
        [zero, real], limit=10, include_zero_volume=True
    )

    assert total == 2
    assert [entry.table_name for entry in entries] == ["queried_once", "never_queried"]
    assert entries[-1].query_volume == 0


def test_multiple_zero_volume_tables_still_tie_break_deterministically() -> None:
    zero_b = _signal(table_name="zero_b")
    zero_a = _signal(table_name="zero_a")

    entries, _ = rank_documentation_worklist(
        [zero_b, zero_a], limit=10, include_zero_volume=True
    )

    assert [entry.table_name for entry in entries] == ["zero_a", "zero_b"]


# --- pagination --------------------------------------------------------------


def test_limit_and_offset_paginate_the_ranked_list() -> None:
    signals = [
        _signal(table_name=f"table_{index:02d}", query_execution_count=100 - index)
        for index in range(5)
    ]

    page_one, total = rank_documentation_worklist(signals, limit=2, offset=0)
    page_two, _ = rank_documentation_worklist(signals, limit=2, offset=2)

    assert total == 5
    assert [entry.table_name for entry in page_one] == ["table_00", "table_01"]
    assert [entry.rank for entry in page_one] == [1, 2]
    assert [entry.table_name for entry in page_two] == ["table_02", "table_03"]
    assert [entry.rank for entry in page_two] == [3, 4]


def test_total_reflects_full_candidate_set_independent_of_limit() -> None:
    signals = [
        _signal(table_name=f"table_{index}", query_execution_count=1) for index in range(20)
    ]

    entries, total = rank_documentation_worklist(signals, limit=5, offset=0)

    assert total == 20
    assert len(entries) == 5


def test_empty_input_returns_empty_page() -> None:
    entries, total = rank_documentation_worklist([], limit=10)

    assert entries == []
    assert total == 0
