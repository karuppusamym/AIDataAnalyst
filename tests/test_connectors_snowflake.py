import json
from unittest.mock import MagicMock, patch

import pytest

from aida.connectors.registry import connector_registry
from aida.connectors.snowflake import (
    SnowflakeConnector,
    _extract_snowflake_explain_estimate,
    _parse_dsn,
    _quote_identifier,
)


def test_quote_identifier() -> None:
    assert _quote_identifier("CUSTOMERS") == '"CUSTOMERS"'
    assert _quote_identifier('CUST"OMER') == '"CUST""OMER"'


def test_parse_dsn_uri() -> None:
    uri = (
        "snowflake://analyst:secret_pass@xy12345.us-east-1/FINANCE_DB/ANALYTICS"
        "?warehouse=COMPUTE_WH&role=ANALYST_ROLE"
    )
    params = _parse_dsn(uri)

    assert params.account == "xy12345.us-east-1"
    assert params.user == "analyst"
    assert params.password == "secret_pass"  # noqa: S105
    assert params.database == "FINANCE_DB"
    assert params.schema == "ANALYTICS"
    assert params.warehouse == "COMPUTE_WH"
    assert params.role == "ANALYST_ROLE"
    assert params.port == 443


def test_parse_dsn_json() -> None:
    payload = json.dumps(
        {
            "account": "org-account",
            "user": "service_user",
            "password": "strong_password_123",
            "database": "DATA_LAKE",
            "schema": "MARTS",
            "warehouse": "QUERY_WH",
            "role": "TRANSFORMER",
        }
    )
    params = _parse_dsn(payload)

    assert params.account == "org-account"
    assert params.user == "service_user"
    assert params.password == "strong_password_123"  # noqa: S105
    assert params.database == "DATA_LAKE"
    assert params.schema == "MARTS"
    assert params.warehouse == "QUERY_WH"
    assert params.role == "TRANSFORMER"


def test_extract_snowflake_explain_estimate() -> None:
    plan_json = {
        "GlobalStats": {
            "bytesAssigned": 10485760,
            "rowsTotal": 50000,
            "partitionsTotal": 100,
            "partitionsAssigned": 15,
        }
    }
    estimate = _extract_snowflake_explain_estimate(plan_json)

    assert estimate.kind == "SNOWFLAKE_EXPLAIN_PLAN"
    assert estimate.estimated_rows == 50000.0
    assert estimate.estimated_bytes == 10485760
    assert estimate.evidence["partitions_total"] == 100
    assert estimate.evidence["partitions_assigned"] == 15
    assert estimate.evidence["pruning_ratio"] == 0.85
    assert estimate.score > 0


def test_snowflake_registry_definition() -> None:
    defn = connector_registry.definition("snowflake")
    assert defn.display_name == "Snowflake"
    assert defn.dialect == "snowflake"
    assert defn.implementation_status == "IMPLEMENTED"
    assert defn.maturity == "BETA"
    assert defn.capabilities["catalogs"] is True
    assert defn.capabilities["schemas"] is True
    assert defn.capabilities["constraints"] is True
    assert defn.capabilities["explain"] is True
    assert defn.capabilities["partitions"] is True


@pytest.mark.asyncio
async def test_snowflake_discover_assembly() -> None:
    connector = SnowflakeConnector(
        "snowflake://user:pass@acc/TEST_DB/PUBLIC?warehouse=WH&role=ROLE"
    )

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor

    mock_cursor.fetchall.side_effect = [
        [
            {
                "table_schema": "PUBLIC",
                "table_name": "CUSTOMERS",
                "table_type": "BASE TABLE",
                "column_name": "ID",
                "ordinal_position": 1,
                "data_type": "NUMBER(38,0)",
                "is_nullable": "NO",
                "column_default": None,
            },
            {
                "table_schema": "PUBLIC",
                "table_name": "CUSTOMERS",
                "table_type": "BASE TABLE",
                "column_name": "NAME",
                "ordinal_position": 2,
                "data_type": "VARCHAR(16777216)",
                "is_nullable": "YES",
                "column_default": None,
            },
            {
                "table_schema": "PUBLIC",
                "table_name": "ORDERS",
                "table_type": "BASE TABLE",
                "column_name": "ORDER_ID",
                "ordinal_position": 1,
                "data_type": "NUMBER(38,0)",
                "is_nullable": "NO",
                "column_default": None,
            },
            {
                "table_schema": "PUBLIC",
                "table_name": "ORDERS",
                "table_type": "BASE TABLE",
                "column_name": "CUSTOMER_ID",
                "ordinal_position": 2,
                "data_type": "NUMBER(38,0)",
                "is_nullable": "NO",
                "column_default": None,
            },
        ],
        [
            {
                "table_schema": "PUBLIC",
                "table_name": "CUSTOMERS",
                "constraint_name": "PK_CUSTOMERS",
                "constraint_type": "PRIMARY KEY",
                "column_name": "ID",
                "ordinal_position": 1,
            },
            {
                "table_schema": "PUBLIC",
                "table_name": "ORDERS",
                "constraint_name": "PK_ORDERS",
                "constraint_type": "PRIMARY KEY",
                "column_name": "ORDER_ID",
                "ordinal_position": 1,
            },
        ],
        [
            {
                "table_schema": "PUBLIC",
                "table_name": "ORDERS",
                "constraint_name": "FK_ORDERS_CUSTOMER",
                "column_name": "CUSTOMER_ID",
                "referenced_schema": "PUBLIC",
                "referenced_table": "CUSTOMERS",
                "referenced_column": "ID",
                "ordinal_position": 1,
            },
        ],
    ]

    with patch.object(connector, "_get_connection", return_value=mock_conn):
        catalogs = await connector.discover()

    assert len(catalogs) == 1
    cat = catalogs[0]
    assert cat.name == "TEST_DB"
    assert len(cat.schemas) == 1
    schema = cat.schemas[0]
    assert schema.name == "PUBLIC"
    assert len(schema.tables) == 2

    cust_table = next(t for t in schema.tables if t.name == "CUSTOMERS")
    assert len(cust_table.columns) == 2
    assert len(cust_table.constraints) == 1
    assert cust_table.constraints[0].constraint_type == "PRIMARY_KEY"

    orders_table = next(t for t in schema.tables if t.name == "ORDERS")
    assert len(orders_table.constraints) == 2
    fk = next(c for c in orders_table.constraints if c.constraint_type == "FOREIGN_KEY")
    assert fk.referenced_table == "CUSTOMERS"
    assert fk.referenced_columns == ("ID",)


@pytest.mark.asyncio
async def test_snowflake_execute_read_query() -> None:
    connector = SnowflakeConnector("snowflake://user:pass@acc/DB/PUBLIC")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.description = [("CUSTOMER_ID",), ("BALANCE",)]
    mock_cursor.fetchmany.return_value = [(101, 4500.50), (102, 920.00)]
    mock_cursor.sfqid = "01a4b5c6-test-query-id"
    mock_conn.cursor.return_value = mock_cursor

    with patch.object(connector, "_get_connection", return_value=mock_conn):
        result = await connector.execute_read_query("SELECT CUSTOMER_ID, BALANCE FROM CUSTOMERS")

    assert len(result.rows) == 2
    assert result.rows[0]["CUSTOMER_ID"] == 101
    assert result.rows[0]["BALANCE"] == 4500.50
    assert result.warehouse_query_id == "01a4b5c6-test-query-id"
