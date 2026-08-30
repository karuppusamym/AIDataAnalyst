import json
from unittest.mock import MagicMock, patch

import pytest

from aida.connectors.registry import connector_registry
from aida.connectors.snowflake import (
    SnowflakeConnector,
    _build_routine,
    _build_view_definition,
    _extract_snowflake_explain_estimate,
    _parse_argument_signature,
    _parse_dsn,
    _quote_identifier,
    _quote_literal,
    _unqualified_name,
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
    """v1.0 assembly, with every envelope 1.1 query refused.

    The fetch sequence stops after the three v1.0 queries, so each supplementary
    query raises. That is deliberate: a source that answers none of the new axes must
    still produce exactly the catalog graph it produced before envelope 1.1, with the
    refusals recorded rather than rendered as empty definitions.
    """
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

    assert schema.routines == ()
    assert schema.grants == ()
    assert cust_table.view_definition is None
    assert set(cat.attributes["envelope_v11_unavailable"]) >= {
        "views",
        "functions",
        "procedures",
        "schema_comments",
    }


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


# --- Envelope 1.1 (gap/02 N1) ------------------------------------------------
#
# Snowflake's two surprises are both tested here: `VIEW_DEFINITION` is NULL on a
# secure view the session's role does not own, and there is no `PARAMETERS` view at
# all -- a routine's arguments arrive as one text blob that has to be parsed.

_ENVELOPE_DSN = "snowflake://user:pass@acc/TEST_DB/PUBLIC?warehouse=WH&role=ROLE"


def _column_row(
    table: str,
    column: str,
    *,
    table_type: str = "BASE TABLE",
    position: int = 1,
    table_comment: str | None = None,
    column_comment: str | None = None,
) -> dict[str, object]:
    return {
        "table_schema": "PUBLIC",
        "table_name": table,
        "table_type": table_type,
        "column_name": column,
        "ordinal_position": position,
        "data_type": "NUMBER(38,0)",
        "is_nullable": "NO",
        "column_default": None,
        "table_comment": table_comment,
        "column_comment": column_comment,
    }


_VIEW_SQL = "CREATE VIEW ACTIVE_CUSTOMERS AS SELECT ID FROM CUSTOMERS WHERE ACTIVE"
_MVIEW_DDL = "create materialized view CUSTOMER_ROLLUP as select ID from CUSTOMERS;"

_ENVELOPE_COLUMN_ROWS = [
    _column_row(
        "CUSTOMERS",
        "ID",
        table_comment="Customer master",
        column_comment="Surrogate key",
    ),
    _column_row("ACTIVE_CUSTOMERS", "ID", table_type="VIEW"),
    _column_row("CUSTOMER_ROLLUP", "ID", table_type="MATERIALIZED VIEW"),
]


def _envelope_fetch_sequence(
    *,
    views: object = None,
    functions: object = None,
    procedures: object = None,
    view_ddl: list[object] | None = None,
    grants: object = None,
) -> list[object]:
    """The exact `fetchall()` sequence `discover()` drives, in call order.

    Written out rather than pattern-matched on SQL so that adding a query to
    `discover()` fails this fixture loudly instead of silently shifting a response
    onto the wrong statement.
    """
    return [
        _ENVELOPE_COLUMN_ROWS,
        [],
        [],
        [{"database_name": "TEST_DB", "comment": "Test warehouse"}],
        [{"schema_name": "PUBLIC", "comment": "Public schema"}],
        (
            views
            if views is not None
            else [
                {
                    "table_schema": "PUBLIC",
                    "table_name": "ACTIVE_CUSTOMERS",
                    "view_definition": _VIEW_SQL,
                    "is_secure": "NO",
                    "is_updatable": "NO",
                    "check_option": "NONE",
                }
            ]
        ),
        (
            functions
            if functions is not None
            else [
                {
                    "routine_schema": "PUBLIC",
                    "routine_name": "NORMALIZE_NAME",
                    "routine_type": "FUNCTION",
                    "routine_language": "SQL",
                    "routine_definition": "SELECT UPPER(TRIM(RAW))",
                    "argument_signature": "(RAW VARCHAR, KEEP_CASE BOOLEAN DEFAULT FALSE)",
                    "data_type": "VARCHAR(16777216)",
                    "is_secure": "NO",
                    "comment": "Canonical customer name",
                }
            ]
        ),
        (
            procedures
            if procedures is not None
            else [
                {
                    "routine_schema": "PUBLIC",
                    "routine_name": "REFRESH_ROLLUP",
                    "routine_type": "PROCEDURE",
                    "routine_language": "JAVASCRIPT",
                    "routine_definition": None,
                    "argument_signature": "()",
                    "data_type": "VARCHAR",
                    "is_secure": "YES",
                    "comment": None,
                }
            ]
        ),
        *(view_ddl if view_ddl is not None else [[{"view_definition": _MVIEW_DDL}]]),
        (
            grants
            if grants is not None
            else [
                {
                    "privilege": "USAGE",
                    "granted_on": "SCHEMA",
                    "name": "TEST_DB.PUBLIC",
                    "granted_to": "ROLE",
                    "grantee_name": "ANALYST_ROLE",
                    "grant_option": "false",
                }
            ]
        ),
    ]


async def _discover_with(sequence: list[object]) -> tuple[object, ...]:
    connector = SnowflakeConnector(_ENVELOPE_DSN)
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.fetchall.side_effect = sequence
    with patch.object(connector, "_get_connection", return_value=mock_conn):
        return await connector.discover()


def test_snowflake_capabilities_declare_every_implemented_envelope_axis() -> None:
    capabilities = SnowflakeConnector(_ENVELOPE_DSN).capabilities
    assert capabilities.views is True
    assert capabilities.routines is True
    assert capabilities.object_comments is True
    assert capabilities.grants is True
    advertised = connector_registry.definition("snowflake").capabilities
    assert advertised["views"] is True
    assert advertised["grants"] is True


@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        (None, ()),
        ("()", ()),
        ("(A NUMBER)", (("A", "NUMBER", None),)),
        # The comma inside NUMBER(38,0) is not an argument separator.
        ("(A NUMBER(38,0), B VARCHAR)", (("A", "NUMBER(38,0)", None), ("B", "VARCHAR", None))),
        ("(A NUMBER DEFAULT 1)", (("A", "NUMBER", "1"),)),
        # A bare type with no name is what Snowflake prints for some built-ins.
        ("(VARIANT)", ((None, "VARIANT", None),)),
    ],
)
def test_argument_signature_parsing(
    signature: str | None, expected: tuple[tuple[str | None, str, str | None], ...]
) -> None:
    """Snowflake has no INFORMATION_SCHEMA.PARAMETERS.

    `ARGUMENT_SIGNATURE` is the only place a routine's arguments appear, so parsing it
    correctly is the whole of the parameter axis on this connector.
    """
    parsed = _parse_argument_signature(signature)
    assert tuple((p.name, p.physical_type, p.default_expression) for p in parsed) == expected
    assert all(p.mode == "IN" for p in parsed)
    assert [p.ordinal_position for p in parsed] == list(range(1, len(parsed) + 1))


