"""R11-FP16: SQL that names a renamed table can be written against what replaced it.

An approved rename repoints every stored reference at the new table and leaves the old one
tombstoned and superseded. A governed tool's SQL text is not a stored reference: it still says
`sales.orders`, which the source no longer has. These tests pin the rewrite that makes a working
proposal possible, and the places it refuses to guess.
"""

from __future__ import annotations

from aida.table_rename_rewrite import rewrite_table_references

RENAMED = {"sales.orders": "sales.orders_2026"}


def test_every_reference_is_renamed_and_its_alias_kept() -> None:
    sql = (
        "SELECT o.id, i.qty FROM sales.orders o "
        "JOIN sales.items i ON i.order_id = o.id WHERE o.id IN (SELECT id FROM sales.orders)"
    )

    rewritten = rewrite_table_references(sql, dialect="postgres", replacements=RENAMED)

    assert rewritten is not None
    assert "sales.orders_2026" in rewritten
    assert "sales.orders " not in rewritten and "sales.orders)" not in rewritten
    # The alias survives, so every `o.` qualifier in the query still resolves.
    assert "AS o" in rewritten
    assert rewritten.count("sales.orders_2026") == 2
    assert "sales.items" in rewritten


def test_the_most_qualified_name_wins() -> None:
    sql = "SELECT * FROM bank.sales.orders JOIN sales.orders ON TRUE"

    rewritten = rewrite_table_references(
        sql,
        dialect="postgres",
        replacements={"bank.sales.orders": "bank.sales.o_three", "sales.orders": "sales.o_two"},
    )

    assert rewritten is not None
    assert "bank.sales.o_three" in rewritten and "sales.o_two" in rewritten


def test_a_name_the_query_defines_for_itself_is_never_rewritten() -> None:
    sql = "WITH orders AS (SELECT 1 AS id) SELECT * FROM orders"

    assert (
        rewrite_table_references(
            sql, dialect="postgres", replacements={"orders": "sales.orders_2026"}
        )
        is None
    )


def test_sql_that_names_none_of_them_is_left_alone() -> None:
    assert (
        rewrite_table_references(
            "SELECT * FROM sales.items", dialect="postgres", replacements=RENAMED
        )
        is None
    )
    assert rewrite_table_references("SELECT 1", dialect="postgres", replacements={}) is None


def test_sql_that_will_not_parse_is_refused_rather_than_patched() -> None:
    assert (
        rewrite_table_references(
            "SELECT FROM WHERE ((", dialect="postgres", replacements=RENAMED
        )
        is None
    )


def test_a_bare_reference_is_rewritten_to_the_qualified_replacement() -> None:
    rewritten = rewrite_table_references(
        "SELECT * FROM orders WHERE region = :region",
        dialect="postgres",
        replacements={"orders": "sales.orders_2026"},
    )

    assert rewritten is not None and "sales.orders_2026" in rewritten


def test_the_dialect_is_the_source_dialect_in_and_out() -> None:
    rewritten = rewrite_table_references(
        "SELECT TOP 5 * FROM sales.orders",
        dialect="tsql",
        replacements=RENAMED,
    )

    assert rewritten is not None
    assert "TOP 5" in rewritten and "sales.orders_2026" in rewritten
