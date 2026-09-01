from uuid import uuid4

import pytest

from aida.dbt_api import _matched_table_id
from aida.dbt_artifacts import (
    DbtArtifactError,
    ParsedDbtResource,
    parse_dbt_catalog,
    parse_dbt_manifest,
    parse_dbt_run_results,
)
from aida.main import app
from aida.schemas import DbtArtifactImportRead


def manifest_fixture() -> dict[str, object]:
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.10.0",
            "generated_at": "2026-08-27T12:00:00Z",
            "invocation_id": "fixture-invocation",
        },
        "nodes": {
            "model.bank.customer_summary": {
                "resource_type": "model",
                "package_name": "bank",
                "name": "customer_summary",
                "alias": "customer_summary",
                "database": "bank",
                "schema": "analytics",
                "relation_name": '"bank"."analytics"."customer_summary"',
                "original_file_path": "models/customer_summary.sql",
                "description": "Curated customer summary",
                "config": {"materialized": "table"},
                "compiled_code": (
                    "select customer_id from bank.raw.customer "
                    "where status = 'PRIVATE_STATUS' and risk_score > 42"
                ),
                "columns": {
                    "customer_id": {
                        "name": "customer_id",
                        "description": "Surrogate primary key for customer",
                        "data_type": "integer",
                    }
                },
                "tags": ["customer", "certified"],
                "depends_on": {"nodes": ["source.bank.customer"]},
            },
            "test.bank.customer_summary_not_null": {
                "resource_type": "test",
                "package_name": "bank",
                "name": "customer_summary_not_null",
                "depends_on": {"nodes": ["model.bank.customer_summary"]},
            },
        },
        "sources": {
            "source.bank.customer": {
                "resource_type": "source",
                "package_name": "bank",
                "name": "customer",
                "identifier": "customer",
                "database": "bank",
                "schema": "raw",
                "columns": {
                    "customer_id": {
                        "name": "customer_id",
                        "description": "Raw source customer identifier",
                    }
                },
                "depends_on": {"nodes": []},
            }
        },
    }


def test_manifest_parser_builds_lineage_and_redacts_sql_literals() -> None:
    parsed = parse_dbt_manifest(manifest_fixture(), "postgres")

    assert len(parsed.resources) == 3
    assert parsed.edges == [
        ("source.bank.customer", "model.bank.customer_summary"),
        ("model.bank.customer_summary", "test.bank.customer_summary_not_null"),
    ]
    model = next(item for item in parsed.resources if item.resource_type == "MODEL")
    assert model.compiled_sql_hash is not None
    assert model.sql_parse_status == "PARSED"
    assert model.compiled_sql_redacted is not None
    assert "PRIVATE_STATUS" not in model.compiled_sql_redacted
    assert "42" not in model.compiled_sql_redacted
    assert "%s" in model.compiled_sql_redacted
    assert model.column_descriptions == {"customer_id": "Surrogate primary key for customer"}
    assert model.column_types == {"customer_id": "integer"}


def test_manifest_preserves_source_column_descriptions() -> None:
    parsed = parse_dbt_manifest(manifest_fixture(), "postgres")
    source = next(item for item in parsed.resources if item.resource_type == "SOURCE")
    assert source.column_descriptions == {"customer_id": "Raw source customer identifier"}


def test_catalog_parser_extracts_column_types() -> None:
    catalog_payload = {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/catalog/v1.json"},
        "nodes": {
            "model.bank.customer_summary": {
                "columns": {
                    "customer_id": {"type": "INT8", "index": 1, "name": "customer_id"},
                    "account_balance": {
                        "type": "NUMERIC(18,2)",
                        "index": 2,
                        "name": "account_balance",
                    },
                }
            }
        },
        "sources": {
            "source.bank.customer": {
                "columns": {
                    "customer_id": {"type": "VARCHAR(64)", "index": 1, "name": "customer_id"}
                }
            }
        },
    }

    parsed_catalog = parse_dbt_catalog(catalog_payload)
    assert parsed_catalog["model.bank.customer_summary"] == {
        "customer_id": "INT8",
        "account_balance": "NUMERIC(18,2)",
    }
    assert parsed_catalog["source.bank.customer"] == {
        "customer_id": "VARCHAR(64)",
    }


def test_catalog_parser_rejects_invalid_root() -> None:
    with pytest.raises(DbtArtifactError, match="valid JSON object"):
        parse_dbt_catalog("not-a-dict")  # type: ignore[arg-type]


