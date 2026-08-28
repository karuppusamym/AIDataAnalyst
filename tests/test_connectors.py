import pytest

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
    assert default_capabilities(databricks) == {}
