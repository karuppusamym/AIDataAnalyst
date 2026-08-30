import json
import re
from typing import Any
from unittest.mock import patch

import pytest

from aida.config import Settings
from aida.connectors.base import QueryEstimate
from aida.connectors.bigquery import (
    BigQueryConnector,
    _assemble_catalog,
    _build_view_definition,
    _parse_credential_payload,
    _profile_expressions,
    _qualified_table,
    _quote_identifier,
    _region_dataset,
    _unquote_option_value,
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


# --- Envelope 1.1 (gap/02 N1) ------------------------------------------------
#
# BigQuery answers three axes from region-qualified INFORMATION_SCHEMA and cannot
# answer the fourth. Grants are not a gap here: BigQuery has no SQL GRANT, so
# `capabilities.grants` stays False and the catalog says why.

_INFORMATION_SCHEMA_VIEW = re.compile(r"INFORMATION_SCHEMA\.([A-Z_]+)")


class _FakeRow:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def items(self) -> Any:
        return self._data.items()


class _FakeJob:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def result(self, timeout: int | None = None) -> list[_FakeRow]:
        return [_FakeRow(row) for row in self._rows]


class _FakeBigQueryClient:
    """Answers each INFORMATION_SCHEMA view by name.

    Dispatching on the view name rather than on call order means a query added to
    `discover()` gets an empty result rather than silently consuming another view's
    rows.
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.queried_views: list[str] = []

    def query(self, sql: str, **kwargs: Any) -> _FakeJob:
        match = _INFORMATION_SCHEMA_VIEW.search(sql)
        view = match.group(1) if match else ""
        self.queried_views.append(view)
        response = self._responses.get(view, [])
        if isinstance(response, Exception):
            raise response
        return _FakeJob(response)


_BQ_COLUMNS = [
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
        "table_name": "active_customer",
        "table_type": "BASE TABLE",
        "column_name": "customer_id",
        "ordinal_position": 1,
        "data_type": "INT64",
        "is_nullable": "NO",
        "column_default": None,
    },
    {
        "table_schema": "retail",
        "table_name": "customer_rollup",
        "table_type": "BASE TABLE",
        "column_name": "customer_id",
        "ordinal_position": 1,
        "data_type": "INT64",
        "is_nullable": "NO",
        "column_default": None,
    },
]

_BQ_VIEW_SQL = "SELECT customer_id FROM `bank-warehouse.retail.customer` WHERE active"
_BQ_MVIEW_DDL = (
    "CREATE MATERIALIZED VIEW `bank-warehouse.retail.customer_rollup` AS "
    "SELECT customer_id FROM `bank-warehouse.retail.customer`"
)


def _bigquery_responses(**overrides: Any) -> dict[str, Any]:
    responses: dict[str, Any] = {
        "COLUMNS": list(_BQ_COLUMNS),
        "KEY_COLUMN_USAGE": [],
        "TABLES": [
            {
                "table_schema": "retail",
                "table_name": "customer",
                "table_type": "BASE TABLE",
                "ddl": "CREATE TABLE ...",
            },
            {
                "table_schema": "retail",
                "table_name": "active_customer",
                "table_type": "VIEW",
                "ddl": "CREATE VIEW ...",
            },
            {
                "table_schema": "retail",
                "table_name": "customer_rollup",
                "table_type": "MATERIALIZED VIEW",
                "ddl": _BQ_MVIEW_DDL,
            },
        ],
        "VIEWS": [
            {
                "table_schema": "retail",
                "table_name": "active_customer",
                "view_definition": _BQ_VIEW_SQL,
                "check_option": None,
            }
        ],
        "ROUTINES": [
            {
                "routine_schema": "retail",
                "routine_name": "risk_score",
                "routine_type": "SCALAR_FUNCTION",
                "data_type": "FLOAT64",
                "routine_body": "SQL",
                "routine_definition": "SELECT balance * 0.1",
                "external_language": None,
                "is_deterministic": None,
                "security_type": None,
            },
            {
                "routine_schema": "retail",
                "routine_name": "enrich_remote",
                "routine_type": "SCALAR_FUNCTION",
                "data_type": "STRING",
                "routine_body": "EXTERNAL",
                "routine_definition": None,
                "external_language": None,
                "is_deterministic": None,
                "security_type": None,
            },
        ],
        "PARAMETERS": [
            {
                "specific_schema": "retail",
                "specific_name": "risk_score",
                "ordinal_position": 0,
                "parameter_mode": None,
                "is_result": "YES",
                "parameter_name": None,
                "data_type": "FLOAT64",
                "parameter_default": None,
            },
            {
                "specific_schema": "retail",
                "specific_name": "risk_score",
                "ordinal_position": 1,
                "parameter_mode": "IN",
                "is_result": "NO",
                "parameter_name": "balance",
                "data_type": "NUMERIC",
                "parameter_default": None,
            },
        ],
        "ROUTINE_OPTIONS": [
            {
                "routine_schema": "retail",
                "routine_name": "risk_score",
                "option_name": "description",
                "option_value": '"Scores a balance"',
            }
        ],
        "TABLE_OPTIONS": [
            {
                "table_schema": "retail",
                "table_name": "customer",
                "option_name": "description",
                "option_value": '"Customer master"',
            }
        ],
        "SCHEMATA_OPTIONS": [
            {
                "schema_name": "retail",
                "option_name": "description",
                "option_value": '"Retail domain"',
            }
        ],
        "COLUMN_FIELD_PATHS": [
            {
                "table_schema": "retail",
                "table_name": "customer",
                "column_name": "customer_id",
                "field_path": "customer_id",
                "description": "Surrogate key",
            }
        ],
    }
    responses.update(overrides)
    return responses


async def _bigquery_discover(**overrides: Any) -> tuple[Any, ...]:
    connector = BigQueryConnector(_VALID_SERVICE_ACCOUNT_DSN)
    client = _FakeBigQueryClient(_bigquery_responses(**overrides))
    with patch.object(connector, "_get_client", return_value=client):
        return await connector.discover()


def test_bigquery_capabilities_declare_only_the_axes_it_implements() -> None:
    capabilities = BigQueryConnector(_VALID_SERVICE_ACCOUNT_DSN).capabilities
    assert capabilities.views is True
    assert capabilities.routines is True
    assert capabilities.object_comments is True
    # Not a gap: BigQuery has no SQL grant surface at all.
    assert capabilities.grants is False
    advertised = connector_registry.definition("bigquery").capabilities
    assert advertised["views"] is True
    assert advertised["grants"] is False


async def test_bigquery_discover_round_trips_a_view_definition() -> None:
    catalogs = await _bigquery_discover()

    schema = catalogs[0].schemas[0]
    view = next(t for t in schema.tables if t.name == "active_customer")
    assert view.object_type == "VIEW"
    assert view.view_definition is not None
    assert view.view_definition.definition_sql == _BQ_VIEW_SQL
    assert view.view_definition.is_materialized is False
    assert view.view_definition.truncated is False
    assert view.view_definition.unavailable_reason is None
    # BigQuery exposes no updatability column and documents CHECK_OPTION as always
    # NULL, so neither is asserted from nothing.
    assert view.view_definition.is_updatable is None
    assert view.view_definition.check_option is None


async def test_a_materialized_view_definition_comes_from_tables_ddl() -> None:
    """`INFORMATION_SCHEMA.VIEWS` has no row for a materialized view.

    `TABLES.DDL` is the only place its statement appears, and it is the whole CREATE
    statement rather than the bare query -- worth knowing downstream, and worth
    pinning here.
    """
    catalogs = await _bigquery_discover()

    schema = catalogs[0].schemas[0]
    rollup = next(t for t in schema.tables if t.name == "customer_rollup")
    assert rollup.object_type == "MATERIALIZED_VIEW"
    assert rollup.view_definition is not None
    assert rollup.view_definition.is_materialized is True
    assert rollup.view_definition.definition_sql == _BQ_MVIEW_DDL


async def test_a_base_table_carries_no_view_definition() -> None:
    catalogs = await _bigquery_discover()
    customer = next(t for t in catalogs[0].schemas[0].tables if t.name == "customer")
    assert customer.object_type == "BASE_TABLE"
    assert customer.view_definition is None


async def test_a_refused_views_query_leaves_a_reason_on_the_view() -> None:
    catalogs = await _bigquery_discover(VIEWS=RuntimeError("403 Access Denied: table VIEWS"))

    schema = catalogs[0].schemas[0]
    view = next(t for t in schema.tables if t.name == "active_customer")
    assert view.view_definition is not None
    assert view.view_definition.definition_sql is None
    assert view.view_definition.unavailable_reason is not None
    assert "403 Access Denied" in view.view_definition.unavailable_reason
    assert "views" in catalogs[0].attributes["envelope_v11_unavailable"]


async def test_a_refused_tables_query_leaves_todays_object_type_default() -> None:
    """`INFORMATION_SCHEMA.COLUMNS` carries no table type.

    If TABLES is refused every object keeps the pre-envelope `BASE TABLE` default
    rather than discovery failing, and the refusal is recorded on the catalog.
    """
    catalogs = await _bigquery_discover(TABLES=RuntimeError("403 Access Denied: table TABLES"))

    schema = catalogs[0].schemas[0]
    assert {table.object_type for table in schema.tables} == {"BASE_TABLE"}
    assert "tables" in catalogs[0].attributes["envelope_v11_unavailable"]


def test_a_view_definition_over_the_cap_is_a_flagged_prefix() -> None:
    definition = _build_view_definition(
        "SELECT " + "x" * 40, object_label="retail.v", max_characters=10
    )
    assert definition.definition_sql == "SELECT xxx"
    assert definition.truncated is True
    assert definition.unavailable_reason is None


async def test_a_routine_round_trips_with_its_parameters_and_description() -> None:
    catalogs = await _bigquery_discover()

    schema = catalogs[0].schemas[0]
    routine = next(r for r in schema.routines if r.name == "risk_score")
    assert routine.routine_type == "SCALAR_FUNCTION"
    assert routine.language == "SQL"
    assert routine.body_sql == "SELECT balance * 0.1"
    assert routine.return_type == "FLOAT64"
    assert routine.source_description == "Scores a balance"
    assert routine.unavailable_reason is None
    assert [(p.name, p.physical_type, p.mode) for p in routine.parameters] == [
        ("balance", "NUMERIC", "IN")
    ]
    # BigQuery documents both columns as always NULL; nothing is invented for them.
    assert routine.is_deterministic is None
    assert routine.security_mode is None


async def test_a_remote_functions_body_is_unavailable_rather_than_empty() -> None:
    catalogs = await _bigquery_discover()

    routine = next(
        r for r in catalogs[0].schemas[0].routines if r.name == "enrich_remote"
    )
    assert routine.body_sql is None
    assert routine.unavailable_reason is not None
    assert "ROUTINE_DEFINITION" in routine.unavailable_reason
    assert routine.attributes["routine_body"] == "EXTERNAL"


async def test_a_refused_routines_query_is_recorded_rather_than_read_as_no_routines() -> None:
    catalogs = await _bigquery_discover(
        ROUTINES=RuntimeError("403 Access Denied: bigquery.routines.list")
    )

    assert catalogs[0].schemas[0].routines == ()
    recorded = catalogs[0].attributes["envelope_v11_unavailable"]
    assert "bigquery.routines.list" in recorded["routines"]


async def test_descriptions_land_at_every_level_bigquery_exposes() -> None:
    catalogs = await _bigquery_discover()

    catalog = catalogs[0]
    # A GCP project has no description in INFORMATION_SCHEMA.
    assert catalog.source_description is None
    schema = catalog.schemas[0]
    assert schema.source_description == "Retail domain"
    customer = next(t for t in schema.tables if t.name == "customer")
    assert customer.source_description == "Customer master"
    assert customer.columns[0].source_description == "Surrogate key"


@pytest.mark.parametrize(
    ("option_value", "expected"),
    [
        ('"Customer master"', "Customer master"),
        ("'Customer master'", "Customer master"),
        (r'"Says \"hello\""', 'Says "hello"'),
        (r'"line one\nline two"', "line one\nline two"),
        ('""', None),
        (None, None),
    ],
)
def test_option_values_are_unwrapped_from_googlesql_source_text(
    option_value: str | None, expected: str | None
) -> None:
    """`option_value` is GoogleSQL source, not the description.

    Storing it verbatim would put a stray pair of quotes in front of every asset
    description in the catalogue.
    """
    assert _unquote_option_value(option_value) == expected


async def test_bigquery_declines_grants_and_records_why() -> None:
    """The grants axis is answered, not skipped.

    BigQuery access is Cloud IAM policy -- inherited down the resource hierarchy,
    optionally conditional, and expressed as role bundles rather than SQL privileges.
    `DiscoveredGrant` models one SQL privilege on one object, so writing IAM bindings
    into it would make "who can already see this" mean something different here than
    on Oracle or Snowflake while looking identical.
    """
    catalogs = await _bigquery_discover()

    assert catalogs[0].schemas[0].grants == ()
    assert "IAM" in catalogs[0].attributes["grants"]
    assert BigQueryConnector(_VALID_SERVICE_ACCOUNT_DSN).capabilities.grants is False