async def test_snowflake_discover_round_trips_a_view_definition() -> None:
    catalogs = await _discover_with(_envelope_fetch_sequence())

    schema = catalogs[0].schemas[0]
    view = next(t for t in schema.tables if t.name == "ACTIVE_CUSTOMERS")
    assert view.view_definition is not None
    assert view.view_definition.definition_sql == _VIEW_SQL
    assert view.view_definition.truncated is False
    assert view.view_definition.unavailable_reason is None
    assert view.view_definition.is_materialized is False
    assert view.view_definition.is_updatable is False
    # Snowflake reports CHECK_OPTION as the literal 'NONE'; that is not a check option.
    assert view.view_definition.check_option is None


async def test_a_materialized_view_definition_comes_from_the_get_ddl_pass() -> None:
    """Snowflake's INFORMATION_SCHEMA.VIEWS has no row for a materialized view at all.

    `GET_DDL` is the only path to its text, so a materialized view that arrived with
    an empty definition would be an extraction failure wearing a lineage gap's
    clothes.
    """
    catalogs = await _discover_with(_envelope_fetch_sequence())

    schema = catalogs[0].schemas[0]
    rollup = next(t for t in schema.tables if t.name == "CUSTOMER_ROLLUP")
    assert rollup.object_type == "MATERIALIZED_VIEW"
    assert rollup.view_definition is not None
    assert rollup.view_definition.is_materialized is True
    assert rollup.view_definition.definition_sql == _MVIEW_DDL


async def test_a_secure_view_is_unavailable_rather_than_empty() -> None:
    catalogs = await _discover_with(
        _envelope_fetch_sequence(
            views=[
                {
                    "table_schema": "PUBLIC",
                    "table_name": "ACTIVE_CUSTOMERS",
                    "view_definition": None,
                    "is_secure": "YES",
                    "is_updatable": "NO",
                    "check_option": "NONE",
                }
            ],
            view_ddl=[RuntimeError("SQL access control error")],
        )
    )

    schema = catalogs[0].schemas[0]
    view = next(t for t in schema.tables if t.name == "ACTIVE_CUSTOMERS")
    assert view.view_definition is not None
    assert view.view_definition.definition_sql is None
    assert view.view_definition.unavailable_reason is not None
    assert "secure view" in view.view_definition.unavailable_reason