def test_manifest_fingerprint_is_deterministic_and_raw_manifest_is_not_a_read_contract() -> None:
    first = parse_dbt_manifest(manifest_fixture(), "postgres")
    second = parse_dbt_manifest(manifest_fixture(), "postgres")

    assert first.fingerprint == second.fingerprint
    assert "manifest" not in DbtArtifactImportRead.model_json_schema()["properties"]


def test_manifest_requires_dbt_metadata() -> None:
    with pytest.raises(DbtArtifactError, match="metadata"):
        parse_dbt_manifest({"nodes": {}}, "postgres")


def test_catalog_matching_prefers_exact_database_schema_relation() -> None:
    exact_table_id = uuid4()
    fallback_table_id = uuid4()
    resource = ParsedDbtResource(
        unique_id="model.bank.customer",
        resource_type="MODEL",
        package_name="bank",
        name="customer",
        database_name="BANK",
        schema_name="RAW",
        relation_name="bank.raw.customer",
        materialization="view",
        original_file_path="models/customer.sql",
        description=None,
        compiled_sql_hash=None,
        compiled_sql_redacted=None,
        sql_parse_status="NOT_PRESENT",
        column_names=[],
        tags=[],
        depends_on_unique_ids=[],
    )

    assert (
        _matched_table_id(
            resource,
            {("bank", "raw", "customer"): exact_table_id},
            {("raw", "customer"): fallback_table_id},
        )
        == exact_table_id
    )


def test_manifest_extracts_exposure_extra_metadata() -> None:
    manifest = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.10.0",
        },
        "exposures": {
            "exposure.bank.executive_dashboard": {
                "resource_type": "exposure",
                "package_name": "bank",
                "name": "executive_dashboard",
                "type": "dashboard",
                "maturity": "high",
                "url": "https://bi.bank.internal/dashboards/exec",
                "owner": {"name": "Risk & Finance Team", "email": "risk-finance@bank.com"},
                "depends_on": {"nodes": ["model.bank.customer_summary"]},
            }
        },
    }
    parsed = parse_dbt_manifest(manifest, "postgres")
    assert len(parsed.resources) == 1
    exposure = parsed.resources[0]
    assert exposure.resource_type == "EXPOSURE"
    assert exposure.extra_metadata["exposure_type"] == "dashboard"
    assert exposure.extra_metadata["maturity"] == "high"
    assert exposure.extra_metadata["url"] == "https://bi.bank.internal/dashboards/exec"
    assert exposure.extra_metadata["owner_name"] == "Risk & Finance Team"
    assert exposure.extra_metadata["owner_email"] == "risk-finance@bank.com"


def test_run_results_parser_extracts_test_outcomes() -> None:
    payload = {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/run_results/v4.json"},
        "results": [
            {
                "unique_id": "test.bank.customer_summary_not_null",
                "status": "pass",
                "failures": 0,
                "execution_time": 0.35,
                "message": None,
            },
            {
                "unique_id": "test.bank.customer_balance_positive",
                "status": "fail",
                "failures": 14,
                "execution_time": 0.82,
                "message": "Got 14 results, configured to fail if != 0",
            },
        ],
    }

    results = parse_dbt_run_results(payload)
    assert len(results) == 2
    pass_test = results["test.bank.customer_summary_not_null"]
    assert pass_test.status == "PASS"
    assert pass_test.failures == 0
    assert pass_test.execution_time == 0.35

    fail_test = results["test.bank.customer_balance_positive"]
    assert fail_test.status == "FAIL"
    assert fail_test.failures == 14
    assert fail_test.message == "Got 14 results, configured to fail if != 0"


def test_run_results_parser_validates_input() -> None:
    with pytest.raises(DbtArtifactError, match="valid JSON object"):
        parse_dbt_run_results(["not-a-dict"])  # type: ignore[arg-type]

    with pytest.raises(DbtArtifactError, match="'results' list"):
        parse_dbt_run_results({"metadata": {}})


def test_dbt_paths_are_published() -> None:
    paths = app.openapi()["paths"]

    assert "/v1/projects/{project_id}/dbt-projects" in paths
    assert "/v1/dbt-projects/{dbt_project_id}/artifact-imports" in paths
    assert "/v1/dbt-artifact-imports/{artifact_id}/lineage" in paths
