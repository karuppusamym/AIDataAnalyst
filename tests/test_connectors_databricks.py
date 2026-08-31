import json
from unittest.mock import MagicMock, patch

import pytest

from aida.connectors.databricks import (
    DatabricksConnector,
    _extract_databricks_explain_cost,
    _parse_dsn,
    _qualified_table,
    _quote_identifier,
)
from aida.connectors.registry import connector_registry

_JSON_DSN = json.dumps(
    {
        "server_hostname": "dbc-a1b2c3d4-e5f6.cloud.databricks.com",
        "http_path": "/sql/1.0/warehouses/abc123def456",
        "access_token": "dapi0123456789abcdef",
        "catalog": "main",
        "schema": "analytics",
    }
)

_URI_DSN = (
    "databricks://token:dapi0123456789abcdef@dbc-a1b2c3d4-e5f6.cloud.databricks.com"
    "/main/analytics?http_path=%2Fsql%2F1.0%2Fwarehouses%2Fabc123def456"
)


def test_quote_identifier_backtick_quotes_and_escapes() -> None:
    assert _quote_identifier("plain") == "`plain`"
    assert _quote_identifier("weird-name") == "`weird-name`"
    assert _quote_identifier("a`b") == "`a``b`"


def test_qualified_table_joins_catalog_schema_table() -> None:
    assert _qualified_table("main", "analytics", "customers") == "`main`.`analytics`.`customers`"


def test_parse_dsn_json() -> None:
    params = _parse_dsn(_JSON_DSN)
    assert params.server_hostname == "dbc-a1b2c3d4-e5f6.cloud.databricks.com"
    assert params.http_path == "/sql/1.0/warehouses/abc123def456"
    assert params.access_token == "dapi0123456789abcdef"  # noqa: S105
    assert params.catalog == "main"
    assert params.schema == "analytics"


def test_parse_dsn_json_requires_core_fields() -> None:
    with pytest.raises(ValueError, match="server_hostname"):
        _parse_dsn(json.dumps({"http_path": "/sql/1.0/warehouses/x", "access_token": "t"}))


def test_parse_dsn_json_rejects_malformed_payload() -> None:
    with pytest.raises(ValueError, match="JSON"):
        _parse_dsn("{not valid json}")


def test_parse_dsn_uri() -> None:
    params = _parse_dsn(_URI_DSN)
    assert params.server_hostname == "dbc-a1b2c3d4-e5f6.cloud.databricks.com"
    assert params.http_path == "/sql/1.0/warehouses/abc123def456"
    assert params.access_token == "dapi0123456789abcdef"  # noqa: S105
    assert params.catalog == "main"
    assert params.schema == "analytics"


def test_parse_dsn_uri_requires_http_path_query_param() -> None:
    with pytest.raises(ValueError, match="http_path"):
        _parse_dsn("databricks://token:secret@host.cloud.databricks.com/main/analytics")


def test_parse_dsn_uri_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="invalid Databricks connection reference"):
        _parse_dsn("postgres://user:pass@host/db")


def test_extract_databricks_explain_cost_takes_largest_statistics_fragment() -> None:
    plan_text = (
        "== Optimized Logical Plan ==\n"
        "Filter (id > 10), Statistics(sizeInBytes=512.0 KiB, rowCount=4000)\n"
        "  Relation main.analytics.customers, "
        "Statistics(sizeInBytes=10.0 MiB, rowCount=50000)\n"
    )
    estimate = _extract_databricks_explain_cost(plan_text)

    assert estimate.kind == "DATABRICKS_EXPLAIN_COST"
    assert estimate.estimated_bytes == 10 * 1024 * 1024
    assert estimate.estimated_rows == 50000.0
    assert estimate.evidence["statistics_fragments_found"] == 2
    assert estimate.score > 0


def test_extract_databricks_explain_cost_falls_back_with_no_statistics() -> None:
    estimate = _extract_databricks_explain_cost("== Physical Plan ==\nScan main.t\n")
    assert estimate.kind == "DATABRICKS_EXPLAIN_FALLBACK"
    assert estimate.score == 1.0
    assert estimate.estimated_bytes is None
    assert estimate.estimated_rows is None


@pytest.mark.parametrize(
    ("unit", "value", "expected_bytes"),
    [
        ("B", "10.0", 10),
        ("KiB", "1.0", 1024),
        ("MiB", "2.0", 2 * 1024 * 1024),
        ("GiB", "1.5", int(1.5 * 1024**3)),
    ],
)
def test_extract_databricks_explain_cost_unit_conversion(
    unit: str, value: str, expected_bytes: int
) -> None:
    plan_text = f"Statistics(sizeInBytes={value} {unit}, rowCount=1)"
    estimate = _extract_databricks_explain_cost(plan_text)
    assert estimate.estimated_bytes == expected_bytes


