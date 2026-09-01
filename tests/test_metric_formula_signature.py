"""Tests for `aida.metric_formula_signature` (AT-17: metric-formula collision
detection).

Pure unit tests -- no database required, no ORM objects, plain dict
snapshots throughout (mirroring `tests/test_semantic_diff.py`'s style for
this module's other pure detector). Covers: exact-formula duplicates across
different metrics, grain-normalized duplicates, genuinely different
formulas correctly NOT flagged, the same-metric-is-not-a-collision rule, and
the documented honest limits (no cross-aggregation algebra, no column-alias
resolution).
"""

from __future__ import annotations

from uuid import uuid4

from aida.metric_formula_signature import (
    FormulaCollision,
    compare_formulas,
    find_formula_collisions,
    normalize_metric_formula,
)


def _snapshot(
    *,
    metric_version_id: object | None = None,
    metric_id: object | None = None,
    metric_name: str = "Revenue",
    aggregation: str = "SUM",
    source_table_id: object | None = None,
    measure_column_id: object | None = None,
    default_time_column_id: object | None = None,
    grain: str = "daily",
) -> dict[str, object]:
    return {
        "metric_version_id": metric_version_id or uuid4(),
        "metric_id": metric_id or uuid4(),
        "metric_name": metric_name,
        "aggregation": aggregation,
        "source_table_id": source_table_id or uuid4(),
        "measure_column_id": measure_column_id,
        "default_time_column_id": default_time_column_id,
        "grain": grain,
    }


class TestNormalizeMetricFormula:
    def test_identity_fields_are_stringified(self) -> None:
        table_id = uuid4()
        column_id = uuid4()
        signature = normalize_metric_formula(
            _snapshot(source_table_id=table_id, measure_column_id=column_id)
        )
        assert signature.source_table_id == str(table_id)
        assert signature.measure_column_id == str(column_id)

    def test_aggregation_is_normalized_to_uppercase(self) -> None:
        signature = normalize_metric_formula(_snapshot(aggregation="sum"))
        assert signature.aggregation == "SUM"

    def test_missing_measure_column_stays_none(self) -> None:
        signature = normalize_metric_formula(
            _snapshot(aggregation="COUNT", measure_column_id=None)
        )
        assert signature.measure_column_id is None

    def test_grain_normalization_is_case_and_whitespace_only(self) -> None:
        signature = normalize_metric_formula(_snapshot(grain="  Daily "))
        assert signature.grain_raw == "  Daily "
        assert signature.grain_normalized == "daily"


class TestCompareFormulas:
    def test_same_metric_id_is_never_a_collision(self) -> None:
        metric_id = uuid4()
        table_id = uuid4()
        column_id = uuid4()
        left = normalize_metric_formula(
            _snapshot(
                metric_id=metric_id,
                source_table_id=table_id,
                measure_column_id=column_id,
            )
        )
        right = normalize_metric_formula(
            _snapshot(
                metric_id=metric_id,
                source_table_id=table_id,
                measure_column_id=column_id,
            )
        )
        assert compare_formulas(left, right) is None

    def test_identical_formula_different_metrics_is_exact_match(self) -> None:
        table_id = uuid4()
        column_id = uuid4()
        left = normalize_metric_formula(
            _snapshot(
                metric_id=uuid4(),
                metric_name="Net Revenue",
                source_table_id=table_id,
                measure_column_id=column_id,
                grain="daily",
            )
        )
        right = normalize_metric_formula(
            _snapshot(
                metric_id=uuid4(),
                metric_name="Daily Sales Total",
                source_table_id=table_id,
                measure_column_id=column_id,
                grain="daily",
            )
        )
        assert compare_formulas(left, right) == "EXACT_MATCH"

    def test_grain_differing_only_by_case_and_whitespace_is_normalized_match(self) -> None:
        table_id = uuid4()
        column_id = uuid4()
        left = normalize_metric_formula(
            _snapshot(
                metric_id=uuid4(),
                source_table_id=table_id,
                measure_column_id=column_id,
                grain="Daily",
            )
        )
        right = normalize_metric_formula(
            _snapshot(
                metric_id=uuid4(),
                source_table_id=table_id,
                measure_column_id=column_id,
                grain=" daily ",
            )
        )
        assert compare_formulas(left, right) == "NORMALIZED_GRAIN_MATCH"
        # An EXACT_MATCH is also technically a NORMALIZED_GRAIN_MATCH candidate,
        # but the raw-string check runs first, so identical grain reports
        # EXACT_MATCH -- verified by the previous test's identical `"daily"`.

    def test_different_aggregation_is_not_flagged(self) -> None:
        table_id = uuid4()
        column_id = uuid4()
        left = normalize_metric_formula(
            _snapshot(
                metric_id=uuid4(),
                aggregation="SUM",
                source_table_id=table_id,
                measure_column_id=column_id,
            )
        )
        right = normalize_metric_formula(
            _snapshot(
                metric_id=uuid4(),
                aggregation="AVG",
                source_table_id=table_id,
                measure_column_id=column_id,
            )
        )
        # This is the module's documented honest limit: SUM and AVG over the
        # *same* column/table/grain are mathematically different (SUM != AVG
        # in general), so not flagging them is correct, not a gap -- and
        # this schema has no ratio/composite metric shape that could pose
        # the "SUM(x)/COUNT(*) == AVG(x)" case as two different metrics at
        # all (see the module docstring).
        assert compare_formulas(left, right) is None

    def test_different_measure_column_is_not_flagged_even_on_same_table(self) -> None:
        table_id = uuid4()
        left = normalize_metric_formula(
            _snapshot(metric_id=uuid4(), source_table_id=table_id, measure_column_id=uuid4())
        )
        right = normalize_metric_formula(
            _snapshot(metric_id=uuid4(), source_table_id=table_id, measure_column_id=uuid4())
        )
        # No column-alias resolution is attempted -- two different
        # `measure_column_id`s are always different, even if they might
        # trace to the same underlying value via lineage.
        assert compare_formulas(left, right) is None

    def test_different_source_table_is_not_flagged(self) -> None:
        column_id = uuid4()
        left = normalize_metric_formula(
            _snapshot(metric_id=uuid4(), source_table_id=uuid4(), measure_column_id=column_id)
        )
        right = normalize_metric_formula(
            _snapshot(metric_id=uuid4(), source_table_id=uuid4(), measure_column_id=column_id)
        )
        assert compare_formulas(left, right) is None

    def test_different_default_time_column_is_not_flagged(self) -> None:
        table_id = uuid4()
        column_id = uuid4()
        left = normalize_metric_formula(
            _snapshot(
                metric_id=uuid4(),
                source_table_id=table_id,
                measure_column_id=column_id,
                default_time_column_id=uuid4(),
            )
        )
        right = normalize_metric_formula(
            _snapshot(
                metric_id=uuid4(),
                source_table_id=table_id,
                measure_column_id=column_id,
                default_time_column_id=uuid4(),
            )
        )
        assert compare_formulas(left, right) is None

    def test_both_count_with_no_measure_column_on_same_table_is_exact_match(self) -> None:
        table_id = uuid4()
        left = normalize_metric_formula(
            _snapshot(
                metric_id=uuid4(),
                aggregation="COUNT",
                source_table_id=table_id,
                measure_column_id=None,
                grain="daily",
            )
        )
        right = normalize_metric_formula(
            _snapshot(
                metric_id=uuid4(),
                aggregation="COUNT",
                source_table_id=table_id,
                measure_column_id=None,
                grain="daily",
            )
        )
        assert compare_formulas(left, right) == "EXACT_MATCH"


