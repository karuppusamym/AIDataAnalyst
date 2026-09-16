import pytest

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


# R11-FP14: a SELECT can call a function that writes, sleeps or reaches outside the engine, so a
# function the guard does not recognise as a built-in is refused unless an operator authorized it.


@pytest.mark.parametrize(
    ("dialect", "sql", "violation"),
    [
        (
            "postgres",
            "SELECT fn_send_mail(customer_id) FROM retail.customer",
            "UNAUTHORIZED_FUNCTION:fn_send_mail",
        ),
        (
            "postgres",
            "SELECT finance.fn_rate(amount) FROM finance.loan",
            "UNAUTHORIZED_FUNCTION:finance.fn_rate",
        ),
        ("tsql", "SELECT dbo.fn_rate(amount) FROM dbo.loan", "UNAUTHORIZED_FUNCTION:dbo.fn_rate"),
        (
            "oracle",
            "SELECT risk_pkg.score(customer_id) FROM retail.customer",
            "UNAUTHORIZED_FUNCTION:risk_pkg.score",
        ),
    ],
)
def test_an_unknown_or_user_defined_function_is_refused(
    dialect: str, sql: str, violation: str
) -> None:
    result = guard().validate(sql, dialect=dialect)

    assert not result.valid
    assert violation in result.violations


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        (
            "postgres",
            "SELECT COALESCE(SUM(amount), 0), DATE_TRUNC('month', booked_at), btrim(note) "
            "FROM finance.loan GROUP BY DATE_TRUNC('month', booked_at), btrim(note)",
        ),
        ("postgres", "SELECT pg_catalog.btrim(note) FROM finance.loan"),
        (
            "tsql",
            "SELECT ISNULL(SUM(amount), 0), DATEPART(year, booked_at), PATINDEX('%x%', note) "
            "FROM dbo.loan GROUP BY DATEPART(year, booked_at), PATINDEX('%x%', note)",
        ),
        ("snowflake", "SELECT IFF(amount > 0, 1, 0), DATEADD(day, 1, booked_at) FROM finance.loan"),
        ("bigquery", "SELECT SAFE.SUBSTR(note, 1, 2), SAFE_DIVIDE(amount, term) FROM finance.loan"),
    ],
)
def test_built_in_functions_are_still_accepted(dialect: str, sql: str) -> None:
    result = guard().validate(sql, dialect=dialect)

    assert result.valid, result.violations


def test_an_operator_authorized_function_is_accepted_by_its_exact_name() -> None:
    authorized = SqlGuard(
        default_row_limit=5000,
        hard_row_limit=100_000,
        allowed_functions=[" Finance.FN_RATE ", "fn_send_mail", ""],
    )

    assert authorized.validate(
        "SELECT finance.fn_rate(amount) FROM finance.loan", dialect="postgres"
    ).valid
    assert authorized.validate(
        "SELECT fn_send_mail(customer_id) FROM retail.customer", dialect="postgres"
    ).valid
    other_schema = authorized.validate(
        "SELECT risk.fn_rate(amount) FROM finance.loan", dialect="postgres"
    )
    assert "UNAUTHORIZED_FUNCTION:risk.fn_rate" in other_schema.violations


def test_authorization_never_lifts_the_adversarial_denylist() -> None:
    result = SqlGuard(
        default_row_limit=5000, hard_row_limit=100_000, allowed_functions=["pg_sleep"]
    ).validate("SELECT pg_sleep(5)", dialect="postgres")

    assert "FORBIDDEN_FUNCTION:pg_sleep" in result.violations


@pytest.mark.parametrize(
    ("name", "sql"),
    [
        ("nvl", "SELECT nvl(c.amount, 0) AS v FROM retail.customer AS c"),
        ("median", "SELECT median(c.amount) AS v FROM retail.customer AS c"),
        ("greatest", "SELECT greatest(c.amount, 0) AS v FROM retail.customer AS c"),
    ],
)
def test_a_call_naming_a_routine_this_source_declares_is_refused(name: str, sql: str) -> None:
    """sqlglot models these names for every dialect, so the parser alone calls them built-ins.

    Discovery read the source's routines, and a name it declares is a user-defined function here
    whatever the parser made of the call -- its effects are as unknown as any other's.
    """
    assert guard().validate(sql, dialect="postgres").valid
    refused = guard().validate(sql, dialect="postgres", user_defined_functions={name})

    assert not refused.valid
    assert f"UNAUTHORIZED_FUNCTION:{name}" in refused.violations


def test_operator_authorization_still_lifts_a_declared_routine() -> None:
    authorized = SqlGuard(
        default_row_limit=5000, hard_row_limit=100_000, allowed_functions=["nvl"]
    )

    result = authorized.validate(
        "SELECT nvl(c.amount, 0) AS v FROM retail.customer AS c",
        dialect="postgres",
        user_defined_functions={"nvl"},
    )

    assert result.valid


def test_a_column_named_like_a_declared_routine_is_still_a_column() -> None:
    result = guard().validate(
        "SELECT c.median FROM retail.customer AS c",
        dialect="postgres",
        user_defined_functions={"median"},
    )

    assert result.valid


@pytest.mark.parametrize(
    ("dialect", "sql"),
    [
        ("oracle", "SELECT order_seq.NEXTVAL AS v FROM retail.customer"),
        ("snowflake", "SELECT order_seq.NEXTVAL AS v FROM retail.customer"),
        ("tsql", "SELECT NEXT VALUE FOR dbo.order_seq AS v"),
    ],
)
def test_advancing_a_sequence_is_refused(dialect: str, sql: str) -> None:
    """A sequence advance writes, and only Postgres enforces a read-only transaction server-side.

    Postgres spells it `nextval('s')`, which the unrecognised-call rule already refuses; these
    three spell it as a qualified column or a `NEXT VALUE FOR` clause, which are not calls.
    """
    result = guard().validate(sql, dialect=dialect)

    assert not result.valid
    assert "SEQUENCE_ADVANCE_FORBIDDEN" in result.violations


def test_a_column_named_nextval_is_still_a_column() -> None:
    result = guard().validate(
        "SELECT c.nextval FROM retail.customer AS c", dialect="oracle"
    )

    assert result.valid
    assert "SEQUENCE_ADVANCE_FORBIDDEN" not in result.violations