def test_databricks_registry_definition() -> None:
    defn = connector_registry.definition("databricks")
    assert defn.display_name == "Databricks SQL"
    assert defn.dialect == "databricks"
    assert defn.implementation_status == "IMPLEMENTED"
    assert defn.maturity == "BETA"
    assert defn.capabilities["catalogs"] is True
    assert defn.capabilities["schemas"] is True
    assert defn.capabilities["constraints"] is True
    assert defn.capabilities["explain"] is True
    assert defn.capabilities["object_comments"] is True
    # Honest gaps: not claimed until a certified, live-verified adapter closes them.
    assert defn.capabilities["views"] is False
    assert defn.capabilities["routines"] is False
    assert defn.capabilities["grants"] is False
    assert defn.capabilities["delegated_identity"] is False


def test_databricks_connector_capabilities_match_registry() -> None:
    caps = DatabricksConnector(_JSON_DSN).capabilities
    advertised = connector_registry.definition("databricks").capabilities
    assert caps.catalogs == advertised["catalogs"]
    assert caps.explain == advertised["explain"]
    assert caps.object_comments == advertised["object_comments"]


def test_databricks_is_registered_as_a_supported_pull_connector_type() -> None:
    assert "databricks" in connector_registry.supported_types


@pytest.mark.asyncio
async def test_databricks_discover_assembles_catalog_with_constraints() -> None:
    connector = DatabricksConnector(_JSON_DSN)

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    mock_cursor.fetchall.side_effect = [
        [
            {
                "table_schema": "analytics",
                "table_name": "customers",
                "table_type": "MANAGED",
                "column_name": "id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "is_nullable": "NO",
                "column_default": None,
                "table_comment": "Customer master",
                "column_comment": "Surrogate key",
            },
            {
                "table_schema": "analytics",
                "table_name": "customers",
                "table_type": "MANAGED",
                "column_name": "name",
                "ordinal_position": 2,
                "data_type": "string",
                "is_nullable": "YES",
                "column_default": None,
                "table_comment": "Customer master",
                "column_comment": None,
            },
            {
                "table_schema": "analytics",
                "table_name": "orders",
                "table_type": "MANAGED",
                "column_name": "order_id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "is_nullable": "NO",
                "column_default": None,
                "table_comment": None,
                "column_comment": None,
            },
            {
                "table_schema": "analytics",
                "table_name": "orders",
                "table_type": "MANAGED",
                "column_name": "customer_id",
                "ordinal_position": 2,
                "data_type": "bigint",
                "is_nullable": "NO",
                "column_default": None,
                "table_comment": None,
                "column_comment": None,
            },
        ],
        [
            {
                "table_schema": "analytics",
                "table_name": "customers",
                "constraint_name": "pk_customers",
                "constraint_type": "PRIMARY KEY",
                "column_name": "id",
                "ordinal_position": 1,
            },
            {
                "table_schema": "analytics",
                "table_name": "orders",
                "constraint_name": "pk_orders",
                "constraint_type": "PRIMARY KEY",
                "column_name": "order_id",
                "ordinal_position": 1,
            },
        ],
        [
            {
                "table_schema": "analytics",
                "table_name": "orders",
                "constraint_name": "fk_orders_customer",
                "column_name": "customer_id",
                "referenced_schema": "analytics",
                "referenced_table": "customers",
                "referenced_column": "id",
                "ordinal_position": 1,
            },
        ],
        [{"schema_name": "analytics", "comment": "Analytics schema"}],
        [{"catalog_name": "main", "comment": "Primary catalog"}],
    ]

    with patch.object(connector, "_get_connection", return_value=mock_conn):
        catalogs = await connector.discover()

    assert len(catalogs) == 1
    catalog = catalogs[0]
    assert catalog.name == "main"
    assert catalog.source_description == "Primary catalog"
    assert len(catalog.schemas) == 1
    schema = catalog.schemas[0]
    assert schema.name == "analytics"
    assert schema.source_description == "Analytics schema"
    assert len(schema.tables) == 2

    customers = next(t for t in schema.tables if t.name == "customers")
    assert customers.source_description == "Customer master"
    assert len(customers.columns) == 2
    assert customers.columns[0].source_description == "Surrogate key"
    assert customers.columns[1].source_description is None
    assert len(customers.constraints) == 1
    assert customers.constraints[0].constraint_type == "PRIMARY_KEY"

    orders = next(t for t in schema.tables if t.name == "orders")
    assert len(orders.constraints) == 2
    fk = next(c for c in orders.constraints if c.constraint_type == "FOREIGN_KEY")
    assert fk.referenced_table == "customers"
    assert fk.referenced_columns == ("id",)


