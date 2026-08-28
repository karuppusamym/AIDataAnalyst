import pytest

from aida.connectors.oracle import (
    OracleConnector,
    _assemble_catalog,
    _parse_dsn,
    _profile_expressions,
    _quote_identifier,
)
from aida.connectors.registry import connector_registry

_VALID_DSN = "oracle://reader:s3cr3t@warehouse.internal:1521/FREEPDB1"


def test_registry_exposes_oracle_connector() -> None:
    assert "oracle" in connector_registry.supported_types
    definition = connector_registry.definition("oracle")
    assert definition.implementation_status == "IMPLEMENTED"
    assert definition.dialect == "oracle"
    assert definition.capabilities["constraints"] is True
    assert definition.capabilities["explain"] is False


def test_oracle_connector_capabilities_are_honest() -> None:
    connector = OracleConnector(_VALID_DSN)
    capabilities = connector.capabilities
    assert capabilities.constraints is True
    assert capabilities.approximate_statistics is True
    assert capabilities.explain is False


def test_quote_identifier_escapes_double_quote() -> None:
    assert _quote_identifier("plain") == '"plain"'
    assert _quote_identifier('odd"name') == '"odd""name"'


def test_parse_dsn_extracts_connection_parameters() -> None:
    params = _parse_dsn(_VALID_DSN)
    assert params.host == "warehouse.internal"
    assert params.port == 1521
    assert params.service_name == "FREEPDB1"
    assert params.user == "reader"
    assert params.password == "s3cr3t"  # noqa: S105 -- test fixture value, not a real credential


def test_parse_dsn_defaults_port_when_absent() -> None:
    params = _parse_dsn("oracle://reader:s3cr3t@warehouse.internal/FREEPDB1")
    assert params.port == 1521


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://reader:s3cr3t@warehouse.internal:1521/FREEPDB1",
        "oracle://warehouse.internal:1521/FREEPDB1",
        "oracle://reader@warehouse.internal:1521/FREEPDB1",
        "oracle://reader:s3cr3t@warehouse.internal:1521/",
        "not-a-url-at-all",
    ],
)
def test_parse_dsn_rejects_invalid_references(dsn: str) -> None:
    with pytest.raises(ValueError, match="Oracle connection reference|invalid Oracle"):
        _parse_dsn(dsn)


def test_connector_construction_rejects_invalid_reference() -> None:
    with pytest.raises(ValueError):
        OracleConnector("unused")


def test_assemble_catalog_groups_columns_and_primary_key() -> None:
    column_rows = [
        {
            "table_schema": "RETAIL",
            "table_name": "CUSTOMER",
            "table_type": "TABLE",
            "column_name": "CUSTOMER_ID",
            "ordinal_position": 1,
            "data_type": "NUMBER",
            "is_nullable": "N",
            "column_default": None,
        },
        {
            "table_schema": "RETAIL",
            "table_name": "CUSTOMER",
            "table_type": "TABLE",
            "column_name": "CUSTOMER_NAME",
            "ordinal_position": 2,
            "data_type": "VARCHAR2",
            "is_nullable": "Y",
            "column_default": None,
        },
    ]
    key_rows = [
        {
            "table_schema": "RETAIL",
            "table_name": "CUSTOMER",
            "constraint_name": "PK_CUSTOMER",
            "constraint_type": "P",
            "column_name": "CUSTOMER_ID",
            "ordinal_position": 1,
        }
    ]

    catalogs = _assemble_catalog("BANK", column_rows, key_rows, [])

    assert len(catalogs) == 1
    schema = catalogs[0].schemas[0]
    assert schema.name == "RETAIL"
    table = schema.tables[0]
    assert table.name == "CUSTOMER"
    assert table.object_type == "TABLE"
    assert [column.name for column in table.columns] == ["CUSTOMER_ID", "CUSTOMER_NAME"]
    assert table.columns[0].nullable is False
    assert table.columns[1].nullable is True
    assert len(table.constraints) == 1
    constraint = table.constraints[0]
    assert constraint.constraint_type == "PRIMARY_KEY"
    assert constraint.columns == ("CUSTOMER_ID",)


def test_assemble_catalog_orders_foreign_key_columns_by_position() -> None:
    column_rows = [
        {
            "table_schema": "RETAIL",
            "table_name": "ACCOUNT",
            "table_type": "TABLE",
            "column_name": "ACCOUNT_ID",
            "ordinal_position": 1,
            "data_type": "NUMBER",
            "is_nullable": "N",
            "column_default": None,
        },
    ]
    foreign_key_rows = [
        {
            "table_schema": "RETAIL",
            "table_name": "ACCOUNT",
            "constraint_name": "FK_ACCOUNT_CUSTOMER",
            "referenced_schema": "RETAIL",
            "referenced_table": "CUSTOMER",
            "column_name": "CUSTOMER_ID",
            "referenced_column": "CUSTOMER_ID",
            "ordinal_position": 1,
        }
    ]

    catalogs = _assemble_catalog("BANK", column_rows, [], foreign_key_rows)

    table = catalogs[0].schemas[0].tables[0]
    assert len(table.constraints) == 1
    constraint = table.constraints[0]
    assert constraint.constraint_type == "FOREIGN_KEY"
    assert constraint.referenced_schema == "RETAIL"
    assert constraint.referenced_table == "CUSTOMER"
    assert constraint.columns == ("CUSTOMER_ID",)
    assert constraint.referenced_columns == ("CUSTOMER_ID",)


def test_profile_expressions_disable_distinct_and_lengths_for_unsupported_types() -> None:
    expressions = _profile_expressions('"PAYLOAD"', 2, "BLOB")
    assert any("CAST(0 AS NUMBER)" in expression for expression in expressions)
    assert any("CAST(NULL AS NUMBER)" in expression for expression in expressions)
