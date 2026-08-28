from aida.sql_guard import SqlGuard


def guard() -> SqlGuard:
    return SqlGuard(default_row_limit=5000, hard_row_limit=100_000)


def test_select_is_normalized_and_bounded() -> None:
    result = guard().validate(
        "SELECT c.customer_id FROM retail.customer AS c",
        dialect="postgres",
    )

    assert result.valid
    assert result.applied_row_limit == 5000
    assert result.referenced_tables == ("retail.customer",)
    assert "LIMIT 5000" in (result.normalized_sql or "")


def test_existing_limit_is_clamped_to_requested_limit() -> None:
    result = guard().validate(
        "SELECT customer_id FROM retail.customer LIMIT 50000",
        dialect="postgres",
        requested_limit=1000,
    )

    assert result.valid
    assert result.applied_row_limit == 1000
    assert "LIMIT 1000" in (result.normalized_sql or "")


def test_delete_is_rejected() -> None:
    result = guard().validate("DELETE FROM retail.customer", dialect="postgres")

    assert not result.valid
    assert "READ_ONLY_QUERY_REQUIRED" in result.violations
    assert "MUTATING_OR_ADMIN_STATEMENT_FORBIDDEN" in result.violations


def test_multiple_statements_are_rejected() -> None:
    result = guard().validate("SELECT 1; SELECT 2", dialect="postgres")

    assert not result.valid
    assert "EXACTLY_ONE_STATEMENT_REQUIRED" in result.violations


def test_cross_join_is_rejected() -> None:
    result = guard().validate(
        "SELECT * FROM retail.customer c CROSS JOIN retail.account a",
        dialect="postgres",
    )

    assert not result.valid
    assert "CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN" in result.violations


def test_select_wildcard_is_rejected_but_count_star_is_allowed() -> None:
    wildcard = guard().validate("SELECT * FROM retail.customer", dialect="postgres")
    count = guard().validate("SELECT COUNT(*) FROM retail.customer", dialect="postgres")

    assert "SELECT_WILDCARD_FORBIDDEN" in wildcard.violations
    assert count.valid


def test_cte_alias_is_not_reported_as_physical_table() -> None:
    result = guard().validate(
        "WITH active AS (SELECT customer_id FROM retail.customer) SELECT customer_id FROM active",
        dialect="postgres",
    )

    assert result.valid
    assert result.referenced_tables == ("retail.customer",)


def test_forbidden_database_function_is_rejected() -> None:
    result = guard().validate("SELECT pg_sleep(5)", dialect="postgres")

    assert not result.valid
    assert "FORBIDDEN_FUNCTION:pg_sleep" in result.violations
