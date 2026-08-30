from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    catalogs: bool = True
    schemas: bool = True
    constraints: bool = False
    indexes: bool = False
    partitions: bool = False
    explain: bool = False
    query_history: bool = False
    delegated_identity: bool = False
    approximate_statistics: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveredColumn:
    name: str
    ordinal_position: int
    physical_type: str
    nullable: bool
    default_expression: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredConstraint:
    name: str
    constraint_type: str
    columns: tuple[str, ...]
    referenced_schema: str | None = None
    referenced_table: str | None = None
    referenced_columns: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiscoveredTable:
    name: str
    object_type: str
    columns: tuple[DiscoveredColumn, ...]
    constraints: tuple[DiscoveredConstraint, ...] = ()
    source_description: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredSchema:
    name: str
    tables: tuple[DiscoveredTable, ...]
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiscoveredCatalog:
    name: str
    schemas: tuple[DiscoveredSchema, ...]
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QueryResult:
    rows: tuple[dict[str, Any], ...]
    warehouse_query_id: str | None


@dataclass(frozen=True, slots=True)
class QueryEstimate:
    score: float
    kind: str
    estimated_rows: float | None = None
    estimated_bytes: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ColumnProfileSnapshot:
    name: str
    null_count: int
    non_null_count: int
    approximate_distinct_count: int
    min_length: int | None
    max_length: int | None


@dataclass(frozen=True, slots=True)
class TableProfileSnapshot:
    row_count_estimate: int | None
    sampled_row_count: int
    columns: tuple[ColumnProfileSnapshot, ...]


class Connector(ABC):
    """Source access with structured arguments only.

    Deliberately has no SQL-accepting member: the `estimate_read_query` /
    `execute_read_query` pair lives on `aida.connectors.sql_execution.SqlExecutor`
    so that INV-2 (one execution choke point) is enforced by the type system and
    the import graph rather than by convention. See that module for the argument.
    """

    connector_type: str
    dialect: str

    @property
    @abstractmethod
    def capabilities(self) -> ConnectorCapabilities:
        raise NotImplementedError

    @abstractmethod
    async def test_connection(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def discover(self) -> tuple[DiscoveredCatalog, ...]:
        raise NotImplementedError

    @abstractmethod
    async def profile_table(
        self,
        schema_name: str,
        table_name: str,
        column_names: tuple[str, ...],
        *,
        sample_rows: int,
        column_batch_size: int,
        timeout_seconds: int,
    ) -> TableProfileSnapshot:
        raise NotImplementedError
