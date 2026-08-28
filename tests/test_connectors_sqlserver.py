import pytest

from aida.connectors.registry import connector_registry
from aida.connectors.sqlserver import (
    SqlServerConnector,
    _assemble_catalog,
    _extract_showplan_estimate,
    _parse_dsn,
    _quote_identifier,
)

_VALID_DSN = "mssql://reader:s3cr3t@warehouse.internal:1433/bank_demo"

_SHOWPLAN_XML = """<?xml version="1.0" encoding="utf-8"?>
<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan"
             Version="1.539" Build="16.0.1000.6">
  <BatchSequence>
    <Batch>
      <Statements>
        <StmtSimple StatementText="SELECT 1" StatementType="SELECT"
                    StatementSubTreeCost="0.0032831" StatementEstRows="1">
          <QueryPlan />
        </StmtSimple>
      </Statements>
    </Batch>
  </BatchSequence>
</ShowPlanXML>"""


def test_registry_exposes_sqlserver_connector() -> None:
    assert "sqlserver" in connector_registry.supported_types
    definition = connector_registry.definition("sqlserver")
    assert definition.implementation_status == "IMPLEMENTED"
    assert definition.dialect == "tsql"


def test_sqlserver_connector_capabilities() -> None:
    connector = SqlServerConnector(_VALID_DSN)
    capabilities = connector.capabilities
    assert capabilities.constraints is True
    assert capabilities.explain is True
    assert capabilities.approximate_statistics is True


def test_quote_identifier_escapes_closing_bracket() -> None:
    assert _quote_identifier("plain") == "[plain]"
    assert _quote_identifier("weird]name") == "[weird]]name]"


def test_parse_dsn_extracts_connection_parameters() -> None:
    params = _parse_dsn(_VALID_DSN)
    assert params.host == "warehouse.internal"
    assert params.port == 1433
    assert params.database == "bank_demo"
    assert params.user == "reader"
    assert params.password == "s3cr3t"  # noqa: S105 -- test fixture value, not a real credential


def test_parse_dsn_defaults_port_when_absent() -> None:
    params = _parse_dsn("mssql://reader:s3cr3t@warehouse.internal/bank_demo")
    assert params.port == 1433


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://reader:s3cr3t@warehouse.internal:1433/bank_demo",
        "mssql://warehouse.internal:1433/bank_demo",
        "mssql://reader@warehouse.internal:1433/bank_demo",
        "mssql://reader:s3cr3t@warehouse.internal:1433/",
        "not-a-url-at-all",
    ],
)
def test_parse_dsn_rejects_invalid_references(dsn: str) -> None:
    with pytest.raises(ValueError, match="SQL Server connection reference|invalid SQL Server"):
        _parse_dsn(dsn)


def test_connector_construction_rejects_invalid_reference() -> None:
    with pytest.raises(ValueError):
        SqlServerConnector("unused")


def test_extract_showplan_estimate_reads_subtree_cost() -> None:
    estimate = _extract_showplan_estimate(_SHOWPLAN_XML)
    assert estimate.score == pytest.approx(0.0032831)
    assert estimate.kind == "SHOWPLAN_XML"
    assert estimate.estimated_rows == pytest.approx(1.0)
    assert estimate.evidence["Plan"]["Node Type"] == "SELECT"


def test_extract_showplan_estimate_rejects_malformed_xml() -> None:
    with pytest.raises(RuntimeError, match="invalid SHOWPLAN_XML"):
        _extract_showplan_estimate("<not><valid")


def test_extract_showplan_estimate_rejects_document_without_statement() -> None:
    empty_plan = (
        '<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan">'
        "<BatchSequence><Batch><Statements /></Batch></BatchSequence></ShowPlanXML>"
    )
    with pytest.raises(RuntimeError, match="without a statement node"):
        _extract_showplan_estimate(empty_plan)


def test_extract_showplan_estimate_rejects_missing_cost_attribute() -> None:
    plan_without_cost = (
        '<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan">'
        "<BatchSequence><Batch><Statements>"
        '<StmtSimple StatementText="SELECT 1" StatementType="SELECT" />'
        "</Statements></Batch></BatchSequence></ShowPlanXML>"
    )
    with pytest.raises(RuntimeError, match="without a subtree cost"):
        _extract_showplan_estimate(plan_without_cost)


def test_assemble_catalog_groups_columns_and_primary_key() -> None:
    column_rows = [
        {
            "table_schema": "retail",
            "table_name": "customer",
            "table_type": "BASE TABLE",
            "column_name": "customer_id",
            "ordinal_position": 1,
            "data_type": "bigint",
            "is_nullable": "NO",
            "column_default": None,
        },
        {
            "table_schema": "retail",
            "table_name": "customer",
            "table_type": "BASE TABLE",
            "column_name": "customer_name",
            "ordinal_position": 2,
            "data_type": "nvarchar",
            "is_nullable": "YES",
            "column_default": None,
        },
    ]
    key_rows = [
        {
            "table_schema": "retail",
            "table_name": "customer",
            "constraint_name": "pk_customer",
            "constraint_type": "PRIMARY KEY",
            "column_name": "customer_id",
            "ordinal_position": 1,
        }
    ]

    catalogs = _assemble_catalog("bank_demo", column_rows, key_rows, [])

    assert len(catalogs) == 1
    schema = catalogs[0].schemas[0]
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


def test_assemble_catalog_orders_foreign_key_columns_by_position() -> None:
    column_rows = [
        {
            "table_schema": "retail",
            "table_name": "account",
            "table_type": "BASE TABLE",
            "column_name": "account_id",
            "ordinal_position": 1,
            "data_type": "bigint",
            "is_nullable": "NO",
            "column_default": None,
        },
    ]
    foreign_key_rows = [
        {
            "table_schema": "retail",
            "table_name": "account",
            "constraint_name": "fk_account_customer",
            "referenced_schema": "retail",
            "referenced_table": "customer",
            "column_name": "customer_id",
            "referenced_column": "customer_id",
            "ordinal_position": 1,
        }
    ]

    catalogs = _assemble_catalog("bank_demo", column_rows, [], foreign_key_rows)

    table = catalogs[0].schemas[0].tables[0]
    assert len(table.constraints) == 1
    constraint = table.constraints[0]
    assert constraint.constraint_type == "FOREIGN_KEY"
    assert constraint.referenced_schema == "retail"
    assert constraint.referenced_table == "customer"
    assert constraint.columns == ("customer_id",)
    assert constraint.referenced_columns == ("customer_id",)
