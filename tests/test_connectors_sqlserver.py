import inspect

import pytest

from aida.connectors import sqlserver
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


# --- envelope 1.1 axes (gap/02 N1) ------------------------------------------


def test_sqlserver_advertises_exactly_the_11_axes_it_implements() -> None:
    """INV-9 for the four capability flags envelope 1.1 adds.

    Each `True` is paired with the system view that backs it. `indexes` and
    `partitions` stay `False`: SQL Server exposes both, but this connector does
    not read them, and a flag that describes the source rather than the connector
    is exactly the optimism INV-9 forbids.
    """
    connector = SqlServerConnector(_VALID_DSN)
    capabilities = connector.capabilities

    assert capabilities.views is True
    assert capabilities.routines is True
    assert capabilities.object_comments is True
    assert capabilities.grants is True
    assert capabilities.indexes is False
    assert capabilities.partitions is False

    source = inspect.getsource(sqlserver)
    assert "sys.sql_modules" in source
    assert "sys.parameters" in source
    assert "sys.extended_properties" in source
    assert "sys.database_permissions" in source


def test_routine_bodies_are_read_from_sys_sql_modules_not_information_schema() -> None:
    """`INFORMATION_SCHEMA.ROUTINES.ROUTINE_DEFINITION` is nvarchar(4000) and
    silently truncates every longer body -- which is precisely the long ETL
    procedure whose text is worth parsing (gap/02 N3, N12).

    `sys.sql_modules.definition` is nvarchar(max). This connector therefore
    reports `truncated = false` because it checked, not because it did not, and
    this test is what stops a later edit from reintroducing the truncating view.
    """
    # Asserted against the statement constants rather than the module source, so
    # that the explanatory comment beside them cannot satisfy its own test.
    statements = (
        sqlserver._ROUTINE_SQL,
        sqlserver._VIEW_DEFINITION_SQL,
        sqlserver._ROUTINE_PARAMETER_SQL,
    )
    for statement in statements:
        assert "ROUTINE_DEFINITION" not in statement.upper()
        assert "SYSCOMMENTS" not in statement.upper()
    assert "m.definition AS body" in sqlserver._ROUTINE_SQL
    assert "m.definition AS definition" in sqlserver._VIEW_DEFINITION_SQL


def test_assemble_catalog_carries_the_11_axes() -> None:
    """Row shapes from `_discover_sync` through to the envelope.

    An encrypted module is included deliberately: SQL Server returns NULL for
    `sys.sql_modules.definition` on `WITH ENCRYPTION`, and that must arrive as
    unavailable-with-a-reason rather than as a view with an empty body.
    """
    column_rows = [
        {
            "table_schema": "retail",
            "table_name": "open_account",
            "table_type": "VIEW",
            "column_name": "account_id",
            "ordinal_position": 1,
            "data_type": "bigint",
            "is_nullable": "NO",
            "column_default": None,
        }
    ]

    catalogs = _assemble_catalog(
        "bank_demo",
        column_rows,
        [],
        [],
        view_rows=[
            {
                "table_schema": "retail",
                "table_name": "open_account",
                "definition": None,
                "is_materialized": 0,
                "is_updatable": 1,
                "check_option": None,
                "unavailable_reason": "module is encrypted",
            }
        ],
        routine_rows=[
            {
                "routine_schema": "retail",
                "routine_name": "usp_close_account",
                "specific_name": "1234",
                "routine_type": "PROCEDURE",
                "language": "SQL",
                "body": "BEGIN SET NOCOUNT ON; END",
                "return_type": None,
                "is_deterministic": 0,
                "security_mode": "INVOKER",
                "description": "closes a deposit account",
                "unavailable_reason": None,
            }
        ],
        routine_parameter_rows=[
            {
                "routine_schema": "retail",
                "specific_name": "1234",
                "parameter_name": "@account_id",
                "ordinal_position": 1,
                "parameter_mode": "IN",
                "data_type": "bigint",
                "parameter_default": None,
            }
        ],
        table_description_rows=[
            {
                "table_schema": "retail",
                "table_name": "open_account",
                "description": "accounts that are still open",
            }
        ],
        column_description_rows=[
            {
                "table_schema": "retail",
                "table_name": "open_account",
                "column_name": "account_id",
                "description": "surrogate key",
            }
        ],
        schema_description_rows=[
            {"schema_name": "retail", "description": "retail banking objects"}
        ],
        catalog_description_row={"description": "the demo bank database"},
        grant_rows=[
            {
                "schema_name": "retail",
                "grantee": "risk_reader",
                "grantee_type": "ROLE",
                "privilege": "SELECT",
                "object_type": "VIEW",
                "object_name": "open_account",
                "is_grantable": 0,
            }
        ],
    )

    catalog = catalogs[0]
    schema = catalog.schemas[0]
    table = schema.tables[0]
    assert catalog.source_description == "the demo bank database"
    assert schema.source_description == "retail banking objects"
    assert table.source_description == "accounts that are still open"
    assert table.columns[0].source_description == "surrogate key"
    assert table.view_definition is not None
    assert table.view_definition.definition_sql is None
    assert table.view_definition.unavailable_reason == "module is encrypted"
    assert table.view_definition.is_updatable is True
    assert table.view_definition.is_materialized is False
    routine = schema.routines[0]
    assert routine.routine_type == "PROCEDURE"
    assert routine.truncated is False
    assert routine.parameters[0].name == "@account_id"
    assert schema.grants[0].object_type == "VIEW"
    assert schema.grants[0].is_grantable is False


def test_the_10_assembly_signature_still_works_unchanged() -> None:
    """The 1.1 row sets are keyword-only with `None` defaults, so a caller that
    knows nothing about 1.1 -- including the two assembly tests above this one --
    produces an envelope where the new axes are absent rather than empty.
    """
    catalogs = _assemble_catalog(
        "bank_demo",
        [
            {
                "table_schema": "retail",
                "table_name": "customer",
                "table_type": "BASE TABLE",
                "column_name": "customer_id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "is_nullable": "NO",
                "column_default": None,
            }
        ],
        [],
        [],
    )

    schema = catalogs[0].schemas[0]
    assert schema.routines == ()
    assert schema.grants == ()
    assert schema.source_description is None
    assert schema.tables[0].view_definition is None
