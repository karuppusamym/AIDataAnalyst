from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from aida.connectors.base import Connector, ConnectorCapabilities
from aida.connectors.bigquery import BigQueryConnector
from aida.connectors.databricks import DatabricksConnector
from aida.connectors.oracle import OracleConnector
from aida.connectors.postgres import PostgresConnector
from aida.connectors.snowflake import SnowflakeConnector
from aida.connectors.sqlserver import SqlServerConnector

ConnectorFactory = Callable[[str], Connector]


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    connector_type: str
    display_name: str
    dialect: str
    implementation_status: str
    transports: tuple[str, ...]
    maturity: str
    version: str
    notes: str
    capabilities: dict[str, bool] = field(default_factory=dict)

    def as_dict(self, *, capabilities: dict[str, bool] | None = None) -> dict[str, Any]:
        result = asdict(self)
        result["transports"] = list(self.transports)
        result["capabilities"] = dict(self.capabilities) if capabilities is None else capabilities
        return result


class ConnectorRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ConnectorFactory] = {}
        self._definitions: dict[str, ConnectorDefinition] = {}

    def register(
        self,
        connector_type: str,
        factory: ConnectorFactory,
        *,
        display_name: str | None = None,
        dialect: str | None = None,
        maturity: str = "DEVELOPMENT",
        version: str = "1.0.0",
        transports: tuple[str, ...] = ("PULL", "PUSH"),
        notes: str = "",
        capabilities: ConnectorCapabilities | None = None,
    ) -> None:
        if connector_type in self._factories:
            raise ValueError(f"connector already registered: {connector_type}")
        self._factories[connector_type] = factory
        self._definitions[connector_type] = ConnectorDefinition(
            connector_type=connector_type,
            display_name=display_name or connector_type.replace("_", " ").title(),
            dialect=dialect or connector_type,
            implementation_status="IMPLEMENTED",
            transports=transports,
            maturity=maturity,
            version=version,
            notes=notes,
            capabilities=asdict(capabilities) if capabilities is not None else {},
        )

    def declare_planned(
        self,
        connector_type: str,
        display_name: str,
        dialect: str,
        *,
        notes: str,
    ) -> None:
        if connector_type in self._definitions:
            raise ValueError(f"connector already declared: {connector_type}")
        self._definitions[connector_type] = ConnectorDefinition(
            connector_type=connector_type,
            display_name=display_name,
            dialect=dialect,
            implementation_status="PLANNED",
            transports=("PUSH",),
            maturity="NOT_CERTIFIED",
            version="0.0.0",
            notes=notes,
            capabilities={},
        )

    def create(self, connector_type: str, dsn: str) -> Connector:
        try:
            factory = self._factories[connector_type]
        except KeyError as exc:
            raise ValueError(f"unsupported connector type: {connector_type}") from exc
        return factory(dsn)

    @property
    def supported_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    @property
    def definitions(self) -> tuple[ConnectorDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def definition(self, connector_type: str) -> ConnectorDefinition:
        try:
            return self._definitions[connector_type]
        except KeyError as exc:
            raise ValueError(f"unknown connector definition: {connector_type}") from exc


connector_registry = ConnectorRegistry()
connector_registry.register(
    "postgres",
    PostgresConnector,
    display_name="PostgreSQL",
    dialect="postgres",
    maturity="BETA",
    version="1.0.0",
    notes="Pull discovery, constraints, bounded profiling, explain and governed queries.",
    capabilities=PostgresConnector.DEFAULT_CAPABILITIES,
)
connector_registry.register(
    "oracle",
    OracleConnector,
    display_name="Oracle Database",
    dialect="oracle",
    maturity="BETA",
    version="1.0.0",
    notes=(
        "Pull discovery, constraints, bounded profiling, and governed read execution. "
        "Query-estimate enforcement remains intentionally unavailable until an Oracle "
        "explain path is certified."
    ),
    capabilities=OracleConnector.DEFAULT_CAPABILITIES,
)
connector_registry.register(
    "sqlserver",
    SqlServerConnector,
    display_name="Microsoft SQL Server",
    dialect="tsql",
    maturity="BETA",
    version="1.0.0",
    notes=(
        "Pull discovery, constraints, bounded profiling, SHOWPLAN_XML-based cost "
        "estimation and governed queries."
    ),
    capabilities=SqlServerConnector.DEFAULT_CAPABILITIES,
)
connector_registry.register(
    "bigquery",
    BigQueryConnector,
    display_name="Google BigQuery",
    dialect="bigquery",
    maturity="BETA",
    version="1.0.0",
    notes=(
        "Pull discovery via region-qualified INFORMATION_SCHEMA (primary-key "
        "constraints only; foreign-key metadata honestly omitted, uncertified), "
        "dry-run byte estimation, and governed read execution bounded by the "
        "gateway's deterministic byte-budget gate."
    ),
    capabilities=BigQueryConnector.DEFAULT_CAPABILITIES,
)
connector_registry.register(
    "snowflake",
    SnowflakeConnector,
    display_name="Snowflake",
    dialect="snowflake",
    maturity="BETA",
    version="1.0.0",
    notes=(
        "Pull discovery across multi-database catalogs via INFORMATION_SCHEMA, "
        "referential constraints, partition-pruned EXPLAIN cost estimation, "
        "approximate statistics profiling, and warehouse query ID traceability."
    ),
    capabilities=SnowflakeConnector.DEFAULT_CAPABILITIES,
)
connector_registry.register(
    "databricks",
    DatabricksConnector,
    display_name="Databricks SQL",
    dialect="databricks",
    maturity="BETA",
    version="1.0.0",
    notes=(
        "Pull discovery of Unity Catalog catalogs/schemas/tables/columns via "
        "per-catalog INFORMATION_SCHEMA, PRIMARY KEY/UNIQUE constraints, "
        "best-effort FOREIGN KEY discovery (degrades to none rather than failing "
        "on older metastores), table/column/schema/catalog comments, EXPLAIN "
        "COST-based query estimation, and bounded profiling. PAT auth only (no "
        "delegated/workload identity); view/routine/grant axes not yet "
        "implemented. Code complete; never exercised against a live Databricks "
        "workspace -- same honesty gap as the Snowflake, Oracle and BigQuery rows."
    ),
    capabilities=DatabricksConnector.DEFAULT_CAPABILITIES,
)
for connector_type, display_name, dialect in (
    ("teradata", "Teradata", "teradata"),
    ("db2", "IBM Db2", "db2"),
):
    connector_registry.declare_planned(
        connector_type,
        display_name,
        dialect,
        notes=(
            "Canonical push ingestion is available; native pull adapter and "
            "certification are pending."
        ),
    )
