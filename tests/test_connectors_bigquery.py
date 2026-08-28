import json

import pytest

from aida.config import Settings
from aida.connectors.base import QueryEstimate
from aida.connectors.bigquery import (
    BigQueryConnector,
    _assemble_catalog,
    _parse_credential_payload,
    _profile_expressions,
    _qualified_table,
    _quote_identifier,
    _region_dataset,
)
from aida.connectors.registry import connector_registry
from aida.query_gateway import gate_query_estimate

_SERVICE_ACCOUNT_INFO = {
    "type": "service_account",
    "project_id": "bank-warehouse",
    "private_key_id": "key123",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
    "client_email": "reader@bank-warehouse.iam.gserviceaccount.com",
    "client_id": "123456789",
    "token_uri": "https://oauth2.googleapis.com/token",
}

_VALID_SERVICE_ACCOUNT_DSN = json.dumps(
    {
        "project_id": "bank-warehouse",
        "location": "US",
        "auth_method": "service_account",
        "service_account_info": _SERVICE_ACCOUNT_INFO,
    }
)

_VALID_WORKLOAD_IDENTITY_DSN = json.dumps(
    {
        "project_id": "bank-warehouse",
        "location": "us-central1",
        "auth_method": "workload_identity",
    }
)


def test_registry_exposes_bigquery_connector() -> None:
    assert "bigquery" in connector_registry.supported_types
    definition = connector_registry.definition("bigquery")
    assert definition.implementation_status == "IMPLEMENTED"
    assert definition.dialect == "bigquery"
    assert definition.capabilities["constraints"] is True
    assert definition.capabilities["explain"] is True
    assert definition.capabilities["indexes"] is False
    assert definition.capabilities["partitions"] is False


def test_bigquery_connector_capabilities_are_honest() -> None:
    connector = BigQueryConnector(_VALID_SERVICE_ACCOUNT_DSN)
    capabilities = connector.capabilities
    assert capabilities.constraints is True
    assert capabilities.explain is True
    assert capabilities.approximate_statistics is True
    assert capabilities.delegated_identity is False


def test_quote_identifier_backtick_quotes() -> None:
    assert _quote_identifier("plain") == "`plain`"
    assert _quote_identifier("weird-name") == "`weird-name`"


def test_qualified_table_joins_project_dataset_table() -> None:
    assert _qualified_table("proj", "retail", "account") == "`proj`.`retail`.`account`"


def test_region_dataset_lowercases_and_prefixes() -> None:
    assert _region_dataset("US") == "region-us"
    assert _region_dataset(" us-central1 ") == "region-us-central1"


def test_parse_credential_payload_service_account() -> None:
    config = _parse_credential_payload(_VALID_SERVICE_ACCOUNT_DSN)
    assert config.project_id == "bank-warehouse"
    assert config.location == "US"
    assert config.auth_method == "service_account"
    assert config.service_account_info is not None
    assert config.service_account_info["client_email"] == _SERVICE_ACCOUNT_INFO["client_email"]


def test_parse_credential_payload_workload_identity() -> None:
    config = _parse_credential_payload(_VALID_WORKLOAD_IDENTITY_DSN)
    assert config.project_id == "bank-warehouse"
    assert config.location == "us-central1"
    assert config.auth_method == "workload_identity"
    assert config.service_account_info is None


@pytest.mark.parametrize(
    "payload",
    [
        "not-json-at-all",
        "[]",
        json.dumps({"location": "US", "auth_method": "workload_identity"}),
        json.dumps({"project_id": "bank-warehouse", "auth_method": "workload_identity"}),
        json.dumps({"project_id": "bank-warehouse", "location": "US"}),
        json.dumps({"project_id": "bank-warehouse", "location": "US", "auth_method": "password"}),
        json.dumps(
            {"project_id": "bank-warehouse", "location": "US", "auth_method": "service_account"}
        ),
        json.dumps(
            {
                "project_id": "bank-warehouse",
                "location": "US",
                "auth_method": "service_account",
                "service_account_info": {"type": "service_account"},
            }
        ),
        json.dumps(
            {
                "project_id": "bank-warehouse",
                "location": "US",
                "auth_method": "workload_identity",
                "service_account_info": _SERVICE_ACCOUNT_INFO,
            }
        ),
        json.dumps(
            {
                "project_id": "bank-warehouse",
                "location": "US",
                "auth_method": "service_account",
                "service_account_info": {**_SERVICE_ACCOUNT_INFO, "type": "user"},
            }
        ),
        json.dumps(
            {
                "project_id": "bank-warehouse",
                "location": "US",
                "auth_method": "workload_identity",
                "unexpected_field": "value",
            }
        ),
    ],
)
def test_parse_credential_payload_rejects_invalid_or_ambiguous_forms(payload: str) -> None:
    with pytest.raises(ValueError):
        _parse_credential_payload(payload)


def test_connector_construction_rejects_invalid_reference() -> None:
    with pytest.raises(ValueError):
        BigQueryConnector("not-json")


@pytest.mark.parametrize(
    ("project_id", "location"),
    [
        ("bank-warehouse`; DROP TABLE x; --", "US"),
        ("bank-warehouse", "US`; SELECT 1; --"),
        ("UPPERCASE-PROJECT", "US"),
    ],
)
def test_parse_credential_payload_rejects_unsafe_identifiers(
    project_id: str, location: str
) -> None:
    payload = json.dumps(
        {
            "project_id": project_id,
            "location": location,
            "auth_method": "workload_identity",
        }
    )
    with pytest.raises(ValueError):
        _parse_credential_payload(payload)