@pytest.mark.asyncio
async def test_databricks_discover_degrades_gracefully_when_optional_queries_fail() -> None:
    """FK discovery and comment queries are best-effort (older metastore, no grant).

    A refusal on any of them must shrink the envelope rather than fail discovery
    outright -- the same fail-open contract the BigQuery adapter's key-query fetch
    uses for its own optional constraint query.
    """
    connector = DatabricksConnector(_JSON_DSN)

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    column_rows = [
        {
            "table_schema": "analytics",
            "table_name": "customers",
            "table_type": "MANAGED",
            "column_name": "id",
            "ordinal_position": 1,
            "data_type": "bigint",
            "is_nullable": "NO",
            "column_default": None,
            "table_comment": None,
            "column_comment": None,
        }
    ]

    call_count = {"n": 0}

    def fetchall() -> list[dict[str, object]]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return column_rows
        if call_count["n"] == 2:
            return []
        raise RuntimeError("REFERENTIAL_CONSTRAINTS not available on this metastore")

    mock_cursor.fetchall.side_effect = fetchall

    with patch.object(connector, "_get_connection", return_value=mock_conn):
        catalogs = await connector.discover()

    assert len(catalogs) == 1
    schema = catalogs[0].schemas[0]
    table = schema.tables[0]
    assert len(table.constraints) == 0
    assert catalogs[0].source_description is None
    assert schema.source_description is None


@pytest.mark.asyncio
async def test_databricks_execute_read_query_captures_query_id() -> None:
    connector = DatabricksConnector(_JSON_DSN)

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.description = [("customer_id",), ("balance",)]
    mock_cursor.fetchmany.return_value = [(101, 4500.50), (102, 920.00)]
    mock_cursor.query_id = "01ee-test-query-id"
    mock_conn.cursor.return_value = mock_cursor

    with patch.object(connector, "_get_connection", return_value=mock_conn):
        result = await connector.execute_read_query("SELECT customer_id, balance FROM customers")

    assert len(result.rows) == 2
    assert result.rows[0]["customer_id"] == 101
    assert result.rows[0]["balance"] == 4500.50
    assert result.warehouse_query_id == "01ee-test-query-id"


@pytest.mark.asyncio
async def test_databricks_estimate_read_query_uses_explain_cost() -> None:
    connector = DatabricksConnector(_JSON_DSN)

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [
        ("Relation main.analytics.customers, Statistics(sizeInBytes=1.0 MiB, rowCount=1000)",)
    ]
    mock_conn.cursor.return_value = mock_cursor

    with patch.object(connector, "_get_connection", return_value=mock_conn):
        estimate = await connector.estimate_read_query("SELECT * FROM customers")

    executed_sql = mock_cursor.execute.call_args[0][0]
    assert executed_sql.startswith("EXPLAIN COST ")
    assert estimate.kind == "DATABRICKS_EXPLAIN_COST"
    assert estimate.estimated_rows == 1000.0
    assert estimate.estimated_bytes == 1024 * 1024


@pytest.mark.asyncio
async def test_databricks_profile_table_computes_bounded_stats() -> None:
    connector = DatabricksConnector(_JSON_DSN)

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.description = [
        ("sampled_row_count",),
        ("n_0",),
        ("nn_0",),
        ("d_0",),
        ("minl_0",),
        ("maxl_0",),
    ]
    mock_cursor.fetchone.return_value = (500,)
    mock_cursor.fetchall.return_value = [(500, 5, 495, 480, 2, 40)]
    mock_conn.cursor.return_value = mock_cursor

    with patch.object(connector, "_get_connection", return_value=mock_conn):
        profile = await connector.profile_table("analytics", "customers", ("name",))

    assert profile.row_count_estimate == 500
    assert profile.sampled_row_count == 500
    assert len(profile.columns) == 1
    column = profile.columns[0]
    assert column.name == "name"
    assert column.null_count == 5
    assert column.non_null_count == 495
    assert column.approximate_distinct_count == 480
    assert column.min_length == 2
    assert column.max_length == 40


@pytest.mark.asyncio
async def test_databricks_profile_table_rejects_non_positive_limits() -> None:
    connector = DatabricksConnector(_JSON_DSN)
    with pytest.raises(ValueError, match="positive"):
        await connector.profile_table("analytics", "customers", ("id",), sample_rows=0)


@pytest.mark.asyncio
async def test_databricks_test_connection_runs_a_lightweight_probe() -> None:
    connector = DatabricksConnector(_JSON_DSN)

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    with patch.object(connector, "_get_connection", return_value=mock_conn):
        await connector.test_connection()

    executed_sql = mock_cursor.execute.call_args[0][0]
    assert "current_catalog" in executed_sql.lower()
    mock_conn.close.assert_called_once()