async def test_a_refused_views_query_still_leaves_a_reason_on_the_view() -> None:
    catalogs = await _discover_with(
        _envelope_fetch_sequence(
            views=RuntimeError("Object 'VIEWS' does not exist"),
            # With the VIEWS query refused, every view-shaped object falls through to
            # the GET_DDL pass, so both of them need a response.
            view_ddl=[
                RuntimeError("Insufficient privileges"),
                RuntimeError("Insufficient privileges"),
            ],
        )
    )

    schema = catalogs[0].schemas[0]
    view = next(t for t in schema.tables if t.name == "ACTIVE_CUSTOMERS")
    assert view.view_definition is not None
    assert view.view_definition.definition_sql is None
    assert view.view_definition.unavailable_reason is not None
    assert "Insufficient privileges" in view.view_definition.unavailable_reason
    assert "views" in catalogs[0].attributes["envelope_v11_unavailable"]


def test_a_view_definition_over_the_cap_is_a_flagged_prefix() -> None:
    definition = _build_view_definition(
        "SELECT " + "x" * 40, object_label="PUBLIC.V", max_characters=10
    )
    assert definition.definition_sql == "SELECT xxx"
    assert definition.truncated is True
    assert definition.unavailable_reason is None


async def test_a_routine_round_trips_with_parameters_parsed_from_its_signature() -> None:
    catalogs = await _discover_with(_envelope_fetch_sequence())

    schema = catalogs[0].schemas[0]
    function = next(r for r in schema.routines if r.name == "NORMALIZE_NAME")
    assert function.routine_type == "FUNCTION"
    assert function.language == "SQL"
    assert function.body_sql == "SELECT UPPER(TRIM(RAW))"
    assert function.return_type == "VARCHAR(16777216)"
    assert function.source_description == "Canonical customer name"
    assert function.unavailable_reason is None
    assert [(p.name, p.physical_type, p.mode) for p in function.parameters] == [
        ("RAW", "VARCHAR", "IN"),
        ("KEEP_CASE", "BOOLEAN", "IN"),
    ]
    assert function.parameters[1].default_expression == "FALSE"
    # Snowflake exposes EXECUTE AS and volatility only through SHOW/DESCRIBE, so
    # neither is claimed here.
    assert function.security_mode is None
    assert function.is_deterministic is None


async def test_a_secure_procedure_body_is_unavailable_rather_than_empty() -> None:
    catalogs = await _discover_with(_envelope_fetch_sequence())

    schema = catalogs[0].schemas[0]
    procedure = next(r for r in schema.routines if r.name == "REFRESH_ROLLUP")
    assert procedure.routine_type == "PROCEDURE"
    assert procedure.body_sql is None
    assert procedure.unavailable_reason is not None
    assert "secure" in procedure.unavailable_reason
    assert procedure.attributes["is_secure"] is True


def test_a_routine_body_over_the_cap_is_a_flagged_prefix() -> None:
    routine = _build_routine(
        {
            "routine_schema": "PUBLIC",
            "routine_name": "BIG",
            "routine_type": "PROCEDURE",
            "routine_definition": "BEGIN " + "x" * 100,
            "argument_signature": "()",
        },
        max_characters=8,
    )
    assert routine.body_sql == "BEGIN xx"
    assert routine.truncated is True
    assert routine.unavailable_reason is None


async def test_comments_land_at_every_level_snowflake_exposes() -> None:
    catalogs = await _discover_with(_envelope_fetch_sequence())

    catalog = catalogs[0]
    assert catalog.source_description == "Test warehouse"
    schema = catalog.schemas[0]
    assert schema.source_description == "Public schema"
    customers = next(t for t in schema.tables if t.name == "CUSTOMERS")
    assert customers.source_description == "Customer master"
    assert customers.columns[0].source_description == "Surrogate key"


async def test_schema_grants_land_on_the_schema() -> None:
    catalogs = await _discover_with(_envelope_fetch_sequence())

    grants = catalogs[0].schemas[0].grants
    assert len(grants) == 1
    grant = grants[0]
    assert grant.grantee == "ANALYST_ROLE"
    assert grant.grantee_type == "ROLE"
    assert grant.privilege == "USAGE"
    assert grant.object_type == "SCHEMA"
    assert grant.object_name == "PUBLIC"
    assert grant.schema_name == "PUBLIC"
    assert grant.is_grantable is False


async def test_a_refused_show_grants_is_recorded_rather_than_read_as_no_grants() -> None:
    catalogs = await _discover_with(
        _envelope_fetch_sequence(grants=RuntimeError("Insufficient privileges on schema"))
    )

    assert catalogs[0].schemas[0].grants == ()
    recorded = catalogs[0].attributes["envelope_v11_unavailable"]
    assert "Insufficient privileges on schema" in recorded["grants:PUBLIC"]


def test_show_grants_object_names_are_unqualified() -> None:
    assert _unqualified_name("TEST_DB.PUBLIC") == "PUBLIC"
    assert _unqualified_name('"TEST_DB"."PUBLIC"') == "PUBLIC"
    assert _unqualified_name(None) == ""


def test_get_ddl_object_names_are_escaped_as_string_literals() -> None:
    assert _quote_literal("DB.SCHEMA.VIEW") == "'DB.SCHEMA.VIEW'"
    assert _quote_literal("o'brien") == "'o''brien'"
