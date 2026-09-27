"""R11-B2: the rules that decide whether a live answer counts as right.

`scripts/execution_match_benchmark.py` scores Ask's generated SQL by the rows
it returns, compared against gold SQL run through the same governed gateway.
The live run costs money and is never run by default, so the part that can go
wrong silently -- the comparison -- is pinned here without a network call.

Each rule is a way a naive comparison lies:

- comparing SQL text marks a correct but differently-written query wrong;
- comparing column names marks `count(*) AS n` wrong against `AS customer_count`;
- comparing row order marks an unordered `GROUP BY` wrong at random;
- comparing numbers literally marks `5` wrong against `Decimal('5.00')`;
- treating the result as a *set* marks a duplicated row right.
"""

from decimal import Decimal

from scripts.execution_match_benchmark import normalise_result, results_match


def test_row_order_does_not_matter() -> None:
    generated = [{"account_type": "SAVINGS", "n": 2}, {"account_type": "CHECKING", "n": 3}]
    gold = [{"account_type": "CHECKING", "n": 3}, {"account_type": "SAVINGS", "n": 2}]

    assert results_match(generated, gold)


def test_column_aliases_do_not_matter() -> None:
    assert results_match([{"n": 5}], [{"customer_count": 5}])


def test_equal_numbers_in_different_types_match() -> None:
    assert results_match([{"total": 5}], [{"total_balance": Decimal("5.00")}])
    assert results_match([{"total": 5.0}], [{"total": "5"}])


def test_different_values_do_not_match() -> None:
    assert not results_match([{"n": 5}], [{"n": 6}])


def test_a_missing_row_does_not_match() -> None:
    gold = [{"type": "A", "n": 1}, {"type": "B", "n": 2}]

    assert not results_match([{"type": "A", "n": 1}], gold)


def test_a_duplicated_row_is_a_different_answer() -> None:
    """A multiset, not a set: returning a row twice is wrong, and a set
    comparison would quietly call it right."""
    assert not results_match([{"n": 1}, {"n": 1}], [{"n": 1}])


def test_nulls_are_compared_as_values() -> None:
    assert results_match([{"closed_at": None}], [{"closed": None}])
    assert not results_match([{"closed_at": None}], [{"closed_at": "2026-01-01"}])


def test_booleans_are_not_confused_with_numbers() -> None:
    """`True` is an `int` in Python. Without care it would normalise to `1`
    and match a count of one."""
    assert not results_match([{"active": True}], [{"n": 1}])


def test_normalisation_is_order_and_alias_free() -> None:
    assert normalise_result([{"b": 2, "a": 1}]) == normalise_result([{"x": 1, "y": 2}])