def test_assemble_catalog_maps_project_to_catalog_and_dataset_to_schema() -> None:
    column_rows = [
        {
            "table_schema": "retail",
            "table_name": "customer",
            "table_type": "BASE TABLE",
            "column_name": "customer_id",
            "ordinal_position": 1,
            "data_type": "INT64",
            "is_nullable": "NO",
            "column_default": None,
        },
        {
            "table_schema": "retail",
            "table_name": "customer",
            "table_type": "BASE TABLE",
            "column_name": "customer_name",
            "ordinal_position": 2,
            "data_type": "STRING",
            "is_nullable": "YES",
            "column_default": None,
        },
    ]
    key_rows = [
        {
            "table_schema": "retail",
            "table_name": "customer",
            "constraint_name": "customer_pk",
            "constraint_type": "PRIMARY KEY",
            "column_name": "customer_id",
            "ordinal_position": 1,
        }
    ]

    catalogs = _assemble_catalog("bank-warehouse", column_rows, key_rows)

    assert len(catalogs) == 1
    catalog = catalogs[0]
    assert catalog.name == "bank-warehouse"
    schema = catalog.schemas[0]
    assert schema.name == "retail"
    table = schema.tables[0]
    assert table.name == "customer"
    assert table.object_type == "BASE_TABLE"
    assert [column.name for column in table.columns] == ["customer_id", "customer_name"]
    assert table.columns[0].nullable is False
    assert table.columns[1].nullable is True
    assert len(table.constraints) == 1
    constraint = table.constraints[0]
    assert constraint.constraint_type == "PRIMARY_KEY"
    assert constraint.columns == ("customer_id",)


def test_assemble_catalog_omits_foreign_keys_honestly() -> None:
    column_rows = [
        {
            "table_schema": "retail",
            "table_name": "account",
            "table_type": "BASE TABLE",
            "column_name": "account_id",
            "ordinal_position": 1,
            "data_type": "INT64",
            "is_nullable": "NO",
            "column_default": None,
        },
    ]

    catalogs = _assemble_catalog("bank-warehouse", column_rows, [])

    table = catalogs[0].schemas[0].tables[0]
    assert table.constraints == ()


def test_profile_expressions_disable_all_aggregates_for_repeated_columns() -> None:
    expressions = _profile_expressions("`tags`", 0, "STRING", "REPEATED")
    assert all("CAST" in expression for expression in expressions)
    assert not any("SUM(CASE" in expression for expression in expressions)
    assert not any("APPROX_COUNT_DISTINCT" in expression for expression in expressions)


def test_profile_expressions_disable_distinct_and_length_for_complex_scalar_types() -> None:
    expressions = _profile_expressions("`payload`", 1, "JSON", "NULLABLE")
    assert any("SUM(CASE" in expression for expression in expressions)
    assert any("COUNT(`payload`)" in expression for expression in expressions)
    assert any("CAST(0 AS INT64) AS d_1" in expression for expression in expressions)
    assert any("CAST(NULL AS INT64) AS minl_1" in expression for expression in expressions)


def test_profile_expressions_use_approx_count_distinct_for_scalar_types() -> None:
    expressions = _profile_expressions("`customer_name`", 0, "STRING", "NULLABLE")
    assert any("APPROX_COUNT_DISTINCT(`customer_name`) AS d_0" in expr for expr in expressions)
    assert any("LENGTH(CAST(`customer_name` AS STRING))" in expr for expr in expressions)


def test_gate_query_estimate_allows_byte_estimate_within_budget() -> None:
    settings = Settings(max_bigquery_dry_run_bytes=10_000_000_000, _env_file=None)
    estimate = QueryEstimate(
        score=5_000_000_000.0, kind="BIGQUERY_DRY_RUN_BYTES", estimated_bytes=5_000_000_000
    )

    plan_cost, rejection_reason = gate_query_estimate(estimate, settings)

    assert plan_cost == 5_000_000_000.0
    assert rejection_reason is None


def test_gate_query_estimate_rejects_byte_estimate_over_budget() -> None:
    settings = Settings(max_bigquery_dry_run_bytes=10_000_000_000, _env_file=None)
    estimate = QueryEstimate(
        score=50_000_000_000.0, kind="BIGQUERY_DRY_RUN_BYTES", estimated_bytes=50_000_000_000
    )

    plan_cost, rejection_reason = gate_query_estimate(estimate, settings)

    assert plan_cost == 50_000_000_000.0
    assert rejection_reason is not None
    assert rejection_reason.startswith("QUERY_BYTES_EXCEED_POLICY")


def test_gate_query_estimate_uses_cost_budget_when_no_byte_estimate_present() -> None:
    """A cost-plan connector's estimate (e.g. SQL Server SHOWPLAN) is unaffected by
    the byte-budget branch: it is selected structurally via estimated_bytes, not by
    inspecting which connector produced the estimate."""
    settings = Settings(max_postgres_plan_cost=1_000.0, _env_file=None)
    over_budget = QueryEstimate(score=5_000.0, kind="SHOWPLAN_XML", estimated_rows=100.0)

    plan_cost, rejection_reason = gate_query_estimate(over_budget, settings)

    assert plan_cost == 5_000.0
    assert rejection_reason is not None
    assert rejection_reason.startswith("QUERY_COST_EXCEEDS_POLICY")


def test_gate_query_estimate_rejects_non_finite_score() -> None:
    settings = Settings(_env_file=None)
    estimate = QueryEstimate(score=float("nan"), kind="SHOWPLAN_XML")

    with pytest.raises(RuntimeError, match="invalid query estimate score"):
        gate_query_estimate(estimate, settings)
