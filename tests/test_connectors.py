import inspect
from datetime import UTC, datetime

import pytest

from aida.connectors import postgres
from aida.connectors.base import ConnectorQueryHistoryUnsupported
from aida.connectors.discovery import (
    apply_column_descriptions,
    apply_table_descriptions,
    apply_view_definitions,
    assemble_catalog,
    build_grants,
    build_routines,
    build_table_map_from_column_rows,
)
from aida.connectors.registry import ConnectorRegistry, connector_registry
from aida.ingestion import default_capabilities


def test_registry_exposes_postgres_connector() -> None:
    assert "postgres" in connector_registry.supported_types


def test_registry_rejects_unknown_connector() -> None:
    registry = ConnectorRegistry()

    with pytest.raises(ValueError, match="unsupported connector"):
        registry.create("unknown", "irrelevant")


def test_registry_definitions_expose_capabilities_without_connector_instantiation() -> None:
    postgres = connector_registry.definition("postgres")
    sqlserver = connector_registry.definition("sqlserver")
    snowflake = connector_registry.definition("snowflake")
    databricks = connector_registry.definition("databricks")

    assert postgres.capabilities["constraints"] is True
    assert postgres.capabilities["explain"] is True
    assert sqlserver.capabilities["approximate_statistics"] is True
    assert snowflake.capabilities["explain"] is True
    assert default_capabilities(postgres) == postgres.capabilities
    assert default_capabilities(sqlserver) == sqlserver.capabilities
    assert default_capabilities(snowflake) == snowflake.capabilities
    assert default_capabilities(databricks) == databricks.capabilities


# --- envelope 1.1 axes (gap/02 N1) ------------------------------------------


def test_postgres_advertises_exactly_the_11_axes_it_implements() -> None:
    """INV-9 for the four capability flags envelope 1.1 adds.

    Every `True` below is backed by a named query in `PostgresConnector.discover`;
    the assertions on the SQL are what stop the flag from outliving the behaviour,
    which is the drift INV-9 exists to catch. `indexes` and `partitions` turned
    `True` under CT-3 (pg_index/pg_am and pg_partitioned_table/pg_inherits
    queries below), backed by the same drift-detection pattern as the other four.
    """
    definition = connector_registry.definition("postgres")

    assert definition.capabilities["views"] is True
    assert definition.capabilities["routines"] is True
    assert definition.capabilities["object_comments"] is True
    assert definition.capabilities["grants"] is True
    assert definition.capabilities["indexes"] is True
    assert definition.capabilities["partitions"] is True

    source = inspect.getsource(postgres)
    assert "pg_get_viewdef" in source
    assert "pg_get_functiondef" in source
    assert "col_description" in source
    assert "obj_description" in source
    assert "information_schema.role_table_grants" in source
    assert "pg_index" in source
    assert "pg_partitioned_table" in source


