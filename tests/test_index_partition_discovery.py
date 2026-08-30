"""CT-3 / CN-8: index and partition extraction at the connector/discovery layer.

These are pure unit tests against `aida.connectors.discovery` and the Oracle
connector's `_assemble_catalog` -- no live database, mirroring the existing
`tests/test_connectors_oracle.py` pattern of feeding already-normalized rows
into the assembly helpers.
"""

from aida.connectors.discovery import (
    append_grouped_index_rows,
    append_partition_rows,
    build_table_map_from_column_rows,
)
from aida.connectors.oracle import OracleConnector, _assemble_catalog
from aida.connectors.postgres import PostgresConnector
from aida.connectors.registry import connector_registry


def _column_rows() -> list[dict[str, object]]:
    return [
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
        {
            "table_schema": "retail",
            "table_name": "account",
            "table_type": "BASE TABLE",
            "column_name": "opened_at",
            "ordinal_position": 2,
            "data_type": "timestamp",
            "is_nullable": "YES",
            "column_default": None,
        },
    ]


def test_append_grouped_index_rows_groups_multi_column_index_in_position_order() -> None:
    tables = build_table_map_from_column_rows(_column_rows())
    index_rows = [
        {
            "table_schema": "retail",
            "table_name": "account",
            "index_name": "account_pkey",
            "index_type": "btree",
            "is_unique": True,
            "is_primary": True,
            "column_name": "account_id",
        },
        {
            "table_schema": "retail",
            "table_name": "account",
            "index_name": "ix_account_opened",
            "index_type": "btree",
            "is_unique": False,
            "is_primary": False,
            "column_name": "opened_at",
        },
        {
            "table_schema": "retail",
            "table_name": "account",
            "index_name": "ix_account_opened",
            "index_type": "btree",
            "is_unique": False,
            "is_primary": False,
            "column_name": "account_id",
        },
    ]

    append_grouped_index_rows(tables, index_rows)

    indexes = {index.name: index for index in tables["retail"]["account"].indexes}
    assert set(indexes) == {"account_pkey", "ix_account_opened"}
    assert indexes["account_pkey"].columns == ("account_id",)
    assert indexes["account_pkey"].is_unique is True
    assert indexes["account_pkey"].is_primary is True
    # Column order follows row order (callers are responsible for ordering by
    # the index's own column position), matching append_grouped_key_rows.
    assert indexes["ix_account_opened"].columns == ("opened_at", "account_id")
    assert indexes["ix_account_opened"].is_primary is False


def test_append_grouped_index_rows_ignores_index_on_unknown_table() -> None:
    tables = build_table_map_from_column_rows(_column_rows())
    append_grouped_index_rows(
        tables,
        [
            {
                "table_schema": "retail",
                "table_name": "does_not_exist",
                "index_name": "ix_ghost",
                "index_type": "btree",
                "is_unique": False,
                "is_primary": False,
                "column_name": "id",
            }
        ],
    )
    assert tables["retail"]["account"].indexes == []


def test_append_partition_rows_attaches_shared_key_columns_per_row() -> None:
    tables = build_table_map_from_column_rows(_column_rows())
    partition_rows = [
        {
            "table_schema": "retail",
            "table_name": "account",
            "partition_name": "account_2025",
            "partition_type": "RANGE",
            "ordinal_position": 1,
            "high_value": "2026-01-01",
            "key_columns": ["opened_at"],
        },
        {
            "table_schema": "retail",
            "table_name": "account",
            "partition_name": "account_2026",
            "partition_type": "RANGE",
            "ordinal_position": 2,
            "high_value": "2027-01-01",
            "key_columns": ["opened_at"],
        },
    ]

    append_partition_rows(tables, partition_rows)

    partitions = tables["retail"]["account"].partitions
    assert [partition.name for partition in partitions] == ["account_2025", "account_2026"]
    assert all(partition.key_columns == ("opened_at",) for partition in partitions)
    assert partitions[0].high_value == "2026-01-01"
    assert partitions[1].ordinal_position == 2


def test_oracle_assemble_catalog_includes_indexes_and_partitions() -> None:
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
        {
            "table_schema": "RETAIL",
            "table_name": "ACCOUNT",
            "table_type": "TABLE",
            "column_name": "OPENED_AT",
            "ordinal_position": 2,
            "data_type": "DATE",
            "is_nullable": "Y",
            "column_default": None,
        },
    ]
    index_rows = [
        {
            "table_schema": "RETAIL",
            "table_name": "ACCOUNT",
            "index_name": "ACCOUNT_PK",
            "index_type": "NORMAL",
            "is_unique": True,
            "is_primary": True,
            "column_name": "ACCOUNT_ID",
        }
    ]
    partition_rows = [
        {
            "table_schema": "RETAIL",
            "table_name": "ACCOUNT",
            "partition_name": "P2025",
            "partition_type": "RANGE",
            "ordinal_position": 1,
            "key_columns": ["OPENED_AT"],
        }
    ]

    catalogs = _assemble_catalog(
        "BANK", column_rows, [], [], index_rows=index_rows, partition_rows=partition_rows
    )

    table = catalogs[0].schemas[0].tables[0]
    assert len(table.indexes) == 1
    assert table.indexes[0].name == "ACCOUNT_PK"
    assert table.indexes[0].is_primary is True
    assert len(table.partitions) == 1
    assert table.partitions[0].name == "P2025"
    assert table.partitions[0].key_columns == ("OPENED_AT",)


def test_oracle_assemble_catalog_defaults_to_no_indexes_or_partitions() -> None:
    """Existing callers (and the pre-existing test suite) call `_assemble_catalog`
    with only 4 positional args; adding index/partition extraction must not
    change that contract.
    """
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
        }
    ]
    catalogs = _assemble_catalog("BANK", column_rows, [], [])
    table = catalogs[0].schemas[0].tables[0]
    assert table.indexes == ()
    assert table.partitions == ()


def test_oracle_and_postgres_now_declare_index_and_partition_capability() -> None:
    """CT-3's exit condition is 'populated by >= 2 adapters'; Oracle and
    PostgreSQL are the most-complete certified/near-certified pull connectors
    in this codebase (Snowflake is still only a planned/push-only connector
    per the registry), so those two now honestly advertise the capability
    they extract.
    """
    assert OracleConnector.DEFAULT_CAPABILITIES.indexes is True
    assert OracleConnector.DEFAULT_CAPABILITIES.partitions is True
    assert PostgresConnector.DEFAULT_CAPABILITIES.indexes is True
    assert PostgresConnector.DEFAULT_CAPABILITIES.partitions is True

    oracle_definition = connector_registry.definition("oracle")
    postgres_definition = connector_registry.definition("postgres")
    assert oracle_definition.capabilities["indexes"] is True
    assert oracle_definition.capabilities["partitions"] is True
    assert postgres_definition.capabilities["indexes"] is True
    assert postgres_definition.capabilities["partitions"] is True

    snowflake_definition = connector_registry.definition("snowflake")
    assert snowflake_definition.implementation_status == "PLANNED"
