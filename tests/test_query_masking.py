from aida.query_gateway import (
    extract_column_lineage,
    redact_sql_literals,
    sensitive_projection_names,
)


def test_sensitive_projection_alias_remains_sensitive() -> None:
    names = sensitive_projection_names(
        "SELECT email_address AS contact FROM retail.customer",
        dialect="postgres",
        sensitive_source_names={"email_address"},
    )

    assert names == {"contact"}


def test_sensitive_derived_expression_remains_sensitive() -> None:
    names = sensitive_projection_names(
        "SELECT lower(customer_name) AS normalized_name FROM retail.customer",
        dialect="postgres",
        sensitive_source_names={"customer_name"},
    )

    assert names == {"normalized_name"}


def test_non_sensitive_projection_is_not_marked() -> None:
    names = sensitive_projection_names(
        "SELECT state_code AS region FROM retail.customer",
        dialect="postgres",
        sensitive_source_names={"email_address"},
    )

    assert names == set()


def test_column_lineage_maps_aliases_and_derived_outputs_without_literals() -> None:
    lineage = extract_column_lineage(
        "SELECT c.customer_id, UPPER(c.state_code) AS state_label "
        "FROM retail.customer AS c WHERE c.is_active = true",
        dialect="postgres",
    )

    assert lineage == [
        {
            "output_column": "customer_id",
            "lineage_type": "DIRECT",
            "source_columns": [{"table": "retail.customer", "column": "customer_id"}],
            "transformations": [],
        },
        {
            "output_column": "state_label",
            "lineage_type": "DERIVED",
            "source_columns": [{"table": "retail.customer", "column": "state_code"}],
            "transformations": ["UPPER"],
        },
    ]
    assert "true" not in str(lineage).lower()


def test_persisted_sql_redacts_string_and_numeric_literals() -> None:
    redacted = redact_sql_literals(
        "SELECT email_address FROM retail.customer "
        "WHERE email_address = 'private@example.com' AND customer_id = 42 LIMIT 10",
        dialect="postgres",
    )

    assert "private@example.com" not in redacted
    assert "42" not in redacted
    assert "email_address" in redacted