def test_postgres_discovery_assembles_every_11_axis() -> None:
    """The connector's row shapes and the assembly helpers have to agree.

    Driven through the same helpers `discover()` calls, with rows shaped exactly
    as the queries in `postgres.py` name their columns. A rename on either side
    fails here rather than producing an envelope that is quietly missing an axis.
    """
    tables = build_table_map_from_column_rows(
        [
            {
                "table_schema": "customer",
                "table_name": "open_account",
                "table_type": "VIEW",
                "column_name": "account_id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "is_nullable": "NO",
                "column_default": None,
            }
        ]
    )
    apply_table_descriptions(
        tables,
        [
            {
                "table_schema": "customer",
                "table_name": "open_account",
                "description": "accounts that are still open",
            }
        ],
    )
    apply_column_descriptions(
        tables,
        [
            {
                "table_schema": "customer",
                "table_name": "open_account",
                "column_name": "account_id",
                "description": "surrogate key",
            }
        ],
    )
    apply_view_definitions(
        tables,
        [
            {
                "table_schema": "customer",
                "table_name": "open_account",
                "definition": "SELECT account_id FROM customer.account",
                "is_materialized": False,
                "is_updatable": "YES",
                "check_option": "NONE",
            }
        ],
    )
    catalogs = assemble_catalog(
        "bank",
        tables,
        routines=build_routines(
            [
                {
                    "routine_schema": "customer",
                    "routine_name": "close_account",
                    "specific_name": "16384",
                    "routine_type": "PROCEDURE",
                    "language": "plpgsql",
                    "body": "BEGIN END;",
                    "return_type": None,
                    "is_deterministic": False,
                    "security_mode": "INVOKER",
                    "description": None,
                }
            ],
            [
                {
                    "routine_schema": "customer",
                    "specific_name": "16384",
                    "parameter_name": "p_account_id",
                    "ordinal_position": 1,
                    "parameter_mode": "IN",
                    "data_type": "bigint",
                }
            ],
        ),
        grants=build_grants(
            [
                {
                    "schema_name": "customer",
                    "grantee": "risk_reader",
                    "grantee_type": "ROLE",
                    "privilege": "select",
                    "object_type": "TABLE",
                    "object_name": "account",
                    "is_grantable": "NO",
                }
            ]
        ),
        schema_descriptions={"customer": "deposit subject area"},
        catalog_description="the consumer banking warehouse",
    )

    catalog = catalogs[0]
    schema = catalog.schemas[0]
    table = schema.tables[0]
    assert catalog.source_description == "the consumer banking warehouse"
    assert schema.source_description == "deposit subject area"
    assert table.source_description == "accounts that are still open"
    assert table.columns[0].source_description == "surrogate key"
    assert table.view_definition is not None
    assert table.view_definition.definition_sql == "SELECT account_id FROM customer.account"
    assert table.view_definition.is_updatable is True
    assert table.view_definition.truncated is False
    assert table.view_definition.unavailable_reason is None
    assert schema.routines[0].parameters[0].physical_type == "bigint"
    assert schema.grants[0].privilege == "SELECT"
    assert schema.grants[0].is_grantable is False


def test_a_null_definition_is_recorded_as_unavailable_with_a_reason() -> None:
    """A source that refuses the text must not look like a view with no text.

    `apply_view_definitions` supplies a generic reason when the source gives none,
    so the unavailable state is never reasonless -- an unexplained NULL is
    indistinguishable from a bug in the connector six months later.
    """
    tables = build_table_map_from_column_rows(
        [
            {
                "table_schema": "customer",
                "table_name": "secret_view",
                "table_type": "VIEW",
                "column_name": "account_id",
                "ordinal_position": 1,
                "data_type": "bigint",
                "is_nullable": "NO",
                "column_default": None,
            }
        ]
    )
    apply_view_definitions(
        tables,
        [
            {
                "table_schema": "customer",
                "table_name": "secret_view",
                "definition": None,
            }
        ],
    )
    view = assemble_catalog("bank", tables)[0].schemas[0].tables[0].view_definition

    assert view is not None
    assert view.definition_sql is None
    assert view.unavailable_reason


def test_a_schema_with_only_routines_survives_assembly() -> None:
    """A procedural schema holds no tables, and dropping it would make the
    routine inventory silently incomplete for exactly the estates where
    procedure parsing (gap/02 N3, N12) matters most.
    """
    catalogs = assemble_catalog(
        "bank",
        {},
        routines=build_routines(
            [
                {
                    "routine_schema": "batch",
                    "routine_name": "nightly_close",
                    "specific_name": "1",
                    "routine_type": "PROCEDURE",
                    "body": "BEGIN END;",
                }
            ]
        ),
    )

    assert [schema.name for schema in catalogs[0].schemas] == ["batch"]
    assert catalogs[0].schemas[0].routines[0].name == "nightly_close"


# --- CN-9: get_query_history() default is fail-closed -----------------------
#
# No connector overrides this yet (that is the whole point of CN-9). PostgresConnector
# is the plain stand-in for "any connector that hasn't implemented it" -- the point
# under test is the base `Connector` default, not anything Postgres-specific.


@pytest.mark.asyncio
async def test_get_query_history_default_fails_closed() -> None:
    connector = postgres.PostgresConnector("postgresql://user:pass@localhost/db")

    with pytest.raises(ConnectorQueryHistoryUnsupported):
        await connector.get_query_history(since=datetime(2026, 1, 1, tzinfo=UTC))