class TestFindFormulaCollisions:
    def test_finds_one_collision_among_three_metrics(self) -> None:
        table_id = uuid4()
        column_id = uuid4()
        colliding_a = _snapshot(
            metric_id=uuid4(),
            metric_name="Net Revenue",
            source_table_id=table_id,
            measure_column_id=column_id,
            grain="daily",
        )
        colliding_b = _snapshot(
            metric_id=uuid4(),
            metric_name="Daily Sales Total",
            source_table_id=table_id,
            measure_column_id=column_id,
            grain="daily",
        )
        unrelated = _snapshot(
            metric_id=uuid4(),
            metric_name="Active Customers",
            aggregation="COUNT",
            source_table_id=uuid4(),
            measure_column_id=None,
            grain="daily",
        )
        collisions = find_formula_collisions([colliding_a, colliding_b, unrelated])
        assert len(collisions) == 1
        collision = collisions[0]
        assert isinstance(collision, FormulaCollision)
        assert collision.match_kind == "EXACT_MATCH"
        assert {collision.left.metric_name, collision.right.metric_name} == {
            "Net Revenue",
            "Daily Sales Total",
        }

    def test_no_snapshots_share_a_formula_yields_no_collisions(self) -> None:
        snapshots = [
            _snapshot(metric_id=uuid4(), source_table_id=uuid4(), measure_column_id=uuid4())
            for _ in range(4)
        ]
        assert find_formula_collisions(snapshots) == []

    def test_multiple_versions_of_the_same_metric_are_never_reported(self) -> None:
        metric_id = uuid4()
        table_id = uuid4()
        column_id = uuid4()
        v1 = _snapshot(
            metric_version_id=uuid4(),
            metric_id=metric_id,
            source_table_id=table_id,
            measure_column_id=column_id,
        )
        v2 = _snapshot(
            metric_version_id=uuid4(),
            metric_id=metric_id,
            source_table_id=table_id,
            measure_column_id=column_id,
        )
        assert find_formula_collisions([v1, v2]) == []

    def test_three_way_collision_reports_each_pair_once(self) -> None:
        table_id = uuid4()
        column_id = uuid4()
        snapshots = [
            _snapshot(
                metric_id=uuid4(),
                metric_name=name,
                source_table_id=table_id,
                measure_column_id=column_id,
                grain="daily",
            )
            for name in ("Net Revenue", "Daily Sales Total", "Total Revenue")
        ]
        collisions = find_formula_collisions(snapshots)
        assert len(collisions) == 3
        pairs = {
            frozenset((collision.left.metric_id, collision.right.metric_id))
            for collision in collisions
        }
        assert len(pairs) == 3
